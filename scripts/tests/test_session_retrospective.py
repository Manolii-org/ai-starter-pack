#!/usr/bin/env python3
"""Unit tests for portable session-retrospective (local mode + failure_class)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACK_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACK_SCRIPTS / "lib"))
from failure_class import FAILURE_CLASSES, classify_from_signals, normalize_failure_class


class TestFailureClass(unittest.TestCase):
    def test_enum_complete(self):
        expected = {
            "instruction-gap",
            "tooling",
            "environment",
            "planning",
            "memory-context",
            "external-dependency",
            "unclassified",
        }
        self.assertEqual(set(FAILURE_CLASSES), expected)

    def test_normalize_unknown(self):
        self.assertEqual(normalize_failure_class("nope"), "unclassified")
        self.assertEqual(normalize_failure_class("tooling"), "tooling")

    def test_classify_tooling_retries(self):
        self.assertEqual(
            classify_from_signals(tool_retries={"Bash": 4}),
            "tooling",
        )

    def test_classify_planning_churn(self):
        self.assertEqual(
            classify_from_signals(edit_churn={"a.py": 3}),
            "planning",
        )


class TestStopLocalMode(unittest.TestCase):
    def test_stop_writes_valid_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts" / "lib").mkdir(parents=True)
            (root / ".ai" / "session-logs").mkdir(parents=True)
            (root / ".ai" / "memory" / "retrospectives").mkdir(parents=True)
            # Copy scripts into scratch so REPO_ROOT resolves correctly
            import shutil
            shutil.copy2(PACK_SCRIPTS / "session-retrospective.py", root / "scripts" / "session-retrospective.py")
            shutil.copy2(PACK_SCRIPTS / "lib" / "failure_class.py", root / "scripts" / "lib" / "failure_class.py")
            log = root / ".ai" / "session-logs" / "session_t.jsonl"
            log.write_text(
                json.dumps({
                    "timestamp": "2026-07-17T10:00:00Z",
                    "message": {"role": "user", "content": "that's wrong, redo it"},
                }) + "\n",
                encoding="utf-8",
            )
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}
            env["PATH"] = os.environ.get("PATH", "/usr/bin")
            proc = subprocess.run(
                [sys.executable, str(root / "scripts" / "session-retrospective.py"),
                 "--mode", "stop", "--session-id", "t1", "--local-only"],
                cwd=root, env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            snaps = list((root / ".ai" / "memory" / "retrospectives").glob("*.json"))
            self.assertTrue(snaps, "expected snapshot json")
            rec = json.loads(snaps[0].read_text(encoding="utf-8"))
            self.assertIn(rec["failure_class"], set(FAILURE_CLASSES))
            self.assertIn("dysfunction_score", rec)
            self.assertEqual(rec["mode"], "stop")
            # No secrets leaked into record
            blob = json.dumps(rec)
            self.assertNotIn("MCP_API_KEY", blob)
            self.assertNotIn("Bearer", blob)


if __name__ == "__main__":
    unittest.main()
