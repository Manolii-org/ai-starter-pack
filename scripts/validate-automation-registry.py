#!/usr/bin/env python3
"""Validate an instance's automation registry against schemas/automation-registry.schema.json.

Structural (stdlib) validation — jsonschema is used when installed but is NOT
required; the checks below cover the contract's invariants directly:

  - schema_version == 1
  - automations[] entries carry name/repo/workflow/trigger/risk_tier/owner
  - names unique; trigger.type in enum; type=schedule requires a parseable cron
  - risk_tier in {green, amber, red}
  - required_secrets look like NAMES (SCREAMING_SNAKE), not values
  - optional cross-check: each declared workflow file exists in a sibling
    checkout (--repos-dir) or via `gh api` (--mode github)

Usage:
    python3 scripts/validate-automation-registry.py config/automation-registry.yaml
    python3 scripts/validate-automation-registry.py config/automation-registry.yaml \
        --mode local --repos-dir /path/to/checkouts   # file-existence check
    python3 scripts/validate-automation-registry.py config/automation-registry.yaml \
        --mode github                               # `gh api` existence check
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

try:
    import jsonschema  # optional — full-schema validation when installed
except ImportError:
    jsonschema = None

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schemas" / "automation-registry.schema.json"
ALLOWED_AUTO_KEYS = {"name", "repo", "workflow", "trigger", "risk_tier", "owner",
                     "description", "required_secrets", "deadman"}
ALLOWED_TRIGGER_KEYS = {"type", "cron", "description"}

RISK_TIERS = {"green", "amber", "red"}
TRIGGER_TYPES = {"schedule", "push", "pull_request", "workflow_dispatch", "webhook", "other"}
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
# GitHub only runs workflows directly under .github/workflows/.
WORKFLOW_PATH_RE = re.compile(r"\.github/workflows/[^/\\]+\.(?:yml|yaml)\Z")
# GitHub job ids: start with a letter or `_`, then alphanumerics, `-`, `_`.
JOB_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
# GitHub's real per-event config keys — anything else can't load.
EVENT_KEYS = {
    "push": {"branches", "branches-ignore", "tags", "tags-ignore",
             "paths", "paths-ignore"},
    "pull_request": {"branches", "branches-ignore", "paths", "paths-ignore",
                     "types"},
    "pull_request_target": {"branches", "branches-ignore", "paths",
                            "paths-ignore", "types"},
    "workflow_dispatch": {"inputs"},
    "workflow_call": {"inputs", "secrets", "outputs"},
    "workflow_run": {"workflows", "types"},
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


def fail(msg: str, errors: list[str]) -> None:
    errors.append(msg)


def validate(doc: dict, errors: list[str]) -> None:
    if not isinstance(doc, dict):
        fail("registry root must be a mapping", errors)
        return
    if doc.get("schema_version") != 1:
        fail(f"schema_version must be 1 (got {doc.get('schema_version')!r})", errors)
    automations = doc.get("automations")
    if not isinstance(automations, list):
        fail("'automations' must be a list", errors)
        return

    seen: set[str] = set()
    for i, auto in enumerate(automations):
        where = f"automations[{i}]"
        if not isinstance(auto, dict):
            fail(f"{where}: must be a mapping", errors)
            continue
        name = auto.get("name", f"<#{i}>")
        for field in ("name", "repo", "workflow", "trigger", "risk_tier", "owner"):
            if field not in auto or not auto[field]:
                fail(f"{where} ({name}): missing required field '{field}'", errors)
        # type checks — without these, truthy non-strings (e.g. `repo: 123`)
        # pass manual validation whenever jsonschema is absent
        for field in ("name", "repo", "workflow", "risk_tier", "owner"):
            if field in auto and not isinstance(auto[field], str):
                fail(f"{where} ({name}): '{field}' must be a string", errors)
        if "trigger" in auto and not isinstance(auto["trigger"], dict):
            fail(f"{where} ({name}): 'trigger' must be a mapping", errors)
            continue
        # only string names are hashable/comparable — a malformed name ([], {})
        # was already reported as a type error; skip dup tracking for it
        if isinstance(name, str):
            if name in seen:
                fail(f"{where}: duplicate automation name '{name}'", errors)
            seen.add(name)

        unknown = set(auto) - ALLOWED_AUTO_KEYS
        if unknown:
            # keys may be non-strings (YAML `1: value`) — sort via str so
            # the report is consolidated, not a TypeError
            fail(f"{name}: unknown automation keys "
                 f"{sorted(unknown, key=str)} (schema is closed)", errors)

        trigger = auto.get("trigger") or {}
        unknown_t = set(trigger) - ALLOWED_TRIGGER_KEYS
        if unknown_t:
            fail(f"{name}: unknown trigger keys "
                 f"{sorted(unknown_t, key=str)}", errors)
        ttype = trigger.get("type")
        if not isinstance(ttype, str):
            fail(f"{name}: trigger.type must be a string (got {ttype!r})", errors)
        elif ttype not in TRIGGER_TYPES:
            fail(f"{name}: trigger.type '{ttype}' not in {sorted(TRIGGER_TYPES)}", errors)
        if ttype == "schedule":
            cron = trigger.get("cron", "")
            if not valid_cron(str(cron)):
                fail(f"{name}: schedule trigger has an invalid cron expression (got {cron!r})", errors)

        # non-string risk_tier was already reported by the field type-check
        # loop; membership against a set needs a hashable value
        if isinstance(auto.get("risk_tier"), str) and auto["risk_tier"] not in RISK_TIERS:
            fail(f"{name}: risk_tier '{auto['risk_tier']}' not in {sorted(RISK_TIERS)}", errors)

        if "deadman" in auto and not isinstance(auto["deadman"], bool):
            fail(f"{name}: deadman must be a boolean", errors)
        secrets = auto.get("required_secrets")
        if secrets is not None and not isinstance(secrets, list):
            fail(f"{name}: required_secrets must be a list", errors)
        # element checks run only for a real list — a truthy non-list
        # (true/123) was already rejected and is not iterable
        if isinstance(secrets, list):
            for secret in secrets:
                if not isinstance(secret, str) or not SECRET_NAME_RE.match(secret):
                    fail(f"{name}: required_secrets entry {secret!r} does not look like a NAME "
                         "(SCREAMING_SNAKE) — never put values here", errors)


def _workflow_trigger_ok(spec: dict, trigger: dict) -> str | None:
    """Verify a parsed workflow's `on:` actually fires for the registered
    trigger — a schedule entry missing/renamed or a drifted cron otherwise
    looks fine to a file-exists check."""
    on = spec.get("on") or spec.get(True) or {}
    if isinstance(on, str):      # `on: push` scalar shorthand
        on = {on: None}
    if isinstance(on, list):     # `on: [push]` shorthand
        # reject the WHOLE list on a non-string member — `on: [push, true]`
        # is a workflow GitHub cannot load, not "push only"
        if any(not isinstance(e, str) for e in on):
            return "on: list contains a non-string event name"
        on = {e: None for e in on}
    if not isinstance(on, dict):
        return "on: is not a recognised trigger block"
    # EVERY declared event must itself be loadable — a malformed sibling
    # (`on: {push: null, schedule: false}`) or an invented event name makes
    # the whole workflow file unloadable, so the registered trigger can
    # never fire either.
    for ev_name, ev_cfg in on.items():
        err = _event_cfg_ok(ev_name, ev_cfg)
        if err:
            return err
    ttype = trigger.get("type")
    if ttype == "schedule":
        sched = on.get("schedule")
        if sched is None:
            return "workflow has no on.schedule entries"
        crons = [s["cron"] for s in sched]  # entries already cron-validated
        want = str(trigger.get("cron", ""))
        if want and want not in crons:
            return f"registered cron {want!r} not in on.schedule {crons!r}"
        # fall through — a valid cron on a job-less workflow must not verify
    # webhook/other have no `on:` counterpart a file can declare
    if ttype in ("push", "pull_request", "workflow_dispatch"):
        if ttype not in on:
            return f"workflow missing on.{ttype} trigger"
    jobs = spec.get("jobs")
    if not isinstance(jobs, dict) or not any(
            _runnable_job(j) for k, j in jobs.items()
            if isinstance(k, str) and JOB_ID_RE.fullmatch(k)):
        # a conformant `on:` on a workflow that executes nothing still
        # verifies — the registry drift check would stay green with the
        # automation's work silently removed
        return "workflow declares no runnable jobs"
    return None


def _event_cfg_ok(name, cfg) -> str | None:
    """One `on.<event>` declaration must be a config GitHub can load: a real
    event name, the right shape for that event, only that event's documented
    filter keys, and well-formed filter values."""
    if not isinstance(name, str) or name not in GH_EVENTS:
        return f"on declares an unknown event {name!r}"
    if cfg is None:
        return None                          # `on: {push:}` — unfiltered
    if name == "schedule":
        # entries: a non-empty list of {cron: <str>} — only key `cron`, and
        # every expression must parse (one bad entry unloads the workflow)
        if not isinstance(cfg, list) or not cfg:
            return "on.schedule contains a malformed entry"
        if any(not isinstance(s, dict) or set(s) != {"cron"}
               or not isinstance(s["cron"], str) for s in cfg):
            return "on.schedule contains a malformed entry"
        if any(not valid_cron(s["cron"]) for s in cfg):
            return "on.schedule contains an invalid cron expression"
        return None
    if not isinstance(cfg, dict):
        return f"on.{name} is not a valid event configuration"
    # events with no documented branch/path grammar only accept `types`;
    # workflow_call/dispatch/run have their own key sets in EVENT_KEYS
    allowed = EVENT_KEYS.get(name, {"types"})
    if not set(cfg) <= allowed:
        return f"on.{name} uses keys GitHub doesn't support"
    if name == "workflow_run" and not (
            isinstance(cfg.get("workflows"), list) and cfg["workflows"]
            and all(isinstance(w, str) and w.strip() for w in cfg["workflows"])):
        return "on.workflow_run requires a non-empty workflows list"
    for k, v in cfg.items():
        if k == "inputs":
            # input definitions must be a mapping of mappings. Each
            # definition must itself stay inside GitHub's input grammar:
            # only the documented keys, a real `type`, a boolean
            # `required`, and a string-list `options`.
            if not isinstance(v, dict):
                return f"on.{name}.inputs must map inputs to definitions"
            for iname, idef in v.items():
                if not isinstance(idef, dict):
                    return f"on.{name}.inputs must map inputs to definitions"
                if not set(idef) <= INPUT_DEF_KEYS:
                    return f"on.{name}.inputs.{iname} uses keys GitHub doesn't support"
                it = idef.get("type")
                if it is not None and it not in INPUT_TYPES:
                    return f"on.{name}.inputs.{iname}.type '{it}' is not a valid input type"
                if "required" in idef and not isinstance(idef["required"], bool):
                    return f"on.{name}.inputs.{iname}.required must be a boolean"
                if "options" in idef and not (
                        isinstance(idef["options"], list) and idef["options"]
                        and all(isinstance(o, str) for o in idef["options"])):
                    return f"on.{name}.inputs.{iname}.options must be a non-empty list of strings"
            continue
        if k in ("secrets", "outputs"):
            if not isinstance(v, dict):
                return f"on.{name}.{k} must be a mapping"
            continue
        if not (isinstance(v, str)
                or (isinstance(v, list)
                    and all(isinstance(x, str) for x in v))):
            return f"on.{name}.{k} is not a valid filter value"
    # an empty positive filter fires nothing — `paths: []` matches no
    # changed file, `types: []` no activity
    if "paths" in cfg and not cfg["paths"]:
        return f"on.{name} declares an empty paths filter"
    if "types" in cfg and not cfg["types"]:
        return f"on.{name} declares an empty types filter"
    if name in ("pull_request", "pull_request_target") and "types" in cfg:
        types = cfg["types"]
        if isinstance(types, str):
            types = [types]
        if any(t not in PR_TYPES for t in types):
            return f"on.{name}.types contains an invalid activity"
    if "branches" in cfg and "branches-ignore" in cfg:
        return f"on.{name} can't combine branches and branches-ignore"
    if "tags" in cfg and "tags-ignore" in cfg:
        return f"on.{name} can't combine tags and tags-ignore"
    return None


def _runnable_job(job) -> bool:
    """Same bar as the deployment-contract checker: a `uses:` call naming a
    callee, or a valid `runs-on` (str / label list / group map) + steps that
    each carry a non-empty `run` or `uses`."""
    if not isinstance(job, dict):
        return False
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


def _repo_dir(args: argparse.Namespace, repo: str) -> Path | None:
    """`OrgA/api` and `OrgB/api` must not share one flat checkout — when the
    registry repeats a basename, require the owner-qualified
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


