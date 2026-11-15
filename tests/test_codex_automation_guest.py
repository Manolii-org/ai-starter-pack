"""Guest assessment/judge passes: orchestration, holds and captured publication."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ci import codex_automation_adapter as adapter
from ci import codex_automation_guest as guest
from test_codex_automation_adapter import guest_module, mounted  # noqa: F401


@pytest.fixture
def sandbox(mounted, monkeypatch, tmp_path):  # noqa: F811
    """Point the guest's fixed mount paths at a synthetic bare repository."""
    value, path, write = mounted
    work = tmp_path / "work-src"
    work.mkdir()
    def git(*args, cwd=work):
        return subprocess.check_output(["git", "-C", str(cwd), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-q")
    git("config", "user.email", "offline@example.invalid")
    git("config", "user.name", "Offline fixture")
    (work / "policy.py").write_text("# null intentionally allowed\ndef allowed(origin):\n    return origin == 'null'\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (work / "test_policy.py").write_text("def test_denial():\n    assert allowed('tenant-b') is False\n")
    git("add", ".")
    git("commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    bare = tmp_path / "source.git"
    subprocess.check_call(["git", "clone", "-q", "--bare", str(work), str(bare)])
    value.update(base_sha=base, head_sha=head)
    write(value)
    monkeypatch.setenv("PR_BASE_SHA", base)
    monkeypatch.setenv("PR_HEAD_SHA", head)
    monkeypatch.setattr(guest, "CONTEXT", path)
    monkeypatch.setattr(guest, "REPO", bare)
    monkeypatch.setattr(guest, "STATE", tmp_path / "state")
    monkeypatch.setattr(guest, "CANDIDATES_IN", tmp_path / "candidates-in")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.chdir(tmp_path)
    return value, write, tmp_path


def fake_stages(monkeypatch, manifest, calls):
    def run(script, args, value, work):
        calls.append((script, args))
        if script == "run-pr-classifier.py":
            Path(args[args.index("--output") + 1]).write_text(json.dumps(manifest))
        else:
            out = Path(args[args.index("--candidates-dir") + 1])
            names = args[args.index("--skills" if "--skills" in args else "--agents") + 1].split(",")
            for name in names:
                (out / f"{name}.json").write_text(json.dumps({"source": name, "findings": []}))
    monkeypatch.setattr(guest, "_run", run)


def test_assessment_pass_runs_every_requested_role_and_reports_held_roster(sandbox, monkeypatch):
    calls = []
    fake_stages(monkeypatch, {"invoke_skills": ["scope-adherence", "sast-scan"],
                              "invoke_agents": ["systems-consistency"], "depth": "standard"}, calls)
    result = guest.assessment(guest._context())
    assert [c[0] for c in calls] == ["run-pr-classifier.py", "run-specialists.py", "run-broad-agents.py"]
    assert "scope-adherence,sast-scan" in calls[1][1]
    assert result["held"] == ["sast-scan"]
    assert set(result["candidates"]) == {"scope-adherence.json", "sast-scan.json", "systems-consistency.json"}
    diff = (guest.STATE / "work/pr.diff").read_text()
    assert "test_policy.py" in diff and "policy.py" in diff


@pytest.mark.parametrize("manifest", [
    {"invoke_skills": [1]}, {"invoke_skills": ["a,b"]}, {"invoke_agents": [None]},
])
def test_invalid_classifier_manifest_holds(sandbox, monkeypatch, manifest):
    fake_stages(monkeypatch, manifest, [])
    with pytest.raises(guest.GuestHold, match="classifier_manifest_invalid"):
        guest.assessment(guest._context())


def test_unreadable_candidate_revision_holds(sandbox, monkeypatch):
    value, write, _ = sandbox
    value["head_sha"] = "f" * 40
    write(value)
    monkeypatch.setenv("PR_HEAD_SHA", "f" * 40)
    with pytest.raises(guest.GuestHold, match="candidate_diff_unavailable"):
        guest.assessment(guest._context())


def test_candidate_output_budget_is_enforced(sandbox, monkeypatch):
    monkeypatch.setattr(guest, "MAX_CANDIDATE_BYTES", 10)
    fake_stages(monkeypatch, {"invoke_skills": ["scope-adherence"]}, [])
    with pytest.raises(guest.GuestHold, match="over_budget"):
        guest.assessment(guest._context())


def test_main_emits_one_held_document_and_never_a_partial_result(sandbox, monkeypatch, capsys):
    def boom(*a, **k):
        raise guest.GuestHold("stage_failed:run-specialists.py")
    monkeypatch.setattr(guest, "assessment", boom)
    assert guest.main() == 3
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and json.loads(out[0]) == {"status": "held", "reason": "stage_failed:run-specialists.py"}


def test_unsupported_role_is_held(sandbox, monkeypatch, capsys):
    value, write, _ = sandbox
    value["role"] = "critic"
    write(value)
    assert guest.main() == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "unsupported_guest_role"


def test_judge_pass_runs_unchanged_decision_logic_and_captures_publication(sandbox, mounted, monkeypatch):  # noqa: F811
    value, write, _ = sandbox
    value["role"] = "judge"
    write(value)
    module = guest_module("run-judge.py", mounted, monkeypatch)
    monkeypatch.setattr(module, "_urlopen_https", lambda *a, **k: pytest.fail("guest must not reach GitHub"))
    candidate = module._policy_module.bind_review_candidate({
        "source": "systems-consistency", "first_party": False,
        "findings": [{"file": "policy.py", "line": 3, "severity": "ERROR",
                      "message": "Origin 'null' is accepted.", "fix": "Reject `null` in `policy.py:3`."}]})
    inbox = guest.CANDIDATES_IN
    inbox.mkdir()
    (inbox / "systems-consistency.json").write_text(json.dumps(candidate))
    calls = []

    def respond(system, user, **kwargs):
        calls.append(kwargs["role"])
        return {"text": json.dumps({"decisions": [], "investigations": []})}
    monkeypatch.setattr(adapter.transport, "invoke", respond)
    result = guest.judge(guest._context(), module=module)
    assert result["pass"] == "judge" and result["publication"], result
    assert all(item["action"] in {"review", "advisory", "dismiss"} for item in result["publication"])
    assert set(calls) <= {"judge"}


def test_stages_share_one_bounded_time_budget(sandbox, monkeypatch):
    monkeypatch.setattr(guest, "DEADLINE", guest.time.monotonic() + 10)
    with pytest.raises(guest.GuestHold, match="guest_time_budget_exhausted"):
        guest._remaining()
    monkeypatch.setattr(guest, "DEADLINE", guest.time.monotonic() + 5000)
    assert guest._remaining() == guest.STAGE_SECONDS


def test_stage_failure_returns_bounded_detail_for_the_artifact(sandbox, monkeypatch, capsys):
    def boom(*a, **k):
        raise guest.GuestHold("stage_failed:run-pr-classifier.py", "x" * 2000 + "TAIL")
    monkeypatch.setattr(guest, "assessment", boom)
    assert guest.main() == 3
    held = json.loads(capsys.readouterr().out)
    assert held["reason"] == "stage_failed:run-pr-classifier.py" and held["detail"].endswith("TAIL")
    assert len(held["detail"]) == 600



class FakeAdapter:
    def __init__(self, text):
        self.text, self.calls = text, []

    def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return self.text


AUTOFIX_PATCH = ("diff --git a/policy.py b/policy.py\n--- a/policy.py\n+++ b/policy.py\n@@ -1,3 +1,3 @@\n"
                 " # null intentionally allowed\n def allowed(origin):\n-    return origin == 'null'\n"
                 "+    return origin == 'null' and origin is not None\n")


def autofix_inbox(sandbox):
    value, write, _ = sandbox
    value["role"] = "autofix"
    value["selected_roles"] = []
    write(value)
    guest.CANDIDATES_IN.mkdir()
    (guest.CANDIDATES_IN / "autofix-findings.json").write_text(json.dumps(
        [{"file": "policy.py", "line": 3, "message": "tighten", "fix": "f"}]))
    return value


def test_autofix_pass_builds_one_bundled_candidate_from_a_valid_proposal(sandbox):
    from ci import codex_automation_patch as patcher
    value = autofix_inbox(sandbox)
    fake = FakeAdapter(json.dumps({"summary": "Tighten origin check", "patch": AUTOFIX_PATCH}))
    result = guest.autofix(guest._context(), adapter=fake, patcher=patcher)
    assert result["outcome"] == "candidate" and result["paths"] == ["policy.py"]
    assert fake.calls[0][2]["role"] == "autofix" and "policy.py" in fake.calls[0][1]
    assert "def allowed" in json.loads(fake.calls[0][1])["files_at_head"]["policy.py"]
    assert result["bundle_b64"] and len(result["candidate_sha"]) == 40
    assert value["head_sha"] != result["candidate_sha"]


def test_autofix_no_safe_fix_produces_no_candidate(sandbox):
    from ci import codex_automation_patch as patcher
    autofix_inbox(sandbox)
    fake = FakeAdapter(json.dumps({"summary": "no safe fix", "patch": ""}))
    assert guest.autofix(guest._context(), adapter=fake, patcher=patcher) == {"pass": "autofix", "outcome": "no_safe_fix"}


@pytest.mark.parametrize("text", [
    "not json", "[]", "1", json.dumps({"summary": "x"}),
    json.dumps({"summary": "x", "patch": "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"}),
    json.dumps({"summary": "x", "patch": AUTOFIX_PATCH.replace("origin == 'null'\n+", "origin == 'nul'\n+")}),
])
def test_autofix_refused_proposals_hold_through_main(sandbox, monkeypatch, capsys, text):
    from ci import codex_automation_patch as patcher
    autofix_inbox(sandbox)
    monkeypatch.setattr(guest, "_adapter", lambda: FakeAdapter(text))
    monkeypatch.setattr(guest, "_load", lambda name: patcher)
    assert guest.main() == 3
    assert json.loads(capsys.readouterr().out)["status"] == "held"



def test_autofix_uses_the_accepted_guest_loader_and_holds_on_import_failure(sandbox, monkeypatch, capsys):
    """Found by a real-container run: a bare file load of the adapter fails its
    package-relative import and crashed the guest instead of holding."""
    import inspect
    assert "subscription_guest.py" in inspect.getsource(guest._adapter)
    autofix_inbox(sandbox)
    def broken():
        raise ImportError("attempted relative import with no known parent package")
    monkeypatch.setattr(guest, "_adapter", broken)
    assert guest.main() == 3
    assert json.loads(capsys.readouterr().out)["status"] == "held"


def test_assessment_passes_admitted_pr_metadata_to_classifier_and_specialists(sandbox, monkeypatch):
    """Codex review P2: scope-adherence needs the title/body; they arrive as
    untrusted data, and a leading '-' must stay a value, not an option."""
    meta = guest.STATE.parent / "pr-meta.json"
    meta.write_text(json.dumps({"title": "-x Fix origin", "body": "Closes #1 and adds docs"}))
    monkeypatch.setattr(guest, "PR_META", meta)
    calls = []
    fake_stages(monkeypatch, {"invoke_skills": ["scope-adherence"]}, calls)
    value = guest._context()
    guest.assessment(value)
    assert "--title=-x Fix origin" in calls[0][1] and "--body=Closes #1 and adds docs" in calls[0][1]
    env = guest._stage_env(value)
    assert env["PR_TITLE"] == "-x Fix origin" and env["PR_BODY"] == "Closes #1 and adds docs"


@pytest.mark.parametrize("content", ['{"title": "t"}', '{"title": 1, "body": ""}', "[]"])
def test_malformed_pr_metadata_holds(sandbox, monkeypatch, content):
    meta = guest.STATE.parent / "pr-meta.json"
    meta.write_text(content)
    monkeypatch.setattr(guest, "PR_META", meta)
    with pytest.raises(guest.GuestHold, match="pr_metadata_invalid"):
        guest._stage_env(guest._context())


def test_absent_pr_metadata_is_empty_not_an_error(sandbox, monkeypatch):
    monkeypatch.setattr(guest, "PR_META", guest.STATE.parent / "absent.json")
    env = guest._stage_env(guest._context())
    assert env["PR_TITLE"] == "" and env["PR_BODY"] == ""


@pytest.mark.parametrize("body", ["界" * 30000, "\U0001F600" * 20000, '"\\' * 40000, "\n\t" * 40000])
def test_worst_case_bodies_written_by_the_host_are_accepted_by_the_guest(sandbox, monkeypatch, body):
    """Independent review P1: JSON escaping of a bounded body must stay within the
    guest's file bound (non-ASCII is written raw; quotes/backslashes at most double)."""
    from ci import codex_automation_worker as worker
    out = guest.STATE.parent / "meta-out"
    out.mkdir()
    path = worker.write_pr_meta(out, {"title": "界" * 2000, "body": body})
    monkeypatch.setattr(guest, "PR_META", path)
    meta = guest._pr_meta()
    assert meta["body"].endswith("[truncated]") or meta["body"] == body
    assert len(meta["body"].encode()) <= 64 * 1024 + 20 and len(meta["title"].encode()) <= 1024 + 20


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b'{"title": "a", "body": "b"'])
def test_undecodable_pr_metadata_holds_with_its_own_reason(sandbox, monkeypatch, raw):
    meta = guest.STATE.parent / "bad-meta.json"
    meta.write_bytes(raw)
    monkeypatch.setattr(guest, "PR_META", meta)
    with pytest.raises(guest.GuestHold, match="pr_metadata_invalid"):
        guest._pr_meta()


def _workflow_repo(tmp_path, files_base, files_head):
    src = tmp_path / "wf-src"
    src.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(src), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-q")
    git("config", "user.email", "offline@example.invalid")
    git("config", "user.name", "Offline fixture")
    for files in (files_base, files_head):
        for name, text in files.items():
            (src / name).parent.mkdir(parents=True, exist_ok=True)
            (src / name).write_text(text)
        git("add", ".")
        git("commit", "-qm", "c")
    base, head = git("rev-parse", "HEAD~1"), git("rev-parse", "HEAD")
    bare = tmp_path / "wf.git"
    subprocess.check_call(["git", "clone", "-q", "--bare", str(src), str(bare)])
    return bare, base, head


DEPLOY = "jobs:\n  deploy:\n    timeout-minutes: {t}\n    steps:\n      - run: flyctl deploy --remote-only\n"


def test_timeout_only_workflow_change_keeps_the_deployment_review_floor(sandbox, monkeypatch, tmp_path):
    """Codex review P2 (#347): the classifier's deployment floor reads complete
    workflow files from its cwd; the guest workspace must hold the admitted
    head's changed workflows or a timeout-only edit drops systems-consistency."""
    bare, base, head = _workflow_repo(tmp_path, {".github/workflows/deploy.yml": DEPLOY.format(t=10)},
                                      {".github/workflows/deploy.yml": DEPLOY.format(t=20)})
    monkeypatch.setattr(guest, "REPO", bare)
    work = tmp_path / "work"
    work.mkdir()
    guest._workflow_heads({"base_sha": base, "head_sha": head}, work)
    assert (work / ".github/workflows/deploy.yml").read_text() == DEPLOY.format(t=20)
    spec = importlib.util.spec_from_file_location("_cls", Path(guest.ROOT) / "scripts/run-pr-classifier.py")
    cls = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cls)
    sections = {".github/workflows/deploy.yml": ["-    timeout-minutes: 10\n+    timeout-minutes: 20\n"]}
    assert cls._deploying_workflows(sections, work) == [".github/workflows/deploy.yml"]
    assert cls._deploying_workflows(sections, tmp_path / "empty") == []


def test_workflow_copies_are_bounded(sandbox, monkeypatch, tmp_path):
    bare, base, head = _workflow_repo(tmp_path, {"a.txt": "a"}, {".github/workflows/big.yml": "x" * 300000})
    monkeypatch.setattr(guest, "REPO", bare)
    with pytest.raises(guest.GuestHold, match="candidate_workflow_over_budget"):
        guest._workflow_heads({"base_sha": base, "head_sha": head}, tmp_path)
