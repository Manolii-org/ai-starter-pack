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
# GitHub job ids: start with a letter or `_`, then alphanumerics, `-`, `_`.
JOB_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
# Closed per-event sets of GitHub's real event-config keys — a typo'd or
# invented key (`branches_ignore`, `brnches`) or a key legal only on the
# other event (`types` on push, `tags` on pull_request) makes the workflow
# unloadable, so the event is non-matching rather than "unfiltered".
EVENT_KEYS = {
    "push": {"branches", "branches-ignore", "tags", "tags-ignore",
             "paths", "paths-ignore"},
    "pull_request": {"branches", "branches-ignore", "paths", "paths-ignore",
                     "types"},
    "pull_request_target": {"branches", "branches-ignore", "paths",
                            "paths-ignore", "types"},
    "workflow_dispatch": {"inputs"},
    "workflow_call": {"inputs", "secrets", "outputs"},
    "workflow_run": {"workflows", "types", "branches", "branches-ignore"},
}
# GitHub's real `on:` event names — an invented event makes the whole
# workflow file unloadable.
GH_EVENTS = {
    "branch_protection_rule", "check_run", "check_suite", "create",
    "delete", "deployment", "deployment_status", "discussion",
    "discussion_comment", "fork", "gollum", "issue_comment", "issues",
    "label", "merge_group", "milestone", "page_build", "project",
    "project_card", "project_column", "public", "pull_request",
    "pull_request_review", "pull_request_review_comment",
    "pull_request_target", "push", "registry_package", "release",
    "repository_dispatch", "schedule", "status", "watch",
    "workflow_call", "workflow_dispatch", "workflow_run",
}
# workflow_dispatch input-definition grammar: only these keys, and `type`
# restricted to GitHub's real input types.
INPUT_DEF_KEYS = {"description", "required", "type", "default", "options",
                  "deprecationMessage"}
INPUT_TYPES = {"boolean", "choice", "number", "environment", "string"}
# workflow_call (reusable-workflow) definitions have a DIFFERENT grammar:
# `type` is mandatory and restricted to boolean/number/string — no `choice`,
# `environment`, `options`, or `deprecationMessage`.
CALL_INPUT_DEF_KEYS = {"description", "required", "type", "default"}
CALL_INPUT_TYPES = {"boolean", "number", "string"}
CALL_SECRET_KEYS = {"description", "required"}
CALL_OUTPUT_KEYS = {"description", "value"}
# GitHub only runs workflows directly under .github/workflows/.
WORKFLOW_PATH_RE = re.compile(r"\.github/workflows/[^/\\]+\.(?:yml|yaml)\Z")
# GitHub's documented pull_request activity types — a made-up name can never
# fire, and GitHub rejects the workflow that declares one.
PR_TYPES = {"assigned", "unassigned", "labeled", "unlabeled", "opened",
            "edited", "closed", "reopened", "synchronize", "converted_to_draft",
            "ready_for_review", "locked", "unlocked", "review_requested",
            "review_request_removed", "auto_merge_enabled",
            "auto_merge_disabled", "milestoned", "demilestoned", "enqueued",
            "dequeued", "head_ref_restored", "head_ref_deleted",
            "marked_as_duplicate", "transferred"}

MONTH_NAMES = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
               "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
WEEKDAY_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4,
                 "FRI": 5, "SAT": 6}
# minute/hour/dom/month/dow — names are only valid in their own field
CRON_FIELDS = ((0, 59, {}), (0, 23, {}), (1, 31, {}),
               (1, 12, MONTH_NAMES), (0, 7, WEEKDAY_NAMES))


def _cron_atom_ok(atom: str, lo: int, hi: int, names: dict[str, int]) -> bool:
    """One cron atom: `*`, `*/n`, `a`, `a-b`, `a-b/n` — ints or field names."""
    if atom == "*":
        return True
    if atom.startswith("*/"):
        return atom[2:].isdigit() and int(atom[2:]) > 0
    step = None
    if "/" in atom:
        atom, step = atom.split("/", 1)
        if not step.isdigit() or int(step) <= 0:
            return False
    if "-" in atom:
        a, b = atom.split("-", 1)
        try:
            lo_v = names.get(a.upper(), int(a) if a.lstrip("-").isdigit() else None)
            hi_v = names.get(b.upper(), int(b) if b.lstrip("-").isdigit() else None)
        except (TypeError, ValueError):
            return False
        if lo_v is None or hi_v is None:
            return False
        return lo <= lo_v <= hi_v <= hi
    v = names.get(atom.upper())
    if v is None:
        if not atom.lstrip("-").isdigit():
            return False
        v = int(atom)
    return lo <= v <= hi