def check_workflow_files(doc: dict, args: argparse.Namespace, errors: list[str]) -> None:
    automations = doc.get("automations")
    if not isinstance(automations, list):
        return  # structural errors already recorded by validate()
    # basename collisions (OrgA/api + OrgB/api) force owner-qualified
    # checkout paths — a shared flat dir would verify the wrong repo.
    # Count DISTINCT repo slugs: two automations on the same repository
    # must not mark its basename as a collision.
    base_count: dict[str, int] = {}
    for slug in {a["repo"] for a in automations
                 if isinstance(a, dict) and isinstance(a.get("repo"), str)}:
        b = slug.split("/")[-1]
        base_count[b] = base_count.get(b, 0) + 1
    args._dup_basenames = {b for b, n in base_count.items() if n > 1}
    for auto in automations:
        if not isinstance(auto, dict):
            continue  # non-mapping entries already reported by validate()
        name = auto.get("name", "?")
        repo = auto.get("repo", "")
        workflow = auto.get("workflow", "")
        if not isinstance(repo, str) or not isinstance(workflow, str) \
                or not repo or not workflow:
            continue  # missing/non-string fields already reported by validate()
        if "*" in repo:
            continue  # wildcard declaration — applies fleet-wide, no single file to fetch
        # GitHub only discovers workflows under .github/workflows/ — a file
        # stored anywhere else (archive/nightly.yml) never runs, so don't
        # verify it
        if not WORKFLOW_PATH_RE.fullmatch(workflow):
            fail(f"{name}: workflow '{workflow}' must live at "
                 ".github/workflows/*.yml|*.yaml", errors)
            continue
        text = None
        if args.mode == "local":
            repo_dir = _repo_dir(args, repo)
            if repo_dir is None:
                fail(f"{name}: repo '{repo}' is not a valid owner/name slug", errors)
                continue
            # keep the path inside THIS checkout — an absolute workflow or
            # one with `..`/`/` traversal would verify a sibling repo's file
            base = repo_dir.resolve()
            path = (repo_dir / workflow).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                fail(f"{name}: {workflow} not found in local checkout {repo_dir}", errors)
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        elif args.mode == "github":
            try:
                out = subprocess.run(
                    ["gh", "api", f"repos/{repo}/contents/{workflow}", "--jq", ".content"],
                    capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                fail(f"{name}: gh api timed out (30s) fetching {repo}/{workflow}", errors)
                continue
            if out.returncode != 0:
                fail(f"{name}: gh api could not fetch {repo}/{workflow} "
                     f"({out.stderr.strip()[:120]})", errors)
                continue
            try:
                text = base64.b64decode(out.stdout).decode("utf-8", "replace")
            except ValueError as exc:
                fail(f"{name}: undecodable workflow content ({exc})", errors)
                continue
        if text is None:
            continue
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            fail(f"{name}: {workflow} is invalid YAML ({exc})", errors)
            continue
        if not isinstance(spec, dict):
            fail(f"{name}: {workflow} does not parse to a workflow mapping", errors)
            continue
        problem = _workflow_trigger_ok(spec, auto.get("trigger") or {})
        if problem:
            fail(f"{name}: {workflow} — {problem}", errors)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("registry", type=Path)
    ap.add_argument("--mode", choices=["none", "local", "github"], default="none",
                    help="also verify each declared workflow file exists")
    ap.add_argument("--repos-dir", default=".", help="dir holding local repo checkouts (mode=local)")
    args = ap.parse_args()
    # A bare --repos-dir reads as "check these checkouts"; honour that intent
    # instead of silently skipping the file-existence check under mode=none.
    if args.repos_dir != "." and args.mode == "none":
        args.mode = "local"

    try:
        doc = yaml.safe_load(args.registry.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"error: cannot parse {args.registry}: {exc}", file=sys.stderr)
        return 2

    errors: list[str] = []
    if jsonschema is not None and SCHEMA_FILE.exists():
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        for err in sorted(jsonschema.Draft202012Validator(schema).iter_errors(doc),
                          key=lambda e: list(e.absolute_path)):
            loc = ".".join(str(p) for p in err.absolute_path) or "<root>"
            fail(f"{loc}: {err.message}", errors)
    elif jsonschema is None:
        print("note: jsonschema not installed — running structural checks only",
              file=sys.stderr)
    else:
        # jsonschema IS installed but the schema file is absent — the artifact
        # is mis-packaged (schemas are bundled with the validator); silently
        # downgrading to structural checks would let unsupported properties
        # and constraints slip through. Fail closed.
        print(f"error: jsonschema installed but schema file missing at "
              f"{SCHEMA_FILE} — bundle schemas/ alongside this script",
              file=sys.stderr)
        return 2
    # Manual invariants always run: the schema does not encode name uniqueness,
    # cron-required-for-schedule, or SECRET_NAME_RE — skipping these when
    # jsonschema is installed would make the gate environment-dependent.
    validate(doc, errors)
    # deadman:true is a declaration for operators — a structural check cannot
    # assert a runtime liveness signal, so surface the obligation instead.
    if isinstance(doc, dict):
        autos = doc.get("automations")
        # iterate only a real list — `automations: true`/null already failed
        # structural validation; a TypeError here would destroy the report
        deadman = [a.get("name", "?") for a in autos
                   if isinstance(a, dict) and a.get("deadman") is True] \
            if isinstance(autos, list) else []
        for name in deadman:
            print(f"note: '{name}' declares deadman — verify its liveness "
                  "signal is wired in monitoring (not assertable here)",
                  file=sys.stderr)
    errors = sorted(set(errors))
    if args.mode != "none" and not errors:
        check_workflow_files(doc, args, errors)

    if errors:
        print(f"automation-registry: {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    n = len(doc.get("automations", []))
    print(f"automation-registry: OK — {n} automation(s) registered"
          + (f", workflow files verified ({args.mode})" if args.mode != "none" else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
