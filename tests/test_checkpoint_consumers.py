"""Exercise retained checkpoint consumers without GitHub or provider calls."""
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "unfinished_checkpoint", ROOT / "scripts/session-unfinished-work-check.py"
)
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


def checkpoint(tmp_path, monkeypatch, **fields):
    path = tmp_path / ".ai/sessions/active-task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    task = {"branch": "feature/test", "steps": [{"id": "child", "status": "done"}]}
    task.update(fields)
    path.write_text(json.dumps(task))
    monkeypatch.setattr(CHECKER, "ACTIVE_TASK_PATH", path)
    return path


@pytest.mark.parametrize("field", ["pr", "pr_url", "pr_number"])
@pytest.mark.parametrize("status", ["active", "in_progress", "merged", ""])
def test_pr_cannot_hide_unfinished_or_unknown_parent(tmp_path, monkeypatch, field, status):
    path = checkpoint(tmp_path, monkeypatch, status=status, **{field: 12})
    before = path.read_bytes()
    findings = CHECKER._active_task_gaps("feature/test")
    assert findings and "parent work" in findings[0]
    assert path.read_bytes() == before


def test_parent_warning_does_not_echo_checkpoint_values(tmp_path, monkeypatch):
    checkpoint(tmp_path, monkeypatch, status="sensitive-value", pr_url="sensitive-value")
    assert "sensitive-value" not in " ".join(CHECKER._active_task_gaps("feature/test"))


@pytest.mark.parametrize("status", sorted(CHECKER.TERMINAL_TASK_STATUSES))
def test_retained_terminal_checkpoint_is_not_unfinished(tmp_path, monkeypatch, status):
    path = checkpoint(tmp_path, monkeypatch, status=status, pr=12)
    before = path.read_bytes()
    assert CHECKER._active_task_gaps("feature/test") == []
    assert path.read_bytes() == before


def test_wait_keeps_existing_owner_even_from_another_branch(tmp_path, monkeypatch):
    checkpoint(tmp_path, monkeypatch, status="waiting_on_ci", pr=12)
    findings = CHECKER._active_task_gaps("main")
    assert "RESUME CI wait" in findings[0]
    assert "do not start a second actor" in findings[0]


def test_unknown_adapter_cannot_be_hidden_by_legacy_done(tmp_path, monkeypatch):
    checkpoint(tmp_path, monkeypatch, status="done", continuation={"version": 99})
    assert "owning adapter" in CHECKER._active_task_gaps("feature/test")[0]


@pytest.mark.parametrize("retained", [
    {"interrupt_value": {}}, {"resume_value": {"decision": "declined"}},
    {"resume_value": {}}, {"active_step_id": "child"},
    {"steps": [{"id": "child", "status": "partial"}]},
    {"steps": [{"id": "child", "status": "done", "background_agent_id": "worker"}]},
])
def test_done_cannot_clear_held_or_unfinished_custody(tmp_path, monkeypatch, retained):
    checkpoint(tmp_path, monkeypatch, status="done", pr=12,
               last_updated="2000-01-01T00:00:00Z", **retained)
    assert CHECKER._active_task_gaps("feature/test")
    assert "Retained terminal checkpoint" not in run_drift(tmp_path)


@pytest.mark.parametrize("status", sorted(CHECKER.TERMINAL_TASK_STATUSES) + ["active", "merged"])
def test_actual_drift_command_preserves_terminal_history(tmp_path, monkeypatch, status):
    path = checkpoint(tmp_path, monkeypatch, status=status, last_updated="2000-01-01T00:00:00Z")
    output = run_drift(tmp_path)
    assert ("STALE CHECKPOINT" in output) == (status in {"active", "merged"})
    assert path.exists()


@pytest.mark.parametrize("timestamp", [None, 123, {}, "bad", "2000-01-01", "9999-01-01T00:00:00Z"])
def test_actual_drift_command_reports_unknown_freshness(tmp_path, monkeypatch, timestamp):
    path = checkpoint(tmp_path, monkeypatch, status="active", last_updated=timestamp)
    before = path.read_bytes()
    assert "freshness unknown" in run_drift(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("bundled", [True, False])
def test_current_command_handles_older_consumer_checker(tmp_path, monkeypatch, bundled):
    path = checkpoint(tmp_path, monkeypatch, status="done")
    before = path.read_bytes()
    old_checker = tmp_path / "scripts/session-unfinished-work-check.py"
    old_checker.parent.mkdir(parents=True)
    old_checker.write_text("# Legacy checker lacks the new local terminal adapter.\n")
    output = run_drift(tmp_path, plugin_root=ROOT if bundled else None)
    assert ("Retained terminal checkpoint" in output) == bundled
    assert ("terminal state unknown" in output) != bundled
    assert path.read_bytes() == before


def run_drift(tmp_path, plugin_root=ROOT):
    text = (ROOT / ".claude/commands/drift-check.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)\n```", text, re.S)
    command = next(block for block in blocks if "if [ -f .ai/sessions/active-task.json ]" in block)
    result = subprocess.run(
        ["bash", "-c", command], cwd=tmp_path, check=True,
        capture_output=True, text=True, timeout=10,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1",
             **({"CLAUDE_PLUGIN_ROOT": str(plugin_root)} if plugin_root else {})},
    )
    return result.stdout