def valid_cron(expr: str) -> bool:
    fields = expr.split()
    if len(fields) != 5:
        return False
    return all(
        all(_cron_atom_ok(atom, lo, hi, names) for atom in field.split(","))
        for field, (lo, hi, names) in zip(fields, CRON_FIELDS)
    )


def _input_def_ok(v, call: bool = False) -> bool:
    """`inputs:` must map names to definitions inside GitHub's grammar —
    dispatch or reusable-call shape."""
    if not isinstance(v, dict):
        return False
    keys = CALL_INPUT_DEF_KEYS if call else INPUT_DEF_KEYS
    types = CALL_INPUT_TYPES if call else INPUT_TYPES
    for m in v.values():
        if not isinstance(m, dict) or not set(m) <= keys:
            return False
        it = m.get("type")
        if call:
            if it not in types:              # type is REQUIRED for workflow_call
                return False
        elif it is not None and it not in types:
            return False
        if "required" in m and not isinstance(m["required"], bool):
            return False
        if "options" in m and not (
                isinstance(m["options"], list) and m["options"]
                and all(isinstance(o, str) for o in m["options"])):
            return False
    return True


def _event_loadable(name, cfg) -> bool:
    """A sibling `on.<event>` declaration must itself be loadable — one bad
    entry (`schedule: false`, an unknown event name, a bad filter value)
    unloads the whole workflow, so the lane's push/PR can never fire either."""
    if not isinstance(name, str) or name not in GH_EVENTS:
        return False
    if cfg is None:
        return True
    if name == "schedule":
        return (isinstance(cfg, list) and cfg and all(
            isinstance(s, dict) and set(s) == {"cron"}
            and isinstance(s["cron"], str) and valid_cron(s["cron"])
            for s in cfg))
    if not isinstance(cfg, dict):
        return False
    allowed = EVENT_KEYS.get(name, {"types"})
    if not set(cfg) <= allowed:
        return False
    if name == "workflow_run" and not (
            isinstance(cfg.get("workflows"), list) and cfg["workflows"]
            and all(isinstance(w, str) and w.strip() for w in cfg["workflows"])):
        return False
    for k, v in cfg.items():
        if k == "inputs":
            if not _input_def_ok(v, call=(name == "workflow_call")):
                return False
            continue
        if k == "secrets" and name == "workflow_call":
            if not isinstance(v, dict) or any(
                    not isinstance(s, dict) or not set(s) <= CALL_SECRET_KEYS
                    or ("required" in s and not isinstance(s["required"], bool))
                    for s in v.values()):
                return False
            continue
        if k == "outputs" and name == "workflow_call":
            if not isinstance(v, dict) or any(
                    not isinstance(o, dict) or not set(o) <= CALL_OUTPUT_KEYS
                    or not isinstance(o.get("value"), str) or not o["value"].strip()
                    for o in v.values()):
                return False
            continue
        if k in ("secrets", "outputs"):
            if not isinstance(v, dict):
                return False
            continue
        if not (isinstance(v, str)
                or (isinstance(v, list)
                    and all(isinstance(x, str) for x in v))):
            return False
    # empty positive filters and mutually-exclusive pairs can't load — same
    # failures as on the checked push/PR event
    if any(k in cfg and not cfg[k] for k in ("branches", "tags", "paths", "types")):
        return False
    if "branches" in cfg and "branches-ignore" in cfg:
        return False
    if "tags" in cfg and "tags-ignore" in cfg:
        return False
    return True


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
        validator_cls = getattr(jsonschema, "Draft202012Validator", None)
        if validator_cls is None:
            # older jsonschema (e.g. 3.x) — pick the validator matching the
            # schema's $schema keyword instead of a hardcoded draft
            validator_cls = jsonschema.validators.validator_for(schema)
        for err in validator_cls(schema).iter_errors(doc):
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
    if not WORKFLOW_PATH_RE.fullmatch(wf_path):
        # GitHub only discovers workflows under .github/workflows/ — a file
        # stored anywhere else can never run for the lane
        return None, f"{wf_path} is not a .github/workflows/*.yml|*.yaml path"
    if args.mode == "local":
        repo_dir = _repo_dir(args, repo)
        if repo_dir is None:
            return None, f"'{repo}' is not a valid owner/name repository slug"
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
        # keep the worktree fallback inside THIS checkout — an absolute
        # workflow path or `..` traversal would verify a sibling repo's file
        base = repo_dir.resolve()
        p = (repo_dir / wf_path).resolve()
        if not p.is_relative_to(base) or not p.is_file():
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


