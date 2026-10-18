"""Execute admission, artifact, container-policy and publisher boundaries."""

import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
SPEC = importlib.util.spec_from_file_location(
    "trusted_worker", ROOT / "scripts/ci/codex_review_worker.py"
)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
HEAD = "a" * 40
BASE = "b" * 40
CONTROLLER = "c" * 40
REPO = "example/project"
REPOSITORY = {"id": 7, "default_branch": "main"}
TARGET = {
    "repository": REPO,
    "pull_request": 1,
    "base_sha": BASE,
    "head_sha": HEAD,
    "lane": "code",
}
PR = {
    "number": 1,
    "head": {"sha": HEAD, "repo": {"full_name": REPO}},
    "base": {"sha": BASE, "repo": {"full_name": REPO}},
    "state": "open",
    "draft": False,
    "author_association": "MEMBER",
    "user": {"login": "review-author"},
}
REQUEST = {
    "id": 3,
    "path": ".github/workflows/codex-pr-review.yml",
    "event": "pull_request",
    "status": "completed",
    "workflow_id": 20,
    "head_sha": HEAD,
    "repository": {"id": 7},
    "head_repository": {"id": 7},
    "pull_requests": [{"number": 1, "head": {"sha": HEAD}}],
}
WORKFLOW = {"id": 21, "path": worker.WORKFLOW}
RUN = {
    "id": 30,
    "workflow_id": 21,
    "path": worker.WORKFLOW,
    "event": "workflow_run",
    "head_branch": "main",
    "repository": {"id": 7},
    "head_repository": {"id": 7},
    "status": "completed",
    "head_sha": CONTROLLER,
}


class FakeGitHub:
    repository = REPO
    token = "synthetic-github-token"

    def __init__(self):
        self.data = {
            "": copy.deepcopy(REPOSITORY),
            "/actions/runs/3": copy.deepcopy(REQUEST),
            "/pulls/1": copy.deepcopy(PR),
            "/actions/workflows/codex-pr-review.yml": {
                "id": 20,
                "path": REQUEST["path"],
            },
            "/actions/workflows/codex-review-worker.yml": copy.deepcopy(WORKFLOW),
            "/actions/runs/30": copy.deepcopy(RUN),
        }
        self.requests = []
        self.artifacts = []
        self.archives = {}

    def json(self, route, **kwargs):
        self.requests.append((route, kwargs))
        if kwargs:
            return {}
        return copy.deepcopy(self.data[route])

    def pages(self, route, key=None):
        self.requests.append((route, {}))
        if route.startswith("/actions/artifacts?"):
            from urllib.parse import parse_qs, urlsplit

            name = parse_qs(urlsplit(route).query)["name"][0]
            return [item for item in self.artifacts if item["name"] == name]
        return self.data.get(route, [])

    def archive(self, artifact):
        return self.archives[artifact]


@pytest.fixture
def metadata(monkeypatch):
    for name, value in {
        "GITHUB_RUN_ID": "30",
        "GITHUB_WORKFLOW_SHA": CONTROLLER,
        "CODEX_REQUEST_RUN_ID": "3",
    }.items():
        monkeypatch.setenv(name, value)


def receipt():
    return worker.receipt_for(
        TARGET,
        "completed",
        "verdict_produced",
        "sha256:" + "d" * 64,
        output="codex\nSynthetic review",
        successful_turns=1,
    )


