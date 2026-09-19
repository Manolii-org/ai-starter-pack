#!/usr/bin/env python3
"""Closed-loop GitHub issue helper for self-refiring automation.

Producers call ``signal`` when a condition is unhealthy and ``recover`` only
when their own positive health check passes. The helper maintains one issue per
stable key and records a machine-readable marker, avoiding append-only incident
ledgers. It never closes human-authored issues or an issue with a human assignee.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

MARKER = "<!-- issue-lifecycle:v1 key={key} -->"


def request(url: str, method: str, token: str, payload: dict | None = None):
    if not url.startswith("https://api.github.com/"):
        raise ValueError("GitHub API URL must use https://api.github.com/")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "issue-lifecycle/1",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc)
        except Exception:
            detail = {"message": str(exc)}
        return exc.code, detail


def list_open(owner: str, repo: str, token: str, http=request) -> list[dict]:
    issues: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"state": "open", "per_page": 100, "page": page})
        status, batch = http(
            f"https://api.github.com/repos/{owner}/{repo}/issues?{query}", "GET", token
        )
        if status != 200 or not isinstance(batch, list):
            raise RuntimeError(f"issue list failed HTTP {status}")
        issues.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            return issues
        page += 1


def marker(key: str) -> str:
    return MARKER.format(key=key)


def select(issues: list[dict], key: str) -> list[dict]:
    needle = marker(key)
    return [issue for issue in issues if needle in str(issue.get("body") or "")]


def human_engaged(issue: dict) -> bool:
    author = issue.get("user") or {}
    if author.get("type") != "Bot" and author.get("login") != "github-actions[bot]":
        return True
    return any((a or {}).get("type") != "Bot" for a in issue.get("assignees") or [])


def signal(owner: str, repo: str, token: str, key: str, title: str, body: str,
           labels: list[str], run_url: str, http=request) -> tuple[str, int]:
    matches = sorted(select(list_open(owner, repo, token, http), key), key=lambda i: i["number"])
    content = f"{marker(key)}\n\n{body}\n\nLatest failing run: {run_url}"
    if matches:
        issue = matches[-1]
        status, _ = http(issue["comments_url"], "POST", token, {"body": content})
        if status != 201:
            raise RuntimeError(f"issue comment failed HTTP {status}")
        return "commented", int(issue["number"])
    status, created = http(
        f"https://api.github.com/repos/{owner}/{repo}/issues", "POST", token,
        {"title": title, "body": content, "labels": labels},
    )
    if status != 201:
        raise RuntimeError(f"issue create failed HTTP {status}")
    return "created", int(created["number"])


def recover(owner: str, repo: str, token: str, key: str, evidence: str,
            run_url: str, http=request) -> list[int]:
    closed: list[int] = []
    for issue in select(list_open(owner, repo, token, http), key):
        if human_engaged(issue):
            continue
        number = int(issue["number"])
        comment = f"{marker(key)}\n\nRecovered: {evidence}\n\nHealthy run: {run_url}"
        base = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
        status, _ = http(f"{base}/comments", "POST", token, {"body": comment})
        if status != 201:
            raise RuntimeError(f"recovery comment failed HTTP {status}")
        status, _ = http(base, "PATCH", token, {"state": "closed", "state_reason": "completed"})
        if status != 200:
            raise RuntimeError(f"issue close failed HTTP {status}")
        closed.append(number)
    return closed


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("signal", "recover"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--key", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--evidence")
    args = parser.parse_args(argv)
    if "/" not in args.repo or not args.token:
        parser.error("--repo owner/name and --token (or corresponding env vars) are required")
    if args.command == "signal" and (not args.title or not args.body):
        parser.error("signal requires --title and --body")
    if args.command == "recover" and not args.evidence:
        parser.error("recover requires --evidence")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    owner, repo = args.repo.split("/", 1)
    if args.command == "signal":
        action, number = signal(owner, repo, args.token, args.key, args.title, args.body,
                                args.label, args.run_url)
        print(f"{action} issue #{number}")
    else:
        closed = recover(owner, repo, args.token, args.key, args.evidence, args.run_url)
        print("closed=" + ",".join(map(str, closed)) if closed else "closed=none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
