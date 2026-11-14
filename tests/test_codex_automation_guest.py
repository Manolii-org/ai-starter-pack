"""Guest assessment/judge passes: orchestration, holds and captured publication."""

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
