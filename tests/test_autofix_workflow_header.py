#!/usr/bin/env python3
"""Regression: Autofix workflow file headers must be comment-only YAML."""
from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTOFIX = (
    ROOT / ".github" / "workflows" / "pr-autofix-loop.yml",
    ROOT / ".github" / "workflows" / "pr-autofix-loop-reusable.yml",
)


class TestAutofixHeaderComments(unittest.TestCase):
    def test_preamble_has_no_bare_sequence(self):
        for path in AUTOFIX:
            text = path.read_text(encoding="utf-8")
            preamble = text.split("\non:", 1)[0]
            for i, line in enumerate(preamble.splitlines(), 1):
                if not line.strip():
                    continue
                if line.lstrip().startswith("#") or line.startswith("name:"):
                    continue
                self.fail(f"{path.name}:{i} non-comment preamble line: {line!r}")


if __name__ == "__main__":
    unittest.main()
