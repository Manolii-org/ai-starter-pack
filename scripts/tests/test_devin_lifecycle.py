import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("devin_lifecycle", Path(__file__).parents[1] / "devin_lifecycle.py")
life = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(life)
NOW = 20000
POLICY = {"org_id": "org-example", "automation_ids": ["auto-example"], "grace_seconds": 3600, "max_sessions": 100, "max_archives": 10}


def fixture():
    session = {"session_id": "devin-example", "created_at": 10000, "updated_at": 15000, "origin": "automation", "automation_id": "auto-example", "is_archived": False, "status": "suspended", "status_detail": "inactivity", "tags": [], "child_session_ids": [], "pull_requests": []}
    receipt = {"version": 1, "outcome": "delivered", "delivery_verified": True, "retain": False, "needs_attention": False, "pending_work": False}
    messages = [{"source": "user", "origin": "automation", "created_at": 10001, "message": "scheduled task"}, {"source": "devin", "created_at": 14900, "message": "Delivered.\n" + life.MARKER + json.dumps(receipt)}]
    return session, messages


def test_delivered_and_noop():
    session, messages = fixture()
    assert life.decision(session, messages, POLICY, NOW) == "eligible"
    messages[-1]["message"] = messages[-1]["message"].replace('"delivered"', '"noop"')
    assert life.decision(session, messages, POLICY, NOW) == "eligible"


@pytest.mark.parametrize("change,reason", [
    ({"status": "running", "status_detail": "finished"}, "not_quiet"),
    ({"status_detail": "usage_limit_exceeded"}, "not_quiet"),
    ({"updated_at": NOW - 3599}, "grace_period"),
    ({"updated_at": NOW + 1}, "grace_period"),
    ({"tags": ["Retain"]}, "retained"),
    ({"tags": ["manual-review"]}, "retained"),
    ({"tags": ["primary-blocker"]}, "retained"),
    ({"tags": ["lifecycle:retain:operator"]}, "retained"),
    ({"tags": None}, "protection_unknown"),
    ({"child_session_ids": ["devin-child"]}, "descendants_or_unknown"),
    ({"child_session_ids": None}, "descendants_or_unknown"),
    ({"pull_requests": [{"pr_state": "open"}]}, "pr_followthrough"),
    ({"pull_requests": [{}]}, "pr_followthrough"),
    ({"pull_requests": None}, "pr_followthrough"),
    ({"origin": "webapp"}, "not_managed"),
    ({"automation_id": "auto-other"}, "not_managed"),
    ({"is_archived": True}, "archived_or_unknown"),
])
def test_protection(change, reason):
    session, messages = fixture()
    session.update(change)
    assert life.decision(session, messages, POLICY, NOW) == reason


@pytest.mark.parametrize("key,value", [("pending_work", True), ("needs_attention", True), ("retain", True), ("delivery_verified", False), ("delivery_verified", "true"), ("outcome", "blocked")])
def test_receipt_fail_closed(key, value):
    session, messages = fixture()
    receipt = life.outcome(messages)
    receipt[key] = value
    messages[-1]["message"] = life.MARKER + json.dumps(receipt)
    assert life.decision(session, messages, POLICY, NOW) != "eligible"


def test_missing_receipt_field():
    session, messages = fixture()
    messages[-1]["message"] = life.MARKER + '{"version":1,"outcome":"delivered"}'
    assert life.decision(session, messages, POLICY, NOW) != "eligible"


def test_human_cannot_supply_or_override_outcome():
    session, messages = fixture()
    messages.append({"source": "user", "created_at": 15100, "message": messages[-1]["message"]})
    assert life.decision(session, messages, POLICY, NOW) == "human_followup"
    assert life.outcome(messages) is None


def test_no_inference_from_suspension_or_pasted_marker():
    session, messages = fixture()
    messages[-1]["message"] += "\nNot actually done."
    assert life.decision(session, messages, POLICY, NOW) == "outcome_unknown"


class FakeClient:
    def __init__(self):
        self.session, self.messages = fixture()
        self.writes = []

    def pages(self, limit, filters=None):
        return [self.session]

    def read(self, sid):
        return copy.deepcopy(self.session), copy.deepcopy(self.messages)

    def request(self, suffix, method):
        self.writes.append((suffix, method))
        self.session["is_archived"] = True
        return self.session