def _repo_dir(args: argparse.Namespace, repo: str) -> Path | None:
    """`OrgA/api` and `OrgB/api` must not share one flat checkout — when the
    contract repeats a basename, require the owner-qualified
    `<repos-dir>/<owner>/<name>` layout. Otherwise prefer it when present,
    falling back to the flat `<repos-dir>/<name>` convention.

    Returns None when `repo` isn't exactly `owner/name` — an absolute or
    `..`-bearing identifier would escape repos_dir and turn an unrelated
    checkout into the trusted containment base."""
    parts = repo.split("/")
    if (len(parts) != 2 or not all(parts)
            or any(p in (".", "..") for p in parts)):
        return None
    name = parts[1]
    if name in getattr(args, "_dup_basenames", ()):
        return Path(args.repos_dir) / repo
    owner_dir = Path(args.repos_dir) / repo
    if owner_dir.is_dir():
        return owner_dir
    return Path(args.repos_dir) / name


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
            neg = p.startswith("!")
            try:
                hit = gh_match(p[1:] if neg else p)
            except re.error:
                return False  # unparseable glob — workflow can't load
            if hit:
                matched = not neg
        return matched

    on = spec.get("on") or spec.get(True) or {}
    if isinstance(on, str):  # `on: push` scalar shorthand — unfiltered
        on = {on: None}
    if isinstance(on, list):  # `on: [push]` shorthand — every listed event unfiltered
        # reject the WHOLE list on a non-string member — `on: [push, true]`
        # is a workflow GitHub cannot load, not "push only"
        if any(not isinstance(e, str) for e in on):
            return False
        on = {e: None for e in on}
    # Anything left that isn't a mapping (`on: true`, `on: 123`, a list of
    # non-scalars) is malformed — GitHub would reject it, so it can never
    # fire: report as non-matching rather than raising on `event in on`.
    if not isinstance(on, dict):
        return False
    # every declared event must be loadable — a malformed sibling
    # (`on: {push: null, schedule: false}`) unloads the whole file
    for ev_name, ev_cfg in on.items():
        if not _event_loadable(ev_name, ev_cfg):
            return False
    for event in ("push", "pull_request"):
        if event not in on:
            continue
        # `on: push` (bare) parses as None — event present, unfiltered.
        # Falsy-but-invalid configs (`push: false`, `push: []`, `push: ""`)
        # are NOT the None shorthand — GitHub rejects the workflow, so the
        # lane reports drift rather than treating the event as unfiltered.
        ev = on.get(event)
        if ev is None:
            ev = {}
        elif not isinstance(ev, dict):
            continue
        # only GitHub's real event-config keys for THIS event — underscore
        # aliases (`branches_ignore`), typo'd keys, and cross-event keys
        # (`types` on push) alike are unloadable
        if not set(ev) <= EVENT_KEYS[event]:
            continue
        # every declared filter value must be a pattern/activity list — a
        # recognized key with `paths: false` or `types: 5` can't load either
        if any(_as_patterns(ev[k]) is None for k in EVENT_KEYS[event] if k in ev):
            continue  # malformed filter value — workflow can't load
        # tag-only triggers never fire for branch pushes: a `push` event
        # whose config filters on `tags`/`tags-ignore` but declares no
        # `branches`/`branches-ignore` runs only on tag pushes. Detection is
        # by KEY PRESENCE (a falsy `tags: []` is still a declared filter).
        tag_keys = ("tags", "tags-ignore")
        has_tag_filter = any(k in ev for k in tag_keys)
        has_branch_filter = any(k in ev for k in ("branches", "branches-ignore"))
        if event == "push" and has_tag_filter and not has_branch_filter:
            continue
        # GitHub forbids `branches` together with `branches-ignore` (and
        # likewise `tags` with `tags-ignore`) on the same event — a workflow
        # declaring both can never run, so the lane reports drift.
        if "branches" in ev and "branches-ignore" in ev:
            continue
        if "tags" in ev and "tags-ignore" in ev:
            continue
        # an explicitly EMPTY positive filter can never fire — `paths: []`
        # matches no changed file, `types: []` no activity
        if ("paths" in ev and not _as_patterns(ev["paths"])
                or "types" in ev and not _as_patterns(ev["types"])):
            continue
        # pull_request `types` entries must be real activity names — an
        # invented activity never fires and GitHub rejects the workflow
        if (event == "pull_request" and "types" in ev
                and any(t not in PR_TYPES for t in _as_patterns(ev["types"]))):
            continue
        branches = _as_patterns(ev.get("branches"))
        ignore = _as_patterns(ev.get("branches-ignore"))
        # A non-string/non-list filter (`branches-ignore: true`) is an invalid
        # event config — it can never run, so report drift rather than crash
        # on iteration or mistake it for "unfiltered".
        if branches is None or ignore is None:
            continue
        # branches-ignore disqualifies a matched branch (ordered semantics too)
        if eval_ordered(ignore) if ignore else False:
            continue
        # an explicitly EMPTY positive filter (`branches: []`) matches
        # nothing — only a MISSING `branches` key means "every branch"
        if "branches" in ev and not branches:
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
        # reject the WHOLE list on any non-string member — dropping bad
        # entries could leave [], which eval_ordered reads as "unfiltered"
        if any(not isinstance(p, str) for p in value):
            return None
        return list(value)
    return None


