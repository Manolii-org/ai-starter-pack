#!/usr/bin/env python3
"""Unit tests for review-thread-lifecycle and related scripts."""

import importlib.util
import unittest
from pathlib import Path


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


RTL = _load("review_thread_lifecycle", Path(__file__).resolve().parents[1] / "ci" / "review-thread-lifecycle.py")


class TestParseFixEvidence(unittest.TestCase):
    def test_valid_evidence_trusted_author(self):
        comments = [{"user": {"login": "github-actions[bot]"}, "body": "<!-- auto-merge:fix-evidence:v1 -->\n**Finding:** https://github.com/org/repo/pull/123/files#discussion_r456\n**Fixed in:** `abc1234def5678`\n**Test:** `npm test`"}]
        result = RTL.parse_fix_evidence(comments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["finding_url"], "https://github.com/org/repo/pull/123/files#discussion_r456")
        self.assertEqual(result[0]["fixed_in_sha"], "abc1234def5678")
        self.assertEqual(result[0]["test_cmd"], "npm test")

    def test_human_authored_evidence_ignored(self):
        comments = [{"user": {"login": "alice"}, "body": "<!-- auto-merge:fix-evidence:v1 -->\n**Finding:** url\n**Fixed in:** `abc1234`"}]
        self.assertEqual(len(RTL.parse_fix_evidence(comments)), 0)

    def test_cursor_bot_evidence_trusted(self):
        comments = [{"user": {"login": "cursor[bot]"}, "body": "<!-- auto-merge:fix-evidence:v1 -->\n**Finding:** https://github.com/org/repo/pull/999#discussion_r300\n**Fixed in:** `abc12345`"}]
        result = RTL.parse_fix_evidence(comments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["fixed_in_sha"], "abc12345")

    def test_missing_marker_ignored(self):
        comments = [{"user": {"login": "github-actions[bot]"}, "body": "**Finding:** url\n**Fixed in:** `abc1234`"}]
        self.assertEqual(len(RTL.parse_fix_evidence(comments)), 0)

    def test_invalid_sha_ignored(self):
        comments = [{"user": {"login": "github-actions[bot]"}, "body": "<!-- auto-merge:fix-evidence:v1 -->\n**Finding:** url\n**Fixed in:** `short`"}]
        self.assertEqual(len(RTL.parse_fix_evidence(comments)), 0)

    def test_malformed_evidence_safe(self):
        comments = [{"user": {"login": "github-actions[bot]"}, "body": None}, {"user": {}, "body": "test"}]
        self.assertEqual(len(RTL.parse_fix_evidence(comments)), 0)


class TestClassifyBlockingThreads(unittest.TestCase):
    def test_fresh_p1_on_head(self):
        threads = [{"isResolved": False, "comments": {"nodes": [{"author": {"login": "coderabbitai"}, "body": "🟠 Major issue here", "commit": {"oid": "abc1234567890"}, "originalCommit": None, "path": "src/app.ts", "line": 42, "url": "https://github.com/org/repo/pull/999#discussion_r100"}]}}]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 1)
        self.assertEqual(result["threads"][0]["classification"], "blocking_fresh")

    def test_carried_p1_old_original(self):
        threads = [{"isResolved": False, "comments": {"nodes": [{"author": {"login": "coderabbitai"}, "body": "🔴 Critical", "commit": {"oid": "abc1234567890"}, "originalCommit": {"oid": "oldsha1111111"}, "path": "src/api.ts", "line": 10, "url": "https://github.com/org/repo/pull/999#discussion_r200"}]}}]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=["coderabbitai"])
        self.assertEqual(result["threads"][0]["classification"], "blocking_carried")

    def test_remediation_pending_with_fix_evidence(self):
        threads = [{"isResolved": False, "comments": {"nodes": [{"author": {"login": "coderabbitai"}, "body": "![P1 Badge] Security issue", "commit": {"oid": "abc1234567890abcdef"}, "originalCommit": None, "path": "lib/auth.py", "line": 25, "url": "https://github.com/org/repo/pull/999#discussion_r300"}]}}]
        comments = [{"user": {"login": "github-actions[bot]"}, "body": "<!-- auto-merge:fix-evidence:v1 -->\n**Finding:** https://github.com/org/repo/pull/999#discussion_r300\n**Fixed in:** `abc12345`"}]
        result = RTL.classify_blocking_threads(threads, "abc1234567890abcdef", bot_logins=["coderabbitai"], issue_comments=comments)
        self.assertEqual(result["threads"][0]["classification"], "remediation_pending_resolution")

    def test_thread_on_old_commit_is_blocking_and_carried(self):
        threads = [{"isResolved": False, "comments": {"nodes": [{"author": {"login": "coderabbitai"}, "body": "🔴 Critical", "commit": {"oid": "oldsha1111111"}, "originalCommit": None, "path": "test.py", "line": 5, "url": "https://github.com/org/repo/pull/999#discussion_r400"}]}}]
        result = RTL.classify_blocking_threads(threads, "newsha2222222", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 1)
        self.assertEqual(result["threads"][0]["classification"], "blocking_carried")

    def test_next_action_does_not_contradict_a_zero_count(self):
        result = RTL.classify_blocking_threads([], "anysha", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 0)
        self.assertNotIn("remains blocked", result["next_action"])

    def test_empty_threads_safe(self):
        self.assertEqual(RTL.classify_blocking_threads([], "abc1234567890")["blocking_count"], 0)


class TestFormatDiagnosticMarkdown(unittest.TestCase):
    def test_markdown_includes_marker(self):
        result = {"head_sha": "abc1234567890", "blocking_count": 0, "has_remediation_pending": False, "threads": [], "next_action": "No action needed"}
        self.assertIn("<!-- auto-merge:unresolved-thread:v1 head=abc1234567890 -->", RTL.format_diagnostic_markdown(result))


if __name__ == "__main__":
    unittest.main()
