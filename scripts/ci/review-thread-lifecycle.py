#!/usr/bin/env python3
"""Review Thread Lifecycle: Classify unresolved high/critical/major review threads."""

import argparse
import json
import logging
import re
import sys

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)

FIX_EVIDENCE_MARKER = "<!-- auto-merge:fix-evidence:v1 -->"
TRUSTED_FIX_EVIDENCE_AUTHORS = frozenset({"github-actions[bot]", "cursor[bot]"})
DEFAULT_SEVERITY_REGEX = r"!\[high\]|🔴 Critical|🟠 Major|!\[P1 Badge\]"


def parse_fix_evidence(issue_comments: list[dict]) -> list[dict]:
    """Extract structured fix-evidence comments from trusted authors only."""
    evidence_list = []
    for comment in issue_comments:
        try:
            user_login = comment.get("user", {}).get("login", "")
            body = comment.get("body", "")
            if user_login not in TRUSTED_FIX_EVIDENCE_AUTHORS or FIX_EVIDENCE_MARKER not in body:
                continue
            finding_url = None
            fixed_in_sha = None
            test_cmd = None
            finding_match = re.search(r"\*\*Finding:\*\*\s+([^\n]+)", body)
            if finding_match:
                finding_url = finding_match.group(1).strip()
            fixed_match = re.search(r"\*\*Fixed in:\*\*\s+`([0-9a-f]+)`", body)
            if fixed_match:
                sha = fixed_match.group(1)
                if 7 <= len(sha) <= 40 and re.fullmatch(r"[0-9a-f]+", sha):
                    fixed_in_sha = sha
            test_match = re.search(r"\*\*Test:\*\*\s+`([^`]+)`", body)
            if test_match:
                test_cmd = test_match.group(1)
            if finding_url and fixed_in_sha:
                evidence_list.append({"finding_url": finding_url, "fixed_in_sha": fixed_in_sha, "test_cmd": test_cmd})
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning(f"Failed to parse fix-evidence comment: {e}")
    return evidence_list


def classify_blocking_threads(
    threads: list[dict],
    head_sha: str,
    *,
    severity_re: str = DEFAULT_SEVERITY_REGEX,
    bot_logins: list[str] | None = None,
    issue_comments: list[dict] | None = None,
) -> dict:
    """Classify unresolved high/critical/major review threads on the current head."""
    if bot_logins is None:
        bot_logins = ["coderabbitai", "chatgpt-codex-connector"]
    if issue_comments is None:
        issue_comments = []
    bot_logins_set = set(bot_logins)
    evidence_list = parse_fix_evidence(issue_comments)
    blocking_threads = []
    severity_pattern = re.compile(severity_re, re.IGNORECASE)
    head_l = (head_sha or "").lower()
    for thread in threads:
        try:
            is_resolved = thread.get("isResolved", True)
            if is_resolved:
                continue
            comments_node = thread.get("comments", {})
            comments_list = comments_node.get("nodes", []) if isinstance(comments_node, dict) else []
            if not comments_list:
                continue
            first_comment = comments_list[0]
            author_login = first_comment.get("author", {}).get("login", "")
            if author_login not in bot_logins_set:
                continue
            body = first_comment.get("body", "")
            if not severity_pattern.search(body):
                continue
            commit_obj = first_comment.get("commit") or {}
            commit_oid = commit_obj.get("oid", "") if isinstance(commit_obj, dict) else ""
            original_obj = first_comment.get("originalCommit")
            original_commit_oid = (
                original_obj.get("oid") if isinstance(original_obj, dict) else None
            )
            path = first_comment.get("path", "")
            line = first_comment.get("line")
            url = first_comment.get("url", "")
            predates_head = (
                (bool(commit_oid) and commit_oid != head_sha)
                or (original_commit_oid is not None and original_commit_oid != head_sha)
            )
            classification = "blocking_fresh"
            fix_evidence_match = None
            for evidence in evidence_list:
                finding = evidence["finding_url"]
                sha = evidence["fixed_in_sha"].lower()
                url_match = bool(url) and finding == url
                sha_match = len(sha) >= 7 and head_l.startswith(sha)
                if url_match and sha_match:
                    classification = "remediation_pending_resolution"
                    fix_evidence_match = evidence
                    break
            if classification == "blocking_fresh" and predates_head:
                classification = "blocking_carried"
            blocking_threads.append({
                "classification": classification,
                "author": author_login,
                "path": path,
                "line": line,
                "url": url,
                "commit_oid": commit_oid,
                "original_commit_oid": original_commit_oid,
                "severity_matched": True,
                "predates_head": predates_head,
                "fix_evidence": fix_evidence_match,
            })
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning(f"Failed to classify thread: {e}")
    has_remediation_pending = any(t["classification"] == "remediation_pending_resolution" for t in blocking_threads)
    if not blocking_threads:
        next_action = (
            "No unresolved high/critical/major bot threads on this PR. This check is not "
            "blocking auto-merge; if auto-merge is still not enabled, another leg is the cause."
        )
    elif has_remediation_pending:
        next_action = (
            "Reviewer/human must resolve the GitHub review thread(s) after verifying the "
            "claimed fix. Fix-evidence does not enable auto-merge."
        )
    else:
        next_action = (
            "Reviewer/human must resolve the unresolved high/critical/major thread(s) on "
            "this PR — including any opened on an earlier commit (or push a fix and then "
            "resolve). Auto-merge remains blocked."
        )
    return {
        "head_sha": head_sha,
        "blocking_count": len(blocking_threads),
        "has_remediation_pending": has_remediation_pending,
        "threads": blocking_threads,
        "next_action": next_action,
    }


