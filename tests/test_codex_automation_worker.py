"""Trusted automation controller: ordering, holds, receipts and isolation."""

import json
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import codex_automation_worker as worker

TARGET = {"repository": "example/automation", "pull_request": 7, "base_sha": "b" * 40,
          "head_sha": "a" * 40, "operation": "assessment", "request_run_id": 11,
          "request_run_attempt": 1, "request_workflow_id": 5, "request_artifact_id": 9,
          "head_ref": "feature"}
COMPLETION = {"model": "gpt-6.1-sol", "model_verification": "provider_reported",
              "usage": {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 0},
              "input_chars": 40, "observed_at": "2026-11-14T03:30:00Z"}


class FakeGitHub:
    token = "fixture-token"
    repository = TARGET["repository"]


class FakeBroker:
    def __init__(self, completions, terminal=None):
        self.completions, self.terminal_error = completions, terminal

    def audit(self):
        return {"text_completions": list(self.completions)}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(worker.profiles, "select_automation",
                        lambda repo, role: events.append(("consent", role)) or {"role": role})
    monkeypatch.setattr(worker.custody, "claim",
                        lambda *a, **k: events.append(("claim", a[1]["operation"])) or "ack")

    @contextmanager
    def session(assignment, repo, role):
        events.append(("session", role))
        yield {"fixture": True}, {}

    terminal = {}

    @contextmanager
    def serve(path, subscription, **kwargs):
        assert kwargs["text_only"] is True
        events.append(("broker", kwargs["expected_effort"]))
        yield FakeBroker([COMPLETION], terminal.get("value"))

    monkeypatch.setattr(worker, "serve", serve)
    monkeypatch.setattr(worker, "Subscription", lambda auth: auth)
    outputs = {"assessment": {"status": "completed", "pass": "assessment",
                              "ran": {"skills": ["scope-adherence"], "agents": []},
                              "candidates": {"scope-adherence.json": {"source": "scope-adherence", "findings": []}}},
               "judge": {"status": "completed", "pass": "judge",
                         "publication": [{"action": "review", "event": "COMMENT", "surviving": []}]}}

    def guest(image, source, broker_dir, context_file, candidates, value, *, seconds):
        events.append(("guest", value["role"]))
        assert oct(Path(context_file).stat().st_mode & 0o777) == "0o444"
        assert json.loads(Path(context_file).read_text())["role"] == value["role"]
        events.append(("roster", value["role"], tuple(value["selected_roles"])))
        if value["role"] == "judge":
            assert (candidates / "scope-adherence.json").exists()
        return outputs[value["role"]]

    def run(**changes):
        kwargs = {"run_id": 31, "run_attempt": 1, "workflow_sha": "c" * 40, "guest": guest,
                  "session": session, "image_builder": lambda d: "sha256:" + "1" * 64,
                  "source_loader": lambda d, t, token: (d / "source.git", ["policy.py"])}
        kwargs.update(changes)
        return worker.execute(FakeGitHub(), dict(TARGET), tmp_path, **kwargs)

    return run, events, outputs, terminal


def test_consent_precedes_custody_which_precedes_any_model_work(harness):
    run, events, _, _ = harness
    result, rows = run()
    assert result["state"] == "completed" and result["publication"][0]["event"] == "COMMENT"
    kinds = [e[0] for e in events]
    assert kinds.index("claim") > kinds.index("consent") and kinds.index("claim") < kinds.index("session")
    assert [e for e in events if e[0] == "guest"] == [("guest", "assessment"), ("guest", "judge")]
    assert [r["role"] for r in rows] == ["assessment", "judge"]
    assert all(r["transport"] == "subscription_broker" and r["cost_usd"] is None for r in rows)
    assert {r["workflow_sha"] for r in rows} == {"c" * 40} and rows[0]["run_id"] == 31


