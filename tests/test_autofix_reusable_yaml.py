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


def test_autofix_acquisition_uses_builtin_token_without_publisher_secret() -> None:
    text = WORKFLOWS[1].read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    steps = yaml.safe_load((ROOT / '.github/workflows/pr-autofix-execution-reusable.yml').read_text())['jobs']['reusable_editor']['steps']
    editor = next(s for s in steps if s.get('id') == 'claude')
    checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout@'))
    restore = next(s for s in steps if s.get('name') == 'Restore trusted instruction surface from PR base')
    # github.token works in called workflows. secrets.GITHUB_TOKEN may be empty;
    # the separate publisher's GH_PAT must never leak into acquisition/editor work.
    assert editor['with']['github_token'] == '${{ github.token }}'
    assert checkout['with']['token'] == '${{ github.token }}'
    assert restore['env']['GH_TOKEN'] == '${{ github.token }}'
    assert 'secrets.GH_PAT ||' not in text


def test_workflow_run_staleness_is_checked_before_checkout_and_agent() -> None:
    """A delayed failure must not check out or invoke an agent for an old SHA."""

    execution = yaml.safe_load((ROOT / ".github/workflows/pr-autofix-execution-reusable.yml").read_text())
    for workflow, variant in zip(WORKFLOWS, ("standalone", "reusable")):
        caller = yaml.safe_load(workflow.read_text())
        steps = [s for job in caller['jobs'].values() for s in job.get('steps', [])]
        steps += execution['jobs'][variant+'_editor']['steps']
        stale = next(i for i,s in enumerate(steps) if '"$WR_HEAD_SHA" != "$CURRENT_HEAD_SHA"' in s.get('run',''))
        checkout = next(i for i,s in enumerate(steps) if s.get('uses','').startswith('actions/checkout@'))
        agent = next(i for i,s in enumerate(steps) if s.get('uses','').startswith('anthropics/claude-code-action@'))
        assert stale < checkout < agent
        assert steps[stale]['env']['WR_HEAD_SHA']=="${{ github.event.workflow_run.head_sha || '' }}"
        assert 'CURRENT_HEAD_SHA' in steps[stale]['run']
        assert '-n "$WR_HEAD_SHA" && -n "$CURRENT_HEAD_SHA"' in steps[stale]['run']
