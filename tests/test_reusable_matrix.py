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
    assert render("one/\ntwo/\n") == "one/\\ntwo/\\n"
    assert render("one") != render("one\n")  # CodeRabbit, PR #245
    assert render("a|b") == "a\\|b"
    assert render("${{ github.sha }}") == "${{ github.sha }}"
    assert render(False) == "false"
    assert render("one\ntwo") != render("one two")


def test_every_input_attribute_reaches_the_matrix() -> None:
    """Required inputs keep their defaults, empty defaults are visible, and a
    non-string type is named (Codex review, PR #245): a change to any of them
    must change the rendered row."""
    entry = _module()._input
    assert entry("a", {"required": True, "default": "x"}) == "`a=x` (req)"
    assert entry("a", {"required": True}) == "`a` (req)"
    assert entry("a", {"default": ""}) == "`a=''`"
    assert entry("a", {}) == "`a`"
    assert entry("a", {"type": "boolean", "default": False}) == "`a=false: boolean`"
    assert entry("a", {"type": "number", "default": 5}) == "`a=5: number`"
    variants = [{}, {"default": ""}, {"default": "x"}, {"required": True},
                {"required": True, "default": "x"}, {"type": "boolean"}, {"type": "number"}]
    assert len({entry("a", v) for v in variants}) == len(variants)
