#!/usr/bin/env python3
"""ledger.py — append advisory bug-bash run rows and evaluate the stop rule.

The ledger is a JSONL file committed to the APP repo (e.g.
e2e/bug-bash/ledger/runs.jsonl): durable, reviewable, never only chat.

  ledger.py append <runs.jsonl> --kind weekly --target bcp-core-local \
      --app-sha <sha> --charters 3 --exit0 2 --exit1 1 --exit-other 0 \
      --candidates confirmed:1,fixture:0,design_intent:0,judge_error:0,unconfirmed:2 \
      --confirmed-prs https://github.com/org/repo/pull/123 \
      --model-calls 120 --tokens 3100000 --wall-minutes 42 --notes "text"
  ledger.py stop-rule <runs.jsonl> [-n 3]

`stop-rule` exits 10 when the last N fully-completed kind=weekly rows (every
charter reached a verdict) all recorded zero confirmed bugs (lane should
pause), 0 otherwise, 2 on usage/parse errors.

`calibration-check` gates real charters on the calibration run: exits 0 when
the latest calibration row since the last reset is fully charter-accounted,
has >= 3 planted bugs, is no older than ~90 days, and scored recall >= 2/3;
11 when uncalibrated/incomplete/stale or recall is below threshold; 2 on
usage/parse errors. Schedulers must run it before exploring the unmodified
build so a failing calibration cannot quietly spend the weekly budget.

A `kind=reset` row marks a charter/model change after a pause: stop-rule
only counts weekly rows appended after the most recent reset row, so the
resumed lane gets a fresh N-run window instead of inheriting the pre-change
empties.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

BUCKETS = ("confirmed", "fixture", "design_intent", "judge_error", "unconfirmed")


def _die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def _int_or_none(v):
    return None if v in (None, "") else int(v)


def _parse_candidates(spec):
    out = {k: 0 for k in BUCKETS}
    if not spec:
        return out
    for part in spec.split(","):
        key, _, val = part.partition(":")
        key = key.strip().replace("-", "_")
        if key not in out:
            _die(f"unknown candidate bucket {key!r} (one of {', '.join(BUCKETS)})")
        try:
            out[key] = int(val)
        except ValueError:
            _die(f"invalid candidate count {val!r} for bucket {key!r}")
        if out[key] < 0:
            _die(f"negative candidate count {val!r} for bucket {key!r}")
    return out


def _parse_prs(spec):
    if not spec:
        return []
    return [p.strip() for p in spec.split(",") if p.strip()]


def cmd_append(ns):
    planted = _int_or_none(ns.planted)
    found = _int_or_none(ns.planted_found)
    for label, v in (("--planted", planted), ("--planted-found", found)):
        if v is not None and v < 0:
            _die(f"{label} must be non-negative, got {v}")
    if ns.kind == "calibration":
        if planted is None or planted < 3:
            _die("calibration rows require --planted >= 3")
        if found is None:
            _die("calibration rows require --planted-found")
    if found is not None and planted is not None and found > planted:
        _die(f"--planted-found {found} exceeds --planted {planted}")
    if found is not None and planted is None:
        _die("--planted-found requires --planted")
    charters = _int_or_none(ns.charters)
    if charters is not None and charters < 1:
        _die(f"--charters must be >= 1, got {charters}")
    for label, v in (("--exit0", ns.exit0), ("--exit1", ns.exit1), ("--exit-other", ns.exit_other)):
        if not isinstance(v, int) or v < 0:
            _die(f"{label} must be a non-negative integer, got {v}")
    row = {
        "run_at": ns.run_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "kind": ns.kind,
        "target": ns.target,
        "app_sha": ns.app_sha,
        "charters": charters,
        "charter_exits": {"0": ns.exit0, "1": ns.exit1, "other": ns.exit_other},
        "planted": planted,
        "planted_found": found,
        "recall": (None if planted in (None, 0) or found is None
                   else round(found / planted, 3)),
        "candidates": _parse_candidates(ns.candidates),
        "confirmed_prs": _parse_prs(ns.confirmed_prs),
        "model_calls": _int_or_none(ns.model_calls),
        "tokens": _int_or_none(ns.tokens),
        "wall_minutes": _int_or_none(ns.wall_minutes),
        "notes": ns.notes or "",
    }
    with open(ns.ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(f"ledger: appended {row['kind']} run at {row['run_at']} "
          f"(confirmed={row['candidates']['confirmed']}, recall={row['recall']})")


def _rows(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):  # UnicodeDecodeError caught below
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    _die(f"malformed JSONL at {path}:{i}")
                if not isinstance(row, dict):
                    _die(f"malformed JSONL at {path}:{i}: row is not an object")
                rows.append(row)
    except FileNotFoundError:
        _die(f"ledger {path} not found")
    except UnicodeDecodeError:
        _die(f"ledger {path} is not valid UTF-8")
    return rows


def _completed(r):
    """True when every charter of the row reached a verdict: exit0+exit1 ==
    charters and zero `other` exits. Rows recorded without full charter
    accounting (aggregation failure, partial coverage, missing/invalid
    fields) return False — they cannot demonstrate coverage."""
    when = r.get("run_at")
    exits = r.get("charter_exits")
    if exits is None:
        return False
    if not isinstance(exits, dict):
        _die(f"invalid charter_exits on row at {when!r}: not an object")
    vals = {}
    for k in ("0", "1", "other"):
        v = exits.get(k, 0)
        if isinstance(v, (bool, float)):
            _die(f"invalid charter_exits[{k!r}] {v!r} on row at {when!r}")
        try:
            v = int(v)
        except (TypeError, ValueError):
            _die(f"invalid charter_exits[{k!r}] {v!r} on row at {when!r}")
        if v < 0:
            _die(f"negative charter_exits[{k!r}] on row at {when!r}")
        vals[k] = v
    done = vals["0"] + vals["1"]
    if done == 0 or vals["other"]:
        return False
    charters = r.get("charters")
    if charters is None:
        return False
    if isinstance(charters, bool) or not isinstance(charters, int) or charters < 1:
        _die(f"invalid charters {charters!r} on row at {when!r}")
    return done == charters


CALIBRATION_MAX_AGE_DAYS = 90


def _run_age_days(r, label):
    when = r.get("run_at")
    if not isinstance(when, str):
        _die(f"{label} row lacks a parseable run_at timestamp")
    try:
        ts = datetime.strptime(when, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _die(f"{label} row has unparseable run_at {when!r}")
    age = (datetime.now(timezone.utc) - ts).days
    if age < 0:
        _die(f"{label} row at {when!r} is future-dated")
    return age


def cmd_stop_rule(ns):
    if ns.n < 1:
        _die("stop-rule requires -n >= 1")
    rows = _rows(ns.ledger)
    last_reset = max((i for i, r in enumerate(rows) if r.get("kind") == "reset"), default=-1)
    # Excluded rows keep the lane ON, the safe direction.
    weekly = [r for r in rows[last_reset + 1:]
              if r.get("kind") == "weekly" and _completed(r)]
    tail = weekly[-ns.n:]
    if len(tail) < ns.n:
        print(f"stop-rule: only {len(tail)}/{ns.n} weekly runs recorded — lane stays ON")
        return 0
    confirmed = []
    for r in tail:
        candidates = r.get("candidates")
        if candidates is None:
            candidates = {}
        if not isinstance(candidates, dict):
            _die(f"weekly row at {r.get('run_at')!r} has a non-object candidates value")
        count = candidates.get("confirmed")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _die(f"weekly row at {r.get('run_at')!r} lacks a valid candidates.confirmed count")
        confirmed.append(count)
    if sum(confirmed) == 0:
        print(f"stop-rule: {ns.n} consecutive weekly runs with 0 confirmed bugs — PAUSE the lane")
        return 10
    print(f"stop-rule: {sum(confirmed)} confirmed bug(s) across last {ns.n} runs — lane stays ON")
    return 0


def cmd_calibration_check(ns):
    rows = _rows(ns.ledger)
    last_reset = max((i for i, r in enumerate(rows) if r.get("kind") == "reset"), default=-1)
    cal = [r for r in rows[last_reset + 1:] if r.get("kind") == "calibration"]
    if not cal:
        print("calibration-check: no calibration row since last reset — do NOT run real charters")
        return 11
    r = cal[-1]
    when = r.get("run_at")
    planted, found = r.get("planted"), r.get("planted_found")
    for field, v in (("planted", planted), ("planted_found", found)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            _die(f"calibration row at {when!r} has invalid {field} {v!r}")
    if planted < 3:
        print(f"calibration-check: planted {planted} < 3 — do NOT run real charters")
        return 11
    if found > planted:
        _die(f"calibration row at {when!r} has found {found} > planted {planted}")
    if not _completed(r):
        print("calibration-check: calibration run did not complete every charter — do NOT run real charters")
        return 11
    age = _run_age_days(r, "calibration")
    if age > CALIBRATION_MAX_AGE_DAYS:
        print(f"calibration-check: calibration is {age}d old (> {CALIBRATION_MAX_AGE_DAYS}d) — recalibrate first")
        return 11
    # Integer form of found/planted >= 2/3 to avoid float rounding at the edge.
    if found * 3 >= planted * 2:
        print(f"calibration-check: recall {found}/{planted} >= 2/3 — real charters may run")
        return 0
    print(f"calibration-check: recall {found}/{planted} < 2/3 — do NOT run real charters")
    return 11


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("append", help="append one run row")
    a.add_argument("ledger")
    a.add_argument("--kind", required=True, choices=("weekly", "calibration", "reset"))
    a.add_argument("--target", required=True)
    a.add_argument("--app-sha", default=None)
    a.add_argument("--run-at", default=None, help="UTC ISO timestamp; default now")
    a.add_argument("--charters", type=int, default=None)
    a.add_argument("--exit0", type=int, default=0)
    a.add_argument("--exit1", type=int, default=0)
    a.add_argument("--exit-other", type=int, default=0)
    a.add_argument("--planted", type=int, default=None)
    a.add_argument("--planted-found", type=int, default=None)
    a.add_argument("--candidates", default="", help="confirmed:N,fixture:N,design_intent:N,judge_error:N,unconfirmed:N")
    a.add_argument("--confirmed-prs", default="", help="comma-separated PR URLs")
    a.add_argument("--model-calls", type=int, default=None)
    a.add_argument("--tokens", type=int, default=None)
    a.add_argument("--wall-minutes", type=int, default=None)
    a.add_argument("--notes", default="")
    a.set_defaults(fn=cmd_append)

    s = sub.add_parser("stop-rule", help="evaluate the pause rule")
    s.add_argument("ledger")
    s.add_argument("-n", type=int, default=3, help="consecutive empty weekly runs to pause on (default 3)")
    s.set_defaults(fn=cmd_stop_rule)

    c = sub.add_parser("calibration-check", help="gate real charters on the latest calibration recall")
    c.add_argument("ledger")
    c.set_defaults(fn=cmd_calibration_check)

    ns = p.parse_args()
    sys.exit(ns.fn(ns))


if __name__ == "__main__":
    main()