def test_missing_consent_never_claims_custody_or_reaches_a_model(harness, monkeypatch):
    run, events, _, _ = harness
    def refuse(repo, role):
        raise worker.profiles.ProfileError("automation_consent_missing")
    monkeypatch.setattr(worker.profiles, "select_automation", refuse)
    result, rows = run()
    assert result == {"state": "blocked_credentials", "reason": "automation_consent_missing"} and rows == []
    assert events == []


def test_prior_custody_holds_without_session_or_guest(harness, monkeypatch):
    run, events, _, _ = harness
    def held(*a, **k):
        raise worker.custody.CustodyHold("prior_execution_claim")
    monkeypatch.setattr(worker.custody, "claim", held)
    result, _ = run()
    assert result["state"] == "held" and not any(e[0] in {"session", "guest"} for e in events)


def test_held_assessment_never_runs_the_judge(harness):
    run, events, outputs, _ = harness
    outputs["assessment"] = {"status": "held", "reason": "stage_failed:run-specialists.py"}
    result, rows = run()
    assert result["state"] == "held" and ("guest", "judge") not in events
    assert len(rows) == 1  # the spent assessment completion is still measured


def test_quota_exhaustion_is_blocked_not_retried(harness):
    run, events, _, terminal = harness
    terminal["value"] = "subscription_quota_exhausted"
    result, _ = run()
    assert result["state"] == "blocked_quota" and [e for e in events if e[0] == "guest"] == [("guest", "assessment")]


@pytest.mark.parametrize("name", ["../escape.json", "a b.json", "x.py"])
def test_guest_candidate_names_cannot_escape_the_handoff_directory(harness, name):
    run, _, outputs, _ = harness
    outputs["assessment"]["candidates"] = {name: {}}
    result, _ = run()
    assert (result["state"], result["reason"]) == ("failed", "candidate_name_invalid")


def test_empty_diff_completes_without_model_work(harness):
    run, events, _, _ = harness
    result, rows = run(source_loader=lambda d, t, token: (d, []))
    assert result["reason"] == "empty_diff" and rows == [] and not any(e[0] == "session" for e in events)


def test_guest_container_is_networkless_read_only_and_credential_free(tmp_path):
    value = worker.context_document(TARGET, worker.policy(), "assessment", 31, 1)
    args = worker.guest_args("sha256:" + "1" * 64, "n", tmp_path / "s", tmp_path / "b",
                             tmp_path / "c.json", None, value)
    joined = " ".join(args)
    assert args[args.index("--network") + 1] == "none" and "--read-only" in args
    assert "dst=/input/repo,readonly" in joined and "dst=/input/codex-automation.json,readonly" in joined
    assert "TOKEN" not in joined and "AUTH" not in joined and "DOPPLER" not in joined
    assert "--env HTTPS_PROXY=" in joined and "/input/candidates" not in joined


def test_policy_roster_excludes_security_and_restricted_roles():
    config = worker.policy()
    for name in config["roster"]:
        path = ROOT / ".claude/agents" / f"{name}.md"
        path = path if path.exists() else ROOT / ".claude/skills" / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        assert "required_security_review: true" not in text and "data_sensitivity: restricted" not in text


@pytest.mark.parametrize("change", [{"schema": "x"}, {"roster": []}, {"roster": ["Bad Name"]},
                                    {"data_sensitivity": "restricted"}, {"max_requests": 0},
                                    {"reasoning_effort": "low"}])
def test_invalid_controller_policy_is_refused(monkeypatch, tmp_path, change):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**json.loads(worker.CONFIG.read_text()), **change}))
    monkeypatch.setattr(worker, "CONFIG", path)
    with pytest.raises(worker.Incomplete):
        worker.policy()


def test_disabled_controller_admits_nothing(monkeypatch):
    monkeypatch.delenv("CODEX_AUTOMATION_ENABLED", raising=False)
    assert worker.prepare(object(), {}) is None


