"""Autofix prefers a scoped GitHub App token and does not hide a broken App behind a PAT."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIN = "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510 # v3.3.0"
WORKFLOWS = (
    ROOT / ".github/workflows/pr-autofix-loop.yml",
    ROOT / ".github/workflows/pr-autofix-loop-reusable.yml",
)


def _mint_steps(spec: dict) -> list[dict]:
    found = []
    for job in spec["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("id") == "github-app":
                found.append(step)
    return found


def test_mint_step_fails_closed_and_is_pinned() -> None:
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        spec = yaml.safe_load(text)
        steps = _mint_steps(spec)
        assert steps, workflow.name
        assert PIN in text
        for step in steps:
            assert step.get("continue-on-error") is not True
            assert step["uses"] == PIN.split(" # ", 1)[0]
            assert "vars.GH_AUTOMATION_APP_ID != ''" in step["if"]
            assert step["with"]["private-key"] == "${{ secrets.GH_AUTOMATION_APP_PRIVATE_KEY }}"
            assert step["with"]["repositories"] == "${{ github.event.repository.name }}"
            assert step["with"]["permission-contents"] == "write"


def test_token_expressions_prefer_the_app_then_the_existing_secret() -> None:
    loop = (ROOT / ".github/workflows/pr-autofix-loop.yml").read_text(encoding="utf-8")
    reusable = (ROOT / ".github/workflows/pr-autofix-loop-reusable.yml").read_text(encoding="utf-8")
    preferred = "steps.github-app.outputs.token || secrets.MN_GH_AUTOMATION_TOKEN || secrets.GITHUB_TOKEN"
    assert loop.count(preferred) == 2
    assert "secrets.GH_PAT || github.token" in reusable
    assert reusable.index("steps.github-app.outputs.token") < reusable.index("secrets.GH_PAT || github.token")
    assert "secrets.GH_PAT || secrets.GITHUB_TOKEN" in reusable
    assert "Neither GH_AUTOMATION_APP_ID nor MN_GH_AUTOMATION_TOKEN" in loop