def archive(data, *, name="receipt.json", duplicates=False, symlink=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        member = zipfile.ZipInfo(name)
        if symlink:
            member.external_attr = 0o120777 << 16
        z.writestr(member, json.dumps(data))
        if data.get("state") == "completed":
            z.writestr("review.txt", "codex\nSynthetic review")
        if duplicates:
            z.writestr("other.json", "{}")
    return output.getvalue()


def test_actual_admission_binds_official_run_and_live_revision():
    target, request, repo = worker.admit(FakeGitHub(), {"workflow_run": {"id": 3}})
    assert target == TARGET and request == REQUEST and repo == REPOSITORY


@pytest.mark.parametrize(
    "route,key,value",
    [
        ("/actions/runs/3", "event", "pull_request_target"),
        ("/actions/runs/3", "path", ".github/workflows/forged.yml"),
        ("/actions/runs/3", "workflow_id", 999),
        ("/actions/runs/3", "head_sha", "d" * 40),
        ("/actions/runs/3", "repository", {"id": 999}),
        ("/actions/runs/3", "head_repository", {"id": 999}),
        ("/actions/runs/3", "pull_requests", []),
        (
            "/actions/runs/3",
            "pull_requests",
            [{"number": 1, "head": {"sha": "e" * 40}}],
        ),
        ("/pulls/1", "draft", True),
        ("/pulls/1", "state", "closed"),
        ("/pulls/1", "author_association", "FIRST_TIME_CONTRIBUTOR"),
        ("/pulls/1", "head", {"sha": HEAD, "repo": {"full_name": "other/fork"}}),
    ],
)
def test_forged_stale_fork_and_unadmitted_requests_fail(route, key, value):
    github = FakeGitHub()
    github.data[route][key] = value
    with pytest.raises(worker.Incomplete):
        worker.admit(github, {"workflow_run": {"id": 3}})


@pytest.mark.parametrize(
    "key,value",
    [
        ("workflow_id", 999),
        ("path", "forged.yml"),
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("repository", {"id": 999}),
        ("head_repository", {"id": 999}),
        ("status", "in_progress"),
        ("head_sha", "short"),
    ],
)
def test_producer_must_be_the_trusted_default_branch_worker(key, value):
    assert not worker.validate_producer({**RUN, key: value}, WORKFLOW, REPOSITORY)


def test_real_inert_receipt_archive_is_accepted(metadata):
    assert (
        worker.read_receipt(
            archive(receipt()),
            TARGET,
            worker.policy_hash(),
            run_id=30,
            workflow_sha=CONTROLLER,
        )["state"]
        == "completed"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("repository", "other/repo"),
        ("pull_request", 2),
        ("base_sha", "f" * 40),
        ("head_sha", "e" * 40),
        ("lane", "security"),
        ("policy_sha256", "old"),
        ("tools", {}),
        ("workflow_sha", HEAD),
        ("producer_run_id", 31),
        ("billing_mode", "api-key"),
        ("auth_boundary", "stored_auth"),
        ("reason", "no_output"),
        ("state", "pending"),
    ],
)
def test_wrong_revision_lane_policy_or_semantics_never_satisfies(metadata, key, value):
    data = {**receipt(), key: value}
    with pytest.raises(worker.Incomplete):
        worker.read_receipt(
            archive(data),
            TARGET,
            worker.policy_hash(),
            run_id=30,
            workflow_sha=CONTROLLER,
        )


@pytest.mark.parametrize(
    "settings",
    [
        {"name": "../receipt.json"},
        {"name": "/receipt.json"},
        {"duplicates": True},
        {"symlink": True},
    ],
)
def test_archive_paths_duplicates_and_symlinks_fail(metadata, settings):
    with pytest.raises(worker.Incomplete):
        worker.read_receipt(
            archive(receipt(), **settings),
            TARGET,
            worker.policy_hash(),
            run_id=30,
            workflow_sha=CONTROLLER,
        )


def add_artifact(github, data, prefix="codex-trusted"):
    github.artifacts.append(
        {
            "id": 99,
            "name": f"{prefix}-1-code-{HEAD}",
            "expired": False,
            "size_in_bytes": 1000,
            "workflow_run": {"id": 30, "head_sha": CONTROLLER},
        }
    )
    github.archives[99] = archive(data)


def test_only_authenticated_producer_artifact_satisfies(metadata):
    github = FakeGitHub()
    add_artifact(github, receipt())
    assert worker.prior_receipt(github, TARGET, REPOSITORY)["state"] == "completed"
    github.data["/actions/runs/30"]["event"] = "pull_request"
    assert worker.prior_receipt(github, TARGET, REPOSITORY) is None


def test_failed_or_cancelled_attempt_is_budget_evidence_only(metadata):
    github = FakeGitHub()
    data = worker.receipt_for(
        TARGET, "blocked_previous_attempt", "attempt_reserved", "sha256:" + "0" * 64
    )
    add_artifact(github, data, "codex-attempt")
    github.data["/actions/runs/30"]["conclusion"] = "cancelled"
    assert (
        worker.prior_receipt(github, TARGET, REPOSITORY)["state"]
        == "blocked_previous_attempt"
    )
    assert worker.prior_receipt(github, TARGET, REPOSITORY, ignore_run=30) is None