def format_diagnostic_markdown(result: dict) -> str:
    lines = [
        f"<!-- auto-merge:unresolved-thread:v1 head={result['head_sha']} -->",
        "",
        "## Auto-Merge Review-Thread Diagnostic",
        "",
        f"**Head SHA:** `{result['head_sha']}`  ",
        f"**Blocking threads:** {result['blocking_count']}  ",
        f"**Remediation pending:** {result['has_remediation_pending']}",
        "",
    ]
    if result["threads"]:
        lines.append("### Unresolved Threads")
        lines.append("")
        for i, thread in enumerate(result["threads"], 1):
            classification = thread["classification"]
            icon = "🔴" if classification == "blocking_fresh" else "🟡" if classification == "blocking_carried" else "🟢"
            lines.append(f"{i}. {icon} **{classification}**")
            lines.append(f"   - Author: `{thread['author']}`")
            lines.append(f"   - Path: `{thread['path']}` (line {thread['line']})")
            lines.append(f"   - URL: {thread['url']}")
            if thread["fix_evidence"]:
                ev = thread["fix_evidence"]
                lines.append(f"   - Fix evidence: `{ev['fixed_in_sha']}` {ev.get('test_cmd') or '(no test)'}")
            lines.append("")
    else:
        lines.append("No blocking threads found.")
        lines.append("")
    lines.append("### Next Action")
    lines.append("")
    lines.append(result["next_action"])
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_This is an **auto-merge control** diagnostic (not a required branch-protection "
        "check failure). The marker does **not** resolve review threads and does **not** "
        "enable auto-merge. A reviewer/human must take action in GitHub._"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify unresolved review threads and format diagnostics.")
    parser.add_argument("--head-sha", required=True, help="Current HEAD SHA (full or prefix >= 7)")
    parser.add_argument("--threads-file", help="Path to GraphQL threads JSON (default: stdin)")
    parser.add_argument("--comments-file", help="Path to issue comments JSON for fix-evidence lookup")
    parser.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    parser.add_argument("--severity-re", default=DEFAULT_SEVERITY_REGEX, help="Severity regex pattern")
    parser.add_argument("--bot-logins", nargs="*", default=["coderabbitai", "chatgpt-codex-connector"])
    args = parser.parse_args()
    try:
        if args.threads_file:
            with open(args.threads_file) as f:
                threads_data = json.load(f)
        else:
            threads_data = json.load(sys.stdin)
        if not isinstance(threads_data, list):
            threads_data = threads_data.get("threads", []) if isinstance(threads_data, dict) else []
        issue_comments: list[dict] = []
        if args.comments_file:
            with open(args.comments_file) as f:
                comments_data = json.load(f)
            issue_comments = comments_data if isinstance(comments_data, list) else comments_data.get("comments", [])
    except (json.JSONDecodeError, OSError, TypeError) as e:
        logger.warning(f"Failed to load input JSON: {e}")
        threads_data = []
        issue_comments = []
    result = classify_blocking_threads(threads_data, args.head_sha, severity_re=args.severity_re, bot_logins=args.bot_logins, issue_comments=issue_comments)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif args.format == "markdown":
        print(format_diagnostic_markdown(result))
    else:
        print(f"head_sha: {result['head_sha']}")
        print(f"blocking_count: {result['blocking_count']}")
        print(f"has_remediation_pending: {result['has_remediation_pending']}")
        if result["threads"]:
            print("threads:")
            for thread in result["threads"]:
                print(f"  - {thread['classification']}: {thread['path']}:{thread['line']} ({thread['author']})")
        print(f"\nnext_action: {result['next_action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