def test_advisory_comment_is_marked_and_bounded():
    body = worker.comment_body(TARGET, {"state": "completed", "reason": "adjudicated", "publication": [
        {"action": "review", "event": "REQUEST_CHANGES",
         "surviving": [{"file": "p.py", "line": 1, "message": "x" * 1000}] * 30}]})
    assert body.startswith(worker.MARKER) and body.count("\n- p.py:1 ") == 20 and "x" * 301 not in body


def test_workflow_is_default_off_trusted_checkout_and_secret_free():
    text = (ROOT / ".github/workflows/codex-automation-worker.yml").read_text()
    flow = yaml.safe_load(text)
    on = flow.get("on", flow.get(True))
    assert on["workflow_run"]["workflows"] == ["Codex Automation Request"]
    job = flow["jobs"]["execute"]
    assert "vars.CODEX_AUTOMATION_ENABLED == 'true'" in job["if"]
    checkout = job["steps"][0]["with"]
    assert checkout["ref"] == "${{ github.workflow_sha }}" and checkout["persist-credentials"] is False
    # Least privilege (zizmor excessive-permissions): nothing at workflow level.
    assert flow["permissions"] == {}
    assert flow["jobs"]["execute"]["permissions"] == {
        "contents": "read", "actions": "read", "pull-requests": "read", "issues": "write", "statuses": "write"}
    # The editor job never sees a secret; only the separate publisher holds one.
    execute_text = yaml.safe_dump(flow["jobs"]["execute"])
    assert "secrets." not in execute_text
    publish = flow["jobs"]["publish_fix"]
    assert publish["needs"] == "execute" and "vars.CODEX_AUTOMATION_PUBLISH == 'true'" in publish["if"]
    assert set(re.findall(r"secrets\.([A-Z_]+)", text)) == {"CODEX_AUTOMATION_PUBLISH_TOKEN"}
    assert publish["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "write"}
    steps = {step.get("name"): step for step in publish["steps"]}
    run = steps["Publish once with the separate credential"]
    assert run["env"]["PUBLICATION_ADMITTED"] == "${{ steps.custody.outputs.skip == 'false' }}"
    assert run["run"] == "python3 -I trusted/scripts/ci/autofix-publish.py"
    assert steps["Native custody and hold admission"]["run"] == "bash trusted/scripts/ci/autofix-custody.sh"


def test_image_closure_includes_the_secret_shape_catalog_or_refuses(monkeypatch, tmp_path):
    captured = {}
    def docker(*args):
        captured["context"] = Path(args[-1])
        assert (captured["context"] / "opt/automation/.ai/security/token-shapes.json").is_file()
        assert (captured["context"] / "opt/automation/scripts/run-judge.py").is_file()
        (tmp_path / "image.id").write_text("sha256:" + "2" * 64)
        return ["true"], {}
    monkeypatch.setattr(worker, "docker", docker)
    assert worker.build_image(tmp_path) == "sha256:" + "2" * 64
    monkeypatch.setattr(worker, "CLOSURE_DATA", ("missing/catalog.json",))
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(worker.Incomplete, match="failed_install"):
        worker.build_image(other)


def test_failed_guest_still_measures_spent_completions(harness):
    run, _, _, _ = harness
    def explode(*a, **k):
        raise worker.Incomplete("failed_scan")
    result, rows = run(guest=explode)
    assert result["state"] == "failed" and len(rows) == 1 and rows[0]["role"] == "assessment"


def test_comment_neutralises_markdown_mentions_and_unknown_events():
    body = worker.comment_body(TARGET, {"state": "completed", "reason": "x`y", "publication": [
        {"action": "review", "event": "APPROVE\n@everyone",
         "surviving": [{"file": "a`b.py", "line": 1,
                        "message": "``` [click](https://evil.invalid) ![i](x) @org/team\nnext"}]}]})
    assert "@\u200borg/team" in body and "@org" not in body
    assert "](https" not in body and "\\[click\\]" in body and "```" not in body
    assert "UNKNOWN" in body and "APPROVE\n" not in body
    assert body.count("\n- ") == 1 and "\nnext" not in body


class PublishGitHub(FakeGitHub):
    def __init__(self, head, comments):
        self.head, self.comments, self.posted = head, comments, []

    def json(self, route, method="GET", data=None):
        if method == "POST":
            self.posted.append(data)
            return {}
        return {"state": "open", "head": {"sha": self.head}}

    def pages(self, route):
        return self.comments


@pytest.mark.parametrize(("head", "comments", "posts"), [
    ("a" * 40, [], 1),
    ("f" * 40, [], 0),
    ("a" * 40, [{"user": {"id": 41898282}, "body": worker.MARKER + "\n<!-- head=" + "a" * 40 + " -->"}], 0),
    ("a" * 40, [{"user": {"id": 5}, "body": worker.MARKER + "\n<!-- head=" + "a" * 40 + " -->"}], 1),
])
def test_publish_rechecks_head_and_dedupes_only_on_trusted_author(monkeypatch, tmp_path, head, comments, posts):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", TARGET["repository"])
    state = tmp_path / "codex-automation"
    (state / "result").mkdir(parents=True)
    (state / "target.json").write_text(json.dumps(TARGET))
    (state / "result/result.json").write_text(json.dumps({"state": "completed", "publication": []}))
    fake = PublishGitHub(head, comments)
    monkeypatch.setattr(worker, "GitHub", lambda repo, token: fake)
    assert worker.main(["x", "publish"]) == 0
    assert len(fake.posted) == posts


def test_judge_context_expects_exactly_the_dispatched_roles(harness):
    """Found by a real-container run: a judge roster wider than the dispatched
    roles makes every undispatched role count as missing evidence."""
    run, events, outputs, _ = harness
    outputs["assessment"]["ran"] = {"skills": ["scope-adherence", "security-boundary-test"], "agents": ["systems-consistency"]}
    result, _ = run()
    rosters = {e[1]: e[2] for e in events if e[0] == "roster"}
    assert result["state"] == "completed"
    assert rosters["judge"] == ("scope-adherence", "security-boundary-test", "systems-consistency")
    assert set(rosters["assessment"]) == set(worker.policy()["roster"])


@pytest.mark.parametrize("ran", [{"skills": ["Bad Name"]}, {"agents": [1]}])
def test_invalid_dispatched_roles_fail_closed_before_the_judge(harness, ran):
    run, events, outputs, _ = harness
    outputs["assessment"]["ran"] = ran
    result, _ = run()
    assert result["reason"] == "dispatched_roles_invalid" and ("guest", "judge") not in events


def test_guest_image_closure_has_only_installed_third_party_imports():
    """Found by a real-container run: the image lacked PyYAML. Every non-stdlib
    import in the guest closure must be installed by the runtime Dockerfile."""
    import ast
    packages = {"yaml": "python3-yaml"}
    closure = ["scripts/run-pr-classifier.py", "scripts/run-specialists.py", "scripts/run-broad-agents.py",
               "scripts/run-judge.py", "scripts/ci/codex_automation_guest.py", "scripts/ci/codex_automation_adapter.py",
               "scripts/ci/codex_automation_context.py", "scripts/ci/codex_subscription_text.py",
               "scripts/ci/codex_text_response.py", "scripts/lib/subscription_guest.py",
               "scripts/lib/agent_capabilities.py", "scripts/lib/sast_evidence.py"]
    local = {p.stem for p in (ROOT / "scripts").rglob("*.py")} | {"lib", "ci"}
    found = set()
    for rel in closure:
        for node in ast.walk(ast.parse((ROOT / rel).read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else [])
            found |= {n.split(".")[0] for n in names}
    third_party = found - set(sys.stdlib_module_names) - local
    assert third_party <= set(packages), third_party
    dockerfile = (ROOT / "scripts/ci/codex-automation-runtime/Dockerfile").read_text()
    for name in third_party:
        assert packages[name] in dockerfile
    assert "https://deb.debian.org" in dockerfile and "ARG APT_PROXY=DIRECT" in dockerfile


def test_held_detail_is_retained_but_never_published():
    body = worker.comment_body(TARGET, {"state": "held", "reason": "stage_failed:run-pr-classifier.py",
                                        "passes": {"assessment": {"detail": "SECRET-LIKE-STDERR"}}})
    assert "SECRET-LIKE-STDERR" not in body



def enable_autofix(monkeypatch, tmp_path, *, rounds=0, admission=True, publish="true"):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**json.loads(worker.CONFIG.read_text()), "autofix": True}))
    monkeypatch.setattr(worker, "CONFIG", path)
    monkeypatch.setenv("CODEX_AUTOMATION_PUBLISH", publish)
    monkeypatch.setattr(worker, "prior_autofix_rounds", lambda g, t: rounds)
    monkeypatch.setattr(worker, "editor_admission", lambda t: admission)


