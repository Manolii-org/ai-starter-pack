"""Activation contract for pr-autofix-loop-reusable (WI-3).

`activation` is `active` when the agent step succeeded and `skipped` when any
skip gate fired. The test keeps the expression in step with the gates: a new
step that writes `skip=true` must be added to it.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/pr-autofix-loop-reusable.yml"


def _spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_activation_output_is_declared() -> None:
    spec = _spec()
    call = spec.get("on", spec.get(True))["workflow_call"]  # PyYAML reads `on:` as True
    assert call["outputs"]["activation"]["value"] == "${{ jobs.autofix.outputs.activation }}"
    assert "fail_on_inert" not in call["inputs"], "autofix has no inert state to fail on"


def test_activation_covers_the_agent_step_and_every_skip_gate() -> None:
    job = _spec()["jobs"]["autofix"]
    expr = job["outputs"]["activation"]
    steps = job["steps"]
    verify = next(s for s in steps if s.get("id") == "verify_exec")
    # `active` must mean the execution file was verified, not merely that the
    # action step exited 0 (Codex review, PR #246).
    assert verify["if"] == "steps.claude.outcome == 'success'"
    assert "EXECUTION_FILE" in verify["env"]
    assert "steps.verify_exec.outcome == 'success' && 'active'" in expr
    assert "steps.claude.outcome" not in expr
    gates = [s["id"] for s in steps if 'echo "skip=true"' in s.get("run", "")]
    assert gates, "expected skip gates in the autofix job"
    for gate in gates:
        assert f"steps.{gate}.outputs.skip == 'true'" in expr, f"gate {gate!r} missing from activation"
    assert "'inert'" not in expr
