#!/usr/bin/env python3
"""Tests for dual-ownership XOR + redaction lint (lint-autofix-xor.py)."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINT_PATH = ROOT / "scripts" / "lint-autofix-xor.py"


def _load():
    spec = importlib.util.spec_from_file_location("lint_autofix_xor", LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestPackSelfXor(unittest.TestCase):
    def test_pack_tree_passes_xor(self):
        mod = _load()
        self.assertEqual(0, mod.check(ROOT))

    def test_pack_does_not_ship_auto_address(self):
        self.assertFalse(
            (ROOT / ".github" / "workflows" / "auto-address-review.yml").exists()
        )

    def test_pack_autofix_accepts_relay_marker(self):
        reusable = (
            ROOT / ".github" / "workflows" / "pr-autofix-loop-reusable.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("github-actions[bot]", reusable)
        self.assertIn("**[@", reusable)
        self.assertIn("max_successful_fixes", reusable)

    def test_claude_code_action_sha_pinned(self):
        for name in ("pr-autofix-loop.yml", "pr-autofix-loop-reusable.yml"):
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertNotIn("claude-code-action@beta", text)
            self.assertRegex(
                text,
                r"anthropics/claude-code-action@[0-9a-f]{40}",
            )


class TestXorDetection(unittest.TestCase):
    def test_violation_when_both_present(self):
        mod = _load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf = root / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "bot-review-relay.yml").write_text("name: Bot Review Relay\n")
            (wf / "pr-autofix-loop.yml").write_text(
                "uses: Manolii-org/ai-starter-pack/.github/workflows/"
                "pr-autofix-loop-reusable.yml@v1.9.3\n"
            )
            (wf / "auto-address-review.yml").write_text("name: Auto Address\n")
            self.assertEqual(1, mod.check(root))

    def test_ok_pack_only(self):
        mod = _load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf = root / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "bot-review-relay.yml").write_text("name: Bot Review Relay\n")
            (wf / "pr-autofix-loop.yml").write_text(
                "uses: x/.github/workflows/pr-autofix-loop-reusable.yml@v1\n"
            )
            self.assertEqual(0, mod.check(root))

    def test_literal_redacted_fails(self):
        mod = _load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf = root / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "pr-autofix-loop.yml").write_text(
                "secrets:\n  [REDACTED]: ${{ secrets.X }}\n"
            )
            self.assertEqual(1, mod.check(root))


if __name__ == "__main__":
    unittest.main()
