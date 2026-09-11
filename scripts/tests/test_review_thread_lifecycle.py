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
    """Test parse_fix_evidence function."""

    def test_valid_evidence_trusted_author(self):
        """Test extraction of valid fix-evidence from github-actions[bot]."""
        comments = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": (
                    "<!-- auto-merge:fix-evidence:v1 -->\n"
                    "**Finding:** https://github.com/org/repo/pull/123/files#discussion_r456\n"
                    "**Fixed in:** `abc1234def5678`\n"
                    "**Test:** `npm test`"
                ),
            }
        ]
        result = RTL.parse_fix_evidence(comments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["finding_url"], "https://github.com/org/repo/pull/123/files#discussion_r456")
        self.assertEqual(result[0]["fixed_in_sha"], "abc1234def5678")
        self.assertEqual(result[0]["test_cmd"], "npm test")

    def test_human_authored_evidence_ignored(self):
        """Test that human-authored evidence is ignored."""
        comments = [{"user": {"login": "alice"}, "body": "<!-- auto-merge:fix-evidence:v1 -->\n**Finding:** url\n**Fixed in:** `abc1234`"}]
        result = RTL.parse_fix_evidence(comments)
        self.assertEqual(len(result), 0)

    def test_cursor_bot_evidence_trusted(self):
        """cursor[bot] is a trusted fix-evidence author (SHA+URL still required)."""
        comments = [
            {
                "user": {"login": "cursor[bot]"},
                "body": (
                    "<!-- auto-merge:fix-evidence:v1 -->\n"
                    "**Finding:** https://github.com/org/repo/pull/999#discussion_r300\n"
                    "**Fixed in:** `abc12345`"
                ),
            }
        ]
        result = RTL.parse_fix_evidence(comments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["fixed_in_sha"], "abc12345")

    def test_missing_marker_ignored(self):
        """Test that comments without marker are ignored."""
        comments = [{"user": {"login": "github-actions[bot]"}, "body": "**Finding:** url\n**Fixed in:** `abc1234`"}]
        result = RTL.parse_fix_evidence(comments)
        self.assertEqual(len(result), 0)

    def test_invalid_sha_ignored(self):
        """Test that invalid SHAs are ignored."""
        comments = [{"user": {"login": "github-actions[bot]"}, "body": "<!-- auto-merge:fix-evidence:v1 -->\n**Finding:** url\n**Fixed in:** `short`"}]
        result = RTL.parse_fix_evidence(comments)
        self.assertEqual(len(result), 0)

    def test_malformed_evidence_safe(self):
        """Test that malformed evidence is skipped safely."""
        comments = [{"user": {"login": "github-actions[bot]"}, "body": None}, {"user": {}, "body": "test"}]
        result = RTL.parse_fix_evidence(comments)
        self.assertEqual(len(result), 0)


