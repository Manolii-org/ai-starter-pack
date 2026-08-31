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
    needs="[apply]",
    scalar="|",
) -> Path:
    p = tmp_path / ".github/workflows/deploy.yml"
    p.parent.mkdir(parents=True)
    publisher = (
        "const conclusions = { DEFERRED: 'neutral', POLICY_HELD: 'neutral', FAILED: 'failure' }; github.rest.checks.create({name: `operation — ${outcome}`, conclusion, output: { summary }});"
        if publish
        else "echo DEFERRED POLICY_HELD FAILED"
    )
    needs_yaml = "needs:\n      - apply" if needs == "block" else f"needs: {needs}"
    p.write_text(
        f"""name: deploy\njobs:\n  apply:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Apply\n        run: {scalar}\n          {apply_run}\n  {reporter_name}:\n    {needs_yaml}\n    if: {reporter_if}\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/github-script@0123456789012345678901234567890123456789\n        with:\n          script: |\n            {publisher}\n"""
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
                        "outcomes": ["DEFERRED", "POLICY_HELD", "FAILED"],
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
    [
        "echo 'missing; skipping'",
        "echo 'deferring'; exit 0",
        "echo 'policy hold'",
        "[ -f receipt.json ] || exit 0",
        "return 0",
    ],
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


@pytest.mark.parametrize("needs", ["apply", "[apply]", "block"])
def test_accepts_all_github_needs_shapes(tmp_path, needs):
    assert LINT.lint(tmp_path, write(tmp_path, needs=needs)) == []


@pytest.mark.parametrize("scalar", ["|", "|-", "|+", ">", ">-", ">+"])
def test_scans_all_yaml_block_scalar_modifiers(tmp_path, scalar):
    errors = LINT.lint(
        tmp_path, write(tmp_path, apply_run="echo 'missing; skipping'", scalar=scalar)
    )
    assert any("classify outside" in item for item in errors)


def test_rejects_failed_outcome_mapped_to_success(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(workflow.read_text().replace("FAILED: 'failure'", "FAILED: 'success'"))
    assert any("FAILED must map" in item for item in LINT.lint(tmp_path, config))


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


def test_committed_registry_is_not_an_empty_noop():
    config = json.loads((ROOT / "config/workflow-outcomes.json").read_text())
    assert config["workflows"]


def test_consumer_quality_workflows_execute_outcome_lint():
    candidates = (
        ROOT / ".github/workflows/quality-base.yml",
        ROOT / "templates/ai-starter-pack/core/.github/workflows/quality-base.yml",
        ROOT / ".github/workflows/ci-reusable.yml",
    )
    found = False
    for path in candidates:
        if not path.is_file():
            continue
        found = True
        text = path.read_text()
        assert "python3 scripts/lint-workflow-outcomes.py" in text
        assert "Partial workflow-outcome adoption" in text
    assert found, "no managed consumer quality workflow was found"