def test_container_has_no_auth_host_network_or_host_process_mounts(tmp_path):
    args = worker.container_args(
        "sha256:" + "d" * 64,
        tmp_path / "source",
        tmp_path / "broker",
        TARGET,
        "fixture",
    )
    text = " ".join(args)
    assert "--network none" in text and "--cap-drop ALL" in text
    assert (
        "no-new-privileges" in text and "seccomp=" in text and "unconfined" not in text
    )
    assert "--read-only" in args and "--privileged" not in args
    assert "--pid" not in args and "docker.sock" not in text
    assert sum(value.startswith("type=bind") for value in args) == 2
    assert all("readonly" in value for value in args if value.startswith("type=bind"))
    assert all(
        secret not in text
        for secret in [
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "DOPPLER",
            "auth.json",
            "OPENAI_API_KEY",
        ]
    )
    assert "HTTP_PROXY=" in args and "HTTPS_PROXY=" in args


@pytest.mark.parametrize(
    "lane,raw,expected",
    [
        ("code", "codex\nNo actionable defects.", "completed"),
        ("code", "startup logs only", "failed_output"),
        (
            "code",
            "codex\nYou have reached your Codex usage limits for code reviews.",
            "blocked_quota",
        ),
        ("security", '{"findings":[]}', "completed"),
        ("security", None, "failed_output"),
        ("security", '{"findings":[1]}', "failed_output"),
    ],
)
def test_actual_publisher_never_calls_missing_output_clean(
    tmp_path, monkeypatch, metadata, lane, raw, expected
):
    target = {**TARGET, "lane": lane}
    env = {
        "RANGE_STATE": "ready",
        "AUTH_PRESENT": "true",
        "INSTALL_OUTCOME": "success",
        "SCAN_OUTCOME": "success",
        "SCAN_RC": "0",
        "GITHUB_REPOSITORY": REPO,
        "PR_NUM": "1",
        "BASE_SHA": BASE,
        "HEAD_SHA": HEAD,
    }
    result = worker.build_receipt(lane, env, raw)
    assert result["state"] == expected
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    github = FakeGitHub()
    worker.publish(
        github,
        target,
        worker.receipt_for(
            target, result["state"], result["reason"], "sha256:" + "d" * 64
        ),
        raw or "",
    )
    body = github.requests[-1][1]["data"]["body"]
    assert HEAD in body and expected in body and summary.read_text() == body
    if expected != "completed":
        assert "No completed review" in body and "No findings" not in body


