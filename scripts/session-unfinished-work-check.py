#!/usr/bin/env python3
"""Detect unfinished agent work.

Advisory by default: exit 0 for clean/advisory findings, 1 for unfinished
work only with --strict, and 2 for lookup errors.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


REPO_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_TASK_PATH = REPO_ROOT / ".ai" / "sessions" / "active-task.json"
DEFAULT_WRITE_PATH = REPO_ROOT / ".ai" / "unfinished-work.md"

HTTP_TIMEOUT_SECONDS = 20
SUBPROCESS_TIMEOUT_SECONDS = 20
TERMINAL_TASK_STATUSES = {"done", "merged", "cancelled", "canceled"}
TOKEN_IN_URL_RE = re.compile(r"(https?://)([^/@\s]+)@")


def _safe_detail(text: str) -> str:
    return TOKEN_IN_URL_RE.sub(r"\1[redacted]@", text).strip()


def _run(
    command: list[str],
    *,
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LookupError(f"command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LookupError(f"command timed out after {timeout}s: {command[0]}") from exc

    if check and result.returncode != 0:
        detail = _safe_detail(result.stderr or result.stdout or "")
        message = f"command failed ({result.returncode}): {command[0]}"
        if detail:
            message = f"{message}: {detail}"
        raise LookupError(message)

    return result


def _git(
    args: list[str],
    *,
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
    check: bool = True,
) -> str:
    return _run(["git", *args], timeout=timeout, check=check).stdout.strip()


def _default_branch() -> str:
    first_error: LookupError | None = None
    try:
        ref = _git(["symbolic-ref", "refs/remotes/origin/HEAD", "--short"])
        return ref.split("/", 1)[1] if "/" in ref else ref
    except LookupError as exc:
        first_error = exc

    for candidate in ("main", "master"):
        try:
            _git(["rev-parse", "--verify", candidate])
            return candidate
        except LookupError:
            continue

    try:
        configured = _git(["config", "--get", "init.defaultBranch"])
        if configured:
            return configured
    except LookupError:
        if first_error is not None:
            raise first_error
        raise

    if first_error is not None:
        raise first_error
    raise LookupError("unable to determine default branch")


def _current_branch() -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"])


def _unique_commit_count(base: str) -> int:
    errors: list[Exception] = []
    for ref in (f"origin/{base}", base):
        try:
            count = _git(["rev-list", "--count", f"{ref}..HEAD"])
            return int(count or "0")
        except (LookupError, ValueError) as exc:
            errors.append(exc)

    raise LookupError(f"unable to count unique commits against {base}") from errors[-1]


def _remote_has_branch(branch: str) -> bool:
    result = _run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False

    detail = _safe_detail(result.stderr or result.stdout or "")
    message = "unable to check remote branch"
    if detail:
        message = f"{message}: {detail}"
    raise LookupError(message)


_REPO_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")


def _repo_slug() -> str | None:
    remote_url = _git(["config", "--get", "remote.origin.url"])

    if "github.com:" in remote_url:
        # scp-style: git@github.com:owner/repo(.git)
        path = remote_url.split("github.com:", 1)[1]
    else:
        parsed = parse.urlparse(remote_url)
        # Hostname must be exactly github.com — a substring match would accept
        # attacker-suffixed hosts like github.com.evil.example.
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")

    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    owner, repo = parts[0], parts[1]
    # Interpolated verbatim into the api.github.com request path — reject
    # anything outside GitHub's name grammar so a crafted remote cannot smuggle
    # a path traversal or query into the authenticated request.
    if not _REPO_SEGMENT.fullmatch(owner) or not _REPO_SEGMENT.fullmatch(repo):
        return None
    return f"{owner}/{repo}"


def _normalise_prs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    prs: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # GitHub REST uses `draft`; `gh pr list --json` uses `isDraft`.
        normalised = dict(item)
        if "draft" not in normalised and "isDraft" in normalised:
            normalised["draft"] = bool(normalised.get("isDraft"))
        elif "draft" in normalised:
            normalised["draft"] = bool(normalised.get("draft"))
        prs.append(normalised)
    return prs


def _github_api_open_prs(slug: str, branch: str, token: str) -> list[dict[str, Any]]:
    owner, _repo = slug.split("/", 1)
    query = parse.urlencode(
        {"head": f"{owner}:{branch}", "state": "open", "per_page": "20"}
    )
    api_request = request.Request(
        f"https://api.github.com/repos/{slug}/pulls?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "session-unfinished-work-check",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with request.urlopen(api_request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # nosec B310 — hardcoded GitHub API URL
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise LookupError(f"GitHub API returned HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LookupError("GitHub API PR lookup failed") from exc
    return _normalise_prs(payload)


def _gh_cli_open_prs(branch: str) -> list[dict[str, Any]]:
    try:
        result = _run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number,url,title,isDraft",
            ],
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise LookupError("gh returned invalid JSON") from exc
    return _normalise_prs(payload)


def _open_prs_for_head(branch: str) -> list[dict[str, Any]]:
    slug = _repo_slug()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    api_error: LookupError | None = None

    if slug and token:
        try:
            return _github_api_open_prs(slug, branch, token)
        except LookupError as exc:
            api_error = exc

    try:
        return _gh_cli_open_prs(branch)
    except LookupError as exc:
        if api_error is not None:
            raise LookupError(
                "unable to list open PRs for current branch"
            ) from api_error
        raise LookupError("unable to list open PRs for current branch") from exc


WAITING_TASK_STATUSES = {
    "waiting_on_ci",
    "watching",
    "waiting",
    "ci-pending",
    "merge-pending",
}


def _load_active_task() -> dict[str, Any] | None:
    if not ACTIVE_TASK_PATH.exists():
        return None
    try:
        task = json.loads(ACTIVE_TASK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"_error": f"{ACTIVE_TASK_PATH} is not valid JSON: {exc.msg}"}
    except OSError as exc:
        raise LookupError(f"unable to read {ACTIVE_TASK_PATH}") from exc
    if not isinstance(task, dict):
        return {"_error": f"{ACTIVE_TASK_PATH} does not contain a JSON object"}
    return task


def _active_task_gaps(branch: str) -> list[str]:
    task = _load_active_task()
    if task is None:
        return []
    if task.get("_error"):
        return [str(task["_error"])]

    task_branch = (
        task.get("branch") or task.get("git_branch") or task.get("current_branch")
    )
    status = str(task.get("status", "")).strip().lower()

    # Resume hint for CI waits — branch may differ (cross-repo watch); always surface.
    if status in WAITING_TASK_STATUSES:
        pr = task.get("pr") or task.get("pr_url") or task.get("pr_number") or "?"
        return [
            f"active-task.json status={status!r} — RESUME CI wait for PR {pr} "
            "(do not start a second actor on this PR; use /watch-pr, never "
            "main-thread sleep>=30; unrelated eligible leftover-Act items may continue)"
        ]

    if task_branch and task_branch != branch:
        return []

    if status in TERMINAL_TASK_STATUSES:
        return []

    if task.get("pr") or task.get("pr_url") or task.get("pr_number"):
        return []

    return [
        "active-task.json is still open on this branch and has no "
        "pr/pr_url/pr_number recorded"
    ]


def assess(*, skip_remote: bool = False) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    branch = _current_branch()
    base = _default_branch()

    assessment: dict[str, Any] = {
        "ok": True,
        "branch": branch,
        "base": base,
        "unique_commits": 0,
        "remote_present": False,
        "open_prs": [],
        "findings": [],
        "checked_at": checked_at,
    }

    early_gaps = _active_task_gaps(branch)
    wait_hints = [f for f in early_gaps if "RESUME CI wait" in f]
    if wait_hints:
        assessment["findings"] = wait_hints
        assessment["ok"] = False
        return assessment
    # Non-wait task gaps (open task, no PR) still apply on the early-return
    # paths below — a zero-commit branch with an open task is not "done".
    task_gaps = [f for f in early_gaps if "RESUME CI wait" not in f]

    if branch in {"main", "master", base}:
        if task_gaps:
            assessment["findings"] = task_gaps
            assessment["ok"] = False
        return assessment

    unique_commits = _unique_commit_count(base)
    assessment["unique_commits"] = unique_commits
    if unique_commits <= 0:
        if task_gaps:
            assessment["findings"] = task_gaps
            assessment["ok"] = False
        return assessment

    findings: list[str] = []
    remote_present = False if skip_remote else _remote_has_branch(branch)
    assessment["remote_present"] = remote_present

    open_prs: list[dict[str, Any]] = []
    if remote_present:
        open_prs = _open_prs_for_head(branch)
        assessment["open_prs"] = open_prs
        if not open_prs:
            findings.append(
                "pushed feature branch has unique commits and no open PR; open "
                "a PR via ManagePullRequest create_pr before ending turn "
                "(draft OK during WIP; mark ready after the quality gate "
                "when no further pushes are expected)"
            )
        else:
            # Draft-only PRs stall Codex monitor + auto-merge until ready_for_review.
            draft_only = all(bool(pr.get("draft")) for pr in open_prs)
            if draft_only:
                findings.append(
                    "open PR is still draft; when the change is complete and no "
                    "further pushes are expected, mark ready through "
                    "ManagePullRequest ready-for-review after the quality gate "
                    "so Codex monitor + auto-merge can run"
                )

    findings.extend(task_gaps)
    assessment["findings"] = findings
    assessment["ok"] = not findings
    return assessment


def render_markdown(assessment: dict[str, Any]) -> str:
    lines = [
        "# Unfinished work check",
        "",
        f"- Checked at: {assessment.get('checked_at', '')}",
        f"- Branch: {assessment.get('branch', '')}",
        f"- Base: {assessment.get('base', '')}",
        f"- Unique commits: {assessment.get('unique_commits', 0)}",
        f"- Remote branch present: {assessment.get('remote_present', False)}",
        f"- Open PRs: {len(assessment.get('open_prs') or [])}",
        "",
        "## Findings",
        "",
    ]

    findings = assessment.get("findings") or []
    if assessment.get("ok"):
        lines.append("- No unfinished work detected.")
        return "\n".join(lines) + "\n"

    lines.extend(f"- {finding}" for finding in findings)
    lines.extend(
        [
            "",
            "## Required close-out",
            "",
            "- Open a PR via ManagePullRequest create_pr before ending turn "
            + "(draft OK during WIP), or record the existing PR in active-task.json.",
            "- When the change is complete, the quality gate passes, and no "
            + "further pushes are expected, mark the PR ready through "
            + "ManagePullRequest ready-for-review so Codex monitor + auto-merge "
            + "can run. Do not mark ready mid-iteration.",
            "- Re-run this check and confirm it reports ok before ending the session.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_report(path: Path, assessment: dict[str, Any]) -> None:
    if assessment.get("ok"):
        if path.exists():
            path.unlink()
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(assessment), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument(
        "--write",
        nargs="?",
        const=str(DEFAULT_WRITE_PATH),
        default=None,
        help=f"write advisory markdown, default {DEFAULT_WRITE_PATH}",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when unfinished work is detected",
    )
    parser.add_argument(
        "--skip-remote",
        action="store_true",
        help="skip remote branch and PR lookups",
    )
    args = parser.parse_args(argv)

    try:
        assessment = assess(skip_remote=args.skip_remote)
        if args.write is not None:
            _write_report(Path(args.write), assessment)
    except (LookupError, OSError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"Lookup error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(assessment, indent=2, sort_keys=True))
    else:
        print(render_markdown(assessment), end="")

    if args.strict and not assessment.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
