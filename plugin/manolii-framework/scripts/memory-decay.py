#!/usr/bin/env python3
"""
Memory decay and consolidation system.

Implements Phase 7 of memory system evolution:
- Confidence decay: adjusted = base - (days_since / 365) * rate, floored
- Deterministic consolidation: lexical Jaccard dedup within entity_scope + type groups
- File I/O: atomic JSONL with tempfile
- CLI: --apply (default dry-run), --decay-only/--consolidate-only, threshold/rate/floor params
- Exits 0 always; reports to stderr
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO timestamp string to timezone-aware UTC datetime. None -> None."""
    if not s:
        return None
    s = s.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _days_since(ts_str: Optional[str], now: datetime) -> float:
    """Compute days from parsed timestamp to now. Missing ts_str -> 0."""
    if not ts_str:
        return 0.0
    dt = _parse_ts(ts_str)
    if not dt:
        return 0.0
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def decay_confidence(
    base: float, days: float, rate: float = 0.1, floor: float = 0.1
) -> float:
    """Apply decay formula: adjusted = base - (days / 365) * rate, floored at floor."""
    adjusted = base - (days / 365.0) * rate
    return max(floor, adjusted)


def tokenize(text: str) -> set[str]:
    """Normalize text to token set: casefold, split on non-alphanumeric, drop <3 chars."""
    text = text.casefold()
    tokens = re.findall(r"\w+", text)
    return {t for t in tokens if len(t) >= 3}


_PATTERN_FIELDS = ("problem", "solution", "rule")


def _comparable_text(row: dict) -> str:
    """Text used for dedup comparison across supported memory schemas.

    facts use `content`; patterns (written by /learn) carry
    problem/solution/rule. A row with no comparable text returns "" and
    must never be merged — two empty strings would read as identical.
    """
    content = row.get("content")
    if isinstance(content, str) and content.strip():
        return content
    parts = [row.get(k, "") for k in _PATTERN_FIELDS]
    return " ".join(p for p in parts if isinstance(p, str) and p.strip())


def _is_pattern(row: dict) -> bool:
    return isinstance(row.get("problem"), str) and row["problem"].strip() != ""


def _rows_mergeable(a: dict, b: dict, threshold: float) -> bool:
    """Whether two rows are close enough to consolidate.

    Pattern rows must be similar on BOTH axes — `problem` (the context)
    and `solution`+`rule` (the answer). Requiring only the answer side
    drops distinct problem contexts; including `problem` in one pooled
    token set lets a shared question dominate and merge different answers.
    Non-pattern rows compare on _comparable_text as before.
    """
    text_a = _comparable_text(a)
    text_b = _comparable_text(b)
    if not text_a or not text_b:
        return False
    if not (_is_pattern(a) and _is_pattern(b)):
        return text_a == text_b or jaccard(tokenize(text_a), tokenize(text_b)) >= threshold
    if jaccard(tokenize(a["problem"]), tokenize(b["problem"])) < threshold:
        return False
    ans_a = " ".join(str(a.get(k, "")) for k in ("solution", "rule"))
    ans_b = " ".join(str(b.get(k, "")) for k in ("solution", "rule"))
    return jaccard(tokenize(ans_a), tokenize(ans_b)) >= threshold