def test_dry_run_apply_and_idempotence(monkeypatch):
    monkeypatch.setattr(life.time, "time", lambda: NOW)
    client = FakeClient()
    dry = life.sweep(client, POLICY, now=NOW)
    assert not client.writes
    assert dry["sessions"][0]["decision"] == "eligible"
    live = life.sweep(client, POLICY, True, dry, NOW)
    assert live["sessions"][0]["decision"] == "archived"
    assert client.writes == [("/devin-example/archive", "POST")]
    life.sweep(client, POLICY, True, dry, NOW)
    assert len(client.writes) == 1


def test_changed_or_stale_review_rejected(monkeypatch):
    monkeypatch.setattr(life.time, "time", lambda: NOW)
    client = FakeClient()
    dry = life.sweep(client, POLICY, now=NOW)
    client.session["updated_at"] += 1
    live = life.sweep(client, POLICY, True, dry, NOW)
    assert live["sessions"][0]["decision"] == "changed_since_review"
    assert not client.writes
    with pytest.raises(ValueError):
        life.sweep(client, POLICY, True, dry, NOW + 901)


def test_protected_at_last_read(monkeypatch):
    monkeypatch.setattr(life.time, "time", lambda: NOW)
    client = FakeClient()
    dry = life.sweep(client, POLICY, now=NOW)
    old_read = client.read
    calls = []

    def read(sid):
        calls.append(sid)
        if len(calls) == 2:
            client.session["child_session_ids"] = ["devin-newchild"]
        return old_read(sid)

    client.read = read
    report = life.sweep(client, POLICY, True, dry, NOW)
    assert report["sessions"][0]["decision"] == "changed_before_archive"
    assert not client.writes


def test_paginate_and_stop_on_bad_cursor():
    client = life.Client("org-example", "test-placeholder")
    calls = []

    def request(suffix):
        calls.append(suffix)
        return {"items": [1], "has_next_page": True, "end_cursor": "repeat"}

    client.request = request
    with pytest.raises(ValueError):
        client.pages(limit=10)
    assert calls == ["?first=10", "?first=9&after=repeat"]


def test_no_post_for_ineligible_session(monkeypatch):
    monkeypatch.setattr(life.time, "time", lambda: NOW)
    client = FakeClient()
    dry = life.sweep(client, POLICY, now=NOW)
    client.messages.append({"source": "user", "created_at": 16000, "message": "Keep working"})
    life.sweep(client, POLICY, True, dry, NOW)
    assert not client.writes


