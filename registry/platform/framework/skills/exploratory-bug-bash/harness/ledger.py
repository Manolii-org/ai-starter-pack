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

`stop-rule` exits 10 when the last N kind=weekly rows all recorded zero
confirmed bugs (lane should pause), 0 otherwise, 2 on usage/parse errors.

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
            raise SystemExit(f"error: unknown candidate bucket {key!r} (one of {', '.join(BUCKETS)})")
        out[key] = int(val)
    return out


def _parse_prs(spec):
    if not spec:
        return []
    return [p.strip() for p in spec.split(",") if p.strip()]


def cmd_append(ns):
    row = {
        "run_at": ns.run_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "kind": ns.kind,
        "target": ns.target,
        "app_sha": ns.app_sha,
        "charters": _int_or_none(ns.charters),
        "charter_exits": {"0": ns.exit0, "1": ns.exit1, "other": ns.exit_other},
        "planted": _int_or_none(ns.planted),
        "planted_found": _int_or_none(ns.planted_found),
        "recall": (None if _int_or_none(ns.planted) in (None, 0) or ns.planted_found is None
                   else round(ns.planted_found / ns.planted, 3)),
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
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    raise SystemExit(f"error: malformed JSONL at {path}:{i}")
    except FileNotFoundError:
        raise SystemExit(f"error: ledger {path} not found")
    return rows


def cmd_stop_rule(ns):
    rows = _rows(ns.ledger)
    last_reset = max((i for i, r in enumerate(rows) if r.get("kind") == "reset"), default=-1)
    weekly = [r for r in rows[last_reset + 1:] if r.get("kind") == "weekly"]
    tail = weekly[-ns.n:]
    if len(tail) < ns.n:
        print(f"stop-rule: only {len(tail)}/{ns.n} weekly runs recorded — lane stays ON")
        return 0
    confirmed = [int((r.get("candidates") or {}).get("confirmed") or 0) for r in tail]
    if sum(confirmed) == 0:
        print(f"stop-rule: {ns.n} consecutive weekly runs with 0 confirmed bugs — PAUSE the lane")
        return 10
    print(f"stop-rule: {sum(confirmed)} confirmed bug(s) across last {ns.n} runs — lane stays ON")
    return 0


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

    ns = p.parse_args()
    sys.exit(ns.fn(ns))


if __name__ == "__main__":
    main()
