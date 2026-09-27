#!/usr/bin/env python3
"""gh-ci.py — compact GitHub CI reads for agents (REST, stdlib only).

Why this exists: the GitHub MCP tools return the whole REST payload and have no
field selector. `actions_list list_workflow_runs` ignores `per_page` and returns
~30 runs (~49k chars, measured 2026-09-01), and `get_job_logs tail_lines=40`
returns only the post-job git cleanup. This script prints one line per item and
strips the noise, so the agent reads 1-2k chars instead of 50-100k.

Works on every agent surface (Claude Code Web, Codex Web, Cursor Web): needs only
python3. Auth: GH_TOKEN / GH_AUTOMATION_TOKEN / GITHUB_TOKEN, else an
api.github.com entry in ~/.netrc, else anonymous (the Claude Code Web proxy
injects GitHub credentials itself, verified 2026-09-01). When the
session proxy blocks api.github.com it prints a single `GH_CI_BLOCKED:` line and
exits 3 — fall back to the `mcp__github__*` tools, do not retry.

Usage (repo defaults to the `origin` remote of the current directory):
  gh-ci.py checks  [--repo o/r] <pr-number|sha>           check runs + statuses on a PR head
  gh-ci.py runs    [--repo o/r] [--workflow ci.yml] [--branch b] [--event e] [-n 10]
  gh-ci.py jobs    [--repo o/r] <run-id>                   jobs + current/last completed step
  gh-ci.py log     [--repo o/r] <job-id> [--tail 150] [--keep-post-job] [--grep RE]
  gh-ci.py failed  [--repo o/r] <run-id> [--tail 60]       log tail of every failed job in a run
Add --json to any command for machine-readable output.

Exit codes: 0 ok · 1 usage/HTTP error · 2 unauthorized (401) · 3 proxy-blocked.
"""

from __future__ import annotations

import argparse
import json
import netrc
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"

# Redirect targets a CI-log fetch may legitimately land on: the Actions
# results host and the Azure blob store backing log/artifact downloads.
# Deliberately narrow — generic *.github.com / *.githubusercontent.com
# subdomains serve user-controlled content that must not be trusted as CI
# output; same-host api.github.com redirects (repo renames) are allowed
# separately below.
_REDIRECT_HOST_SUFFIXES = (
    ".actions.githubusercontent.com",
    ".blob.core.windows.net",
)
USER_AGENT = "gh-ci.py (ai-starter-pack)"
CA_BUNDLE_CANDIDATES = (
    os.environ.get("SSL_CERT_FILE"),
    os.environ.get("REQUESTS_CA_BUNDLE"),
    "/root/.ccr/ca-bundle.crt",
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ?")
POST_JOB_MARKERS = ("Post job cleanup.", "##[group]Post ", "Cleaning up orphan processes")
BLOCK_HINTS = (
    "not permitted through this proxy",
    "proxy",
    "GraphQL proxying",
    "GitHub access is not enabled for this session",
)


class Blocked(Exception):
    """api.github.com is unreachable through the session proxy."""


class Unauthorized(Exception):
    """GitHub answered 401: no usable credential (env, ~/.netrc, or proxy-injected)."""


class NotFound(Exception):
    """GitHub answered 404 for a requested resource."""


# --------------------------------------------------------------------------- auth

def find_token() -> tuple[str | None, str]:
    for name in ("GH_TOKEN", "GH_AUTOMATION_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"env:{name}"
    try:
        auth = netrc.netrc().authenticators("api.github.com") or netrc.netrc().authenticators(
            "github.com"
        )
    except (FileNotFoundError, netrc.NetrcParseError):
        auth = None
    if auth and auth[2]:
        return auth[2], "netrc"
    return None, "none"


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for path in CA_BUNDLE_CANDIDATES:
        if path and os.path.exists(path):
            try:
                ctx.load_verify_locations(path)
            except ssl.SSLError as e:
                # A present-but-unloadable bundle is a configuration error, not a
                # proxy block; say so instead of letting the request fail later
                # and be misreported as GH_CI_BLOCKED.
                raise SystemExit(f"gh-ci: CA_BUNDLE_ERROR — cannot load {path}: {e}") from None
    return ctx


# --------------------------------------------------------------------------- http

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_context()), _NoRedirect()
    )