def test_autofix_is_off_by_default_and_never_claims_or_runs(harness):
    run, events, outputs, _ = harness
    outputs["judge"]["publication"] = [{"action": "review", "event": "COMMENT",
                                        "surviving": [{"file": "p.py", "line": 1, "message": "m"}]}]
    result, _ = run()
    assert "autofix" not in result and not any(e[1:] == ("autofix",) for e in events if len(e) > 1)
    assert worker.policy().get("autofix", False) is False


def test_autofix_runs_after_judge_with_its_own_consent_custody_and_receipts(harness, monkeypatch, tmp_path):
    run, events, outputs, _ = harness
    enable_autofix(monkeypatch, tmp_path)
    outputs["judge"]["publication"] = [{"action": "review", "event": "REQUEST_CHANGES", "surviving": [
        {"file": "p.py", "line": 2, "message": "wildcard accepted", "fix": "reject", "severity": "ERROR",
         "finding_id": "abc", "extra": "dropped"}]}]
    claims = []
    monkeypatch.setattr(worker.custody, "claim", lambda g, t, **k: claims.append(t["operation"]) or "ack")
    outputs["autofix"] = {"status": "completed", "pass": "autofix", "outcome": "no_safe_fix"}
    seen = {}

    result, rows = run(guest=lambda image, source, b, c, cand, value, *, seconds: (
        seen.setdefault(value["role"], json.loads((cand / "autofix-findings.json").read_text()) if value["role"] == "autofix" else None),
        outputs[value["role"]])[1])
    assert claims == ["assessment", "autofix"]
    assert ("consent", "autofix") in events
    assert seen["autofix"] == [{"file": "p.py", "line": 2, "severity": "ERROR", "message": "wildcard accepted",
                                "fix": "reject", "finding_id": "abc"}]
    assert result["autofix"] == {"outcome": "no_safe_fix"}
    assert [r["role"] for r in rows] == ["assessment", "judge", "autofix"]


