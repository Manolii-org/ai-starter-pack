"""Pack Autofix reusable workflow must be valid YAML.

A bare markdown line at column 0 (e.g. **[autofix-budget]**) ends a
`run: |` block and GitHub reports "workflow file issue" with zero jobs —
observed on Buro-Built/buromaster Autofix runs after v1.9.4 (2026-08-05).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github/workflows/pr-autofix-loop.yml",
    ROOT / ".github/workflows/pr-autofix-loop-reusable.yml",
)


def test_all_autofix_workflows_parse_as_yaml() -> None:
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        # No bare ** markdown at column 0 (breaks run:| blocks)
        for i, line in enumerate(text.splitlines(), 1):
            assert not line.startswith("**"), (
                f"{workflow.name}:{i} bare '**' at column 0 breaks YAML "
                f"(pack Autofix zero-job failures). Line: {line[:80]!r}"
            )
        spec = yaml.safe_load(text)
        assert "jobs" in spec

    reusable = yaml.safe_load(WORKFLOWS[1].read_text(encoding="utf-8"))
    assert "workflow_call" in (reusable.get("on") or reusable.get(True) or {})


def test_autofix_uses_github_token_fallback() -> None:
    text = WORKFLOWS[1].read_text(encoding="utf-8")
    expected = "github_token: ${{ secrets.GH_PAT || github.token }}"
    assert expected in text, (
        "reusable Autofix must fall back to github.token — secrets.GITHUB_TOKEN "
        "is empty in called workflows and breaks private-repo git fetch"
    )
    assert "github_token: ${{ secrets.GH_PAT || secrets.GITHUB_TOKEN }}" not in text


def test_workflow_run_staleness_is_checked_before_checkout_and_agent() -> None:
    """A delayed failure must not check out or invoke an agent for an old SHA."""

    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        stale_check = '"$WR_HEAD_SHA" != "$CURRENT_HEAD_SHA"'
        checkout = "uses: actions/checkout@"
        agent = "uses: anthropics/claude-code-action@"
        assert "WR_HEAD_SHA: ${{ github.event.workflow_run.head_sha || '' }}" in text
        assert "gh api \"repos/${REPOSITORY}/pulls/${PR_NUM}\" --jq '.head.sha'" in text
        assert text.index(stale_check) < text.index(checkout) < text.index(agent)
        # Missing API/SHA evidence is deliberately fail-open so a current failure
        # still reaches repair; only a proven inequality suppresses dispatch.
        assert '-n "$WR_HEAD_SHA" && -n "$CURRENT_HEAD_SHA"' in text