def api_get(path: str, token: str | None, params: dict | None = None, raw: bool = False):
    url = path if path.startswith("http") else API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        try:
            with _opener().open(req, timeout=30) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            redirect = e.headers.get("Location") if e.code in (301, 302, 303, 307) else None
            if not redirect:
                raise
            # Follow the redirect only over https — never a file:/ or custom
            # scheme GitHub could not legitimately hand back (bandit B310).
            target = urllib.parse.urlsplit(redirect)
            if target.scheme != "https":
                # Never echo the redirect URL itself: blob-storage links carry a
                # signed token in the query string.
                raise SystemExit(
                    f"gh-ci: refusing non-https redirect (scheme={target.scheme!r}, host={target.netloc!r})"
                )
            # Bound the redirect target: GitHub hands back same-host API links
            # (repo renames) and — only on Actions endpoints — log/artifact
            # downloads on its own properties or the Azure blob store Actions
            # uses. Anything else is not a destination this tool should fetch —
            # a compromised or confused endpoint must not be able to send the
            # agent after arbitrary hosts, and blob content only substitutes
            # real CI output on the paths that return it.
            target_host = (target.hostname or "").lower()
            cross_host_ok = "/actions/" in url and any(
                target_host.endswith(s) for s in _REDIRECT_HOST_SUFFIXES
            )
            if not (
                target_host == urllib.parse.urlsplit(API).hostname
                or cross_host_ok
            ):
                raise SystemExit(
                    f"gh-ci: refusing redirect to non-GitHub host {target.netloc!r} (from {url})"
                )
            # A same-host API redirect (renamed/transferred repo) still needs the
            # token; a cross-host one (job logs → blob storage) rejects a
            # forwarded Authorization header with 401, so send it only to
            # api.github.com itself.
            headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
            if token and target.netloc == urllib.parse.urlsplit(API).netloc:
                headers["Authorization"] = f"Bearer {token}"
            with _opener().open(urllib.request.Request(redirect, headers=headers), timeout=60) as resp:
                body = resp.read()
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:300]
        if e.code in (403, 405, 407) and any(h.lower() in text.lower() for h in BLOCK_HINTS):
            raise Blocked(f"HTTP {e.code} from proxy on {url}: {text.strip()}") from None
        if e.code == 407:
            raise Blocked(f"HTTP 407 proxy auth required on {url}") from None
        if e.code == 401:
            raise Unauthorized(f"HTTP 401 on {url} (token source: {'none' if not token else 'present'})") from None
        if e.code == 404:
            raise NotFound(f"HTTP 404 on {url}") from None
        raise SystemExit(f"gh-ci: HTTP {e.code} on {url}: {text.strip()}")
    except urllib.error.URLError as e:
        raise Blocked(f"network error on {url}: {e.reason}") from None
    if raw:
        return body.decode("utf-8", "replace")
    return json.loads(body.decode("utf-8"))


def api_get_all(path: str, token: str | None, key: str, params: dict | None = None, page_size: int = 100) -> list:
    """Walk every page of a list endpoint whose items live under `key`.

    Pass key="__list__" for endpoints that return a bare JSON array.
    """
    items: list = []
    page = 1
    while True:
        data = api_get(path, token, {**(params or {}), "per_page": page_size, "page": page})
        batch = data if key == "__list__" else data.get(key, [])
        items.extend(batch)
        if len(batch) < page_size:
            return items
        page += 1


# --------------------------------------------------------------------------- repo

def detect_repo(explicit: str | None) -> str:
    """--repo, else the current checkout's origin, else $GITHUB_REPOSITORY.

    Origin wins over the env var: in a multi-repo session GITHUB_REPOSITORY
    names the launch repo while the agent may be in a sibling checkout.
    """
    if explicit:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", explicit):
            raise SystemExit(f"invalid --repo value: {explicit!r} (expected owner/name)")
        return explicit
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        url = ""
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env_repo:
        return env_repo
    raise SystemExit("gh-ci: pass --repo owner/name (no GitHub origin in cwd and GITHUB_REPOSITORY unset)")


# --------------------------------------------------------------------------- commands

def resolve_sha(repo: str, ref: str, token: str | None) -> str:
    if re.fullmatch(r"[0-9a-f]{7,40}", ref):
        return ref
    if ref.isdigit():
        pr = api_get(f"/repos/{repo}/pulls/{ref}", token)
        return pr["head"]["sha"]
    commit = api_get(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}", token)
    return commit["sha"]