class TestClassifyBlockingThreads(unittest.TestCase):
    """Test classify_blocking_threads function."""

    def test_fresh_p1_on_head(self):
        """Test fresh P1 badge on head → blocking_fresh."""
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "🟠 Major issue here",
                            "commit": {"oid": "abc1234567890"},
                            "originalCommit": None,
                            "path": "src/app.ts",
                            "line": 42,
                            "url": "https://github.com/org/repo/pull/999#discussion_r100",
                        }
                    ]
                },
            }
        ]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 1)
        self.assertEqual(result["threads"][0]["classification"], "blocking_fresh")
        self.assertFalse(result["threads"][0]["predates_head"])

    def test_carried_p1_old_original(self):
        """Test P1 on head but original on old commit → blocking_carried."""
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "🔴 Critical",
                            "commit": {"oid": "abc1234567890"},
                            "originalCommit": {"oid": "oldsha1111111"},
                            "path": "src/api.ts",
                            "line": 10,
                            "url": "https://github.com/org/repo/pull/999#discussion_r200",
                        }
                    ]
                },
            }
        ]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=["coderabbitai"])
        self.assertEqual(result["threads"][0]["classification"], "blocking_carried")
        self.assertTrue(result["threads"][0]["predates_head"])

    def test_remediation_pending_with_fix_evidence(self):
        """Test thread with matching fix-evidence → remediation_pending_resolution."""
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "![P1 Badge] Security issue",
                            "commit": {"oid": "abc1234567890abcdef"},
                            "originalCommit": None,
                            "path": "lib/auth.py",
                            "line": 25,
                            "url": "https://github.com/org/repo/pull/999#discussion_r300",
                        }
                    ]
                },
            }
        ]
        comments = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": "<!-- auto-merge:fix-evidence:v1 -->\n**Finding:** https://github.com/org/repo/pull/999#discussion_r300\n**Fixed in:** `abc12345`",
            }
        ]
        result = RTL.classify_blocking_threads(
            threads, "abc1234567890abcdef", bot_logins=["coderabbitai"], issue_comments=comments
        )
        self.assertEqual(result["threads"][0]["classification"], "remediation_pending_resolution")
        self.assertTrue(result["has_remediation_pending"])
        self.assertIsNotNone(result["threads"][0]["fix_evidence"])

    def test_cursor_bot_matching_sha_is_remediation_pending(self):
        """cursor[bot] + exact URL + head SHA prefix → remediation_pending_resolution."""
        url = "https://github.com/org/repo/pull/999#discussion_r301"
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "![P1 Badge] Docs P1",
                            "commit": {"oid": "abc1234567890abcdef"},
                            "originalCommit": None,
                            "path": "docs/pr-autofix-loop-policy.md",
                            "line": 10,
                            "url": url,
                        }
                    ]
                },
            }
        ]
        comments = [
            {
                "user": {"login": "cursor[bot]"},
                "body": (
                    "<!-- auto-merge:fix-evidence:v1 -->\n"
                    f"**Finding:** {url}\n"
                    "**Fixed in:** `abc12345`"
                ),
            }
        ]
        result = RTL.classify_blocking_threads(
            threads, "abc1234567890abcdef", bot_logins=["coderabbitai"], issue_comments=comments
        )
        self.assertEqual(result["threads"][0]["classification"], "remediation_pending_resolution")

    def test_cursor_bot_wrong_sha_still_blocking(self):
        """cursor[bot] evidence for a different SHA must not remediate the thread."""
        url = "https://github.com/org/repo/pull/999#discussion_r302"
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "![P1 Badge] Docs P1",
                            "commit": {"oid": "abc1234567890abcdef"},
                            "originalCommit": None,
                            "path": "docs/pr-autofix-loop-policy.md",
                            "line": 10,
                            "url": url,
                        }
                    ]
                },
            }
        ]
        comments = [
            {
                "user": {"login": "cursor[bot]"},
                "body": (
                    "<!-- auto-merge:fix-evidence:v1 -->\n"
                    f"**Finding:** {url}\n"
                    "**Fixed in:** `deadbeef`"
                ),
            }
        ]
        result = RTL.classify_blocking_threads(
            threads, "abc1234567890abcdef", bot_logins=["coderabbitai"], issue_comments=comments
        )
        self.assertEqual(result["threads"][0]["classification"], "blocking_fresh")
        self.assertFalse(result["has_remediation_pending"])

    def test_thread_on_old_commit_is_blocking_and_carried(self):
        """An unresolved severity thread on an EARLIER commit still blocks."""
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "🔴 Critical",
                            "commit": {"oid": "oldsha1111111"},
                            "originalCommit": None,
                            "path": "test.py",
                            "line": 5,
                            "url": "https://github.com/org/repo/pull/999#discussion_r400",
                        }
                    ]
                },
            }
        ]
        result = RTL.classify_blocking_threads(threads, "newsha2222222", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 1)
        self.assertEqual(result["threads"][0]["classification"], "blocking_carried")
        self.assertTrue(result["threads"][0]["predates_head"])

    def test_next_action_does_not_contradict_a_zero_count(self):
        """With nothing blocking, the report must not say auto-merge is blocked."""
        result = RTL.classify_blocking_threads([], "anysha", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 0)
        self.assertNotIn("remains blocked", result["next_action"])
        md = RTL.format_diagnostic_markdown(result)
        self.assertIn("No blocking threads found.", md)
        self.assertNotIn("Auto-merge remains blocked", md)

    def test_resolved_thread_on_old_commit_still_does_not_block(self):
        """Widening the commit scope must not make resolution stop working."""
        threads = [
            {
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "🔴 Critical",
                            "commit": {"oid": "oldsha1111111"},
                            "originalCommit": None,
                            "path": "test.py",
                            "line": 5,
                            "url": "https://github.com/org/repo/pull/999#discussion_r401",
                        }
                    ]
                },
            }
        ]
        result = RTL.classify_blocking_threads(threads, "newsha2222222", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 0)

    def test_p2_badge_not_matched(self):
        """Test P2 badge not matched by default severity regex."""
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "![P2 Badge] Minor issue",
                            "commit": {"oid": "abc1234567890"},
                            "originalCommit": None,
                            "path": "docs/readme.md",
                            "line": 1,
                            "url": "https://github.com/org/repo/pull/999#discussion_r500",
                        }
                    ]
                },
            }
        ]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 0)

    def test_resolved_thread_ignored(self):
        """Test that resolved threads are not included."""
        threads = [
            {
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "🔴 Critical",
                            "commit": {"oid": "abc1234567890"},
                            "originalCommit": None,
                            "path": "src/index.ts",
                            "line": 1,
                            "url": "https://github.com/org/repo/pull/999#discussion_r600",
                        }
                    ]
                },
            }
        ]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 0)

    def test_bot_filter_applied(self):
        """Test that threads from non-bot authors are excluded."""
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "alice"},
                            "body": "🔴 Critical",
                            "commit": {"oid": "abc1234567890"},
                            "originalCommit": None,
                            "path": "src/main.py",
                            "line": 100,
                            "url": "https://github.com/org/repo/pull/999#discussion_r700",
                        }
                    ]
                },
            }
        ]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 0)

    def test_default_bot_logins_when_none(self):
        """None bot_logins defaults to coderabbitai + chatgpt-codex-connector."""
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "coderabbitai"},
                            "body": "🔴 Critical",
                            "commit": {"oid": "abc1234567890"},
                            "originalCommit": None,
                            "path": "a.py",
                            "line": 1,
                            "url": "https://example.com/r1",
                        }
                    ]
                },
            }
        ]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=None)
        self.assertEqual(result["blocking_count"], 1)

    def test_empty_threads_safe(self):
        """Test empty threads list returns empty result."""
        result = RTL.classify_blocking_threads([], "abc1234567890")
        self.assertEqual(result["blocking_count"], 0)

    def test_malformed_thread_skipped(self):
        """Test that malformed threads are skipped safely."""
        threads = [{"isResolved": False, "comments": None}, {"isResolved": False, "comments": {"nodes": [None]}}]
        result = RTL.classify_blocking_threads(threads, "abc1234567890", bot_logins=["coderabbitai"])
        self.assertEqual(result["blocking_count"], 0)


