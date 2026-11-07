#!/usr/bin/env python3
"""Merge a PR only when every merge precondition holds at its exact head.

For agents that merge interactively (REST, an MCP tool, a session script) rather
than through a repository's auto-merge workflow, which has its own gate. It
refuses unless, at the PR's current head:

  1. the PR is open, not a draft, and still at the head the caller reviewed
     (--head, when given);
  2. GitHub reports mergeable_state "clean" (or "has_hooks"). "blocked" and
     "unstable" are refused with the reason: a blocked PR merged with an admin
     token silently bypasses a rule;
  3. every required status check of the base branch (rulesets and classic
     protection) has a latest run that succeeded, was skipped or neutral;
  4. no review thread is unresolved anywhere on the PR (a thread opened on an
     earlier commit still counts);
  5. the Codex advisory review bound to this head has no [P0]/[P1] finding, and
     any [P2]/[P3] finding is acknowledged with --accept-findings, so reading the
     review is a precondition rather than a habit. Head binding and the trusted
     author follow the pack's codex-pr-review comment format.

It also lists the path guards the PR trips (.ai/guards.json at the base, matched
as the check-guarded-paths action matches them) and whether each is acknowledged
by a guard-ok:<id> label or a Guarded-Path: <id> commit trailer. Into a
promotion base (main, prod, release/*) an unacknowledged guard is refused:
review the guarded diff, then label. Nothing here applies labels.

Usage:
  GH_TOKEN=... scripts/agent-merge.py owner/repo 123 [--head SHA] [--check]
      [--merge-method merge|squash|rebase] [--accept-findings "why the P2s are fine"]
      [--allow-unstable]

--check runs every gate and prints the verdict without merging. Exit 0 = merged
(or would merge under --check), 1 = refused, 2 = usage or GitHub error. It never
approves, rejects or cancels a deployment; production environment approvals stay
with their owners.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
OK_CONCLUSIONS = {"success", "skipped", "neutral"}
MERGEABLE_OK = {"clean", "has_hooks"}
CODEX_MARKER = "<!-- codex-pr-review:v1 -->"
CODEX_FOOTER = re.compile(r"<sub>head `([0-9a-f]{10,40})`</sub>\s*$", re.I)
GITHUB_ACTIONS_BOT_ID = 41898282
BLOCKING = re.compile(r"\[P[01]\]")
FINDING = re.compile(r"\[P[0-3]\]")
TRAILER = re.compile(r"^Guarded-Path:\s*([a-z0-9][a-z0-9-]{0,63})\s*$", re.M)
PROMOTION_BASES = re.compile(r"^(main|prod|release/.+)$")


class Refused(Exception):
    """A merge precondition does not hold."""


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, body=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        req = urllib.request.Request(
            API + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            detail = error.read().decode(errors="replace")[:300]
            raise RuntimeError(f"GitHub {method} {path}: HTTP {error.code} {detail}") from None

    def get(self, path):
        return self.request("GET", path)

    def paged(self, path, key=None):
        items, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            data = self.get(f"{path}{sep}per_page=100&page={page}") or ([] if key is None else {})
            batch = data if key is None else data.get(key, [])
            items += batch
            if len(batch) < 100:
                return items
            page += 1


# ---- pure decision helpers (unit-tested) ------------------------------------

def codex_review_at(comments: list, head: str):
    """Newest trusted Codex advisory comment whose footer binds this head."""
    found = []
    for c in comments:
        user = c.get("user") or {}
        body = c.get("body") or ""
        if (user.get("id") != GITHUB_ACTIONS_BOT_ID or user.get("type") != "Bot"
                or not body.startswith(CODEX_MARKER)):
            continue
        m = CODEX_FOOTER.search(body)
        if m and head.lower().startswith(m.group(1).lower()):
            found.append(c)
    return max(found, key=lambda c: (str(c.get("updated_at") or ""), int(c.get("id") or 0)), default=None)


def codex_findings(body: str) -> list[str]:
    """One line per finding: the bullet that carries a [Pn] marker (deduplicated)."""
    seen, out = set(), []
    for line in body.splitlines():
        if FINDING.search(line):
            text = line.strip().lstrip("-* ").replace("​", "")
            if text not in seen:
                seen.add(text)
                out.append(text)
    return out


def latest_runs(check_runs: list) -> dict:
    """Latest check run per name (highest id wins; reruns get new ids)."""
    latest = {}
    for run in check_runs:
        name = run.get("name")
        if name and (name not in latest or run.get("id", 0) > latest[name].get("id", 0)):
            latest[name] = run
    return latest


def required_problems(required: set, runs: dict, statuses: dict) -> list[str]:
    problems = []
    for ctx in sorted(required):
        run = runs.get(ctx)
        if run is not None:
            if run.get("status") != "completed":
                problems.append(f"required check '{ctx}' is {run.get('status')}")
            elif run.get("conclusion") not in OK_CONCLUSIONS:
                problems.append(f"required check '{ctx}' concluded {run.get('conclusion')}")
        elif ctx in statuses:
            if statuses[ctx] != "success":
                problems.append(f"required status '{ctx}' is {statuses[ctx]}")
        else:
            problems.append(f"required check '{ctx}' has not reported on this head")
    return problems


def glob_match(path: str, pattern: str) -> bool:
    """Same matcher as the pack's check-guarded-paths (fnmatch: * also crosses /),
    so this preflight and the `guards` check agree."""
    return fnmatch.fnmatch(path, pattern)


def tripped_guards(policy: dict, files: list[str]) -> dict:
    """Blocking guards whose paths the PR touches -> the matching files."""
    hit = {}
    for guard in (policy or {}).get("guards", []):
        if guard.get("mode", "block") != "block":
            continue
        matched = [f for f in files if any(glob_match(f, p) for p in guard.get("paths", []))]
        if matched:
            hit[guard["id"]] = matched
    return hit


def trailer_block(message: str) -> str:
    """The final paragraph, if every line is a `Key: value` trailer (git's rule,
    approximately: the pack check parses with git interpret-trailers)."""
    paragraphs = [p for p in message.strip().split("\n\n") if p.strip()]
    if len(paragraphs) < 2:
        return ""
    last = paragraphs[-1].strip().splitlines()
    return "\n".join(last) if all(re.match(r"^[A-Za-z0-9-]+:\s", line) for line in last) else ""


def acknowledged_guards(labels: list[str], commit_messages: list[str]) -> set:
    ack = {lab.split(":", 1)[1] for lab in labels if lab.startswith("guard-ok:")}
    for message in commit_messages:
        ack |= set(TRAILER.findall(trailer_block(message)))
    return ack


# ---- gates -------------------------------------------------------------------

def mergeable_state(gh: GitHub, repo: str, number: int) -> dict:
    for _ in range(6):
        pr = gh.get(f"/repos/{repo}/pulls/{number}")
        if pr is None:
            raise RuntimeError(f"{repo}#{number} not found")
        if pr.get("mergeable_state") not in (None, "unknown"):
            return pr
        time.sleep(5)
    return pr


def required_contexts(gh: GitHub, repo: str, base: str) -> set:
    ctx = set()
    for rule in gh.get(f"/repos/{repo}/rules/branches/{base}") or []:
        if rule.get("type") == "required_status_checks":
            ctx |= {c["context"] for c in rule["parameters"]["required_status_checks"]}
    try:
        classic = gh.get(f"/repos/{repo}/branches/{base}/protection/required_status_checks")
    except RuntimeError:
        classic = None  # no admin read of classic protection; rulesets still apply
    ctx |= set((classic or {}).get("contexts") or [])
    return ctx


def thread_resolved(thread: dict) -> bool:
    """The REST route reports `resolved`; GraphQL `isResolved`. Unknown shape = unresolved."""
    for key in ("resolved", "isResolved", "is_resolved"):
        if key in thread:
            return bool(thread[key])
    return False


def unresolved_threads(gh: GitHub, repo: str, number: int) -> int:
    # Claude Code web sessions: GraphQL is not forwarded, a REST route is.
    try:
        threads = gh.get(f"/repos/{repo}/pulls/{number}/ccr/review_threads")
    except RuntimeError:
        threads = None
    if isinstance(threads, list):
        return sum(1 for t in threads if not thread_resolved(t))
    owner, name = repo.split("/")
    query = ("query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p)"
             "{reviewThreads(first:100){nodes{isResolved}}}}}")
    data = gh.request("POST", "/graphql", {"query": query, "variables": {"o": owner, "n": name, "p": number}})
    nodes = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest", {}) \
        .get("reviewThreads", {}).get("nodes")
    if nodes is None:
        raise RuntimeError("could not read review threads (neither REST route nor GraphQL answered)")
    return sum(1 for n in nodes if not n.get("isResolved"))


def evaluate(gh: GitHub, repo: str, number: int, expected_head: str | None,
             accept_findings: str | None, allow_unstable: bool) -> tuple[dict, list[str], list[str]]:
    """Return (pr, refusals, notes)."""
    refusals, notes = [], []
    pr = mergeable_state(gh, repo, number)
    head, base = pr["head"]["sha"], pr["base"]["ref"]
    notes.append(f"{repo}#{number}: {pr['head']['ref']} -> {base} at {head[:10]}")
    if pr.get("state") != "open" or pr.get("merged"):
        raise Refused(f"PR is {pr.get('state')}{' (merged)' if pr.get('merged') else ''}")
    if pr.get("draft"):
        refusals.append("PR is a draft")
    if expected_head and not head.startswith(expected_head.lower()):
        raise Refused(f"head moved: PR is at {head[:10]}, you reviewed {expected_head[:10]}")

    # Required checks at the exact head.
    runs = latest_runs(gh.paged(f"/repos/{repo}/commits/{head}/check-runs", key="check_runs"))
    statuses = {s["context"]: s["state"] for s in
                (gh.get(f"/repos/{repo}/commits/{head}/status") or {}).get("statuses", [])}
    required = required_contexts(gh, repo, base)
    refusals += required_problems(required, runs, statuses)
    other_red = sorted(n for n, r in runs.items() if n not in required and r.get("status") == "completed"
                       and r.get("conclusion") not in OK_CONCLUSIONS)
    pending = sorted(n for n, r in runs.items() if r.get("status") != "completed")
    if pending:
        refusals.append("checks still running on this head: " + ", ".join(pending))

    # Mergeable state, explained.
    state = pr.get("mergeable_state")
    if state not in MERGEABLE_OK:
        if state == "unstable" and allow_unstable and not pending:
            notes.append("unstable accepted (--allow-unstable): non-required red checks: " + ", ".join(other_red))
        else:
            detail = f"; non-required red checks: {', '.join(other_red)}" if other_red else ""
            refusals.append(f"mergeable_state is '{state}'{detail}")

    # Review threads, PR-wide.
    unresolved = unresolved_threads(gh, repo, number)
    if unresolved:
        refusals.append(f"{unresolved} unresolved review thread(s)")

    # Codex advisory review at this head.
    comments = gh.paged(f"/repos/{repo}/issues/{number}/comments")
    review = codex_review_at(comments, head)
    codex_running = [n for n, r in runs.items() if "codex" in n.lower() and r.get("status") != "completed"]
    if review is None:
        if codex_running:
            refusals.append("Codex advisory review for this head is still running")
        else:
            notes.append("no Codex advisory review for this head (advisory; not required)")
    else:
        findings = codex_findings(review["body"])
        if any(BLOCKING.search(f) for f in findings):
            refusals.append("Codex advisory review has P0/P1 findings at this head")
        elif findings and not accept_findings:
            refusals.append("Codex advisory review has findings at this head; read them and pass "
                            "--accept-findings \"<why they do not block>\"")
        for f in findings:
            notes.append("codex: " + f[:300])
        if findings and accept_findings:
            notes.append("findings accepted: " + accept_findings)

    # Path guards (acknowledgement is required only where they block: main/prod/release).
    policy_file = gh.get(f"/repos/{repo}/contents/.ai/guards.json?ref={base}")
    if policy_file and policy_file.get("content"):
        policy = json.loads(base64.b64decode(policy_file["content"]))
        files = [f["filename"] for f in gh.paged(f"/repos/{repo}/pulls/{number}/files")]
        tripped = tripped_guards(policy, files)
        if tripped:
            commits = gh.paged(f"/repos/{repo}/pulls/{number}/commits")
            ack = acknowledged_guards([lab["name"] for lab in pr.get("labels", [])],
                                      [c["commit"]["message"] for c in commits])
            for gid, matched in sorted(tripped.items()):
                state_txt = "acknowledged" if gid in ack else "NOT acknowledged"
                notes.append(f"guard '{gid}' ({state_txt}): {', '.join(matched[:5])}"
                             + (" ..." if len(matched) > 5 else ""))
                if gid not in ack and PROMOTION_BASES.match(base):
                    refusals.append(f"guard '{gid}' tripped into {base}: review that diff, then add "
                                    f"label guard-ok:{gid}")
    return pr, refusals, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", help="owner/name")
    ap.add_argument("number", type=int)
    ap.add_argument("--head", help="the head SHA (or prefix) you reviewed; refuse if the PR moved")
    ap.add_argument("--check", action="store_true", help="evaluate only; do not merge")
    ap.add_argument("--merge-method", default="merge", choices=["merge", "squash", "rebase"])
    ap.add_argument("--accept-findings", help="why the Codex P2/P3 findings at this head do not block")
    ap.add_argument("--allow-unstable", action="store_true",
                    help="accept 'unstable' (only non-required checks red) after reading them")
    args = ap.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN is not set", file=sys.stderr)
        return 2
    gh = GitHub(token)
    try:
        pr, refusals, notes = evaluate(gh, args.repo, args.number, args.head,
                                       args.accept_findings, args.allow_unstable)
    except Refused as why:
        print(f"REFUSED: {why}")
        return 1
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    for note in notes:
        print("  " + note)
    if refusals:
        print("REFUSED:")
        for r in refusals:
            print("  - " + r)
        return 1
    head = pr["head"]["sha"]
    if args.check:
        print(f"OK: would merge at {head[:10]}")
        return 0
    try:
        result = gh.request("PUT", f"/repos/{args.repo}/pulls/{args.number}/merge",
                            {"sha": head, "merge_method": args.merge_method})
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if not (result or {}).get("merged"):
        print(f"ERROR: GitHub did not merge: {result}", file=sys.stderr)
        return 2
    print(f"MERGED {args.repo}#{args.number} at {head[:10]} -> {result.get('sha')}")
    if PROMOTION_BASES.match(pr["base"]["ref"]):
        print("  a production deploy may now wait for an owner's environment approval; report it, never approve it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
