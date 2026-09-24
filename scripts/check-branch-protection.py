#!/usr/bin/env python3
"""Branch-protection required-checks auditor.

Companion to check-deployment-contract.py: the contract declares WHICH
check-run names each protected branch must require; this script verifies the
live branch-protection settings via `gh api` (there is no local file to diff —
protection is repo admin state, not committed code).

Audited branches per repo = every declared lane branch + `protected_branches`
(non-lane branches like `develop` that still carry PR gates). Required names:
a lane's own `required_checks` OVERRIDES the repo-level list for its branch
(present-but-empty means 'audited, nothing required'); a lane without the key
and every `protected_branches` entry inherit the repo-level list. Overrides
exist because lane branches run whatever workflows were last promoted —
declaring a check a lane's workflows cannot produce would deadlock the
promotion PRs the lanes exist for.

A branch that returns 404 (unprotected) and carries declared required_checks
reports every name missing. A read error (403 — token lacks admin scope) is a
warning, not a finding: the audit degrades rather than fabricating drift.

Fix payloads: `--emit-fixes DIR` writes one ready-to-apply PUT body per
branch (`<owner>__<repo>__<branch>.json`) plus `apply-fixes.md` holding the
exact `gh api -X PUT repos/{r}/branches/{b}/protection --input <file>`
commands a human can run. Nothing is applied — flipping required checks can
deadlock merges when a check name is wrong, so a human reviews each payload.

Usage:
    python3 scripts/check-branch-protection.py config/deployment-contracts.yaml \
        [--emit-fixes DIR] [--summary FILE] [--warn-only] [--fixtures DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

# check-deployment-contract.py owns the contract parser + schema — load it by
# path (its module name is not importable due to the hyphen).
_CDC = Path(__file__).resolve().with_name("check-deployment-contract.py")
_spec = importlib.util.spec_from_file_location("check_deployment_contract", _CDC)
_cdc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cdc)
load_contract = _cdc.load_contract

SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
# Put-body surface GitHub accepts. Anything else in a GET response (urls,
# contexts_url, nested read-only fields) must be stripped or the PUT fails.
# required_signatures is NOT a PUT body field (it has its own endpoint) —
# including it gets the payload rejected.
PUT_BOOL_KEYS = ("required_linear_history", "allow_force_pushes",
                 "allow_deletions", "block_creations",
                 "required_conversation_resolution",
                 "lock_branch", "allow_fork_syncing")


def legacy_contexts(protection: dict) -> list[str]:
    """Required check-run names from a legacy GET protection response only,
    tolerating both API shapes (`checks[].context` and `contexts[]`).
    Does NOT include ruleset-derived contexts (see `_ruleset_contexts`)."""
    rsc = protection.get("required_status_checks") or {}
    if not isinstance(rsc, dict):
        return []
    checks = rsc.get("checks")
    if isinstance(checks, list):
        return [c["context"] for c in checks
                if isinstance(c, dict) and isinstance(c.get("context"), str)]
    ctxs = rsc.get("contexts")
    if isinstance(ctxs, list):
        return [c for c in ctxs if isinstance(c, str)]
    return []


def current_contexts(protection: dict) -> list[str]:
    """Effective required contexts: legacy protection UNION ruleset-required
    checks. A check satisfied only by a ruleset still counts as enforced —
    but fix_body must not copy it into a legacy PUT, so the two sources
    stay distinguishable via `_ruleset_contexts`."""
    seen = legacy_contexts(protection)
    for c in protection.get("_ruleset_contexts") or []:
        if c not in seen:
            seen.append(c)
    return seen


def _ruleset_contexts(repo: str, ref: str) -> tuple[list[str], bool, str | None]:
    """(contexts, ruleset_present, error) from repos/{repo}/rules/branches/{ref}.
    ruleset_present=True when the branch is governed by ANY ruleset — even one
    without a required-status-checks rule — since fix payloads need the
    'edit the ruleset, not legacy protection' caveat either way."""
    rules, rerr = _gh_json(f"repos/{repo}/rules/branches/{ref}?per_page=100")
    if rerr and rerr != "404":
        return [], False, rerr
    ctxs: list[str] = []
    present = isinstance(rules, list) and bool(rules)
    for rule in rules or []:
        if rule.get("type") == "required_status_checks":
            params = rule.get("parameters") or {}
            ctxs += [c["context"] for c in params.get("required_status_checks") or []
                     if isinstance(c, dict) and c.get("context")]
    return ctxs, present, None


def _gh_json(endpoint: str, timeout: int = 30) -> tuple[list | dict | None, str | None]:
    """gh api helper: (json_body, None) on success; (None, '404') on not-found;
    (None, <err>) otherwise."""
    try:
        out = subprocess.run(["gh", "api", endpoint],
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"gh api timed out ({timeout}s)"
    except OSError as exc:
        return None, f"gh not runnable: {exc}"
    if out.returncode != 0:
        err = out.stderr.strip()[:160]
        if "404" in err or "Not Found" in err:
            return None, "404"
        return None, err or f"exit {out.returncode}"
    try:
        return json.loads(out.stdout), None
    except ValueError as exc:
        return None, f"undecodable JSON: {exc}"


def fixture_name(repo: str, branch: str) -> str:
    """Filesystem-safe stem for fixture/fix-payload files — '/' and other
    non-[A-Za-z0-9_.-] chars in lane branches would otherwise descend into
    nonexistent subdirs. The sha1 suffix keeps colliding sanitizations
    (e.g. 'rc/a' vs 'rc_a') from sharing one payload file."""
    return (f"{repo.replace('/', '__')}__"
            f"{re.sub(r'[^A-Za-z0-9_.-]', '_', branch)}-"
            f"{hashlib.sha1(branch.encode()).hexdigest()[:8]}")


def fetch_protection(repo: str, branch: str,
                     args: argparse.Namespace) -> tuple[dict | None, str | None]:
    """Return (protection_json, error). protection_json=None means the branch
    is unprotected (no legacy protection AND no rulesets); error is set only
    for real fetch failures — including a branch that does not exist."""
    if getattr(args, "fixtures", None):
        stem = Path(args.fixtures) / fixture_name(repo, branch)
        body_path = Path(f"{stem}.json")
        if Path(f"{stem}.404").exists():
            return None, None
        if not body_path.is_file():
            return None, f"no fixture for {repo}@{branch} ({body_path})"
        try:
            return json.loads(body_path.read_text(encoding="utf-8")), None
        except (OSError, ValueError) as exc:
            return None, f"bad fixture {body_path}: {exc}"
    if not SLUG_RE.fullmatch(repo):
        return None, f"'{repo}' is not a valid owner/name slug"
    ref = urllib.parse.quote(branch, safe="")
    body, err = _gh_json(f"repos/{repo}/branches/{ref}/protection")
    if err and err != "404":
        return None, f"gh api protection read failed for {repo}@{branch}: {err}"
    # Legacy protection and rulesets can coexist — always check both, so a
    # check enforced only by a ruleset isn't falsely reported missing.
    rctx, rpresent, rerr = _ruleset_contexts(repo, ref)
    if err is None and isinstance(body, dict):
        body["_ruleset_contexts"] = rctx
        if rpresent:
            body["_ruleset_managed"] = True
        if rerr:  # ruleset read failed but legacy exists — warn, don't fail
            body["_ruleset_fetch_error"] = rerr
        return body, None
    # Legacy 404: check rulesets before calling the branch unprotected.
    # Also distinguishes nonexistent branches.
    if rerr:
        return None, f"gh api rulesets read failed for {repo}@{branch}: {rerr}"
    if rpresent:
        # Synthetic protection view: fix payloads are emitted but carry a
        # ruleset caveat (a legacy PUT cannot edit rulesets).
        return {"required_status_checks": {"strict": False,
                "checks": []},
                "_ruleset_managed": True,
                "_ruleset_contexts": rctx}, None
    exists, eerr = _gh_json(f"repos/{repo}/branches/{ref}")
    if eerr == "404" or (eerr is None and exists is None):
        # GitHub also 404s branches on repos the token cannot see — confirm
        # repo visibility before declaring the branch missing.
        _, gerr = _gh_json(f"repos/{repo}")
        if gerr:
            return None, (f"repo {repo} unreadable ({gerr}) — cannot verify "
                          f"branch {branch}")
        return None, f"branch {repo}@{branch} does not exist"
    if eerr:  # a read failure is unverifiable state, not "unprotected"
        return None, f"gh api branch read failed for {repo}@{branch}: {eerr}"
    return None, None  # exists but unprotected — a finding, not an error


def _enabled(node) -> bool:
    """A GET protection sub-object carries {enabled: bool, url} — the PUT body
    wants the bare boolean."""
    return bool(isinstance(node, dict) and node.get("enabled"))


def _name_list(node) -> dict | None:
    """Normalize GET-shaped users/teams/apps sub-objects to the PUT shape
    {users: [login], teams: [slug], apps: [slug]}."""
    if not isinstance(node, dict):
        return None
    return {
        "users": [u.get("login") for u in node.get("users") or []
                  if isinstance(u, dict) and u.get("login")],
        "teams": [t.get("slug") for t in node.get("teams") or []
                  if isinstance(t, dict) and t.get("slug")],
        "apps": [a.get("slug") for a in node.get("apps") or []
                 if isinstance(a, dict) and a.get("slug")],
    }


def fix_body(protection: dict | None, required: list[str]) -> dict:
    """Build a complete PUT /protection body: existing settings preserved,
    required_status_checks.checks merged with the missing contexts appended.

    protection=None (unprotected branch) yields a baseline body — 1 required
    approval + conversation resolution + enforce_admins, never a bare checks-
    only PUT that would leave merges review-free. The caller still warns the
    human to review the payload before applying it."""
    body: dict = {}
    if protection:
        existing = (protection.get("required_status_checks") or {}).get("checks") or []
        app_ids = {e.get("context"): e.get("app_id")
                   for e in existing if isinstance(e, dict)}
        # Base the PUT on LEGACY contexts only — ruleset-enforced contexts
        # counted toward the audit union must not be copied into legacy
        # protection (they belong to the ruleset). `required` here is the
        # contract list; append only what's absent from the union.
        union = set(current_contexts(protection))
        checks = []
        for c in legacy_contexts(protection):
            entry: dict = {"context": c}
            aid = app_ids.get(c)
            if isinstance(aid, int):  # GET may omit app_id — a null one 422s
                entry["app_id"] = aid
            checks.append(entry)
        checks += [{"context": c} for c in required if c not in union]
        rsc = protection.get("required_status_checks") or {}
        body["required_status_checks"] = {
            "strict": bool(rsc.get("strict")), "checks": checks}
        body["enforce_admins"] = _enabled(protection.get("enforce_admins"))
        rpr = protection.get("required_pull_request_reviews")
        if isinstance(rpr, dict):
            clean = {k: v for k, v in rpr.items() if k != "url"}
            # Nested users/teams/apps must be reduced to login/slug arrays or
            # the PUT is rejected 422.
            for k in ("dismissal_restrictions", "bypass_pull_request_allowances"):
                if isinstance(clean.get(k), dict):
                    clean[k] = _name_list(clean[k])
            body["required_pull_request_reviews"] = clean
        else:
            body["required_pull_request_reviews"] = None
        body["restrictions"] = _name_list(protection.get("restrictions"))
        for k in PUT_BOOL_KEYS:
            body[k] = _enabled(protection.get(k))
    else:
        body = {
            "required_status_checks": {
                "strict": False,
                "checks": [{"context": c} for c in required],
            },
            "enforce_admins": True,
            "required_pull_request_reviews": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": False,
                "require_last_push_approval": False,
            },
            "required_conversation_resolution": True,
            "restrictions": None,
        }
        for k in PUT_BOOL_KEYS:
            if k == "required_conversation_resolution":
                continue
            body[k] = False
    return body


def audited(repo_spec: dict) -> dict[str, list[str]]:
    """branch -> required check names (lane overrides repo-level)."""
    base = list(repo_spec.get("required_checks") or [])
    per_branch: dict[str, list[str] | None] = {}
    for b in repo_spec.get("protected_branches") or []:
        per_branch.setdefault(b, None)
    for lane in repo_spec.get("lanes") or []:
        b = lane.get("branch")
        if not isinstance(b, str) or not b:
            continue
        if "required_checks" in lane:
            per_branch[b] = list(lane.get("required_checks") or [])
        else:
            per_branch.setdefault(b, None)
    return {b: (list(base) if extra is None else extra)
            for b, extra in per_branch.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("contract", type=Path)
    ap.add_argument("--emit-fixes", type=Path, default=None,
                    help="dir receiving merged PUT bodies + apply-fixes.md")
    ap.add_argument("--summary", type=Path, default=None,
                    help="append a markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--warn-only", action="store_true",
                    help="findings print as warnings; exit 0 regardless")
    ap.add_argument("--fixtures", type=Path, default=None,
                    help="offline mode: read <owner>__<repo>__<branch>.json|"
                         ".404 fixtures instead of gh api")
    args = ap.parse_args()

    doc = load_contract(args.contract)
    findings: list[str] = []
    warnings: list[str] = []
    report: list[str] = ["| repo | branch | required checks |",
                         "|---|---|---|"]
    fixes: list[dict] = []

    for r in doc["repos"]:
        repo = r.get("repo")
        if not repo:
            continue
        branches = audited(r)
        if not branches:
            continue
        for branch, required in sorted(branches.items()):
            where = f"{repo}@{branch}"
            if not required:
                # 'required_checks: []' means 'nothing required', not 'skip the
                # branch' — an unprotected or missing prod branch is still a
                # finding; only a verified protected branch reports OK.
                protection, err = fetch_protection(repo, branch, args)
                if err and "does not exist" in err:
                    # Definitive contract mismatch, not a scope failure — a
                    # deleted/misspelled lane must not pass as 'unverifiable'.
                    findings.append(f"{where}: {err}")
                    report.append(f"| {repo} | {branch} | ❌ {err} |")
                elif err:
                    warnings.append(f"{where}: {err}")
                    report.append(f"| {repo} | {branch} | ⚠️ unverifiable — {err} |")
                elif protection is None:
                    findings.append(f"{where}: branch unprotected (no checks declared)")
                    report.append(f"| {repo} | {branch} | ❌ unprotected (no checks declared) |")
                    if args.emit_fixes:
                        slug = fixture_name(repo, branch)
                        fixes.append({"repo": repo, "branch": branch,
                                      "file": f"{slug}.json",
                                      "body": fix_body(None, []),
                                      "warnings": ["contract declares NO required checks — "
                                                   "payload enables basic protection only"]})
                else:
                    # Surplus check applies here too — a stale required context
                    # on a 'no checks declared' lane blocks merges silently.
                    extra = current_contexts(protection)
                    if extra:
                        warnings.append(f"{where}: surplus required checks not declared: "
                                        f"{', '.join(extra)}")
                        report.append(f"| {repo} | {branch} | OK (protected, no checks required) "
                                      f"(+{len(extra)} undeclared: `{'`, `'.join(extra)}`) |")
                    else:
                        report.append(f"| {repo} | {branch} | OK (protected, no checks required) |")
                continue
            protection, err = fetch_protection(repo, branch, args)
            if err and "does not exist" in err:
                findings.append(f"{where}: {err}")
                report.append(f"| {repo} | {branch} | ❌ {err} |")
                continue
            if err:
                warnings.append(f"{where}: {err}")
                report.append(f"| {repo} | {branch} | ⚠️ unverifiable — {err} |")
                continue
            rerr = (protection or {}).get("_ruleset_fetch_error")
            if rerr:
                warnings.append(f"{where}: ruleset read failed: {rerr}")
            present = current_contexts(protection) if protection else []
            missing = [c for c in required if c not in present]
            # Surplus live contexts (required by protection but undeclared in
            # the contract) escape a one-sided diff — a stale check CI never
            # emits can block merges forever. Flagged as a warning, not a
            # finding: the contract is a floor, not a ceiling.
            surplus = [c for c in present if c not in required]
            if surplus:
                warnings.append(f"{where}: surplus required checks not declared: "
                                f"{', '.join(surplus)}")
            if missing and rerr:
                # Union may be incomplete — a finding would be a guess, and the
                # emitted PUT could duplicate ruleset-managed checks.
                report.append(f"| {repo} | {branch} | ⚠️ unverifiable — ruleset read failed: {rerr} |")
                continue
            if not missing:
                tail = f" (+{len(surplus)} undeclared: `{'`, `'.join(surplus)}`)" if surplus else ""
                report.append(f"| {repo} | {branch} | OK ({len(required)} required){tail} |")
                continue
            why = "branch unprotected" if protection is None else "missing from required checks"
            findings.append(f"{where}: {why}: {', '.join(missing)}")
            report.append(f"| {repo} | {branch} | ❌ {why}: `{', '.join(missing)}` |")
            if args.emit_fixes:
                slug = fixture_name(repo, branch)
                body = fix_body(protection, required)
                warns = [] if protection else [
                    "branch had no protection — payload sets ONLY "
                    "required_status_checks + enforce_admins; review "
                    "reviews/signatures/restrictions before applying"]
                if protection and (protection.get("required_signatures") or {}).get("enabled"):
                    warns.append("GET showed required_signatures enabled — PUT cannot "
                                 "carry it (separate endpoint); verify signing is still "
                                 "required after applying")
                if protection and protection.get("_ruleset_managed"):
                    warns.append("branch is ruleset-managed — a PUT sets legacy "
                                 "protection alongside the ruleset; prefer "
                                 "editing the ruleset itself")
                fixes.append({"repo": repo, "branch": branch, "file": f"{slug}.json",
                              "body": body, "warnings": warns})

    for w in warnings:
        print(f"::warning::{w}", file=sys.stderr)

    if args.emit_fixes and fixes:
        d = args.emit_fixes
        d.mkdir(parents=True, exist_ok=True)
        lines = ["# Branch-protection fix payloads",
                 "",
                 "Review each body before applying — the PUT replaces the WHOLE",
                 "protection config, so a stale snapshot could drop settings a",
                 "human added since the audit ran.",
                 "",
                 "Run each command from THIS directory (the extracted artifact",
                 "root) — `--input` paths are relative to it, not the runner.",
                 ""]
        for f in fixes:
            (d / f["file"]).write_text(json.dumps(f["body"], indent=2) + "\n",
                                       encoding="utf-8")
            lines += [f"## {f['repo']}@{f['branch']}", "",
                      "```sh",
                      # URI-encode the branch (reads already do); the endpoint
                      # is single-quoted so metachars in branch names stay inert.
                      f"gh api -X PUT 'repos/{f['repo']}/branches/"
                      f"{urllib.parse.quote(f['branch'], safe='')}/protection' "
                      f"--input '{f['file']}'",
                      "```", ""]
            for w in f["warnings"]:
                lines.append(f"> ⚠️ {w}\n")
        (d / "apply-fixes.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"emitted {len(fixes)} fix payload(s) to {d}/ (apply-fixes.md)")

    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write("\n### Branch-protection required-checks audit\n\n")
            fh.write("\n".join(report) + "\n")

    # OK count = branches that actually verified; skipped/unverifiable are
    # excluded so a fleet of 403s cannot masquerade as a conforming fleet.
    ok_count = sum(1 for line in report if "| OK (" in line)
    if findings:
        level = "warning" if args.warn_only else "error"
        print(f"branch-protection: {len(findings)} finding(s) [{level}]:")
        for f in findings:
            print(f"  - {f}")
        return 0 if args.warn_only else 1
    print(f"branch-protection: OK — {ok_count} branch(es) conform, "
          f"{len(warnings)} unverifiable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
