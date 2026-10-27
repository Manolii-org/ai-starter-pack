"""Activation contract for e2e-playwright-reusable (WI-3).

Runs the real "Run Playwright suite" step body with a fake `npx` that prints a
canned Playwright summary, and checks the `activation` output and the
`fail_on_inert` behaviour.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/e2e-playwright-reusable.yml"


def _spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _run_step() -> dict:
    steps = _spec()["jobs"]["e2e"]["steps"]
    return next(s for s in steps if s.get("id") == "run")


def _execute(tmp_path: Path, output: str, exit_code: int = 0, fail_on_inert: str = "false"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx = bin_dir / "npx"
    (tmp_path / "out.txt").write_text(output, encoding="utf-8")
    npx.write_text(f"#!/bin/sh\ncat '{tmp_path / 'out.txt'}'\nexit {exit_code}\n", encoding="utf-8")
    npx.chmod(npx.stat().st_mode | stat.S_IEXEC)
    gh_output = tmp_path / "gh_output"
    gh_output.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(gh_output),
        "PLAYWRIGHT_ARGS_JSON": "[]",
        "TEST_ENV_LINES": "",
        "FAIL_ON_INERT": fail_on_inert,
    }
    proc = subprocess.run(["bash", "-c", _run_step()["run"]], env=env, cwd=tmp_path,
                          capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in gh_output.read_text().splitlines() if "=" in line)
    return proc, outputs


def test_contract_shape() -> None:
    spec = _spec()
    call = spec.get("on", spec.get(True))["workflow_call"]  # PyYAML reads `on:` as True
    assert call["inputs"]["fail_on_inert"] == {
        "type": "boolean", "required": False, "default": False,
        "description": call["inputs"]["fail_on_inert"]["description"],
    }
    assert call["outputs"]["activation"]["value"] == "${{ jobs.e2e.outputs.activation }}"
    assert _spec()["jobs"]["e2e"]["outputs"]["activation"] == "${{ steps.run.outputs.activation }}"
    assert _run_step()["env"]["FAIL_ON_INERT"] == "${{ inputs.fail_on_inert }}"


@pytest.mark.parametrize("output,expected", [
    ("Running 3 tests\n\n  3 passed (4.1s)\n", "active"),
    ("  1 failed\n  2 passed (3.0s)\n", "active"),
    ("  \x1b[33m4 skipped\x1b[39m\n", "inert"),
    ("  2 skipped\n  1 did not run\n", "inert"),
    ("No tests found.\n", "inert"),
    ("{\"suites\": []}\n", "unknown"),
])
def test_activation_classification(tmp_path, output, expected) -> None:
    proc, outputs = _execute(tmp_path, output)
    assert outputs["activation"] == expected, proc.stderr
    assert proc.returncode == 0
    assert ("INERT" in proc.stdout) == (expected == "inert")


def test_fail_on_inert_fails_an_inert_green_run(tmp_path) -> None:
    proc, outputs = _execute(tmp_path, "  4 skipped\n", fail_on_inert="true")
    assert outputs["activation"] == "inert"
    assert proc.returncode == 1
    assert "fail_on_inert" in proc.stderr


def test_fail_on_inert_leaves_active_runs_and_failures_alone(tmp_path) -> None:
    proc, _ = _execute(tmp_path, "  3 passed\n", fail_on_inert="true")
    assert proc.returncode == 0
    failing = tmp_path / "f"
    failing.mkdir()
    proc, outputs = _execute(failing, "  1 failed\n", exit_code=1, fail_on_inert="true")
    assert proc.returncode == 1 and outputs["activation"] == "active"


def test_default_inert_run_still_passes(tmp_path) -> None:
    """v1 compatibility: without the opt-in an inert run warns but passes."""
    proc, _ = _execute(tmp_path, "  4 skipped\n")
    assert proc.returncode == 0
