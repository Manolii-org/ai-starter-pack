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
            fail(f"{name}: unknown automation keys {sorted(unknown)} (schema is closed)", errors)

        trigger = auto.get("trigger") or {}
        unknown_t = set(trigger) - ALLOWED_TRIGGER_KEYS
        if unknown_t:
            fail(f"{name}: unknown trigger keys {sorted(unknown_t)}", errors)
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
        on = {e: None for e in on if isinstance(e, str)}
    if not isinstance(on, dict):
        return "on: is not a recognised trigger block"
    ttype = trigger.get("type")
    if ttype == "schedule":
        sched = on.get("schedule")
        if not isinstance(sched, list) or not sched:
            return "workflow has no on.schedule entries"
        crons = [str(s.get("cron")) for s in sched if isinstance(s, dict)]
        want = str(trigger.get("cron", ""))
        if want and want not in crons:
            return f"registered cron {want!r} not in on.schedule {crons!r}"
        return None
    # webhook/other have no `on:` counterpart a file can declare
    if ttype in ("push", "pull_request", "workflow_dispatch"):
        if ttype not in on:
            return f"workflow missing on.{ttype} trigger"
        # a parseable but invalid event config (`on: {push: true}`) can
        # never run — key presence alone is not conformance
        ev = on[ttype]
        if ev is not None and not isinstance(ev, dict):
            return f"on.{ttype} is not a valid event configuration"
    return None


def check_workflow_files(doc: dict, args: argparse.Namespace, errors: list[str]) -> None:
    for auto in doc.get("automations", []):
        name = auto.get("name", "?")
        repo = auto.get("repo", "")
        workflow = auto.get("workflow", "")
        if not repo or not workflow:
            continue
        if "*" in repo:
            continue  # wildcard declaration — applies fleet-wide, no single file to fetch
        text = None
        if args.mode == "local":
            repo_dir = Path(args.repos_dir) / repo.split("/")[-1]
            path = repo_dir / workflow
            if not path.exists():
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
        deadman = [a.get("name", "?") for a in doc.get("automations", [])
                   if isinstance(a, dict) and a.get("deadman") is True]
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