def test_http_wire_contract_and_no_credential_redirect(monkeypatch):
    import io

    requests = []
    responses = [{"items": [{"session_id": "devin-example"}], "has_next_page": False}, {"session_id": "devin-example"}, {"items": [], "has_next_page": False}, {"is_archived": True}]

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return io.BytesIO(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr(life.urllib.request, "build_opener", lambda handler: Opener())
    client = life.Client("org-example", "test-placeholder")
    client.pages(limit=100)
    client.read("devin-example")
    client.request("/devin-example/archive", "POST")
    assert [r.full_url for r, _ in requests] == [
        "https://api.devin.ai/v3/organizations/org-example/sessions?first=100",
        "https://api.devin.ai/v3/organizations/org-example/sessions/devin-example",
        "https://api.devin.ai/v3/organizations/org-example/sessions/devin-example/messages?first=100",
        "https://api.devin.ai/v3/organizations/org-example/sessions/devin-example/archive",
    ]
    assert requests[-1][0].method == "POST"
    assert all(timeout == 30 and r.get_header("Authorization") == "Bearer test-placeholder" for r, timeout in requests)
    assert life.NoRedirect().redirect_request(None, None, 302, None, None, "https://other.example") is None


def test_attempt_is_audited_before_ambiguous_write(monkeypatch):
    monkeypatch.setattr(life.time, "time", lambda: NOW)
    client = FakeClient()
    dry = life.sweep(client, POLICY, now=NOW)
    events = []

    def request(suffix, method):
        assert events[-1]["event"] == "archive_attempt"
        raise TimeoutError()

    client.request = request
    with pytest.raises(TimeoutError):
        life.sweep(client, POLICY, True, dry, NOW, audit=events.append)
    assert len(events) == 1


def test_early_human_without_launch_is_not_exempted():
    session, messages = fixture()
    messages[0].pop("origin")
    assert life.decision(session, messages, POLICY, NOW) == "human_followup"
    policy = {**POLICY, "launch_event_ids": {"devin-example": "event-reviewed"}}
    messages[0]["event_id"] = "event-human"
    assert life.decision(session, messages, policy, NOW) == "human_followup"
    messages[0]["event_id"] = "event-reviewed"
    assert life.decision(session, messages, policy, NOW) == "eligible"


@pytest.mark.parametrize("ids", [[None], ["auto-example", None], [""], [1], ["other"], []])
def test_invalid_allowlist_rejected(ids):
    with pytest.raises(ValueError):
        life.validate_policy({**POLICY, "automation_ids": ids})
    session, messages = fixture()
    session["automation_id"] = None
    assert life.decision(session, messages, {**POLICY, "automation_ids": ids}, NOW) == "not_managed"


def test_report_expires_during_reads(monkeypatch):
    monkeypatch.setattr(life.time, "time", lambda: NOW)
    client = FakeClient()
    dry = life.sweep(client, POLICY, now=NOW)
    read = client.read

    def slow_read(sid):
        result = read(sid)
        monkeypatch.setattr(life.time, "time", lambda: NOW + 901)
        return result

    client.read = slow_read
    with pytest.raises(ValueError, match="expired"):
        life.sweep(client, POLICY, True, dry, NOW)
    assert not client.writes


@pytest.mark.parametrize("sid", ["c0667731c17a480b80248ddf5cf9cf5c", "devin-example"])
def test_api_accepts_bare_or_prefixed_id(monkeypatch, sid):
    client = life.Client("org-example", "placeholder")
    monkeypatch.setattr(client, "request", lambda suffix: {"session_id": sid})
    monkeypatch.setattr(client, "pages", lambda *a: [])
    assert client.read(sid)[0]["session_id"] == sid


@pytest.mark.parametrize("sid", ["../other", "a/b", "devin-a?x=1", "devin-a%2farchive", "a#b", ""])
def test_api_rejects_path_injection(sid):
    with pytest.raises(ValueError):
        life.Client("org-example", "placeholder").read(sid)


@pytest.mark.parametrize("change", [{"apply": True}, {"apply": None}, {"version": True}, {"version": 2}])
def test_only_versioned_dry_run_can_authorize_apply(monkeypatch, change):
    monkeypatch.setattr(life.time, "time", lambda: NOW)
    client = FakeClient()
    report = life.sweep(client, POLICY, now=NOW)
    report.update(change)
    with pytest.raises(ValueError):
        life.sweep(client, POLICY, True, report, NOW)
    assert not client.writes


def test_empty_continuation_cannot_make_unbounded_requests(monkeypatch):
    client = life.Client("org-example", "placeholder")
    calls = []

    def response(suffix):
        calls.append(suffix)
        return {"items": [], "has_next_page": True, "end_cursor": str(len(calls))}

    monkeypatch.setattr(client, "request", response)
    with pytest.raises(ValueError):
        client.pages()
    assert len(calls) == 1


@pytest.mark.parametrize("sid", ["example", "devin-example"])
@pytest.mark.parametrize("protected", ["example", "devin-example"])
def test_protected_id_is_prefix_independent(sid, protected):
    session, messages = fixture()
    session["session_id"] = sid
    assert life.decision(session, messages, {**POLICY, "protected_session_ids": [protected]}, NOW) == "protected"


@pytest.mark.parametrize("sid", ["example", "devin-example"])
@pytest.mark.parametrize("mapped", ["example", "devin-example"])
def test_reviewed_launch_id_is_prefix_independent(sid, mapped):
    session, messages = fixture()
    session["session_id"] = sid
    messages[0].pop("origin")
    messages[0]["event_id"] = "event-reviewed-launch"
    policy = {**POLICY, "launch_event_ids": {mapped: "event-reviewed-launch"}}
    assert life.decision(session, messages, policy, NOW) == "eligible"


@pytest.mark.parametrize("protected", [None, "example", [None], [1], ["../example"]])
def test_invalid_protected_ids_rejected(protected):
    with pytest.raises((ValueError, TypeError)):
        life.validate_policy({**POLICY, "protected_session_ids": protected})


def test_ambiguous_launch_aliases_rejected():
    with pytest.raises(ValueError):
        life.validate_policy({**POLICY, "launch_event_ids": {"example": "event-1", "devin-example": "event-2"}})
