#!/usr/bin/env python3
"""relevance_gate.py — stdlib-only scope classifier for the relevance-gate
composite action (policy R3).

Contract:
  * Output `scope` is ALWAYS one of: full | reduced.
  * EVERY failure path emits scope=full (the heavy job runs). The gate may
    only ever *reduce* work when the changed-file set is fully known.
  * pull_request: enumerate PR files via REST (cap 3000, fail-closed).
  * push/merge_group/dispatch/schedule: scope=full unconditionally — the
    action itself is expected to be gated `if: event == pull_request`, but
    if it runs anyway it must not emit reduced.
  * RELEVANCE_FORCE_FULL (repo variable) forces scope=full — kill switch.
  * --files a,b,c bypasses enumeration entirely (tests / local runs).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TIMEOUT = 30


def _split_globs(raw: str) -> list[str]:
    out = []
    for chunk in raw.replace("\n", ",").split(","):
        g = chunk.strip()
        if g:
            out.append(g)
    return out


def _get(url: str, token: str) -> tuple[int, object]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "relevance-gate/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # nosec B310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001 — report as failure, caller fails open
        return None, str(e).encode()


def pr_files(owner_repo: str, number: int, token: str, cap: int) -> list[str] | None:
    """All changed filenames in a PR; None on any failure (caller fails open)."""
    files: list[str] = []
    page = 1
    while len(files) <= cap:
        url = (
            f"{API}/repos/{owner_repo}/pulls/{number}/files"
            f"?per_page=100&page={page}"
        )
        status, payload = _get(url, token)
        if status != 200 or not isinstance(payload, list):
            return None
        if not payload:
            return files
        files.extend(str(item.get("filename", "")) for item in payload)
        if len(payload) < 100:
            return files
        page += 1
    return None  # over the cap — fail closed


def push_files(owner_repo: str, base: str, head: str, token: str, cap: int) -> list[str] | None:
    if not base or base.startswith("0000000"):
        return None
    url = f"{API}/repos/{owner_repo}/compare/{urllib.parse.quote(base)}...{urllib.parse.quote(head)}"
    status, payload = _get(url, token)
    if status != 200 or not isinstance(payload, dict):
        return None
    files = payload.get("files") or []
    names = [str(f.get("filename", "")) for f in files]
    if len(names) > cap or payload.get("total_commits", 0) > 250:
        return None
    return names


def classify(files: list[str], globs: list[str]) -> tuple[str, list[str]]:
    matched = sorted({f for f in files for g in globs if fnmatch.fnmatch(f, g)})
    return ("full" if matched else "reduced"), matched


def emit(name: str, value: str) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--paths", required=True)
    p.add_argument("--always-globs", default="")
    p.add_argument("--files-cap", type=int, default=3000)
    p.add_argument("--files", default="")
    args = p.parse_args(argv)

    globs = _split_globs(args.paths) + _split_globs(args.always_globs)
    scope = "full"
    matched: list[str] = []
    changed: list[str] | None = None
    reason = "unset"

    if os.environ.get("RELEVANCE_FORCE_FULL", "").lower() in ("1", "true", "yes"):
        reason = "force-full kill switch"
    elif args.files.strip():
        changed = [f for f in args.files.split(",") if f.strip()]
        reason = "explicit files"
    else:
        event = os.environ.get("EVENT_NAME", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        if event == "pull_request":
            num = os.environ.get("PR_NUMBER", "")
            if repo and token and num:
                changed = pr_files(repo, int(num), token, args.files_cap)
                reason = "pr files" if changed is not None else "pr enum failed"
            else:
                reason = "missing repo/token/number"
        elif event == "push" and repo and token:
            changed = push_files(
                repo,
                os.environ.get("PUSH_BASE", ""),
                os.environ.get("PUSH_HEAD", ""),
                token,
                args.files_cap,
            )
            reason = "push compare" if changed is not None else "push compare failed"
        else:
            reason = f"non-PR event ({event or 'unknown'})"

    if changed is not None:
        scope, matched = classify(changed, globs)
    emit("scope", scope)
    emit("matched", ",".join(matched))
    emit("changed_count", str(len(changed) if changed is not None else -1))
    print(
        f"relevance-gate: scope={scope} reason={reason} "
        f"changed={len(changed) if changed is not None else 'unknown'} "
        f"matched={matched[:8]}",
        file=sys.stderr,
    )
    return 0  # always succeed — a failed step would block the fail-open path


if __name__ == "__main__":
    sys.exit(main())
