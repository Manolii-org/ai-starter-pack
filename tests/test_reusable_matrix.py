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