def test_autofix_without_surviving_findings_spends_nothing(harness, monkeypatch, tmp_path):
    run, events, _, _ = harness
    enable_autofix(monkeypatch, tmp_path)
    result, rows = run()
    assert result["autofix"] == {"outcome": "nothing_to_fix"} and ("consent", "autofix") not in events
    assert [r["role"] for r in rows] == ["assessment", "judge"]


def test_autofix_consent_refusal_never_claims_custody(harness, monkeypatch, tmp_path):
    run, _, outputs, _ = harness
    enable_autofix(monkeypatch, tmp_path)
    outputs["judge"]["publication"] = [{"action": "review", "event": "COMMENT",
                                        "surviving": [{"file": "p.py", "line": 1, "message": "m"}]}]
    claims = []
    monkeypatch.setattr(worker.custody, "claim", lambda g, t, **k: claims.append(t["operation"]) or "ack")

    def consent(repo, role):
        if role == "autofix":
            raise worker.profiles.ProfileError("automation_role_not_consented")
        return {"role": role}
    monkeypatch.setattr(worker.profiles, "select_automation", consent)
    result, _ = run()
    assert result["autofix"] == {"outcome": "blocked_credentials", "reason": "automation_role_not_consented"}
    assert claims == ["assessment"]


@pytest.mark.parametrize("event", ["APPROVE", "UNKNOWN"])
def test_only_findings_from_a_blocking_or_comment_review_are_repairable(event):
    assert worker.surviving_findings([{"action": "review", "event": event,
                                       "surviving": [{"file": "p.py", "line": 1}]}]) == []
    assert worker.surviving_findings([{"action": "advisory", "detail": "x"}]) == []



