"""secret-scan dependency audits must report honestly when they cannot fail.

Regression: a repository with no root package-lock.json and no Python
manifest got two green audit jobs that had audited nothing. The activation
contract (docs/workflow-outcome-convention.md) now exposes that as
`inert`, and `fail_on_inert` turns it red.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/secret-scan-reusable.yml"


def _spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(job: str, step_id: str) -> dict:
    return next(s for s in _spec()["jobs"][job]["steps"] if s.get("id") == step_id)


def _detect(tmp_path: Path, job: str, files: tuple[str, ...], fail_on_inert: str = "false"):
    for name in files:
        (tmp_path / name).write_text("", encoding="utf-8")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    env = {**os.environ, "GITHUB_OUTPUT": str(out), "FAIL_ON_INERT": fail_on_inert}
    proc = subprocess.run(["bash", "-e", "-c", _step(job, "detect")["run"]], cwd=tmp_path,
                          env=env, capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return proc, outputs


def test_inputs_and_outputs_are_additive_with_v1_defaults() -> None:
    spec = _spec()
    call = (spec.get("on") or spec[True])["workflow_call"]
    flag = call["inputs"]["fail_on_inert"]
    assert flag["type"] == "boolean" and flag["default"] is False
    assert call["outputs"]["npm_audit_activation"]["value"] == "${{ jobs.npm-audit.outputs.activation }}"
    assert call["outputs"]["pip_audit_activation"]["value"] == "${{ jobs.pip-audit.outputs.activation }}"
    for job in ("npm-audit", "pip-audit"):
        assert spec["jobs"][job]["outputs"]["activation"] == \
            "${{ steps.audit.outputs.activation || steps.detect.outputs.activation }}"
    # Job names feed consumers' required checks; they must not change.
    assert spec["jobs"]["npm-audit"]["name"] == "Dependency Vulnerability Audit"
    assert spec["jobs"]["pip-audit"]["name"] == "Python Dependency Audit"
    assert _step("npm-audit", "detect")["env"]["FAIL_ON_INERT"] == "${{ inputs.fail_on_inert }}"


@pytest.mark.parametrize("job", ["npm-audit", "pip-audit"])
def test_active_is_set_only_by_the_step_that_audits(job: str) -> None:
    audit = _step(job, "audit")
    assert audit["if"] == "steps.detect.outputs.found == 'true'"
    assert 'echo "activation=active" >> "$GITHUB_OUTPUT"' in audit["run"]
    assert "activation=active" not in _step(job, "detect")["run"]


def _pip_activation_block() -> str:
    """The activation decision at the top of the pip audit step, runnable alone."""
    run = _step("pip-audit", "audit")["run"]
    start = run.index("if ls requirements*.txt")
    return run[start:run.index("fi\n", start) + 3]


@pytest.mark.parametrize(("files", "expected"), [
    (("requirements.txt",), "active"),
    (("pyproject.toml", "uv.lock"), "active"),
    # Regression (review of this change): a bare pyproject.toml runs a
    # fallback `pip-audit` that sees only the runner's own packages.
    (("pyproject.toml",), "inert"),
])
def test_pip_reports_active_only_for_a_project_dependency_graph(tmp_path: Path, files, expected) -> None:
    for name in files:
        (tmp_path / name).write_text("", encoding="utf-8")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    proc = subprocess.run(["bash", "-e", "-c", _pip_activation_block()], cwd=tmp_path,
                          env={**os.environ, "GITHUB_OUTPUT": str(out)}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert out.read_text().strip() == f"activation={expected}"


def test_no_manifest_at_all_is_inert_and_warns(tmp_path: Path) -> None:
    proc, outputs = _detect(tmp_path, "npm-audit", ())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs == {"found": "false", "activation": "inert"}
    assert "::warning::secret-scan INERT: no package-lock.json, requirements*.txt or " \
           "uv.lock" in proc.stdout


def test_fail_on_inert_fails_only_when_nothing_can_be_audited(tmp_path: Path) -> None:
    proc, outputs = _detect(tmp_path, "npm-audit", (), fail_on_inert="true")
    assert proc.returncode == 1
    assert outputs["activation"] == "inert"
    assert "::error::secret-scan INERT" in proc.stdout
    assert "cannot fail" not in proc.stdout, "a failing run must not claim it cannot fail"


@pytest.mark.parametrize("manifests", [("requirements.txt",), ("requirements-dev.txt",),
                                       ("pyproject.toml", "uv.lock")])
def test_python_only_repo_is_npm_inert_without_failing(tmp_path: Path, manifests) -> None:
    proc, outputs = _detect(tmp_path, "npm-audit", manifests, fail_on_inert="true")
    assert proc.returncode == 0, proc.stdout
    assert outputs["activation"] == "inert"
    assert "::warning::" not in proc.stdout and "::error::" not in proc.stdout


@pytest.mark.parametrize("files", [
    ("pyproject.toml",),  # tool config only: the pip fallback sees no project graph
    ("uv.lock",),  # Codex review: the pip job skips without pyproject.toml
])
def test_unauditable_python_files_do_not_count(tmp_path: Path, files) -> None:
    """Whatever the npm job counts as auditable, the pip job must report active."""
    proc, outputs = _detect(tmp_path, "npm-audit", files, fail_on_inert="true")
    assert proc.returncode == 1
    assert outputs["activation"] == "inert"
    assert "::error::secret-scan INERT" in proc.stdout


def test_pnpm_repo_keeps_its_notice(tmp_path: Path) -> None:
    proc, outputs = _detect(tmp_path, "npm-audit", ("pnpm-lock.yaml", "requirements.txt"))
    assert proc.returncode == 0
    assert outputs["activation"] == "inert"
    assert "::notice::Skipping npm audit" in proc.stdout


def test_lockfile_present_defers_activation_to_the_audit(tmp_path: Path) -> None:
    proc, outputs = _detect(tmp_path, "npm-audit", ("package-lock.json",), fail_on_inert="true")
    assert proc.returncode == 0
    assert outputs == {"found": "true"}


@pytest.mark.parametrize(("files", "expected"), [
    ((), {"found": "false", "activation": "inert"}),
    (("package-lock.json",), {"found": "false", "activation": "inert"}),
    (("pyproject.toml",), {"found": "true"}),
    (("requirements.txt",), {"found": "true"}),
])
def test_pip_detect_never_fails_and_reports_inert(tmp_path: Path, files, expected) -> None:
    proc, outputs = _detect(tmp_path, "pip-audit", files, fail_on_inert="true")
    assert proc.returncode == 0
    assert outputs == expected