def cmd_checks(args, token):
    sha = resolve_sha(args.repo, args.ref, token)
    check_runs = api_get_all(f"/repos/{args.repo}/commits/{sha}/check-runs", token, "check_runs")
    # Legacy commit statuses: the combined endpoint gives the overall state,
    # the list endpoint gives every context (paginated, so none is missed).
    combined_state = api_get(f"/repos/{args.repo}/commits/{sha}/status", token).get("state")
    # The list endpoint returns the full history per context, newest first;
    # only the latest event per context counts (that is what the combined
    # state is computed from), so a retried context that went failure →
    # success must not keep its stale failure in the verdict.
    seen_contexts: set[str] = set()
    legacy_statuses = [
        st
        for st in api_get_all(f"/repos/{args.repo}/commits/{sha}/statuses", token, "__list__")
        if not (st["context"] in seen_contexts or seen_contexts.add(st["context"]))
    ]
    # Check runs likewise get one entry per attempt: a rerun creates a new
    # run, so collapse to the latest (highest id) per (name, app) — a retried
    # failure gone green must not keep the verdict red.
    latest_runs: dict[tuple[str, str | None], dict] = {}
    for cr in check_runs:
        run_key = (cr["name"], (cr.get("app") or {}).get("slug"))
        if run_key not in latest_runs or cr["id"] > latest_runs[run_key]["id"]:
            latest_runs[run_key] = cr
    rows = []
    for cr in latest_runs.values():
        rows.append(
            {
                "name": cr["name"],
                "status": cr["status"],
                "conclusion": cr.get("conclusion"),
                "app": (cr.get("app") or {}).get("slug"),
                "id": cr["id"],
                "url": cr.get("html_url"),
            }
        )
    for st in legacy_statuses:
        rows.append(
            {
                "name": st["context"],
                "status": "status",
                "conclusion": st["state"],
                "app": None,
                "id": st.get("id"),
                "url": st.get("target_url"),
            }
        )
    # Split "still running" from "actually failed": a queued/in-progress check
    # run (conclusion None) or a pending legacy status is not a failure, it is
    # a reason to wait — report it as PENDING, still green=false.
    pending_rows = [r["name"] for r in rows if r["conclusion"] in (None, "pending")]
    not_success = [
        r["name"]
        for r in rows
        if r["conclusion"] not in ("success", "skipped", "neutral", None, "pending")
    ]
    combined = combined_state
    # Green requires evidence: at least one check and nothing non-green. The
    # legacy combined state is only meaningful when legacy statuses exist —
    # GitHub reports `pending` for an EMPTY status set, so honouring it
    # unconditionally would leave check-run-only repos non-green forever. A
    # combined `failure`/`error` with statuses present always wins (covers a
    # failing context the list somehow missed). An empty set means CI has not
    # started (or the SHA is wrong) — never report that as safe.
    if not rows:
        verdict = "NO CHECKS YET"
    elif not_success:
        verdict = "NOT GREEN"
    elif legacy_statuses and combined in ("failure", "error"):
        verdict = "NOT GREEN (combined status)"
        not_success = [f"combined_status={combined}"]
    elif pending_rows:
        verdict = "PENDING"
    elif legacy_statuses and combined == "pending":
        verdict = "PENDING (combined status)"
    else:
        verdict = "ALL GREEN"
    summary = {
        "sha": sha,
        "combined_status": combined,
        "total": len(rows),
        "not_success": not_success,
        "pending": pending_rows,
        "green": verdict == "ALL GREEN",
        "verdict": verdict,
    }
    if args.json:
        print(json.dumps({"summary": summary, "checks": rows}, indent=1))
        return
    print(f"head {sha[:10]}  combined_status={combined}  checks={summary['total']}")
    width = max((len(r["name"]) for r in rows), default=10)
    for r in sorted(rows, key=lambda r: (r["conclusion"] in ("success", "skipped", "neutral"), r["name"])):
        concl = r["conclusion"] or r["status"]
        extra = f"  [{r['app']}]" if r["app"] else ""
        print(f"  {r['name']:<{width}}  {concl:<14} id={r['id']}{extra}  {r['url'] or ''}")
    if not_success:
        print(f"{verdict} ({len(not_success)}): " + ", ".join(not_success))
    elif verdict == "PENDING":
        print(f"PENDING ({len(pending_rows)} still running): " + ", ".join(pending_rows))
    else:
        print(verdict)


