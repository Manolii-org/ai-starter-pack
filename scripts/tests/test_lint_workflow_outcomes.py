from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "lint_workflow_outcomes", ROOT / "scripts/lint-workflow-outcomes.py"
)
assert SPEC and SPEC.loader
LINT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LINT)


def write_contract(tmp_path: Path, workflow: dict) -> Path:
    path = tmp_path / ".github/workflows/deploy.yml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
    config = tmp_path / "config/workflow-outcomes.yaml"
    config.parent.mkdir()
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "workflows": {
                    ".github/workflows/deploy.yml": {
                        "load_bearing_jobs": ["apply"],
                        "outcome_reporter_job": "outcome",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config


def reporter(needs: list[str] | None = None, condition: str = "${{ always() }}") -> dict:
    return {
        "needs": needs or ["apply"],
        "if": condition,
        "runs-on": "ubuntu-latest",
        "steps": [
            {
                "name": "Publish DEFERRED, POLICY_HELD, or FAILED check",
                "run": "echo DEFERRED POLICY_HELD FAILED",
            }
        ],
    }


def test_accepts_explicit_terminal_outcome_reporter(tmp_path: Path) -> None:
    config = write_contract(
        tmp_path,
        {
            "jobs": {
                "apply": {"runs-on": "ubuntu-latest", "steps": [{"run": "./apply"}]},
                "outcome": reporter(),
            }
        },
    )
    assert LINT.lint(tmp_path, config) == []


@pytest.mark.parametrize(
    "command",
    [
        'echo "pre-production gate active; deferring"; exit 0',
        '[ -n "$TOKEN" ] || { echo "secret missing; skipping"; exit 0; }',
        'echo "policy hold" && exit 0',
    ],
)
def test_rejects_green_skip_in_load_bearing_job(tmp_path: Path, command: str) -> None:
    config = write_contract(
        tmp_path,
        {
            "jobs": {
                "apply": {"runs-on": "ubuntu-latest", "steps": [{"name": "Apply", "run": command}]},
                "outcome": reporter(),
            }
        },
    )
    assert any("can report success after skipping work" in item for item in LINT.lint(tmp_path, config))


def test_reporter_must_run_always_and_need_every_load_bearing_job(tmp_path: Path) -> None:
    config = write_contract(
        tmp_path,
        {
            "jobs": {
                "apply": {"runs-on": "ubuntu-latest", "steps": [{"run": "./apply"}]},
                "outcome": reporter(needs=["other"], condition="${{ success() }}"),
            }
        },
    )
    errors = LINT.lint(tmp_path, config)
    assert any("if: always()" in item for item in errors)
    assert any("does not need load-bearing" in item for item in errors)


def test_committed_registry_is_valid() -> None:
    assert LINT.lint(ROOT, ROOT / "config/workflow-outcomes.yaml") == []
