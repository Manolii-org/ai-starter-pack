#!/usr/bin/env python3
"""ledger.py — append advisory bug-bash run rows and evaluate the stop rule.

The ledger is a JSONL file committed to the APP repo (e.g.
e2e/bug-bash/ledger/runs.jsonl): durable, reviewable, never only chat.

  ledger.py append <runs.jsonl> --kind weekly --target bcp-core-local \
      --app-sha <sha> --charters 3 --exit0 2 --exit1 1 --exit-other 0 \
      --candidates confirmed:1,fixture:0,design_intent:0,judge_error:0,unconfirmed:2 \
      --confirmed-prs https://github.com/org/repo/pull/123 \
      --model-calls 120 --tokens 3100000 --wall-minutes 42 --notes "text"
  ledger.py stop-rule <runs.jsonl> --target bcp-core-local [-n 3]

`stop-rule` exits 10 when the last N fully-completed kind=weekly rows (every
charter reached a verdict) all recorded zero confirmed bugs (lane should
pause), 0 otherwise, 2 on usage/parse errors.

`calibration-check` gates real charters on the calibration run: exits 0 when
the latest calibration row since the last reset is fully charter-accounted,
has >= 3 planted bugs, is no older than ~90 days, scored recall >= 2/3, and
(when `--expect-fingerprint` is given) carries the same runtime fingerprint;
11 when uncalibrated/incomplete/stale/mismatched or recall is below
threshold; 2 on usage/parse errors. Schedulers must run it before exploring
the unmodified build so a failing calibration cannot quietly spend the
weekly budget.

Both consumers take `--target` and only see rows recorded for that target,
so one shared ledger can serve several apps/environments without an empty
run for one target pausing another (one ledger per target is still the
recommended layout).

`--fingerprint` (append) records the runtime fingerprint the run was
calibrated under — recommended recipe: a sha256 over the actor/judge model
names, the harness ledger.py + fanout.sh bytes, the charters file, and the
e2e driver config. `--expect-fingerprint` (calibration-check) then refuses
to authorize charters with a calibration produced by a different model,
harness, or charter set; a row recorded without a fingerprint fails the
check whenever the flag is passed.

A `kind=reset` row marks a charter/model change after a pause: consumers
only count rows executed (run_at) after the most recent reset *for the
same target*, so the resumed lane gets a fresh N-run window instead of
inheriting the pre-change empties. The boundary is by execution time,
not append order — a delayed aggregation appending a run that executed
before the reset stays excluded.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

BUCKETS = ("confirmed", "fixture", "design_intent", "judge_error", "unconfirmed")


def _die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def _int_or_none(v):
    return None if v in (None, "") else int(v)


def _parse_candidates(spec):
    """Returns (counts, provided_keys): counts is zero-filled for all BUCKETS;
    provided_keys is the set of buckets the spec actually named."""
    out = {k: 0 for k in BUCKETS}
    seen = set()
    if not spec:
        return out, seen
    for part in spec.split(","):
        key, _, val = part.partition(":")
        key = key.strip().replace("-", "_")
        if key not in out:
            _die(f"unknown candidate bucket {key!r} (one of {', '.join(BUCKETS)})")
        if key in seen:
            _die(f"duplicate candidate bucket {key!r} — an ambiguous aggregation cannot be recorded")
        seen.add(key)
        try:
            out[key] = int(val)
        except ValueError:
            _die(f"invalid candidate count {val!r} for bucket {key!r}")
        if out[key] < 0:
            _die(f"negative candidate count {val!r} for bucket {key!r}")
    return out, seen


def _nonempty_target(v):
    if not v or not v.strip():
        _die("--target must be a non-empty string")
    return v.strip()


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
    if ns.kind == "weekly" and ns.candidates is None:
        _die("weekly rows require explicit --candidates accounting "
             "(pass all-zero buckets when triage genuinely found nothing)")
    candidates, provided = _parse_candidates(ns.candidates)
    if ns.kind == "weekly" and provided != set(BUCKETS):
        missing = ", ".join(k for k in BUCKETS if k not in provided)
        _die(f"weekly rows require --candidates covering every bucket "
             f"(missing: {missing}) — pass explicit 0 for buckets triage found none of")
    if ns.kind == "weekly" and sum(candidates.values()) < ns.exit1:
        _die(f"weekly row reports {ns.exit1} candidate exit(s) but only "
             f"{sum(candidates.values())} triaged — every candidate must land in a bucket")
    if ns.kind == "calibration":
        total_candidates = sum(candidates.values())
        if total_candidates < found:
            _die(f"calibration row reports {found} planted bugs found but only "
                 f"{total_candidates} total candidates — pass --candidates covering found")
        if found and ns.exit1 < 1:
            _die(f"calibration row reports {found} planted bugs found but "
                 "--exit1 is 0 — findings need a candidate-producing charter exit")
    for label, v in (("--exit0", ns.exit0), ("--exit1", ns.exit1), ("--exit-other", ns.exit_other)):
        if not isinstance(v, int) or v < 0:
            _die(f"{label} must be a non-negative integer, got {v}")
    for label, v in (("--model-calls", ns.model_calls), ("--tokens", ns.tokens), ("--wall-minutes", ns.wall_minutes)):
        if v is not None and v < 0:
            _die(f"{label} must be non-negative, got {v}")
    run_at = None
    if ns.run_at:
        try:
            dt = datetime.fromisoformat(ns.run_at.replace("Z", "+00:00"))
        except ValueError:
            _die(f"--run-at {ns.run_at!r} is not a parseable ISO 8601 timestamp")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # 60s tolerance for cross-machine clock skew; a genuinely future
        # run_at would poison every timestamp-ordered read below and the
        # append-only ledger cannot retract it.
        if dt.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(seconds=60):
            _die(f"--run-at {ns.run_at!r} is in the future")
        run_at = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    row = {
        "run_at": run_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "kind": ns.kind,
        "target": ns.target,
        "app_sha": ns.app_sha,
        "charters": charters,
        "charter_exits": {"0": ns.exit0, "1": ns.exit1, "other": ns.exit_other},
        "planted": planted,
        "planted_found": found,
        "recall": (None if planted in (None, 0) or found is None
                   else round(found / planted, 3)),
        "candidates": candidates,
        "confirmed_prs": _parse_prs(ns.confirmed_prs),
        "fingerprint": (ns.fingerprint.strip() or None) if ns.fingerprint else None,
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


def _exit1(r):
    """Count of candidate-producing charter exits recorded on the row."""
    exits = r.get("charter_exits")
    if not isinstance(exits, dict):
        return 0
    v = exits.get("1", 0)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


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
    if set(exits) != {"0", "1", "other"}:
        _die(f"invalid charter_exits on row at {when!r}: buckets must be exactly "
             f"['0', '1', 'other'], got {sorted(exits)!r}")
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


def _run_ts(r, label):
    """Parse the row's run_at as a UTC datetime (the run's execution time).
    Dies on missing/malformed values — a row that cannot be placed on the
    timeline cannot demonstrate window membership."""
    when = r.get("run_at")
    if not isinstance(when, str):
        _die(f"{label} row lacks a parseable run_at timestamp")
    try:
        return datetime.strptime(when, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _die(f"{label} row has unparseable run_at {when!r}")


def _run_age_days(r, label):
    ts = _run_ts(r, label)
    delta = datetime.now(timezone.utc) - ts
    # Same 60s skew allowance as append: a writer clock slightly ahead can
    # store a minute timestamp that is marginally in the future here.
    if delta < -timedelta(seconds=60):
        _die(f"{label} row at {r.get('run_at')!r} is future-dated")
    return max(delta.days, 0)


def _reset_ts(rows, target):
    """run_at of the reset with the greatest execution timestamp for
    `target`, or None. The boundary is execution-time only: a reset record
    appended late must not discard rows whose run_at post-dates it, and
    `>=` keeps the same-minute reset→recalibrate cycle."""
    ts = None
    for r in rows:
        if r.get("kind") == "reset" and r.get("target") == target:
            t = _run_ts(r, "reset")
            # A persisted future-dated reset (committed by hand or imported)
            # bypasses the append-time check; same skew allowance applies.
            if t > datetime.now(timezone.utc) + timedelta(seconds=60):
                _die(f"reset row at {r.get('run_at')!r} is future-dated")
            if ts is None or t > ts:
                ts = t
    return ts


def cmd_stop_rule(ns):
    if ns.n < 1:
        _die("stop-rule requires -n >= 1")
    rows = _rows(ns.ledger)
    reset_ts = _reset_ts(rows, ns.target)
    # Excluded rows keep the lane ON, the safe direction.
    weekly = []
    for r in rows:
        if r.get("kind") != "weekly" or r.get("target") != ns.target or not _completed(r):
            continue
        if reset_ts is not None and _run_ts(r, "weekly") < reset_ts:
            continue
        weekly.append(r)
    # Window is by execution time, not append order, and one completed pass
    # per ISO week counts once: extra completed rows in the same week are
    # retries/dupes of that week's outcome (keep the latest).
    by_week = {}
    for r in weekly:
        ts = _run_ts(r, "weekly")
        wk = (ts.isocalendar().year, ts.isocalendar().week)
        # >= breaks same-minute ties toward the later-appended row: run_at is
        # minute-precision, so the later retry in a shared minute is the
        # week's true latest outcome.
        if wk not in by_week or ts >= by_week[wk][0]:
            by_week[wk] = (ts, r)
    tail = [r for ts, r in sorted(by_week.values())[-ns.n:]]
    if len(tail) < ns.n:
        print(f"stop-rule: only {len(tail)}/{ns.n} distinct completed weeks recorded for target {ns.target} — lane stays ON")
        return 0
    confirmed = []
    for r in tail:
        _run_age_days(r, "weekly")  # dies on missing/unparseable/future run_at
        candidates = r.get("candidates")
        if candidates is None:
            candidates = {}
        if not isinstance(candidates, dict):
            _die(f"weekly row at {r.get('run_at')!r} has a non-object candidates value")
        extra = set(candidates) - set(BUCKETS)
        if extra:
            _die(f"weekly row at {r.get('run_at')!r} has unrecognized candidates buckets {sorted(extra)!r}")
        for b in BUCKETS:
            v = candidates.get(b)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                _die(f"weekly row at {r.get('run_at')!r} lacks a valid candidates.{b} count")
        triaged = sum(candidates[b] for b in BUCKETS)
        if triaged < _exit1(r):
            _die(f"weekly row at {r.get('run_at')!r} reports {_exit1(r)} candidate "
                 f"exit(s) but only {triaged} triaged — imported accounting is inconsistent")
        confirmed.append(candidates["confirmed"])
    if sum(confirmed) == 0:
        print(f"stop-rule: {ns.n} consecutive weekly runs with 0 confirmed bugs — PAUSE the lane")
        return 10
    print(f"stop-rule: {sum(confirmed)} confirmed bug(s) across last {ns.n} runs — lane stays ON")
    return 0


def cmd_calibration_check(ns):
    rows = _rows(ns.ledger)
    reset_ts = _reset_ts(rows, ns.target)
    cal = []
    for r in rows:
        if r.get("kind") != "calibration" or r.get("target") != ns.target:
            continue
        if reset_ts is not None and _run_ts(r, "calibration") < reset_ts:
            continue
        cal.append(r)
    if not cal:
        print("calibration-check: no calibration row since last reset — do NOT run real charters")
        return 11
    # Gate applies to the latest calibration by execution time, not append
    # order — a backfilled older row must not mask a newer failing result.
    # run_at is minute-precision, so >= makes equal-timestamp ties resolve to
    # the later-appended row (the retry's verdict).
    r = cal[0]
    best = _run_ts(r, "calibration")
    for row in cal[1:]:
        ts = _run_ts(row, "calibration")
        if ts >= best:
            best = ts
            r = row
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
    if found > 0 and _exit1(r) < 1:
        _die(f"calibration row at {when!r} reports {found} planted found but "
             "charter_exits['1']==0 — findings need a candidate-producing exit")
    cands = r.get("candidates")
    if not isinstance(cands, dict):
        cands = {}
    extra = set(cands) - set(BUCKETS)
    if extra:
        _die(f"calibration row at {when!r} has unrecognized candidates buckets {sorted(extra)!r}")
    total_candidates = sum(v for k, v in cands.items()
                           if k in BUCKETS and isinstance(v, int)
                           and not isinstance(v, bool) and v >= 0)
    if total_candidates < found:
        print("calibration-check: calibration row reports "
              f"{found} planted bugs found but only {total_candidates} total "
              "candidates — no candidate evidence")
        return 11
    if not _completed(r):
        print("calibration-check: calibration run did not complete every charter — do NOT run real charters")
        return 11
    if ns.expect_fingerprint is not None:
        expected = ns.expect_fingerprint.strip() or None
        if expected is None:
            _die("--expect-fingerprint was given an empty value")
        stored = r.get("fingerprint")
        if stored != expected:
            print("calibration-check: calibration fingerprint "
                  f"{stored!r} != active {expected!r} — recalibrate for this model/harness/charter set")
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
    a.add_argument("--target", required=True, type=_nonempty_target)
    a.add_argument("--app-sha", default=None)
    a.add_argument("--run-at", default=None, help="UTC ISO timestamp; default now")
    a.add_argument("--charters", type=int, default=None)
    a.add_argument("--exit0", type=int, default=0)
    a.add_argument("--exit1", type=int, default=0)
    a.add_argument("--exit-other", type=int, default=0)
    a.add_argument("--planted", type=int, default=None)
    a.add_argument("--planted-found", type=int, default=None)
    a.add_argument("--candidates", help="confirmed:N,fixture:N,design_intent:N,judge_error:N,unconfirmed:N")
    a.add_argument("--confirmed-prs", default="", help="comma-separated PR URLs")
    a.add_argument("--fingerprint", default=None,
                   help="runtime fingerprint (model names + harness/charter/config hashes) this run was calibrated under")
    a.add_argument("--model-calls", type=int, default=None)
    a.add_argument("--tokens", type=int, default=None)
    a.add_argument("--wall-minutes", type=int, default=None)
    a.add_argument("--notes", default="")
    a.set_defaults(fn=cmd_append)

    s = sub.add_parser("stop-rule", help="evaluate the pause rule")
    s.add_argument("ledger")
    s.add_argument("--target", required=True, type=_nonempty_target, help="only weekly rows for this target count")
    s.add_argument("-n", type=int, default=3, help="consecutive empty weekly runs to pause on (default 3)")
    s.set_defaults(fn=cmd_stop_rule)

    c = sub.add_parser("calibration-check", help="gate real charters on the latest calibration recall")
    c.add_argument("ledger")
    c.add_argument("--target", required=True, type=_nonempty_target, help="only calibration rows for this target authorize runs")
    c.add_argument("--expect-fingerprint", default=None,
                   help="required fingerprint on the latest calibration row; a row without one fails")
    c.set_defaults(fn=cmd_calibration_check)

    ns = p.parse_args()
    sys.exit(ns.fn(ns))


if __name__ == "__main__":
    main()
