"""Actual native history is the fence; an expiring artifact never releases it."""

import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
import codex_automation_custody as custody  # noqa: E402

HEAD, SOURCE = "a" * 40, "b" * 40


def target():
    return {
        "repository": "example/project",
        "pull_request": 7,
        "operation": "assessment",
        "head_sha": HEAD,
    }


class GitHub:
    repository = "example/project"

    def __init__(self):
        self.rows, self.writes = [], []
        self.lose_ack, self.lose_readback, self.race = False, False, False
        repo = {"id": 1, "full_name": self.repository}
        self.data = {
            "": repo,
            "/actions/runs/10": {
                "id": 10,
                "path": custody.WORKFLOW,
                "workflow_id": 3,
                "event": "workflow_run",
                "head_sha": SOURCE,
                "status": "in_progress",
                "conclusion": None,
                "repository": repo,
                "head_repository": repo,
            },
            "/actions/workflows/codex-automation-worker.yml": {
                "id": 3,
                "path": custody.WORKFLOW,
            },
        }

    def json(self, route, *, method="GET", data=None):
        if method == "GET":
            assert route in self.data, "unapproved route"
            return copy.deepcopy(self.data[route])
        assert method == "POST" and route == f"/statuses/{HEAD}"
        self.writes.append(copy.deepcopy(data))
        row = {**data, "id": 25, "creator": {"id": custody.BOT_ID}}
        self.rows.append(row)
        if self.race:
            self.rows.append({**row, "id": 26})
        return {} if self.lose_ack else copy.deepcopy(row)

    def pages(self, route):
        assert route == f"/commits/{HEAD}/statuses"
        return [] if self.lose_readback and self.writes else copy.deepcopy(self.rows)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(custody, "_sleep", slept.append)
    return slept


def claim(github, *, enabled=True):
    return custody.claim(
        github, target(), producer_run_id=10, workflow_sha=SOURCE, enabled=enabled
    )


def test_pending_claim_is_appended_and_read_back_before_execution_admission():
    github = GitHub()
    result = claim(github)
    assert result["status_id"] == 25 and result["workflow_sha"] == SOURCE
    assert github.writes == [
        {
            "context": "codex-automation-execution/assessment/pr-7",
            "state": "pending",
            "target_url": "https://github.com/example/project/actions/runs/10",
            "description": "Execution claimed; outcome requires reconciliation.",
        }
    ]
    with pytest.raises(custody.CustodyHold, match="already_claimed"):
        claim(github)
    assert len(github.writes) == 1


@pytest.mark.parametrize("state", ["pending", "failure", "error", "success"])
@pytest.mark.parametrize("enabled", [True, False])
def test_old_native_state_holds_even_after_success_expiry_policy_change_or_rollback(
    state, enabled
):
    github = GitHub()
    github.rows = [
        {
            "context": "codex-automation-execution/assessment/pr-7",
            "state": state,
            "id": 1,
            "creator": {"id": 999},
            "target_url": "historical-source",
        }
    ]
    with pytest.raises(custody.CustodyHold, match="already_claimed"):
        claim(github, enabled=enabled)
    assert not github.writes


def test_disabled_new_execution_preserves_existing_history_check_but_makes_no_claim():
    github = GitHub()
    assert claim(github, enabled=False) is None
    assert not github.writes


def test_a_lagging_status_list_is_re_read_before_holding(no_sleep):
    github = GitHub()
    lagging = {"reads": 0}
    pages = github.pages

    def lagged(route):
        lagging["reads"] += 1
        # The first two reads after the write do not yet show the new row.
        return [] if github.writes and lagging["reads"] <= 3 else pages(route)

    github.pages = lagged
    result = claim(github)
    assert result["status_id"] == 25 and len(github.writes) == 1
    assert no_sleep == list(custody.READBACK_DELAYS[:2])


@pytest.mark.parametrize("lost", ["lose_readback", "race"])
def test_an_acknowledged_but_unconfirmed_claim_is_marked_for_release(lost, no_sleep):
    github = GitHub()
    setattr(github, lost, True)
    with pytest.raises(custody.ClaimUnconfirmed, match="ack_unknown"):
        claim(github)
    assert len(github.writes) == 1


def test_a_read_back_error_after_an_acknowledged_write_is_marked_for_release(no_sleep):
    github = GitHub()
    pages = github.pages

    def failing(route):
        if github.writes:
            raise RuntimeError("status list read failed")
        return pages(route)

    github.pages = failing
    with pytest.raises(custody.ClaimUnconfirmed, match="ack_unknown"):
        claim(github)
    assert len(github.writes) == 1 and no_sleep == list(custody.READBACK_DELAYS)


def test_a_read_back_error_that_clears_still_confirms_the_claim(no_sleep):
    github = GitHub()
    pages, calls = github.pages, {"n": 0}

    def flaky(route):
        if github.writes:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
        return pages(route)

    github.pages = flaky
    assert claim(github)["status_id"] == 25


def test_an_unacknowledged_write_is_not_marked_for_release():
    github = GitHub()
    github.lose_ack = True
    with pytest.raises(custody.CustodyHold, match="ack_unknown") as raised:
        claim(github)
    assert type(raised.value) is custody.CustodyHold


@pytest.mark.parametrize("lost", ["lose_ack", "lose_readback", "race"])
def test_unknown_ack_or_competing_claim_never_admits_model_work(lost):
    github = GitHub()
    setattr(github, lost, True)
    with pytest.raises(custody.CustodyHold, match="ack_unknown"):
        claim(github)
    assert github.rows and len(github.writes) == 1
    setattr(github, lost, False)
    with pytest.raises(custody.CustodyHold, match="already_claimed"):
        claim(github)
    assert len(github.writes) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("event", "workflow_dispatch"),
        ("path", ".github/workflows/other.yml"),
        ("head_sha", "c" * 40),
        ("workflow_id", True),
        ("head_repository", {"id": 99}),
        ("repository", {"id": True}),
        ("status", "completed"),
        ("conclusion", "failure"),
    ],
)
def test_only_actual_default_source_worker_producer_can_claim(key, value):
    github = GitHub()
    github.data["/actions/runs/10"][key] = value
    with pytest.raises(custody.CustodyHold, match="controller_refused"):
        claim(github)
    assert not github.writes


@pytest.mark.parametrize("value", [None, [{}], ["unreadable"]])
def test_unreadable_history_is_not_no_prior_execution(value):
    github = GitHub()
    github.rows = value
    with pytest.raises(custody.CustodyHold, match="history_unreadable"):
        claim(github)
    assert not github.writes