FINDING_REVIEW = [{"action": "review", "event": "COMMENT", "surviving": [{"file": "p.py", "line": 1, "message": "m"}]}]


@pytest.mark.parametrize(("kwargs", "outcome"), [
    ({"publish": "false"}, {"outcome": "publication_disabled"}),
    ({"rounds": 1}, {"outcome": "round_cap_reached", "rounds": 1}),
    ({"rounds": None}, {"outcome": "held", "reason": "autofix_history_unverified"}),
    ({"admission": "editor_admission_held"}, {"outcome": "held", "reason": "editor_admission_held"}),
])
def test_autofix_preconditions_hold_before_consent_custody_or_model(harness, monkeypatch, tmp_path, kwargs, outcome):
    """Review P1/P2: no self-repair loop, no editor without publication, and the
    accepted custody/owner gate runs before any editor work."""
    run, events, outputs, _ = harness
    enable_autofix(monkeypatch, tmp_path, **kwargs)
    outputs["judge"]["publication"] = FINDING_REVIEW
    claims = []
    monkeypatch.setattr(worker.custody, "claim", lambda g, t, **k: claims.append(t["operation"]) or "ack")
    result, rows = run()
    assert result["autofix"] == outcome and result["publication"] == FINDING_REVIEW
    assert claims == ["assessment"] and ("consent", "autofix") not in events
    assert [r["role"] for r in rows] == ["assessment", "judge"]


def test_autofix_failure_never_discards_the_finished_judge_outcome(harness, monkeypatch, tmp_path):
    run, _, outputs, _ = harness
    enable_autofix(monkeypatch, tmp_path)
    outputs["judge"]["publication"] = FINDING_REVIEW
    def boom(*a, **k):
        raise OSError("custody POST failed")
    monkeypatch.setattr(worker.custody, "claim", lambda g, t, **k: boom() if t["operation"] == "autofix" else "ack")
    result, _ = run()
    assert result["state"] == "completed" and result["publication"] == FINDING_REVIEW
    assert result["autofix"] == {"outcome": "failed", "reason": "OSError"}


class CommitsGitHub(FakeGitHub):
    def __init__(self, messages):
        self.messages = messages

    def pages(self, route):
        assert route == "/pulls/7/commits"
        return [{"commit": {"message": m}} for m in self.messages]


def test_prior_rounds_count_only_autofix_commits():
    assert worker.prior_autofix_rounds(CommitsGitHub(["feat: x", "[autofix] a", "fix: [autofix] b"]), TARGET) == 1
    assert worker.prior_autofix_rounds(CommitsGitHub(["x"] * 250), TARGET) is None


def test_editor_admission_uses_the_accepted_custody_script(monkeypatch, tmp_path):
    seen = {}
    def fake_run(cmd, env, **kwargs):
        seen["cmd"], seen["env"] = cmd, env
        Path(env["GITHUB_OUTPUT"]).write_text("skip=false\n")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    assert worker.editor_admission(TARGET) is True
    assert seen["cmd"][1].endswith("scripts/ci/autofix-custody.sh")
    assert seen["env"]["EXPECTED_HEAD"] == TARGET["head_sha"] and seen["env"]["PR_NUMBER"] == "7"
    def held(cmd, env, **kwargs):
        Path(env["GITHUB_OUTPUT"]).write_text("skip=true\n")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(worker.subprocess, "run", held)
    assert worker.editor_admission(TARGET) == "editor_admission_held"
