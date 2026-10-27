"""release-tag.yml accepts pack and component tags; only pack tags move v1."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/release-tag.yml").read_text(encoding="utf-8"))
STEPS = {s.get("name"): s for s in WORKFLOW["jobs"]["tag"]["steps"]}


def _validate(tag: str) -> int:
    script = STEPS["Validate tag format"]["run"]
    return subprocess.run(["bash", "-c", script], env={**os.environ, "TAG": tag},
                          capture_output=True, text=True).returncode


@pytest.mark.parametrize("tag", [
    "v1.23.0", "v2.0.0", "email-capture-v0.2.6", "feature-controls-v0.1.0",
    "feature-controls-v0.2.0-rc.1", "email-capture-v1.0.0-beta.2",
])
def test_accepts_pack_and_component_tags(tag: str) -> None:
    assert _validate(tag) == 0


@pytest.mark.parametrize("tag", [
    "v1", "v1.23", "1.23.0", "v1.23.0-rc.1", "email-capture-0.2.6", "other-v1.0.0",
    "email-capture-v0.2", "feature-controls-v0.1.0-dev.1", "v1.23.0\nv2", "",
])
def test_refuses_everything_else(tag: str) -> None:
    assert _validate(tag) != 0


def _alias(tag: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the alias step with a curl stub that records each call."""
    log = tmp_path / "curl.log"
    stub = tmp_path / "curl"
    stub.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\ncat >/dev/null\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", "TAG": tag,
           "SHA": "a" * 40, "REPO": "example/repo", "GH_TOKEN": "unused"}
    proc = subprocess.run(["bash", "-c", STEPS["Move major-version alias"]["run"]], env=env,
                          capture_output=True, text=True)
    proc.calls = log.read_text() if log.exists() else ""  # type: ignore[attr-defined]
    return proc


def test_only_pack_tags_move_the_major_alias(tmp_path: Path) -> None:
    assert "if" not in STEPS["Move major-version alias"], "outcome contract: load-bearing step"
    component = _alias("email-capture-v0.2.6", tmp_path)
    assert component.returncode == 0 and component.calls == ""  # type: ignore[attr-defined]
    pack = _alias("v1.23.0", tmp_path)
    assert pack.returncode == 0 and "git/refs/tags/v1" in pack.calls  # type: ignore[attr-defined]


def test_tag_is_annotated_with_optional_notes() -> None:
    run = STEPS["Create tag ref"]["run"]
    assert "/git/tags" in run and "os.environ['NOTES'] or 'Release '" in run
    assert STEPS["Create tag ref"]["env"]["NOTES"] == "${{ inputs.notes }}"
