"""coverage-ratchet must report honestly when it cannot fail.

Regression: five consumers bound `coverage / coverage` as a required check in
`mode: enforce` with no baseline file; the reusable printed "Seed the baseline
file to activate." and exited 0, so the gate could never fail.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/coverage-ratchet-reusable.yml"


def _spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _compare_script() -> str:
    steps = _spec()["jobs"]["coverage"]["steps"]
    run = next(s for s in steps if s.get("id") == "compare")["run"]
    return run.split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0]


def _compare(tmp_path: Path, *, baseline: str | None, current: str = "71.5",
             mode: str = "enforce", require: str = "false", seeding: str = "false"):
    if baseline is not None:
        (tmp_path / ".ci").mkdir()
        (tmp_path / ".ci/coverage-baseline.txt").write_text(baseline, encoding="utf-8")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    env = {**os.environ, "CURRENT": current, "BASELINE_FILE": ".ci/coverage-baseline.txt",
           "MODE": mode, "REQUIRE_BASELINE": require, "SEEDING": seeding,
           "GITHUB_OUTPUT": str(out)}
    proc = subprocess.run(["python3", "-c", _compare_script()], cwd=tmp_path, env=env,
                          capture_output=True, text=True)
    outputs = dict(l.split("=", 1) for l in out.read_text().splitlines() if "=" in l)
    return proc, outputs


def test_inputs_are_additive_with_v1_defaults() -> None:
    spec = _spec()
    call = (spec.get("on") or spec[True])["workflow_call"]
    req = call["inputs"]["require_baseline"]
    assert req["type"] == "boolean" and req["default"] is False
    assert call["outputs"]["activation"]["value"] == "${{ jobs.coverage.outputs.activation }}"
    assert "name" not in _spec()["jobs"]["coverage"], "job name feeds the `coverage / coverage` check"


@pytest.mark.parametrize("mode", ["enforce", "report"])
def test_absent_baseline_is_inert_and_says_so(tmp_path: Path, mode: str) -> None:
    proc, outputs = _compare(tmp_path, baseline=None, mode=mode)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs["activation"] == "inert"
    assert "::warning::coverage-ratchet INERT: no baseline at .ci/coverage-baseline.txt; " \
           "this required check cannot fail" in proc.stdout


@pytest.mark.parametrize("mode", ["enforce", "report"])
def test_require_baseline_fails_when_absent(tmp_path: Path, mode: str) -> None:
    proc, outputs = _compare(tmp_path, baseline=None, mode=mode, require="true")
    assert proc.returncode == 1
    assert outputs["activation"] == "inert"
    assert "::error::require_baseline is true" in proc.stdout
    assert "cannot fail" not in proc.stdout, "a failing run must not claim it cannot fail"


def test_require_baseline_lets_auto_commit_seed(tmp_path: Path) -> None:
    proc, _ = _compare(tmp_path, baseline=None, require="true", seeding="true")
    assert proc.returncode == 0, proc.stdout
    assert "coverage-ratchet INERT" in proc.stdout


def _auto_commit(tmp_path: Path, *, require: str, activation: str):
    """Run the auto-commit step with a fake gh whose branch head has moved."""
    steps = _spec()["jobs"]["coverage"]["steps"]
    run = next(s for s in steps if str(s.get("name", "")).startswith("Auto-commit"))["run"]
    script = run.split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0]
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\necho \'{"object": {"sha": "%s"}}\'\n' % ("b" * 40), encoding="utf-8")
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", "CURRENT": "71.5",
           "BASELINE_FILE": ".ci/coverage-baseline.txt", "REPO": "example/repo", "BRANCH": "main",
           "TRIGGER_SHA": "a" * 40, "REQUIRE_BASELINE": require, "ACTIVATION": activation}
    return subprocess.run(["python3", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)


def test_seeding_exemption_fails_when_the_seed_is_not_written(tmp_path: Path) -> None:
    proc = _auto_commit(tmp_path, require="true", activation="inert")
    assert proc.returncode != 0
    assert "did not seed the baseline" in proc.stderr


def test_head_move_still_passes_without_the_exemption(tmp_path: Path) -> None:
    assert _auto_commit(tmp_path, require="false", activation="inert").returncode == 0
    assert _auto_commit(tmp_path, require="true", activation="active").returncode == 0


def test_present_baseline_is_active_and_still_enforces(tmp_path: Path) -> None:
    (tmp_path / "below").mkdir()
    proc, outputs = _compare(tmp_path / "below", baseline="80.0\n", current="79.9")
    assert proc.returncode == 1 and outputs["activation"] == "active"
    (tmp_path / "equal").mkdir()
    proc, outputs = _compare(tmp_path / "equal", baseline="80.0\n", current="80.0")
    assert proc.returncode == 0 and outputs["activation"] == "active"


def test_seeding_gate_mirrors_auto_commit_gate() -> None:
    steps = _spec()["jobs"]["coverage"]["steps"]
    seeding = " ".join(next(s for s in steps if s.get("id") == "compare")["env"]["SEEDING"].split())
    auto = " ".join(next(s for s in steps if str(s.get("name", "")).startswith("Auto-commit"))["if"].split())
    assert seeding == "${{ " + auto + " }}"