def _executable(job: dict) -> bool:
    """A job id only counts when GitHub could actually run it — a reusable
    `uses:` call naming a callee, or a normal job with a non-empty `runs-on`
    (string, label list, or group map) and a non-empty list of step maps.
    `{uses: ""}`, `{runs-on: null, steps: "x"}`, and `{}` parse but execute
    nothing — counting them would satisfy `required_jobs` with a dead job."""
    cond = job.get("if")
    if cond is False or (isinstance(cond, str) and cond.strip().lower() == "false"):
        return False  # `if: false` — permanently skipped, satisfies nothing
    uses = job.get("uses")
    if "uses" in job:
        # reusable-call form — GitHub rejects it when it also carries the
        # normal job-execution fields
        if not (isinstance(uses, str) and uses.strip()):
            return False
        return "runs-on" not in job and "steps" not in job
    return _runner_ok(job.get("runs-on")) and (
        isinstance(job.get("steps"), list) and job["steps"]
        and all(_step_ok(s) for s in job["steps"]))


def _runner_ok(runner) -> bool:
    """`runs-on` may be a label string, a non-empty list of label strings,
    or a `group`/`labels` map — only those keys, only string values."""
    if isinstance(runner, str):
        return bool(runner.strip())
    if isinstance(runner, list):
        return bool(runner) and all(
            isinstance(x, str) and x.strip() for x in runner)
    if isinstance(runner, dict):
        if not runner or not set(runner) <= {"group", "labels"}:
            return False  # `runs-on: {bogus: true}` parses but never runs
        # every PRESENT key must hold a usable value — `{group: null}` or
        # `{labels: null}` selects no runner even though .get() reads it as
        # "absent"
        if "group" in runner and not (
                isinstance(runner["group"], str) and runner["group"].strip()):
            return False
        if "labels" in runner:
            labels = runner["labels"]
            if isinstance(labels, str):
                if not labels.strip():
                    return False
            elif not (isinstance(labels, list) and labels and all(
                    isinstance(x, str) and x.strip() for x in labels)):
                return False
        return True
    return False


def _step_ok(step) -> bool:
    """A runnable step carries a non-empty `run` command or `uses` action —
    exactly one of them. `{}` / `with`-only / `run: ""` steps are rejected by
    GitHub, and so is a step declaring both (`run` + `uses` is invalid syntax)."""
    if not isinstance(step, dict):
        return False
    has_run, has_uses = "run" in step, "uses" in step
    if has_run == has_uses:
        return False  # need exactly one execution form
    v = step.get("run") if has_run else step.get("uses")
    return isinstance(v, str) and bool(v.strip())


def jobs_map(spec: dict) -> dict:
    # Only runnable job definitions count — `jobs: {deploy: null}` or
    # `jobs: {deploy: {}}` parse but are invalid workflows, so they can never
    # satisfy a required_jobs or rollback-name check.
    jobs = spec.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    # keys must be strings — a `jobs: {1: {...}}` entry would feed an int
    # to the rollback regex (TypeError) and isn't a valid GitHub job id
    return {k: v for k, v in jobs.items()
            if isinstance(k, str) and JOB_ID_RE.fullmatch(k)
            and isinstance(v, dict) and _executable(v)}


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

    # basename collisions (OrgA/api + OrgB/api) force owner-qualified
    # checkout paths — a shared flat dir would verify the wrong repo
    # count DISTINCT repo slugs — the same repo listed twice must not mark
    # its basename as a collision
    base_count: dict[str, int] = {}
    for slug in {r["repo"] for r in doc["repos"]
                 if isinstance(r.get("repo"), str)}:
        b = slug.split("/")[-1]
        base_count[b] = base_count.get(b, 0) + 1
    args._dup_basenames = {b for b, n in base_count.items() if n > 1}

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
