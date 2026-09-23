#!/usr/bin/env python3
"""Deployment-contract drift checker.

Reads an instance-owned contract (schemas/deployment-contract.schema.json —
the declarative "what lanes must exist") and diffs it against each repo's
actual workflow files: does the workflow exist, trigger on the declared
branch, define the required job ids, wire a rollback job on lanes that
require one, and serialize deploys under a concurrency group?

Two sources for repo files — each lane is verified against the workflow
file AS IT EXISTS ON THE LANE'S OWN BRANCH (a `push` trigger runs the
branch's copy of the file, not the default branch's):
  --mode local   `git -C <repo> show <branch>:<path>` under --repos-dir/
                 (falls back to the working-tree file when the ref is absent)
  --mode github  `gh api repos/<repo>/contents/<path>?ref=<branch>`

A workflow that delegates to reusable workflows gets job-id leniency: a
required job may appear either as a top-level `jobs.<id>` or as a `uses:`
call-step whose callee name matches (jobs.*.<id>.uses).

Usage:
    python3 scripts/check-deployment-contract.py config/deployment-contracts.yaml \
        --mode local --repos-dir ~/repos
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

try:
    import jsonschema  # optional — full-schema validation when installed
except ImportError:
    jsonschema = None

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schemas" / "deployment-contract.schema.json"
ROLLBACK_RE = re.compile(r"rollback", re.IGNORECASE)

LANE_REQUIRED = ("branch", "environment", "workflow")
REPO_REQUIRED = ("repo", "lanes")
# Closed property sets — mirror the schema's additionalProperties: false so
# the structural fallback (no jsonschema) can't silently accept a typo like
# `required_job` / `concurrency_grop` that disables a drift assertion.
LANE_KEYS = LANE_REQUIRED + ("required_jobs", "rollback_required",
                             "concurrency_group", "health_check", "notes")
REPO_KEYS = REPO_REQUIRED + ("rollback_required", "default_branch",
                             "secrets_required")


def load_contract(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"{path}: cannot parse ({exc})")
    problems: list[str] = []
    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: contract root must be a mapping")
    if doc.get("schema_version") != 1:
        problems.append("schema_version must be 1")
    repos = doc.get("repos")
    if not isinstance(repos, list) or not repos:
        problems.append("'repos' must be a non-empty list")
    else:
        for i, r in enumerate(repos):
            where = f"repos[{i}]"
            if not isinstance(r, dict):
                problems.append(f"{where}: must be a mapping")
                continue
            for field in REPO_REQUIRED:
                if field not in r or r[field] in (None, "", []):
                    problems.append(f"{where} ({r.get('repo', '?')}): missing '{field}'")
            for key in r:
                if key not in REPO_KEYS:
                    problems.append(f"{where} ({r.get('repo', '?')}): "
                                    f"unknown key '{key}'")
            if "repo" in r and not isinstance(r["repo"], str):
                problems.append(f"{where}: 'repo' must be a string")
            if ("rollback_required" in r
                    and not isinstance(r["rollback_required"], bool)):
                problems.append(f"{where} ({r.get('repo','?')}): "
                                "'rollback_required' must be a boolean")
            if "default_branch" in r and not isinstance(r["default_branch"], str):
                problems.append(f"{where} ({r.get('repo','?')}): "
                                "'default_branch' must be a string")
            if ("secrets_required" in r
                    and not (isinstance(r["secrets_required"], list)
                             and all(isinstance(s, str)
                                     for s in r["secrets_required"]))):
                problems.append(f"{where} ({r.get('repo','?')}): "
                                "'secrets_required' must be a list of strings")
            lanes = r.get("lanes")
            if isinstance(lanes, list):
                for j, lane in enumerate(lanes):
                    if not isinstance(lane, dict):
                        problems.append(f"{where}.lanes[{j}]: must be a mapping")
                        continue
                    for key in lane:
                        if key not in LANE_KEYS:
                            problems.append(f"{where}.lanes[{j}] "
                                            f"({r.get('repo','?')}): unknown key '{key}'")
                    for field in LANE_REQUIRED:
                        if field not in lane or lane[field] in (None, ""):
                            problems.append(
                                f"{where}.lanes[{j}] ({r.get('repo','?')}): missing '{field}'")
                        elif not isinstance(lane[field], str):
                            problems.append(
                                f"{where}.lanes[{j}] ({r.get('repo','?')}): "
                                f"'{field}' must be a string")
                    if "required_jobs" in lane and not isinstance(lane["required_jobs"], list):
                        problems.append(f"{where}.lanes[{j}]: required_jobs must be a list")
                    elif "required_jobs" in lane and not all(
                            isinstance(j, str) for j in lane["required_jobs"]):
                        problems.append(f"{where}.lanes[{j}]: required_jobs entries must be strings")
                    if ("rollback_required" in lane
                            and not isinstance(lane["rollback_required"], bool)):
                        problems.append(f"{where}.lanes[{j}]: "
                                        "rollback_required must be a boolean")
                    # truthy non-string (e.g. `concurrency_group: [prod]`) must be
                    # rejected here — check_lane uses it as a string operand
                    for opt in ("concurrency_group", "health_check"):
                        if opt in lane and not isinstance(lane[opt], str):
                            problems.append(f"{where}.lanes[{j}]: "
                                            f"'{opt}' must be a string")
            elif "lanes" in r:
                problems.append(f"{where} ({r.get('repo','?')}): 'lanes' must be a list")
    if jsonschema is not None and SCHEMA_FILE.exists():
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        for err in jsonschema.Draft202012Validator(schema).iter_errors(doc):
            loc = ".".join(str(p) for p in err.absolute_path) or "<root>"
            problems.append(f"{loc}: {err.message}")
    elif jsonschema is None:
        print("note: jsonschema not installed — running structural checks only",
              file=sys.stderr)
    else:
        # jsonschema installed but schema file absent — mis-packaged artifact;
        # fail closed rather than silently downgrade to structural checks.
        raise SystemExit(
            f"error: jsonschema installed but schema file missing at "
            f"{SCHEMA_FILE} — bundle schemas/ alongside this script")
    if problems:
        raise SystemExit(f"{path}: malformed contract — "
                         + "; ".join(sorted(set(problems))))
    return doc


def fetch_workflow(repo: str, wf_path: str, branch: str,
                   args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Return (yaml_text, error) for the workflow file ON THE LANE'S BRANCH."""
    if args.mode == "local":
        repo_dir = Path(args.repos_dir) / repo.split("/")[-1]
        # origin/<branch> is authoritative: once it resolves, the lane is judged
        # by that ref ALONE — a divergent local branch can't paper over a remote
        # lane that lacks the workflow.
        origin_ref = f"origin/{branch}"
        if subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--verify", "-q", origin_ref],
                          capture_output=True).returncode == 0:
            out = subprocess.run(
                ["git", "-C", str(repo_dir), "show", f"{origin_ref}:{wf_path}"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0:
                return out.stdout, None
            return None, f"{wf_path} absent on '{origin_ref}' in {repo_dir.name}"
        if subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--verify", "-q", branch],
                          capture_output=True).returncode == 0:
            out = subprocess.run(
                ["git", "-C", str(repo_dir), "show", f"{branch}:{wf_path}"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0:
                return out.stdout, None
            return None, f"{wf_path} absent on branch '{branch}' in {repo_dir.name}"
        p = repo_dir / wf_path
        if not p.exists():
            return None, f"{wf_path} not found at {branch} or in worktree of {repo_dir.name}"
        return p.read_text(encoding="utf-8", errors="replace"), None
    ref = urllib.parse.quote(branch, safe="")
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{wf_path}?ref={ref}", "--jq", ".content"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None, f"gh api timed out (30s) fetching {repo}/{wf_path}@{branch}"
    if out.returncode != 0:
        return None, f"gh api failed for {repo}/{wf_path}@{branch}: {out.stderr.strip()[:120]}"
    try:
        return base64.b64decode(out.stdout).decode("utf-8", "replace"), None
    except ValueError as exc:
        return None, f"undecodable workflow content: {exc}"


def workflow_triggers_branch(spec: dict, branch: str) -> bool:
    """GitHub branch-trigger semantics: an event with no branches filter runs
    on every branch; a filter list uses fnmatch-style patterns (incl. '*').
    branches-ignore disqualifies a match.
    """
    def gh_match(pattern: str) -> bool:
        """GitHub filter-pattern grammar (workflow-syntax cheat sheet):
        `*` zero+ chars but not '/', `**` any chars incl. '/', `+`/`?` are
        quantifiers on the PRECEDING atom (one-or-more / zero-or-one),
        `[...]` a character class, `\\` escapes the next char.
        (Plain fnmatch lets `*` cross '/' — `release/*` must NOT match
        `release/3/hotfix`.)"""
        out = []
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == "\\" and i + 1 < len(pattern):
                out.append(re.escape(pattern[i + 1]))
                i += 2
            elif c == "*":
                if pattern[i:i + 3] == "**/":
                    # `**/` may consume ZERO path segments: `release/**/prod`
                    # matches `release/prod` as well as `release/a/b/prod`.
                    out.append("(?:.*/)?")
                    i += 3
                elif i + 1 < len(pattern) and pattern[i + 1] == "*":
                    out.append(".*")
                    i += 2
                else:
                    out.append("[^/]*")
                    i += 1
            elif c == "[":
                j = pattern.find("]", i + 1)
                if j == -1:
                    out.append(re.escape(c))
                    i += 1
                else:
                    cls = pattern[i + 1:j]
                    out.append("[" + ("\\" if cls.startswith("^") else "") + cls + "]")
                    i = j + 1
            elif c in ("+", "?"):
                if out:  # quantifier on the preceding atom
                    out[-1] = f"(?:{out[-1]}){c}"
                else:
                    out.append(re.escape(c))
                i += 1
            else:
                out.append(re.escape(c))
                i += 1
        return re.fullmatch("".join(out), branch) is not None

    def eval_ordered(patterns: list[str]) -> bool:
        """GitHub ordered pattern semantics: patterns apply in declaration
        order, a later match overrides an earlier one, and a leading '!'
        negates. `['releases/**', '!releases/**-alpha']` excludes the alpha
        branch even though the first pattern matched it."""
        if not patterns:
            return True  # unfiltered trigger fires on every branch
        matched = False
        for p in patterns:
            if not isinstance(p, str):
                continue
            if p.startswith("!"):
                if gh_match(p[1:]):
                    matched = False
            elif gh_match(p):
                matched = True
        return matched

    on = spec.get("on") or spec.get(True) or {}
    if isinstance(on, str):  # `on: push` scalar shorthand — unfiltered
        on = {on: None}
    if isinstance(on, list):  # `on: [push]` shorthand — every listed event unfiltered
        on = {e: None for e in on if isinstance(e, str)}
    # Anything left that isn't a mapping (`on: true`, `on: 123`, a list of
    # non-scalars) is malformed — GitHub would reject it, so it can never
    # fire: report as non-matching rather than raising on `event in on`.
    if not isinstance(on, dict):
        return False
    for event in ("push", "pull_request"):
        if event not in on:
            continue
        # `on: push` (bare) parses as None — event present, unfiltered
        ev = on.get(event) or {}
        # A structurally invalid event config (on: {push: [main]}, push: true)
        # cannot run at all — treat as non-matching so the lane reports drift
        # rather than crashing on .get.
        if not isinstance(ev, dict):
            continue
        # tag-only triggers never fire for branch pushes: a `push` event
        # whose config filters on `tags`/`tags-ignore` but declares no
        # `branches`/`branches-ignore` runs only on tag pushes.
        has_tag_filter = bool(ev.get("tags") or ev.get("tags-ignore")
                              or ev.get("tags_ignore"))
        has_branch_filter = any(k in ev for k in
                                ("branches", "branches-ignore", "branches_ignore"))
        if event == "push" and has_tag_filter and not has_branch_filter:
            continue
        branches = _as_patterns(ev.get("branches"))
        ignore = _as_patterns(ev.get("branches-ignore")
                              or ev.get("branches_ignore"))
        # A non-string/non-list filter (`branches-ignore: true`) is an invalid
        # event config — it can never run, so report drift rather than crash
        # on iteration or mistake it for "unfiltered".
        if branches is None or ignore is None:
            continue
        # branches-ignore disqualifies a matched branch (ordered semantics too)
        if eval_ordered(ignore) if ignore else False:
            continue
        if eval_ordered(branches):
            return True
    return False


def _as_patterns(value) -> list[str] | None:
    """A GitHub branch/tag filter value → list of patterns, or None when the
    value is a malformed type (bool/number/mapping) — GitHub would reject it,
    so callers must treat it as "can never run", not "unfiltered"."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [p for p in value if isinstance(p, str)]
    return None


def _executable(job: dict) -> bool:
    """A job id only counts when GitHub could actually run it — a reusable
    `uses:` call naming a callee, or a normal job with a non-empty `runs-on`
    (string, label list, or group map) and a non-empty list of step maps.
    `{uses: ""}`, `{runs-on: null, steps: "x"}`, and `{}` parse but execute
    nothing — counting them would satisfy `required_jobs` with a dead job."""
    uses = job.get("uses")
    if isinstance(uses, str):
        return bool(uses.strip())
    runner = job.get("runs-on")
    runner_ok = (isinstance(runner, str) and bool(runner.strip())
                 or isinstance(runner, (list, dict)) and bool(runner))
    steps = job.get("steps")
    return runner_ok and (isinstance(steps, list) and steps
                          and all(isinstance(s, dict) for s in steps))


def jobs_map(spec: dict) -> dict:
    # Only runnable job definitions count — `jobs: {deploy: null}` or
    # `jobs: {deploy: {}}` parse but are invalid workflows, so they can never
    # satisfy a required_jobs or rollback-name check.
    jobs = spec.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    return {k: v for k, v in jobs.items()
            if isinstance(v, dict) and _executable(v)}


def job_ids(spec: dict) -> set[str]:
    return set(jobs_map(spec).keys())


def check_lane(repo: str, repo_spec: dict, lane: dict, args: argparse.Namespace,
               errors: list[str], warnings: list[str]) -> None:
    branch, env = lane.get("branch", "?"), lane.get("environment", "?")
    where = f"{repo}@{branch}({env})"
    wf_path = lane.get("workflow")
    if not wf_path:
        fail(f"{where}: lane declares no workflow", errors)
        return

    text, err = fetch_workflow(repo, wf_path, branch, args)
    if err:
        fail(f"{where}: {err}", errors)
        return
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        fail(f"{where}: {wf_path} is not valid YAML ({exc})", errors)
        return
    if not isinstance(spec, dict):
        fail(f"{where}: {wf_path} parsed to a non-mapping", errors)
        return

    if not workflow_triggers_branch(spec, branch):
        fail(f"{where}: {wf_path} does not trigger on branch '{branch}'", errors)

    jmap = jobs_map(spec)
    jobs = set(jmap.keys())
    for required in lane.get("required_jobs") or []:
        if required not in jobs:
            # `uses:` callee match by filename stem only — required 'smoke' must
            # call a workflow literally named smoke*.yml, not any substring match
            # like 'contest.yml'. Callers may be 'org/repo/.github/workflows/x.yml@ref'
            # or './.github/workflows/x.yml'.
            uses_hit = any(
                isinstance(jmap.get(j), dict)
                and Path(str(jmap[j].get("uses", "")).split("@")[0]).stem == required
                for j in jobs
            )
            if not uses_hit:
                fail(f"{where}: required job '{required}' not found in {wf_path}", errors)

    # branch→environment binding: if the workflow's jobs declare environments,
    # at least one must equal the lane's declared environment — otherwise the
    # contract's central mapping isn't actually enforced by the pipeline.
    declared_envs: set[str] = set()
    for j in jobs:
        job = jmap.get(j)
        if not isinstance(job, dict):
            continue
        env_field = job.get("environment")
        if isinstance(env_field, str) and env_field:
            declared_envs.add(env_field)
        elif isinstance(env_field, dict) and env_field.get("name"):
            declared_envs.add(str(env_field["name"]))
    if env != "?":
        literal_envs = {e for e in declared_envs if "${{" not in e}
        expr_envs = declared_envs - literal_envs
        if env in literal_envs:
            pass  # literal match — verified
        elif expr_envs:
            warnings.append(f"{where}: workflow maps environments via "
                            f"expression {sorted(expr_envs)} — verify "
                            f"'{env}' resolves on branch '{branch}'")
        elif literal_envs:
            fail(f"{where}: lane environment '{env}' not among workflow job "
                 f"environments {sorted(literal_envs)}", errors)
        else:
            warnings.append(f"{where}: no job-level `environment:` in {wf_path} — "
                            "lane→environment binding unverified")

    rollback_required = lane.get("rollback_required")
    if rollback_required is None:
        # repo-level convenience default wins over the schema default (true)
        rollback_required = repo_spec.get("rollback_required", True)
    if rollback_required and not any(ROLLBACK_RE.search(j) for j in jobs):
        fail(f"{where}: rollback_required but no job id matching /rollback/i in {wf_path}", errors)

    group = lane.get("concurrency_group")
    if group:
        conc = spec.get("concurrency")
        # Only the declared group value counts — serialising the whole
        # mapping lets `cancel-in-progress` expressions satisfy the check
        # while deployments actually serialise on the wrong group.
        if isinstance(conc, dict):
            hay = str(conc.get("group", ""))
        else:
            hay = str(conc or "")
        if group not in hay:
            fail(f"{where}: expected concurrency group containing '{group}'", errors)

    if lane.get("health_check") and "health_check" in lane:
        # advisory: assert the literal check string appears somewhere in the workflow
        if str(lane["health_check"]).split()[0] not in text:
            warnings.append(f"{where}: health_check '{lane['health_check']}' "
                            "not found verbatim in workflow (verify manually)")


def fail(msg: str, errors: list[str]) -> None:
    errors.append(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("contract", type=Path)
    ap.add_argument("--mode", choices=["local", "github"], required=True)
    ap.add_argument("--repos-dir", default=".", help="dir holding local repo checkouts (mode=local)")
    args = ap.parse_args()

    doc = load_contract(args.contract)
    errors: list[str] = []
    warnings: list[str] = []

    for r in doc["repos"]:
        repo = r.get("repo")
        if not repo:
            fail("repos[] entry missing 'repo'", errors)
            continue
        for lane in r.get("lanes") or []:
            check_lane(repo, r, lane, args, errors, warnings)

    for w in warnings:
        print(f"warning: {w}")
    if errors:
        print(f"deployment-contract: {len(errors)} drift finding(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    lanes = sum(len(r.get("lanes") or []) for r in doc["repos"])
    print(f"deployment-contract: OK — {len(doc['repos'])} repo(s), {lanes} lane(s) conform")
    return 0


if __name__ == "__main__":
    sys.exit(main())