def cmd_runs(args, token):
    path = f"/repos/{args.repo}/actions/runs"
    if args.workflow:
        path = f"/repos/{args.repo}/actions/workflows/{urllib.parse.quote(args.workflow, safe='')}/runs"
    params = {"branch": args.branch, "event": args.event, "status": args.status}
    # GitHub caps per_page at 100 regardless of what's requested, so -n above
    # that needs real pagination, not a bigger single ask.
    runs: list = []
    total_count = None
    page = 1
    page_size = min(100, args.n)
    while len(runs) < args.n:
        data = api_get(path, token, {**params, "per_page": page_size, "page": page})
        if total_count is None:
            total_count = data.get("total_count")
        batch = data.get("workflow_runs", [])
        runs.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    rows = [
        {
            "id": r["id"],
            "workflow": r["name"],
            "sha": r["head_sha"][:8],
            "branch": r["head_branch"],
            "event": r["event"],
            "status": r["status"],
            "conclusion": r.get("conclusion"),
            "created": r["created_at"],
            "url": r["html_url"],
        }
        for r in runs[: args.n]
    ]
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    print(f"{args.repo}  total_count={total_count}  showing={len(rows)}")
    for r in rows:
        concl = r["conclusion"] or r["status"]
        print(
            f"  {r['id']}  {r['sha']}  {r['event']:<20} {concl:<10} {r['workflow']} ({r['branch']})  {r['created']}  {r['url']}"
        )


def _jobs(repo, run_id, token):
    jobs = api_get_all(f"/repos/{repo}/actions/runs/{run_id}/jobs", token, "jobs")
    rows = []
    for j in jobs:
        steps = j.get("steps", [])
        failed_steps = [s["name"] for s in steps if s.get("conclusion") == "failure"]
        active_steps = [s["name"] for s in steps if s.get("status") == "in_progress"]
        completed_steps = [s["name"] for s in steps if s.get("status") == "completed"]
        rows.append(
            {
                "id": j["id"],
                "name": j["name"],
                "status": j["status"],
                "conclusion": j.get("conclusion"),
                "failed_steps": failed_steps,
                "active_step": active_steps[-1] if active_steps else None,
                "last_completed_step": completed_steps[-1] if completed_steps else None,
                "url": j.get("html_url"),
            }
        )
    return rows


def cmd_jobs(args, token):
    rows = _jobs(args.repo, args.run_id, token)
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    print(f"run {args.run_id}  jobs={len(rows)}")
    for j in rows:
        concl = j["conclusion"] or j["status"]
        steps = f"  failed_steps={j['failed_steps']}" if j["failed_steps"] else ""
        if j["active_step"]:
            steps += f"  active_step={j['active_step']}"
        elif j["last_completed_step"]:
            steps += f"  last_step={j['last_completed_step']}"
        print(f"  {j['id']}  {concl:<10} {j['name']}{steps}")


def clean_log(text: str, tail: int, keep_post_job: bool, grep: str | None) -> tuple[list[str], int]:
    lines = [ANSI_RE.sub("", TS_RE.sub("", ln)).rstrip() for ln in text.splitlines()]
    total = len(lines)
    if not keep_post_job:
        for i, ln in enumerate(lines):
            if any(ln.startswith(m) for m in POST_JOB_MARKERS):
                lines = lines[:i]
                break
    if grep:
        rx = re.compile(grep, re.IGNORECASE)
        lines = [ln for ln in lines if rx.search(ln)]
    return lines[-tail:] if tail > 0 else lines, total


def fetch_log(repo, job_id, token) -> str:
    return api_get(f"/repos/{repo}/actions/jobs/{job_id}/logs", token, raw=True)


def cmd_log(args, token):
    text = fetch_log(args.repo, args.job_id, token)
    lines, total = clean_log(text, args.tail, args.keep_post_job, args.grep)
    if args.json:
        print(json.dumps({"job_id": args.job_id, "total_lines": total, "lines": lines}))
        return
    print(f"job {args.job_id}  total_lines={total}  showing={len(lines)}" + ("  (post-job cleanup stripped)" if not args.keep_post_job else ""))
    print("\n".join(lines))


