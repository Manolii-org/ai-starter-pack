#!/usr/bin/env python3
"""Classify authenticated GitHub review evidence, without granting merge authority.

Callers fetch complete review history from GitHub and retain every finding, trust,
authorization, CI and merge-queue check. A clean summary is evidence of actual
independent review, not an approval submitted under the implementing user's name.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime

BOTS = {"coderabbitai[bot]", "chatgpt-codex-connector[bot]"}
PREFIX = "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->"


def full_sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def bot_review_verdict(reviews: object, head: str) -> str:
    """An empty COMMENTED acknowledgement is never a substantive review."""
    if not full_sha(head) or not isinstance(reviews, list):
        return "none"
    matching = [r for r in reviews if isinstance(r, dict)
                and r.get("commit_id") == head and isinstance(r.get("user"), dict)
                and r["user"].get("login") in BOTS and r["user"].get("type") == "Bot"]
    if any(r.get("state") == "CHANGES_REQUESTED" for r in matching):
        return "block"
    return "satisfy" if any(r.get("state") == "APPROVED" for r in matching) else "none"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate review coverage field")
        result[key] = value
    return result


def _updated(comment: dict):
    """Malformed ordering metadata cannot select an older successful attempt."""
    ident = comment.get("id")
    if type(ident) is not int or ident <= 0:
        raise ValueError("Missing comment identity")
    date = datetime.fromisoformat(comment["updated_at"].replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Missing update timezone")
    return date, ident


def coderabbit_clean_summary(comments: object, head: str, repo: str, pr: int) -> bool:
    """Require the latest canonical bot summary to attest complete head coverage.

    GitHub author/type and issue_url must come from the authenticated API, never
    from PR text or a copied receipt. Coverage markers must be generated outside
    the walkthrough that can contain quoted, contributor-controlled content.
    """
    if not full_sha(head) or not isinstance(comments, list) or type(pr) is not int or pr <= 0:
        return False
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return False
    issue_url = f"https://api.github.com/repos/{repo}/issues/{pr}"
    candidates = []
    identities = {}
    for comment in comments:
        if not isinstance(comment, dict) or comment.get("issue_url") != issue_url:
            continue
        user, body = comment.get("user"), comment.get("body")
        if not isinstance(user, dict) or user.get("login") != "coderabbitai[bot]" or user.get("type") != "Bot":
            continue
        if not isinstance(body, str) or not body.startswith("<!-- This is an auto-generated comment:"):
            continue
        try:
            order = _updated(comment)
            if order[1] in identities and identities[order[1]] != (order, body):
                return False
            identities[order[1]] = (order, body)
            candidates.append((order, body))
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
    if not candidates:
        return False
    _, body = max(candidates, key=lambda row: row[0])
    if not body.startswith(PREFIX):
        return False
    # Remove fenced examples before recognizing generated top-level markers.
    body = re.sub(r"^(```|~~~)[^\n]*\n.*?^\1[ \t]*$", "", body, flags=re.MULTILINE | re.DOTALL)
    markers = ("recent_review_start", "recent_review_end", "walkthrough_start", "walkthrough_end")
    positions = []
    for marker in markers:
        matches = list(re.finditer(r"^<!-- " + marker + r" -->[ \t]*$", body, re.MULTILINE))
        if len(matches) != 1:
            return False
        positions.append(matches[0].start())
    if positions != sorted(positions):
        return False
    status = body[:positions[2]]
    if re.search(r"review (?:in progress|skipped)|rate.?limit|limit reached", status, re.IGNORECASE):
        return False
    recent = re.search(r"^<!-- recent_review_start -->[ \t]*$(.*?)^<!-- recent_review_end -->[ \t]*$",
                       status, re.MULTILINE | re.DOTALL)
    if not recent or not re.match(r"\s*No actionable comments were generated in the recent review\.", recent[1]):
        return False
    coverage = list(re.finditer(r"^<!-- final_review_risk_coverage:(.*?) -->[ \t]*$", body, re.MULTILINE))
    if len(coverage) != 1 or coverage[0].start() < positions[3]:
        return False
    try:
        value = json.loads(coverage[0][1], object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError):
        return False
    return (isinstance(value, dict) and value.get("kind") == "reviewed"
            and value.get("sourceCommitId") == head and value.get("coveredCommitId") == head)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--comments", action="store_true")
    parser.add_argument("--repo")
    parser.add_argument("--pr", type=int)
    args = parser.parse_args()
    try:
        data = json.load(sys.stdin)
        verdict = ("satisfy" if coderabbit_clean_summary(data, args.head_sha, args.repo, args.pr) else "none") if args.comments else bot_review_verdict(data, args.head_sha)
    except (OSError, ValueError, TypeError):
        verdict = "none"
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
