#!/usr/bin/env python3
"""Offline, metadata-only security audit evidence checks; no network or writes."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo


REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z")
REVISION = re.compile(r"[0-9a-fA-F]{40}\Z")
STATUSES = {"covered", "needs_validation", "deferred", "out_of_scope"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def refs(value):
    return isinstance(value, list) and bool(value) and all(text(v) for v in value)


def timestamp(value):
    require(text(value), "timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.utcoffset() is not None, "timezone required")
    return parsed


def number(value, *, integer=False):
    require(not isinstance(value, bool), "boolean is not a measurement")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid measurement") from exc
    require(parsed.is_finite() and parsed >= 0, "finite nonnegative measurement required")
    require(not integer or parsed == parsed.to_integral_value(), "integer required")
    return parsed


def validate_receipt(doc):
    require(isinstance(doc, dict), "receipt must be an object")
    require(doc.get("schema") == "security-audit-evidence/v1", "unsupported receipt schema")
    owner = doc.get("owner")
    require(text(owner), "owner required")
    sources = doc.get("source_revisions")
    require(isinstance(sources, list) and sources, "source revisions required")
    revisions = {}
    for source in sources:
        require(isinstance(source, dict), "invalid source revision")
        repo, revision = source.get("repository"), source.get("revision")
        require(isinstance(repo, str) and REPOSITORY.fullmatch(repo), "invalid repository")
        require(repo.split("/")[0].lower() == owner.lower(), "mixed owners refused")
        require(isinstance(revision, str) and REVISION.fullmatch(revision), "commit revision required")
        require(source.get("dirty") is False, "clean committed source required")
        require(repo.lower() not in revisions, "duplicate source repository")
        revisions[repo.lower()] = revision.lower()
    budget = doc.get("budget")
    require(isinstance(budget, dict), "budget required")
    maximum = number(budget.get("max_invocations"), integer=True)
    used = number(budget.get("used_invocations"), integer=True)
    require(maximum > 0 and used <= maximum, "invocation budget exceeded or invalid")
    execution = doc.get("execution")
    require(isinstance(execution, dict), "execution policy required")
    require(execution.get("mode") in {"source_only", "isolated"}, "invalid execution mode")
    require(execution.get("production_access") is False, "production access refused")
    require(execution.get("secret_environment") is False, "secret environment refused")
    coverage = doc.get("coverage")
    require(isinstance(coverage, list) and coverage, "coverage units required")
    units, counts = {}, Counter()
    for unit in coverage:
        require(isinstance(unit, dict), "invalid coverage unit")
        uid, repo = unit.get("id"), unit.get("repository")
        require(text(uid) and uid not in units, "duplicate or missing coverage unit")
        require(isinstance(repo, str) and repo.lower() in revisions, "unknown source repository")
        revision = unit.get("revision")
        require(text(revision) and revision.lower() == revisions[repo.lower()], "stale coverage revision")
        require(text(unit.get("boundary")) and text(unit.get("attack_class")), "boundary and attack class required")
        require(unit.get("status") in STATUSES, "invalid coverage status")
        if unit["status"] == "covered":
            require(refs(unit.get("evidence")), "covered unit requires evidence")
        units[uid] = unit
        counts[unit["status"]] += 1
    findings = doc.get("findings")
    require(isinstance(findings, list), "findings list required")
    verdicts = Counter()
    for finding in findings:
        require(isinstance(finding, dict), "invalid finding")
        require(finding.get("unit_id") in units, "finding references unknown coverage")
        verdict = finding.get("verdict")
        require(verdict in {"confirmed", "needs_validation", "rejected"}, "invalid finding verdict")
        require(refs(finding.get("evidence")), "finding evidence required")
        if verdict == "confirmed":
            hunter, verifier = finding.get("hunter_id"), finding.get("verifier_id")
            require(text(hunter) and text(verifier) and hunter != verifier, "independent verifier required")
            method = finding.get("validation")
            require(method in {"source_trace", "sandbox_reproducer"}, "validation method required")
            if method == "sandbox_reproducer":
                require(execution["mode"] == "isolated" and execution.get("isolation_verified") is True
                        and execution.get("network_disabled") is True, "verified networkless isolation required")
        verdicts[verdict] += 1
    return {"schema": "security-audit-summary/v1", "sources": len(revisions),
            "coverage": dict(counts), "findings": dict(verdicts),
            "coverage_complete": counts["covered"] == len(units) and not verdicts["needs_validation"],
            "vulnerability_correctness_verified": False}


def compare_metadata(doc, baseline, comparison, days, timezone):
    require(isinstance(doc, dict) and doc.get("schema") == "security-scan-metadata/v1", "unsupported metadata schema")
    require(type(days) is int and 0 < days <= 366, "invalid calendar window")
    zone = ZoneInfo(timezone)
    starts = [datetime.combine(date.fromisoformat(d), time.min, zone) for d in (baseline, comparison)]
    require(starts[0] + timedelta(days=days) <= starts[1], "comparison windows overlap")
    complete_after, as_of = timestamp(doc.get("history_complete_after")), timestamp(doc.get("as_of"))
    require(complete_after <= as_of, "invalid history interval")
    require(isinstance(doc.get("scans"), list), "scan metadata list required")
    rows, unique = [], {}
    for row in doc["scans"]:
        require(isinstance(row, dict), "invalid scan metadata")
        repo, sid = row.get("repository"), row.get("id")
        require(isinstance(repo, str) and REPOSITORY.fullmatch(repo) and text(sid), "repository and scan id required")
        start = timestamp(row.get("started_at"))
        require(start <= as_of, "scan starts after snapshot")
        require(text(row.get("status")), "scan status required")
        require(row.get("success") is None or type(row["success"]) is bool, "invalid scan outcome")
        if row.get("finished_at") is not None:
            require(start <= timestamp(row["finished_at"]) <= as_of, "invalid finish timestamp")
        key = (repo.lower(), sid)
        if key in unique:
            require(unique[key] == row, "conflicting duplicate scan")
            continue
        unique[key] = row
        for field in ("recorded_cost_usd", "input_tokens", "output_tokens", "analyst_minutes"):
            if row.get(field) is not None:
                number(row[field], integer=field.endswith("tokens"))
        rows.append((row, start))
    owners = {}
    for row, _ in rows:
        owner = row["repository"].split("/")[0]
        owners.setdefault(owner.lower(), owner)
    result = {"schema": "security-scan-comparison/v1", "timezone": timezone, "days": days,
              "attributed_savings_usd": None, "attribution": "not_measured", "owners": {}}
    for owner_key, owner in sorted(owners.items()):
        windows = []
        for start in starts:
            end = start + timedelta(days=days)
            complete = complete_after <= start and as_of >= end
            selected = [row for row, at in rows if row["repository"].split("/")[0].lower() == owner_key and start <= at < end]
            window = {"start": start.isoformat(), "end_exclusive": end.isoformat(),
                      "scan_starts": len(selected), "history_complete": complete,
                      "successful": sum(row.get("success") is True for row in selected)}
            for field in ("recorded_cost_usd", "input_tokens", "output_tokens", "analyst_minutes"):
                available = [number(row[field]) for row in selected if row.get(field) is not None]
                window[field] = str(sum(available)) if complete and selected and len(available) == len(selected) else None
                window[field + "_observed_scans"] = len(available)
            windows.append(window)
        before, after = windows
        comparable = before["history_complete"] and after["history_complete"]
        reduction = ((before["scan_starts"] - after["scan_starts"]) / before["scan_starts"] * 100
                     if comparable and before["scan_starts"] else None)
        costs = [w["recorded_cost_usd"] for w in windows]
        result["owners"][owner] = {"baseline": before, "comparison": after,
                                   "scan_start_reduction_percent": round(reduction, 1) if reduction is not None else None,
                                   "observed_spend_reduction_usd": str(Decimal(costs[0]) - Decimal(costs[1])) if all(c is not None for c in costs) else None}
    return result


def read_json(path):
    require(path.is_file() and path.stat().st_size <= 10 * 1024 * 1024, "missing or oversized metadata file")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    validate = subs.add_parser("validate")
    validate.add_argument("input", type=Path)
    compare = subs.add_parser("compare")
    compare.add_argument("input", type=Path)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--comparison", required=True)
    compare.add_argument("--days", type=int, required=True)
    compare.add_argument("--timezone", default="UTC")
    args = parser.parse_args(argv)
    try:
        doc = read_json(args.input)
        result = (validate_receipt(doc) if args.command == "validate" else
                  compare_metadata(doc, args.baseline, args.comparison, args.days, args.timezone))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        # Parser/IO exceptions can contain user-controlled text. Never echo them.
        print(json.dumps({"status": "invalid_evidence", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