def test_scanner_nonzero_exit_cannot_be_clean(tmp_path, monkeypatch, metadata):
    target = {**TARGET, "lane": "security"}
    monkeypatch.setattr(worker, "prior_receipt", lambda *_, **__: None)
    monkeypatch.setattr(worker, "git_input", lambda *_: (tmp_path, ["auth/token.py"]))
    monkeypatch.setattr(worker, "build_image", lambda *_: "sha256:" + "d" * 64)
    monkeypatch.setattr(worker, "qualify_sandbox", lambda *_: None)
    monkeypatch.setattr(
        worker,
        "scoped_subscription",
        lambda *_: worker.Subscription(
            {
                "tokens": {
                    "access_token": "synthetic-private-token-123456789",
                    "account_id": "synthetic-private-account",
                }
            }
        ),
    )
    monkeypatch.setattr(
        worker,
        "run_container",
        lambda *_, **__: {"returncode": 9, "output": '{"findings":[]}'},
    )
    (tmp_path / "codex-review-worker.json").write_text(
        '{"enabled":true,"identity":"synthetic"}'
    )
    # Keep source policy hashing real, but provide this test's isolated config.
    original = Path.read_text

    def read_config(path, *args, **kwargs):
        return (
            '{"enabled":true,"identity":"synthetic"}'
            if path == worker.HERE / "codex-review-worker.json"
            else original(path, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "read_text", read_config)
    result, _, _ = worker.execute(FakeGitHub(), target, REPOSITORY, tmp_path)
    assert result["state"] == "failed_scan"


def test_range_keeps_renamed_sensitive_source(tmp_path, monkeypatch):
    repo = tmp_path / "origin"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "human-merge-policy.yaml").write_text("labels: [human-merge]\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("mv", "human-merge-policy.yaml", "ordinary.yaml")
    git("commit", "-qam", "rename")
    head = git("rev-parse", "HEAD")
    original = subprocess.Popen

    def local_remote(command, **kwargs):
        if "remote" in command and "add" in command and "origin" in command:
            command = command[:-1] + [str(repo)]
        if "fetch" in command:
            assert "synthetic-github-token" not in " ".join(command)
            assert kwargs["start_new_session"] is True and kwargs["preexec_fn"]
        return original(command, **kwargs)

    monkeypatch.setattr(worker.subprocess, "Popen", local_remote)
    state = tmp_path / "state"
    state.mkdir()
    source, paths = worker.git_input(
        state, {**TARGET, "base_sha": base, "head_sha": head}, "synthetic-github-token"
    )
    assert source.name == "source.git"
    assert set(paths) == {"human-merge-policy.yaml", "ordinary.yaml"}
    assert worker.security_required(paths)
    assert not (source / "human-merge-policy.yaml").exists()


def test_workflow_secrets_and_controller_revision_contract():
    for name in worker.LANES:
        data = yaml.safe_load((ROOT / name).read_text())
        text = (ROOT / name).read_text()
        assert (
            "DOPPLER_TOKEN" not in text and "CODEX_SUBSCRIPTION_AUTH_JSON" not in text
        )
        assert (
            "id-token" not in data["permissions"]
            and "write" not in data["permissions"].values()
        )
        assert "REVIEW_STATE" not in text and "REVIEW_HEAD" not in text
        completion = data["jobs"]["completion"]
        checkout = completion["steps"][1]["with"]
        assert checkout["ref"] == "${{ github.event.pull_request.base.sha }}"
        assert checkout["persist-credentials"] is False
    data = yaml.safe_load((ROOT / worker.WORKFLOW).read_text())
    assert set(data.get("on", data.get(True))) == {"workflow_run"}
    assert (
        data["jobs"]["execute"]["steps"][0]["with"]["ref"]
        == "${{ github.workflow_sha }}"
    )
    steps = data["jobs"]["execute"]["steps"]
    reserve = next(i for i, item in enumerate(steps) if item.get("id") == "reserve")
    execute = next(i for i, item in enumerate(steps) if item.get("id") == "worker")
    assert (
        reserve < execute
        and "steps.reserve.outcome == 'success'" in steps[execute]["if"]
    )


def test_completed_receipt_requires_full_output_and_host_provider_evidence(metadata):
    value = receipt()
    value["successful_provider_turns"] = 0
    with pytest.raises(worker.Incomplete, match="completed_output_unavailable"):
        worker.read_receipt(
            archive(value),
            TARGET,
            worker.policy_hash(),
            run_id=30,
            workflow_sha=CONTROLLER,
        )
    value = receipt()
    value["output_sha256"] = "0" * 64
    with pytest.raises(worker.Incomplete, match="completed_output_unavailable"):
        worker.read_receipt(
            archive(value),
            TARGET,
            worker.policy_hash(),
            run_id=30,
            workflow_sha=CONTROLLER,
        )


@pytest.mark.parametrize(
    "terminal,expected",
    [
        (None, "failed_output"),
        ("subscription_quota_exhausted", "blocked_quota"),
        ("subscription_upstream_refused", "failed_scan"),
        ("review_request_budget_exhausted", "failed_scan"),
    ],
)
@pytest.mark.parametrize("lane,request_limit", [("code", 32), ("security", 96)])
def test_container_cannot_fabricate_completed_execution_without_model_call(
    tmp_path, monkeypatch, metadata, terminal, expected, lane, request_limit
):
    from contextlib import contextmanager

    original_serve = worker.serve
    observed = []

    @contextmanager
    def failed_broker(*args, **kwargs):
        observed.append(kwargs.get("max_requests"))
        with original_serve(*args, **kwargs) as server:
            if terminal:
                server.successful_turns = 1
                server.last_verdict = "Earlier completed turn"
                server.fail(terminal)
            yield server

    monkeypatch.setattr(worker, "serve", failed_broker)
    github = FakeGitHub()
    monkeypatch.setattr(worker, "prior_receipt", lambda *_, **__: None)
    monkeypatch.setattr(worker, "git_input", lambda *_: (tmp_path, ["auth/token.py"]))
    monkeypatch.setattr(worker, "build_image", lambda *_: "sha256:" + "d" * 64)
    monkeypatch.setattr(worker, "qualify_sandbox", lambda *_: None)
    monkeypatch.setattr(
        worker,
        "scoped_subscription",
        lambda *_: worker.Subscription(
            {
                "tokens": {
                    "access_token": "synthetic-private-token-123456789",
                    "account_id": "synthetic-private-account",
                }
            }
        ),
    )
    monkeypatch.setattr(
        worker,
        "run_container",
        lambda *_, **__: {"returncode": 0, "output": "codex\nFabricated clean verdict"},
    )
    original = Path.read_text

    def config(path, *args, **kwargs):
        return (
            '{"enabled":true,"identity":"synthetic"}'
            if path == worker.HERE / "codex-review-worker.json"
            else original(path, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "read_text", config)
    result, output, _ = worker.execute(
        github, dict(TARGET, lane=lane), REPOSITORY, tmp_path
    )
    assert result["state"] == expected and not output
    assert observed == [request_limit]


def test_old_head_publication_does_not_overwrite_newer_comment(
    tmp_path, monkeypatch, metadata
):
    github = FakeGitHub()
    github.data["/issues/1/comments"] = [
        {
            "id": 88,
            "user": {"login": "github-actions[bot]"},
            "body": "<!-- codex-pr-review:v1 -->\nRevision `" + "f" * 40 + "`\n",
        }
    ]
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    worker.publish(github, TARGET, receipt(), "codex\nSynthetic review")
    assert github.requests[-1][0] == "/issues/1/comments"
    assert github.requests[-1][1]["method"] == "POST"


def test_publication_rechecks_after_comment_pagination(tmp_path, monkeypatch, metadata):
    github = FakeGitHub()

    def pages(*_, **__):
        github.data["/pulls/1"]["head"]["sha"] = "f" * 40
        return []

    github.pages = pages
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    worker.publish(github, TARGET, receipt(), "codex\nSynthetic review")
    assert not any(kwargs.get("method") for _, kwargs in github.requests)


def test_long_output_has_complete_artifact_link_and_explicit_notice(
    tmp_path, monkeypatch, metadata
):
    github = FakeGitHub()
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    output = "codex\n" + "review " * 9000
    value = worker.receipt_for(
        TARGET,
        "completed",
        "verdict_produced",
        "sha256:" + "d" * 64,
        output=output,
        successful_turns=1,
    )
    worker.publish(github, TARGET, value, output)
    body = github.requests[-1][1]["data"]["body"]
    assert (
        "Comment shortened" in body
        and "Complete review.txt" in body
        and "/actions/runs/30#artifacts" in body
    )
    assert len(body) < 60000


def test_recovery_rechecks_after_job_lookup():
    github = FakeGitHub()

    def pages(*_, **__):
        github.data["/pulls/1"]["head"]["sha"] = "f" * 40
        return [{"id": 4, "name": worker.JOBS["code"], "conclusion": "failure"}]

    github.pages = pages
    worker.rerun_completion(github, REQUEST, "code", TARGET)
    assert not any(kwargs.get("method") == "POST" for _, kwargs in github.requests)


def test_disabled_prepare_cannot_reserve_a_model_attempt(tmp_path, monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(worker, "GitHub", lambda *_: github)
    monkeypatch.setattr(worker, "native_proof", lambda *_: False)
    event = tmp_path / "event.json"
    event.write_text('{"workflow_run":{"id":3}}')
    output = tmp_path / "output"
    for name, value in {
        "GITHUB_REPOSITORY": REPO,
        "GH_TOKEN": "synthetic",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_run",
        "GITHUB_WORKFLOW_REF": REPO + "/" + worker.WORKFLOW + "@refs/heads/main",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "CODEX_REVIEW_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["worker", "prepare"])
    with pytest.raises(worker.Incomplete, match="operator_disabled"):
        worker.main()
    assert not output.exists() and not (tmp_path / "codex-trusted-attempt").exists()