def cmd_failed(args, token):
    # A timed-out job is the one whose log you need most; treat it as failed.
    jobs = [
        j
        for j in _jobs(args.repo, args.run_id, token)
        if j["conclusion"] in ("failure", "timed_out", "startup_failure")
    ]
    if not jobs:
        print("[]" if args.json else f"run {args.run_id}: no failed jobs")
        return
    out = []
    for j in jobs:
        # startup_failure (e.g. runner never provisioned) often has no log
        # archive at all — a 404 there must not abort the whole command.
        try:
            text = fetch_log(args.repo, j["id"], token)
        except NotFound as e:
            if j["conclusion"] != "startup_failure":
                raise
            out.append({"job": j, "total_lines": 0, "lines": [], "log_error": str(e)})
            continue
        lines, total = clean_log(text, args.tail, False, None)
        out.append({"job": j, "total_lines": total, "lines": lines})
    if args.json:
        print(json.dumps(out, indent=1))
        return
    for item in out:
        j = item["job"]
        print(f"=== FAILED job {j['id']} {j['name']}  failed_steps={j['failed_steps']}  total_lines={item['total_lines']}")
        if item.get("log_error"):
            print(f"  (no log archive: {item['log_error']})")
            continue
        print("\n".join(item["lines"]))


# --------------------------------------------------------------------------- main

class _UsageErrorParser(argparse.ArgumentParser):
    """argparse defaults usage errors to exit(2); reserve 2 for HTTP 401 (see module docstring)."""

    def error(self, message):  # noqa: D102
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    # --repo / --json are accepted both before and after the subcommand, as the
    # usage text shows. SUPPRESS keeps a subparser from overwriting a value
    # given at the top level with its own default.
    common = _UsageErrorParser(add_help=False)
    common.add_argument("--repo", default=argparse.SUPPRESS, help="owner/name (default: origin remote of cwd, else $GITHUB_REPOSITORY)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    p = _UsageErrorParser(prog="gh-ci.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("checks", help="check runs + commit statuses on a PR head or sha", parents=[common])
    c.add_argument("ref", help="PR number, sha, or branch")
    c.set_defaults(fn=cmd_checks)

    r = sub.add_parser("runs", help="recent workflow runs, one line each", parents=[common])
    r.add_argument("--workflow", help="workflow file name or id, e.g. ci.yml")
    r.add_argument("--branch")
    r.add_argument("--event")
    r.add_argument("--status", help="queued|in_progress|completed|... (GitHub values)")
    r.add_argument("-n", type=int, default=10, help="max runs (default 10)")
    r.set_defaults(fn=cmd_runs)

    j = sub.add_parser("jobs", help="jobs of a run", parents=[common])
    j.add_argument("run_id", type=int)
    j.set_defaults(fn=cmd_jobs)

    lg = sub.add_parser("log", help="cleaned job log tail", parents=[common])
    lg.add_argument("job_id", type=int)
    lg.add_argument("--tail", type=int, default=150, help="lines to keep (default 150, 0 = all)")
    lg.add_argument("--keep-post-job", action="store_true", help="do not strip the post-job cleanup block")
    lg.add_argument("--grep", help="regex; keep only matching lines (before tail)")
    lg.set_defaults(fn=cmd_log)

    f = sub.add_parser("failed", help="cleaned log tail of every failed job in a run", parents=[common])
    f.add_argument("run_id", type=int)
    f.add_argument("--tail", type=int, default=60)
    f.set_defaults(fn=cmd_failed)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.repo = getattr(args, "repo", None)
    args.json = getattr(args, "json", False)
    token, source = find_token()
    # No token is not fatal: the Claude Code Web session proxy injects GitHub
    # credentials on api.github.com (verified 2026-09-01), so try anonymously
    # and only complain if GitHub answers 401.
    args.repo = detect_repo(args.repo)
    try:
        args.fn(args, token)
    except Unauthorized as e:
        print(
            f"gh-ci: NO_TOKEN — {e}\nSet GH_TOKEN / GH_AUTOMATION_TOKEN / GITHUB_TOKEN or add api.github.com to "
            "~/.netrc.",
            file=sys.stderr,
        )
        return 2
    except Blocked as e:
        print(
            f"GH_CI_BLOCKED: {e}\n"
            "api.github.com is not reachable from this session (token source: "
            f"{source}). Fall back to mcp__github__* (see docs/tool-traps.md); do not retry with other tokens.",
            file=sys.stderr,
        )
        return 3
    except NotFound as e:
        print(f"gh-ci: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
