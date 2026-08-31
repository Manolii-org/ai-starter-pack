from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "lint_workflow_outcomes", ROOT / "scripts/lint-workflow-outcomes.py"
)
assert SPEC and SPEC.loader
LINT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LINT)


def write(
    tmp_path: Path,
    apply_run: str = "./apply",
    reporter_if: str = "${{ always() }}",
    publish: bool = True,
    reporter_name="outcome",
) -> Path:
    p = tmp_path / ".github/workflows/deploy.yml"
    p.parent.mkdir(parents=True)
    publisher = (
        "github.rest.checks.create({name: `operation — ${outcome}`, conclusion}); // DEFERRED POLICY_HELD FAILED"
        if publish
        else "echo DEFERRED POLICY_HELD FAILED"
    )
    p.write_text(
        f"""name: deploy\njobs:\n  apply:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Apply\n        run: |\n          {apply_run}\n  {reporter_name}:\n    needs: [apply]\n    if: {reporter_if}\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/github-script@0123456789012345678901234567890123456789\n        with:\n          script: |\n            {publisher}\n"""
    )
    c = tmp_path / "config/workflow-outcomes.json"
    c.parent.mkdir()
    c.write_text(
        json.dumps(
            {
                "version": 1,
                "workflows": {
                    ".github/workflows/deploy.yml": {
                        "load_bearing_jobs": ["apply"],
                        "outcome_reporter_job": reporter_name,
                    }
                },
            }
        )
    )
    return c


def test_accepts_structured_publisher(tmp_path):
    assert LINT.lint(tmp_path, write(tmp_path)) == []


@pytest.mark.parametrize(
    "command",
    ["echo 'missing; skipping'", "echo 'deferring'; exit 0", "echo 'policy hold'"],
)
def test_rejects_any_skip_semantics_in_load_bearing_job(tmp_path, command):
    assert any(
        "classify outside" in x for x in LINT.lint(tmp_path, write(tmp_path, command))
    )


def test_rejects_conditional_reporter(tmp_path):
    assert any(
        "exactly if" in x
        for x in LINT.lint(
            tmp_path, write(tmp_path, reporter_if="${{ always() && false }}")
        )
    )


def test_rejects_echo_only_reporter(tmp_path):
    assert any(
        "check-run" in x for x in LINT.lint(tmp_path, write(tmp_path, publish=False))
    )


def test_rejects_non_string_reporter_name(tmp_path):
    c = write(tmp_path)
    data = json.loads(c.read_text())
    data["workflows"][".github/workflows/deploy.yml"]["outcome_reporter_job"] = {}
    c.write_text(json.dumps(data))
    assert any("non-empty string" in x for x in LINT.lint(tmp_path, c))


def test_committed_registry_is_valid():
    assert LINT.lint(ROOT, ROOT / "config/workflow-outcomes.json") == []
