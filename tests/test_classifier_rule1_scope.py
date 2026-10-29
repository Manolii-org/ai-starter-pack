"""RULE 1 must not route every workflow change to systems-consistency.

Regression (#252): CI-only workflow PRs ran systems-consistency without any
deployment files, so every check was "could not be evaluated"; the Judge then
truncated adjudicating those notes and the PR got "assessment error".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = [ROOT / ".claude/agents/pr-classifier.md",
          ROOT / "plugin/manolii-framework/agents/pr-classifier.md",
          ROOT / "registry/platform/framework/agents/pr-classifier.md"]


def _rule1(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^RULE 1:.*?(?=^RULE 2:)", text, re.M | re.S)
    assert match, f"RULE 1 not found in {path}"
    return match.group(0)


@pytest.mark.parametrize("path", [p for p in COPIES if p.exists()], ids=lambda p: str(p.relative_to(ROOT)))
def test_rule1_requires_a_deploy_step_for_workflow_changes(path: Path) -> None:
    rule = _rule1(path)
    first_line = rule.splitlines()[0]
    assert ".github/workflows" not in first_line, "workflow paths alone must not trigger RULE 1"
    assert "deploy step" in rule
    assert "does not trigger RULE 1" in rule
    assert 'add "systems-consistency"' in rule
    assert "deploy/**" in first_line and "fly.toml" in first_line
    # Codex review: installing flyctl deploys nothing.
    assert "Installing a CLI" in rule and "is not a deploy step" in rule


def test_agent_trigger_description_matches_rule1() -> None:
    text = (ROOT / ".claude/agents/systems-consistency.md").read_text(encoding="utf-8")
    assert "A workflow change with no deploy step does not trigger it." in text
    assert "`.github/workflows/**`, or `config.yaml`" not in text
