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
    apply_if=None,
) -> Path:
    p = tmp_path / ".github/workflows/deploy.yml"
    p.parent.mkdir(parents=True)
    publisher = (
        "const outcome = condition ? 'DEFERRED' : condition2 ? 'POLICY_HELD' : 'FAILED'; const conclusions = { DEFERRED: 'neutral', POLICY_HELD: 'neutral', FAILED: 'failure' }; const summary = `${outcome}: evidence`; github.rest.checks.create({name: `operation — ${outcome}`, conclusion: conclusions[outcome], output: { summary }});"
        if publish
        else "echo DEFERRED POLICY_HELD FAILED"
    )
    needs_yaml = "needs:\n      - apply" if needs == "block" else f"needs: {needs}"
    apply_if_yaml = f"        if: {apply_if}\n" if apply_if else ""
    p.write_text(
        f"""name: deploy\npermissions:\n  checks: write\njobs:\n  apply:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Apply\n{apply_if_yaml}        run: {scalar}\n          {apply_run}\n  {reporter_name}:\n    {needs_yaml}\n    if: {reporter_if}\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/github-script@0123456789012345678901234567890123456789\n        with:\n          script: |\n            {publisher}\n"""
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
                        "load_bearing_steps": {"apply": ["Apply"]},
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


def test_rejects_conditional_load_bearing_step(tmp_path):
    errors = LINT.lint(
        tmp_path, write(tmp_path, apply_if="${{ env.DEPLOY_TOKEN != '' }}")
    )
    assert any("must not be conditional" in item for item in errors)


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ("    runs-on: ubuntu-latest", "    continue-on-error: true\n    runs-on: ubuntu-latest"),
        ("        run: |", "        continue-on-error: true\n        run: |"),
    ],
)
def test_rejects_tolerated_load_bearing_failures(tmp_path, needle, replacement):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(workflow.read_text().replace(needle, replacement, 1))
    assert any("must not continue on error" in item for item in LINT.lint(tmp_path, config))


def test_requires_steps_for_every_load_bearing_job(tmp_path):
    config = write(tmp_path)
    data = json.loads(config.read_text())
    data["workflows"][".github/workflows/deploy.yml"]["load_bearing_steps"] = {}
    config.write_text(json.dumps(data))
    assert any("every load-bearing job" in item for item in LINT.lint(tmp_path, config))


@pytest.mark.parametrize("needs", ["apply", "[apply]", "block"])
def test_accepts_all_github_needs_shapes(tmp_path, needs):
    assert LINT.lint(tmp_path, write(tmp_path, needs=needs)) == []


@pytest.mark.parametrize("scalar", ["|", "|-", "|+", ">", ">-", ">+"])
def test_scans_all_yaml_block_scalar_modifiers(tmp_path, scalar):
    errors = LINT.lint(
        tmp_path, write(tmp_path, apply_run="echo 'missing; skipping'", scalar=scalar)
    )
    assert any("classify outside" in item for item in errors)


@pytest.mark.parametrize(
    "block",
    ["      - run: echo 'missing; skipping'", "      - run: |\n          exit 0"],
)
def test_scans_single_key_run_steps(block):
    assert LINT.announces_green_skip(block)


def test_scans_anonymous_multiline_run_body():
    block = "      - run: |\n        echo 'deferred; skipping'\n        exit 0"
    assert LINT.announces_green_skip(block)


def test_named_step_stops_at_anonymous_following_step():
    job = """  apply:
    steps:
      - name: Apply
        run: ./apply
      - run: ./cleanup
        if: always()
"""
    block = LINT.step_block(job, "Apply")
    assert block is not None
    assert "cleanup" not in block


@pytest.mark.parametrize("quoted", ['"Apply"', "'Apply'"])
def test_accepts_quoted_load_bearing_step_names(tmp_path, quoted):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(workflow.read_text().replace("- name: Apply", f"- name: {quoted}"))
    assert LINT.lint(tmp_path, config) == []


def test_rejects_failed_outcome_mapped_to_success(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(workflow.read_text().replace("FAILED: 'failure'", "FAILED: 'success'"))
    assert any("conclusions must explicitly map" in item for item in LINT.lint(tmp_path, config))


def test_rejects_reporter_job_permission_override(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "    needs: [apply]", "    permissions:\n      contents: read\n    needs: [apply]"
        )
    )
    assert any("checks: write" in item for item in LINT.lint(tmp_path, config))


def test_accepts_reporter_job_checks_write(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "    needs: [apply]", "    permissions:\n      checks: write\n    needs: [apply]"
        )
    )
    assert LINT.lint(tmp_path, config) == []


@pytest.mark.parametrize("value", ['"write"', "'write'"])
def test_accepts_quoted_checks_write(tmp_path, value):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(workflow.read_text().replace("checks: write", f"checks: {value}"))
    assert LINT.lint(tmp_path, config) == []


def test_requires_mapping_on_created_check(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "github.rest.checks.create({name:",
            "const unrelated = { conclusion: conclusions[outcome] }; github.rest.checks.create({name:",
        ).replace(
            ", conclusion: conclusions[outcome], output:",
            ", conclusion: 'success', output:",
        )
    )
    assert any("conclusions must explicitly map" in item for item in LINT.lint(tmp_path, config))


def test_accepts_semicolon_free_conclusions_object(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(workflow.read_text().replace("FAILED: 'failure' };", "FAILED: 'failure' }"))
    assert LINT.lint(tmp_path, config) == []


def test_rejects_echo_only_reporter(tmp_path):
    assert any(
        "check-run" in x for x in LINT.lint(tmp_path, write(tmp_path, publish=False))
    )


def test_rejects_commented_out_check_publisher(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "github.rest.checks.create", "// github.rest.checks.create"
        )
    )
    assert any("must publish a GitHub check-run" in item for item in LINT.lint(tmp_path, config))


def test_rejects_block_commented_check_publisher(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    text = workflow.read_text()
    workflow.write_text(
        text.replace("const outcome", "/* const outcome").replace("output: { summary }});", "output: { summary }}); */")
    )
    errors = LINT.lint(tmp_path, config)
    assert any("must publish a GitHub check-run" in item for item in errors)


def test_rejects_summary_without_selected_outcome(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(
        workflow.read_text().replace("`${outcome}: evidence`", "`evidence only`")
    )
    assert any("summary cannot identify" in item for item in LINT.lint(tmp_path, config))


def test_accepts_dynamic_summary_property(tmp_path):
    config = write(tmp_path)
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "const summary = `${outcome}: evidence`;", ""
        ).replace("output: { summary }", "output: { summary: `${outcome}: evidence` }")
    )
    assert LINT.lint(tmp_path, config) == []


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


def test_rejects_empty_workflow_registry(tmp_path):
    config = tmp_path / "config/workflow-outcomes.json"
    config.parent.mkdir()
    config.write_text('{"version": 1, "workflows": {}}')
    assert any("non-empty object" in item for item in LINT.lint(tmp_path, config))


def test_consumer_quality_workflows_execute_outcome_lint():
    candidates = (
        ROOT / ".github/workflows/quality-base.yml",
        ROOT / ".github/workflows/ci.yml",
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
