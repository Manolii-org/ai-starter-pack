"""Pack Autofix reusable workflow must be valid YAML.

A bare markdown line at column 0 (e.g. **[autofix-budget]**) ends a
`run: |` block and GitHub reports "workflow file issue" with zero jobs —
observed on Buro-Built/buromaster Autofix runs after v1.9.4 (2026-08-05).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github/workflows/pr-autofix-loop-reusable.yml"


def test_autofix_reusable_parses_as_yaml() -> None:
    text = WF.read_text(encoding="utf-8")
    # No bare ** markdown at column 0 (breaks run:| blocks)
    for i, line in enumerate(text.splitlines(), 1):
        assert not line.startswith("**"), (
            f"{WF.name}:{i} bare '**' at column 0 breaks YAML "
            f"(pack Autofix zero-job failures). Line: {line[:80]!r}"
        )
    spec = yaml.safe_load(text)
    assert "jobs" in spec
    assert "workflow_call" in (spec.get("on") or spec.get(True) or {})


def test_autofix_uses_github_token_fallback() -> None:
    text = WF.read_text(encoding="utf-8")
    assert "github.token" in text, (
        "reusable Autofix must fall back to github.token — secrets.GITHUB_TOKEN "
        "is empty in called workflows and breaks private-repo git fetch"
    )
    assert "secrets.GITHUB_TOKEN" not in text or "github.token" in text