class TestFormatDiagnosticMarkdown(unittest.TestCase):
    """Test format_diagnostic_markdown function."""

    def test_markdown_includes_marker(self):
        """Test that markdown includes the marker line."""
        result = {
            "head_sha": "abc1234567890",
            "blocking_count": 0,
            "has_remediation_pending": False,
            "threads": [],
            "next_action": "No action needed",
        }
        md = RTL.format_diagnostic_markdown(result)
        self.assertIn("<!-- auto-merge:unresolved-thread:v1 head=abc1234567890 -->", md)

    def test_markdown_includes_next_action(self):
        """Test that markdown includes next_action."""
        result = {
            "head_sha": "abc1234567890",
            "blocking_count": 1,
            "has_remediation_pending": False,
            "threads": [
                {
                    "classification": "blocking_fresh",
                    "author": "coderabbitai",
                    "path": "src/test.py",
                    "line": 10,
                    "url": "https://github.com/org/repo/pull/999#discussion_r100",
                    "commit_oid": "abc1234567890",
                    "original_commit_oid": None,
                    "severity_matched": True,
                    "predates_head": False,
                    "fix_evidence": None,
                }
            ],
            "next_action": "Reviewer must resolve threads",
        }
        md = RTL.format_diagnostic_markdown(result)
        self.assertIn("Reviewer must resolve threads", md)
        self.assertIn("blocking_fresh", md)

    def test_markdown_with_fix_evidence(self):
        """Test markdown includes fix-evidence when present."""
        result = {
            "head_sha": "abc1234567890",
            "blocking_count": 1,
            "has_remediation_pending": True,
            "threads": [
                {
                    "classification": "remediation_pending_resolution",
                    "author": "coderabbitai",
                    "path": "src/test.py",
                    "line": 10,
                    "url": "https://github.com/org/repo/pull/999#discussion_r100",
                    "commit_oid": "abc1234567890",
                    "original_commit_oid": None,
                    "severity_matched": True,
                    "predates_head": False,
                    "fix_evidence": {"finding_url": "...", "fixed_in_sha": "abc12345", "test_cmd": "npm test"},
                }
            ],
            "next_action": "Verify fix",
        }
        md = RTL.format_diagnostic_markdown(result)
        self.assertIn("abc12345", md)
        self.assertIn("npm test", md)

    def test_markdown_notes_auto_merge_control(self):
        """Diagnostic notes this is an auto-merge control, not branch protection."""
        result = {
            "head_sha": "abc1234567890",
            "blocking_count": 0,
            "has_remediation_pending": False,
            "threads": [],
            "next_action": "No action needed",
        }
        md = RTL.format_diagnostic_markdown(result)
        self.assertIn("auto-merge control", md)
        self.assertIn("branch-protection", md)


if __name__ == "__main__":
    unittest.main()
