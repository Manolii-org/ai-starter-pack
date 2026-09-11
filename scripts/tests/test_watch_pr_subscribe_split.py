#!/usr/bin/env python3
"""Pack watch-pr must ship the M1 subscribe split, not always-subscribe + 60s poll."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WatchPrSubscribeSplitTests(unittest.TestCase):
    def _read(self, *parts: str) -> str:
        return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_jinja_has_split_not_always_subscribe(self):
        text = self._read(".claude", "commands", "watch-pr.md.jinja")
        self.assertIn("Subscribe (split", text)
        self.assertNotIn("Poll every 60s", text)
        self.assertNotIn("mcp__github__subscribe_ci", text)
        self.assertIn("must not", text)
        self.assertIn("mcp__github__subscribe_pr_activity", text)
        self.assertIn("Docs/CI with **none**", text)

    def test_plugin_command_matches_split(self):
        text = self._read("plugin", "manolii-framework", "commands", "watch-pr.md")
        self.assertIn("Subscribe (split", text)
        self.assertNotIn("Poll every 60s", text)
        self.assertNotIn("mcp__github__subscribe_ci", text)


if __name__ == "__main__":
    unittest.main()
