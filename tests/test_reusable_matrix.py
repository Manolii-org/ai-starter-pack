"""REUSABLE-WORKFLOWS.md matrix must match every workflow_call schema."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen-reusable-matrix.py"


def test_matrix_is_generated_and_current() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT), "--check"], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_reusable_has_a_row() -> None:
    doc = (ROOT / ".github/workflows/REUSABLE-WORKFLOWS.md").read_text(encoding="utf-8")
    block = doc.split("reusable-matrix:begin", 1)[1].split("reusable-matrix:end", 1)[0]
    for path in sorted((ROOT / ".github/workflows").glob("*-reusable.yml")):
        assert f"| **{path.stem}** |" in block, path.name


def _module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_reusable_matrix", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_defaults_are_rendered_in_full() -> None:
    """A truncated default hides later changes from --check (CodeRabbit, PR #245)."""
    render = _module()._default
    long_value = "a" * 64 + "Z"
    assert render(long_value) == long_value
    assert render("one/\ntwo/\n") == "one/\\ntwo/"
    assert render("a|b") == "a\\|b"
    assert render("${{ github.sha }}") == "${{ github.sha }}"
    assert render(False) == "false"
    assert render("one\ntwo") != render("one two")
