#!/usr/bin/env python3
"""Pack /watch-pr must ship the M1 subscribe split, not always-subscribe + 60s poll."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WatchPrSubscribeSplitTests(unittest.TestCase):
    def _read(self, *parts: str) -> str:
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def _surfaces(self):
        return (
            self._read(".claude", "commands", "watch-pr.md.jinja"),
            self._read("plugin", "manolii-framework", "commands", "watch-pr.md"),
        )

    def test_docs_ci_does_not_always_subscribe_pr_activity(self):
        for text in self._surfaces():
            self.assertIn("version: 2.1.0", text)
            self.assertIn(
                "### Step 2: Subscribe (split — do not always subscribe PR activity)",
                text,
            )
            self.assertNotIn("### Step 2: Subscribe to PR Activity", text)
            self.assertIn("**must not** call", text)
            self.assertIn("mcp__github__subscribe_pr_activity", text)
            self.assertIn("subscribe_github_pr", text)
            self.assertIn("actually armed", text)
            self.assertNotIn("or will be", text)
            self.assertIn("reports/**", text)
            self.assertIn("docs/**", text)
            self.assertIn("reports/INDEX.md", text)
            self.assertIn(".github/pull_request_template.md", text)
            self.assertIn("Docs/CI with **none**", text)
            self.assertIn("do **not** enter the 60s poll", text)
            self.assertNotIn("Poll every 60s", text)
            self.assertNotIn("Wait 5 minutes**, then re-poll", text)
            self.assertNotIn("mcp__github__subscribe_ci", text)
            self.assertIn("no CI-only MCP equivalent", text)
            self.assertIn(
                "Arm a ≥20-minute heartbeat only if CI subscribe is on.",
                text,
            )

    def test_product_red_keeps_pr_and_ci_subscribe_and_heartbeat(self):
        for text in self._surfaces():
            self.assertIn("Product-red", text)
            self.assertIn("subscribe PR **and** CI", text)
            self.assertIn("bounded ≥20-minute heartbeat", text)
            self.assertIn("subscribe_github_ci", text)
            self.assertIn("Cursor Cloud only", text)
            self.assertIn(
                "**3a. Product-red / CI-subscribe only — event-led, not a 60s sleep loop:**",
                text,
            )
            self.assertIn("Skip this loop for docs/CI `none`.", text)
            self.assertIn("Re-arm the ≥20-minute heartbeat", text)


if __name__ == "__main__":
    unittest.main()