def jaccard(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity: |intersection| / |union|."""
    if not a and not b:
        return 1.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union > 0 else 0.0


def load_jsonl(path: str | Path) -> tuple[list[dict], int]:
    """Load JSONL, skip blank/malformed lines. Log skips to stderr."""
    rows = []
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line or not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"[memory-decay] skipped line {i}: {e}", file=sys.stderr)
                    skipped += 1
                    continue
                if not isinstance(row, dict):
                    print(f"[memory-decay] skipped line {i}: expected JSON object", file=sys.stderr)
                    skipped += 1
                    continue
                rows.append(row)
    except FileNotFoundError:
        # Missing file is expected on first run → return empty list.
        pass
    if skipped > 0:
        print(f"[memory-decay] skipped {skipped} malformed lines", file=sys.stderr)
    return rows, skipped


def save_jsonl(path: str | Path, rows: list[dict]) -> None:
    """Save rows to JSONL atomically via tempfile + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            # Cleanup failure is ignored so it can't mask the original write error.
            pass
        raise


def apply_decay(
    rows: list[dict], now: datetime, rate: float = 0.1, floor: float = 0.1
) -> list[dict]:
    """Apply decay: set last_seen default, add adjusted_confidence field."""
    for row in rows:
        if "last_seen" not in row and "created" in row:
            row["last_seen"] = row["created"]
        ts = row.get("last_seen") or row.get("created")
        days = _days_since(ts, now)
        base = float(row.get("confidence", 1.0))
        row["adjusted_confidence"] = round(decay_confidence(base, days, rate, floor), 3)
    return rows


def consolidate(
    rows: list[dict], threshold: float = 0.6, now: Optional[datetime] = None
) -> tuple[list[dict], list[dict]]:
    """
    Deduplicate within entity_scope + type groups via Jaccard.
    Returns (consolidated_rows, merge_records).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Group by (entity_scope, type)
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        scope = row.get("entity_scope", "unknown")
        type_ = row.get("type", "unknown")
        key = (scope, type_)
        if key not in groups:
            groups[key] = []
        groups[key].append(row)

    consolidated = []
    merges = []

    for (scope, type_), group in groups.items():
        if len(group) <= 1:
            consolidated.extend(group)
            continue

        # Lexical clustering via Jaccard
        clusters: list[list[dict]] = []
        assigned = set()

        for i, row_a in enumerate(group):
            if i in assigned:
                continue
            cluster = [row_a]
            assigned.add(i)
            for j, row_b in enumerate(group):
                if j <= i or j in assigned:
                    continue
                if _rows_mergeable(row_a, row_b, threshold):
                    cluster.append(row_b)
                    assigned.add(j)

            clusters.append(cluster)

        # Merge each cluster
        for cluster in clusters:
            if len(cluster) == 1:
                consolidated.append(cluster[0])
                continue

            # Canonical = highest confidence (tie -> earliest created). Parse
            # created safely: a missing/malformed value sorts as "latest"
            # (float inf) so it is never preferred as the tie-break earliest.
            def _created_ts(r: dict) -> float:
                dt = _parse_ts(r.get("created"))
                return dt.timestamp() if dt else float("inf")

            canonical = max(
                cluster,
                key=lambda r: (float(r.get("confidence", 1.0)), -_created_ts(r)),
            )

            # Merge fields
            tags = set()
            for row in cluster:
                if "tags" in row:
                    tag_list = row["tags"]
                    if isinstance(tag_list, list):
                        tags.update(tag_list)
            if tags:
                canonical["tags"] = sorted(tags)

            # Confidence: min(0.95, max_conf + 0.05 * (cluster_size - 1))
            max_conf = max(float(r.get("confidence", 1.0)) for r in cluster)
            canonical["confidence"] = min(0.95, max_conf + 0.05 * (len(cluster) - 1))

            # last_seen = max of members' last_seen/created OR now
            last_seen_candidates = []
            for row in cluster:
                ts = row.get("last_seen") or row.get("created")
                if ts:
                    parsed = _parse_ts(ts)
                    if parsed:
                        last_seen_candidates.append(parsed)
            if last_seen_candidates:
                # Consolidation reaffirms the fact — count now as a sighting so the
                # merged row does not immediately read as stale to the decay pass.
                last_seen_candidates.append(now)
                canonical["last_seen"] = max(last_seen_candidates).isoformat()
            else:
                canonical["last_seen"] = now.isoformat()

            # Reinforced count
            reinforced_sum = sum(r.get("reinforced", 0) for r in cluster)
            canonical["reinforced"] = reinforced_sum + (len(cluster) - 1)

            consolidated.append(canonical)

            # Record merge
            dropped_ids = [r.get("id") for r in cluster if r is not canonical]
            merges.append(
                {
                    "kept_id": canonical.get("id"),
                    "dropped_ids": dropped_ids,
                    "cluster_size": len(cluster),
                }
            )

    return consolidated, merges


def run(
    file: str | Path,
    apply: bool = False,
    do_decay: bool = True,
    do_consolidate: bool = True,
    threshold: float = 0.6,
    rate: float = 0.1,
    floor: float = 0.1,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Execute decay and/or consolidation on memory file.
    Returns report dict.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    rows, skipped = load_jsonl(file)
    if apply and skipped:
        raise SystemExit(
            f"[memory-decay] refusing --apply: {skipped} malformed line(s) in {file}; "
            "repair or remove them first"
        )
    initial_count = len(rows)

    merges = []
    if do_consolidate:
        rows, merges = consolidate(rows, threshold, now)

    if do_decay:
        rows = apply_decay(rows, now, rate, floor)

    consolidated_count = len(rows)
    dropped_count = initial_count - consolidated_count
    decayed_count = len(rows) if do_decay else 0
    below_floor = (
        sum(1 for r in rows if r.get("adjusted_confidence", 1.0) <= floor)
        if do_decay
        else 0
    )

    if apply:
        save_jsonl(file, rows)
        mode = "apply"
    else:
        mode = "dry-run"

    report = {
        "file": str(file),
        "entries": initial_count,
        "consolidated": consolidated_count,
        "dropped": dropped_count,
        "decayed": decayed_count,
        "below_floor": below_floor,
        "merges": merges,
        "mode": mode,
    }

    return report


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Memory decay and consolidation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Decay formula: adjusted = base - (days_since / 365) * rate, floored at floor.
Consolidation: lexical Jaccard dedup within entity_scope + type groups.
Default: dry-run (report only). Pass --apply to write changes.
        """,
    )

    # Compute default file path
    repo_root = Path(__file__).resolve().parent.parent
    default_file = repo_root / ".ai" / "memory" / "facts.jsonl"

    parser.add_argument("--file", type=str, default=str(default_file))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes back (default: dry-run)",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--decay-only",
        action="store_true",
        help="Run decay only (skip consolidation)",
    )
    mode_group.add_argument(
        "--consolidate-only",
        action="store_true",
        help="Run consolidation only (skip decay)",
    )

    parser.add_argument(
        "--threshold",
        type=float,
        default=0.6,
        help="Jaccard threshold for consolidation (default: 0.6)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=0.1,
        help="Decay rate (default: 0.1)",
    )
    parser.add_argument(
        "--floor",
        type=float,
        default=0.1,
        help="Confidence floor (default: 0.1)",
    )

    args = parser.parse_args(argv)

    # Determine which modes to run
    do_decay = not args.consolidate_only
    do_consolidate = not args.decay_only

    report = run(
        file=args.file,
        apply=args.apply,
        do_decay=do_decay,
        do_consolidate=do_consolidate,
        threshold=args.threshold,
        rate=args.rate,
        floor=args.floor,
    )

    # Print summary to stderr
    summary = (
        f"[memory-decay] file={report['file']} entries={report['entries']} "
        f"consolidated={report['consolidated']}(-{report['dropped']} dropped) "
        f"decayed={report['decayed']} below_floor={report['below_floor']} "
        f"mode={report['mode']}"
    )
    print(summary, file=sys.stderr)

    # In dry-run, print merge records
    if report["mode"] == "dry-run" and report["merges"]:
        print(f"\n[memory-decay] {len(report['merges'])} consolidation merges:", file=sys.stderr)
        for merge in report["merges"]:
            print(
                f"  kept={merge['kept_id']} cluster_size={merge['cluster_size']} "
                f"dropped={merge['dropped_ids']}",
                file=sys.stderr,
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
