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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BOTS = {"coderabbitai[bot]", "chatgpt-codex-connector[bot]"}
PREFIX = "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->"
GENERATED_HEADER = "<!-- coderabbit-ai-generated:v1 -->\n\n"
REPLY_PREFIX = "<!-- This is an auto-generated reply by CodeRabbit -->"
PROVENANCE_FIELDS = """id databaseId body createdAt updatedAt lastEditedAt includesCreatedEdit
author { login __typename } editor { login __typename }
userContentEdits(first:100, after:$cursor) { totalCount
  nodes { id editedAt editor { login __typename } deletedAt }
  pageInfo { hasNextPage endCursor } }"""


def date(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Missing timestamp timezone")
    return result


def trusted_comment(comment: dict) -> bool:
    """REST authorship alone does not authenticate a mutable issue-comment body."""
    try:
        user = comment["user"]
        login = user["login"]
        if login not in BOTS or user["type"] != "Bot":
            return False
        actor = {"login": login.removesuffix("[bot]"), "__typename": "Bot"}
        node = comment["review_provenance"]
        if (type(comment["id"]) is not int or comment["id"] <= 0
                or type(node["databaseId"]) is not int
                or not isinstance(comment["node_id"], str) or not comment["node_id"]
                or node["id"] != comment["node_id"] or node["databaseId"] != comment["id"]
                or node["author"] != actor or node["body"] != comment["body"]
                or date(node["createdAt"]) != date(comment["created_at"])
                or date(node["updatedAt"]) != date(comment["updated_at"])):
            return False
        history = node["userContentEdits"]
        edits = history["nodes"]
        if (not isinstance(edits, list) or type(history["totalCount"]) is not int
                or history["totalCount"] != len(edits)
                or history["pageInfo"]["hasNextPage"] is not False):
            return False
        if node["lastEditedAt"] is None:
            return (not edits and node["includesCreatedEdit"] is False and node["editor"] is None
                    and date(node["updatedAt"]) == date(node["createdAt"]))
        if not edits or node["includesCreatedEdit"] is not True or node["editor"] != actor:
            return False
        identities = set()
        times = []
        for edit in edits:
            if (edit["editor"] != actor or edit["deletedAt"] is not None
                    or not isinstance(edit["id"], str) or not edit["id"] or edit["id"] in identities):
                return False
            identities.add(edit["id"])
            times.append(date(edit["editedAt"]))
        return (min(times) == date(node["createdAt"])
                and max(times) == date(node["lastEditedAt"]) <= date(node["updatedAt"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def gh_read(arguments, payload=None):
    """Read only, using the caller's existing GitHub CLI authentication."""
    result = subprocess.run(["gh", "api", *arguments], input=payload, text=True,
                            capture_output=True, timeout=30, check=True)
    return result.stdout


def fetch_comments(repo: str, pr: int, head: str) -> list:
    """Fetch all REST pages and complete, stable GraphQL edit provenance."""
    if not full_sha(head) or not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo) or type(pr) is not int or pr <= 0:
        raise ValueError("Invalid PR context")
    raw = gh_read([f"repos/{repo}/issues/{pr}/comments?per_page=100", "--paginate"])
    comments = []
    decoder = json.JSONDecoder()
    while raw.strip():
        page, end = decoder.raw_decode(raw.lstrip())
        if not isinstance(page, list):
            raise ValueError("Incomplete comment pagination")
        comments.extend(page)
        raw = raw.lstrip()[end:]
    for comment in comments:
        if not isinstance(comment, dict):
            raise ValueError("Malformed GitHub comment")
        comment.pop("review_provenance", None)
    needed = []
    recent = ordered_comments(comments, "coderabbitai[bot]",
                              f"https://api.github.com/repos/{repo}/issues/{pr}")
    for index, comment in enumerate(recent):
        if coderabbit_summary_candidate(comment["body"]):
            if clean_coderabbit_body(comment["body"], head):
                needed.extend(recent[:index + 1])
            break
    recognizer = canonical_codex()
    if recognizer is not None:
        recent = ordered_comments(comments, "chatgpt-codex-connector[bot]",
                                  f"https://api.github.com/repos/{repo}/issues/{pr}")
        for index, comment in enumerate(recent):
            if recognizer.clean_pass_satisfies([comment], head):
                needed.extend(recent[:index + 1])
                break
    # Authenticate only bodies that can affect a current review leg. Older
    # histories and unrelated acknowledgements after a selected source are unused.
    for comment in needed:
        cursor = None
        snapshot = None
        edits = []
        cursors = set()
        for _ in range(20):
            query = "query($id:ID!,$cursor:String){node(id:$id){... on IssueComment{" + PROVENANCE_FIELDS + "}}}"
            response = json.loads(gh_read(["graphql", "--input", "-"], json.dumps(
                {"query": query, "variables": {"id": comment["node_id"], "cursor": cursor}})))
            if not isinstance(response, dict) or response.get("errors"):
                raise ValueError("Comment edit provenance unavailable")
            node = response["data"]["node"]
            history = node["userContentEdits"]
            identity = {k: v for k, v in node.items() if k != "userContentEdits"}
            identity["editCount"] = history["totalCount"]
            if snapshot is not None and snapshot != identity:
                raise ValueError("Comment changed during provenance pagination")
            snapshot = identity
            edits.extend(history["nodes"])
            if history["pageInfo"]["hasNextPage"] is False:
                node["userContentEdits"]["nodes"] = edits
                comment["review_provenance"] = node
                break
            if history["pageInfo"]["hasNextPage"] is not True:
                raise ValueError("Incomplete edit pagination")
            cursor = history["pageInfo"]["endCursor"]
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("Invalid edit cursor")
            cursors.add(cursor)
        else:
            raise ValueError("Edit pagination bound reached")
    return comments


def canonical_codex():
    # Retain the installed parser rather than another copy of its phrase rules.
    path = Path(__file__).with_name("codex_clean_pass.py")
    if not path.is_file():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("canonical_codex_clean", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ordered_comments(comments, author, issue_url=None):
    """Order on native metadata; mutable bodies cannot remove newer evidence."""
    if not isinstance(comments, list):
        raise ValueError("Incomplete comment list")
    identities = {}
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get("user"), dict):
            continue
        if comment["user"].get("login") != author or issue_url is not None and comment.get("issue_url") != issue_url:
            continue
        if comment["user"].get("type") != "Bot" or not isinstance(comment.get("body"), str):
            raise ValueError("Malformed bot identity or body")
        order = _updated(comment)
        if order[1] in identities and identities[order[1]] != comment:
            raise ValueError("Conflicting comment snapshots")
        identities[order[1]] = comment
    return sorted(identities.values(), key=lambda comment: _updated(comment), reverse=True)


def codex_clean_verdict(comments, head):
    """Authenticate intervening bodies before the existing Codex recognizer."""
    if not full_sha(head) or not isinstance(comments, list):
        return "none"
    recognizer = canonical_codex()
    if recognizer is None:
        return "none"
    try:
        for comment in ordered_comments(comments, "chatgpt-codex-connector[bot]"):
            if not trusted_comment(comment):
                return "none"
            if recognizer.clean_pass_satisfies([comment], head):
                return "satisfy"
    except (KeyError, TypeError, ValueError, AttributeError):
        return "none"
    return "none"


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
    return date(comment["updated_at"]), ident


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
    try:
        for comment in ordered_comments(comments, "coderabbitai[bot]",
                                        f"https://api.github.com/repos/{repo}/issues/{pr}"):
            # A writer can remove the canonical prefix. Authenticate BEFORE
            # treating a newer bot body as an unrelated acknowledgement.
            if not trusted_comment(comment):
                return False
            if coderabbit_summary_candidate(comment["body"]):
                return clean_coderabbit_body(comment["body"], head)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return False


def coderabbit_body(body):
    """Normalize only the known generated header; authenticate the raw body."""
    return body.removeprefix(GENERATED_HEADER)


def coderabbit_summary_candidate(body):
    """Unknown or repeated generated envelopes cannot hide a newer attempt."""
    # Be conservative when selecting attempts: malformed leading whitespace
    # must not turn a newer pending review into an unrelated acknowledgement.
    # The clean parser below still requires the exact envelope and prefix.
    normalized = coderabbit_body(body).lstrip()
    return (normalized.startswith("<!-- This is an auto-generated comment:")
            or (body.lstrip().startswith("<!-- coderabbit-ai-generated:")
                and not normalized.startswith(REPLY_PREFIX)))


def clean_coderabbit_body(body, head):
    """Pure shape check, used for fetch planning only until provenance passes."""
    body = coderabbit_body(body)
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
    parser.add_argument("--head-sha")
    parser.add_argument("--comments", action="store_true")
    parser.add_argument("--codex", action="store_true")
    parser.add_argument("--fetch-comments", action="store_true")
    parser.add_argument("--repo")
    parser.add_argument("--pr", type=int)
    args = parser.parse_args()
    try:
        if args.fetch_comments:
            print(json.dumps(fetch_comments(args.repo, args.pr, args.head_sha)))
            return 0
        data = json.load(sys.stdin)
        if args.codex:
            verdict = codex_clean_verdict(data, args.head_sha)
        else:
            verdict = ("satisfy" if coderabbit_clean_summary(data, args.head_sha, args.repo, args.pr) else "none") if args.comments else bot_review_verdict(data, args.head_sha)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        if args.fetch_comments:
            print("Review comment provenance unavailable; comment review remains unsatisfied.", file=sys.stderr)
            return 1
        verdict = "none"
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
