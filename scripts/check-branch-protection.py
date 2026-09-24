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
PUT_BOOL_KEYS = ("required_linear_history", "allow_force_pushes",
                 "allow_deletions", "block_creations",
                 "required_conversation_resolution", "required_signatures",
                 "lock_branch", "allow_fork_syncing")


def current_contexts(protection: dict) -> list[str]:
    """Extract required check-run names from a GET protection response,
    tolerating both API shapes (`checks[].context` and legacy `contexts[]`)."""
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


def fetch_protection(repo: str, branch: str,
                     args: argparse.Namespace) -> tuple[dict | None, str | None]:
    """Return (protection_json, error). protection_json=None means the branch
    is unprotected (404); error is set only for real fetch failures."""
    if getattr(args, "fixtures", None):
        base = Path(args.fixtures) / f"{repo.replace('/', '__')}__{branch}"
        body_path = base.with_suffix(".json")
        if base.with_suffix(".404").exists():
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
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/branches/{ref}/protection"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None, f"gh api timed out (30s) on {repo}@{branch}"
    except OSError as exc:
        return None, f"gh not runnable: {exc}"
    if out.returncode != 0:
        err = out.stderr.strip()[:160]
        if "404" in err or "Not Found" in err:
            return None, None  # unprotected branch — a finding, not an error
        return None, f"gh api protection read failed for {repo}@{branch}: {err}"
    try:
        return json.loads(out.stdout), None
    except ValueError as exc:
        return None, f"undecodable protection JSON for {repo}@{branch}: {exc}"


def _enabled(node) -> bool:
    """A GET protection sub-object carries {enabled: bool, url} — the PUT body
    wants the bare boolean."""
    return bool(isinstance(node, dict) and node.get("enabled"))


def fix_body(protection: dict | None, required: list[str]) -> dict:
    """Build a complete PUT /protection body: existing settings preserved,
    required_status_checks.checks merged with the missing contexts appended.

    protection=None (unprotected branch) yields a minimal body — the caller
    warns the human to review review/signature/restriction settings before
    applying, since none existed to preserve."""
    body: dict = {}
    if protection:
        existing = (protection.get("required_status_checks") or {}).get("checks") or []
        app_ids = {e.get("context"): e.get("app_id")
                   for e in existing if isinstance(e, dict)}
        checks = [{"context": c, "app_id": app_ids.get(c)}
                  for c in current_contexts(protection)]
        checks += [{"context": c} for c in required
                   if c not in {x["context"] for x in checks}]
        rsc = protection.get("required_status_checks") or {}
        body["required_status_checks"] = {
            "strict": bool(rsc.get("strict")), "checks": checks}
        body["enforce_admins"] = _enabled(protection.get("enforce_admins"))
        rpr = protection.get("required_pull_request_reviews")
        body["required_pull_request_reviews"] = (
            {k: v for k, v in rpr.items() if k != "url"}
            if isinstance(rpr, dict) else None)
        rst = protection.get("restrictions")
        if isinstance(rst, dict):
            body["restrictions"] = {
                "users": [u.get("login") for u in rst.get("users") or []
                          if isinstance(u, dict) and u.get("login")],
                "teams": [t.get("slug") for t in rst.get("teams") or []
                          if isinstance(t, dict) and t.get("slug")],
                "apps": [a.get("slug") for a in rst.get("apps") or []
                         if isinstance(a, dict) and a.get("slug")],
            }
        else:
            body["restrictions"] = None
        for k in PUT_BOOL_KEYS:
            body[k] = _enabled(protection.get(k))
    else:
        body = {
            "required_status_checks": {
                "strict": False,
                "checks": [{"context": c} for c in required],
            },
            "enforce_admins": True,
            "required_pull_request_reviews": None,
            "restrictions": None,
        }
        for k in PUT_BOOL_KEYS:
            body[k] = None
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
                report.append(f"| {repo} | {branch} | none declared — skipped |")
                continue
            protection, err = fetch_protection(repo, branch, args)
            if err:
                warnings.append(f"{where}: {err}")
                report.append(f"| {repo} | {branch} | ⚠️ unverifiable — {err} |")
                continue
            present = current_contexts(protection) if protection else []
            missing = [c for c in required if c not in present]
            if not missing:
                report.append(f"| {repo} | {branch} | OK ({len(required)} required) |")
                continue
            why = "branch unprotected" if protection is None else "missing from required checks"
            findings.append(f"{where}: {why}: {', '.join(missing)}")
            report.append(f"| {repo} | {branch} | ❌ {why}: `{', '.join(missing)}` |")
            if args.emit_fixes:
                slug = f"{repo.replace('/', '__')}__{branch}"
                body = fix_body(protection, required)
                warns = [] if protection else [
                    "branch had no protection — payload sets ONLY "
                    "required_status_checks + enforce_admins; review "
                    "reviews/signatures/restrictions before applying"]
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
                 ""]
        for f in fixes:
            (d / f["file"]).write_text(json.dumps(f["body"], indent=2) + "\n",
                                       encoding="utf-8")
            lines += [f"## {f['repo']}@{f['branch']}", "",
                      "```sh",
                      f"gh api -X PUT repos/{f['repo']}/branches/{f['branch']}/protection "
                      f"--input {f['file']}",
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

    if findings:
        level = "warning" if args.warn_only else "error"
        print(f"branch-protection: {len(findings)} finding(s) [{level}]:")
        for f in findings:
            print(f"  - {f}")
        return 0 if args.warn_only else 1
    print(f"branch-protection: OK — "
          f"{sum(len(audited(r)) for r in doc['repos'])} branch(es) conform")
    return 0


if __name__ == "__main__":
    sys.exit(main())
