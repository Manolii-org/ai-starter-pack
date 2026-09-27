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

# memory-keeper's documented fact schema writes `entry`, not `content`.
_FACT_TEXT_FIELDS = ("content", "entry")

# memory-keeper's pattern schema: `pattern` + `context` + `example` —
# distinct from the /learn problem/solution/rule shape.
_KEEPER_PATTERN_FIELDS = ("pattern", "context", "example")


def _comparable_text(row: dict) -> str:
    """Text used for dedup comparison across supported memory schemas.

    facts use `content` (or the memory-keeper legacy `entry`); patterns
    (written by /learn) carry problem/solution/rule; memory-keeper
    patterns carry pattern/context/example. A row with no comparable
    text returns "" and must never be merged — two empty strings would
    read as identical.
    """
    for field in _FACT_TEXT_FIELDS:
        content = row.get(field)
        if isinstance(content, str) and content.strip():
            return content
    parts = [row.get(k, "") for k in _PATTERN_FIELDS + _KEEPER_PATTERN_FIELDS]
    return " ".join(p for p in parts if isinstance(p, str) and p.strip())


def _pattern_axes(row: dict) -> tuple[str, str, str] | None:
    """(context_text, answer_text, schema) for pattern-shaped rows, else None.

    /learn patterns compare `problem` (context) against `solution`+`rule`
    (answer); memory-keeper patterns compare `context` against
    `pattern`+`example`. The context axis keeps distinct problem contexts
    from merging; an empty answer axis can never establish equivalence.
    """
    if isinstance(row.get("problem"), str) and row["problem"].strip() != "":
        answer = " ".join(str(row.get(k, "")) for k in ("solution", "rule"))
        return (row.get("problem", ""), answer, "learn")
    if isinstance(row.get("pattern"), str) and row["pattern"].strip() != "":
        answer = " ".join(str(row.get(k, "")) for k in ("pattern", "example"))
        return (row.get("context", ""), answer, "keeper")
    return None


# Negation tokens: a Jaccard overlap that differs only by negation
# ("flag is enabled" vs "flag is not enabled") must never consolidate —
# the retained row would silently assert the opposite of the dropped one.
_NEGATION_TOKENS = {
    "not", "never", "cannot", "cant", "wont", "dont", "doesnt",
    "didnt", "isnt", "arent", "wasnt", "werent", "shouldnt", "couldnt",
    "mustnt", "without", "disable", "disabled", "disallow",
}


_NEGATION_WORD_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_NEGATION_TOKENS | {"no", "nor", "neither", "isn't", "won't", "can't", "don't", "doesn't", "didn't", "aren't", "wasn't", "weren't", "shouldn't", "couldn't", "mustn't"})) + r")\b"
)


def _negations(text: str) -> set[str]:
    """Negation words from the RAW text — tokenize() drops 2-char words like 'no'."""
    return {m.group(0) for m in _NEGATION_WORD_RE.finditer(text.lower())}


def _same_polarity(text_a: str, text_b: str) -> bool:
    return _negations(text_a) == _negations(text_b)


# Digit-bearing tokens (ports, versions, sizes, IDs) carry values: rows
# whose value tokens differ assert different values — "port 3000" vs
# "port 4000" are not duplicates no matter how lexically similar.
_VALUE_TOKEN_RE = re.compile(r"[\w.-]+")


def _value_tokens(text: str) -> set[str]:
    return {t for t in _VALUE_TOKEN_RE.findall(text.casefold()) if any(c.isdigit() for c in t)}


def _rows_mergeable(a: dict, b: dict, threshold: float) -> bool:
    """Whether two rows are close enough to consolidate.

    Pattern rows must be similar on BOTH axes — the context side
    (`problem` for /learn, `context` for memory-keeper) and the answer
    side (`solution`+`rule` / `pattern`+`example`). Requiring only the
    answer side drops distinct contexts; pooling the context lets a
    shared question dominate and merge different answers.
    Non-pattern rows compare on _comparable_text as before.
    """
    text_a = _comparable_text(a)
    text_b = _comparable_text(b)
    if not text_a or not text_b:
        return False
    if _value_tokens(text_a) != _value_tokens(text_b):
        return False
    if not _same_polarity(text_a, text_b):
        return False
    axes_a = _pattern_axes(a)
    axes_b = _pattern_axes(b)
    if axes_a is None and axes_b is None:
        if text_a == text_b:
            return True
        toks_a, toks_b = tokenize(text_a), tokenize(text_b)
        if sorted(toks_a) == sorted(toks_b):
            # Identical token multiset in a different order — a reversed
            # relationship ("A uses B" vs "B uses A"), not a paraphrase.
            return False
        set_a, set_b = set(toks_a), set(toks_b)
        if not (set_a <= set_b or set_b <= set_a):
            # Neither claim contains the other: an in-place token substitution
            # ("primary database" → "replica database") can reach jaccard
            # threshold yet assert a different fact — never auto-merge.
            return False
        return jaccard(toks_a, toks_b) >= threshold
    if axes_a is None or axes_b is None or axes_a[2] != axes_b[2]:
        # A pattern vs a fact row, or two different pattern schemas, is
        # never a safe merge — shared vocabulary doesn't mean same claim.
        return False
    ctx_a, ctx_b = tokenize(axes_a[0]), tokenize(axes_b[0])
    if not ctx_a or not ctx_b:
        # Context too short to tokenize — compare raw text instead of
        # letting jaccard(∅, ∅) report a perfect match.
        if axes_a[0].strip().casefold() != axes_b[0].strip().casefold():
            return False
    elif jaccard(ctx_a, ctx_b) < threshold:
        return False
    ans_a, ans_b = axes_a[1].strip(), axes_b[1].strip()
    # An unanswered pattern has no answer to establish equivalence with —
    # jaccard(∅, ∅) would return 1.0 and merge them on context alone.
    if not ans_a or not ans_b:
        return False
    set_a, set_b = tokenize(ans_a), tokenize(ans_b)
    if not (set_a <= set_b or set_b <= set_a):
        # Same substitution rule as non-pattern rows: answers differing in a
        # content token on both sides ("primary" vs "replica" database) assert
        # different advice — high overlap alone is not equivalence.
        return False
    return jaccard(set_a, set_b) >= threshold


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


_CATEGORICAL_CONFIDENCE = {"high": 0.9, "medium": 0.6, "low": 0.3}


def _confidence_value(raw) -> float:
    """Numeric confidence for decay.

    memory-keeper writes categorical values ("high"/"medium"/"low") per its
    documented schema; plain rows may carry floats or numeric strings.
    Unrecognized values fall back to 1.0 rather than crashing the dry-run.
    """
    if isinstance(raw, bool):
        return 1.0
    if isinstance(raw, (int, float)):
        # Persisted scores are on a 0–1 scale; clamp out-of-range input rather
        # than letting it propagate into adjusted_confidence.
        return min(1.0, max(0.0, float(raw)))
    if isinstance(raw, str):
        lowered = raw.strip().casefold()
        if lowered in _CATEGORICAL_CONFIDENCE:
            return _CATEGORICAL_CONFIDENCE[lowered]
        try:
            return min(1.0, max(0.0, float(lowered)))
        except ValueError:
            pass
    print(f"[memory-decay] unrecognized confidence {raw!r} — treating as 1.0", file=sys.stderr)
    return 1.0


_CONFIDENCE_LABELS = [(0.8, "high"), (0.45, "medium"), (0.0, "low")]


def _confidence_label(value: float) -> str:
    """Map a merged numeric score back to the fact schema's categorical scale."""
    for cutoff, label in _CONFIDENCE_LABELS:
        if value >= cutoff:
            return label
    return "low"


def apply_decay(
    rows: list[dict], now: datetime, rate: float = 0.1, floor: float = 0.1
) -> list[dict]:
    """Apply decay: set last_seen default, add adjusted_confidence field."""
    for row in rows:
        # memory-keeper facts carry `date` rather than created/last_seen
        # (its documented schema) — fall back to it or they never age.
        if "last_seen" not in row:
            if "created" in row:
                row["last_seen"] = row["created"]
            elif "date" in row:
                row["last_seen"] = row["date"]
        ts = row.get("last_seen") or row.get("created") or row.get("date")
        days = _days_since(ts, now)
        base = _confidence_value(row.get("merged_confidence", row.get("confidence", 1.0)))
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

    # Group by (entity_scope, type, category). memory-keeper facts carry
    # `category` instead of entity_scope/type — including it keeps a
    # 'deployment' fact and an 'incident' fact with similar text from merging
    # into one another.
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        scope = row.get("entity_scope", "unknown")
        type_ = row.get("type", "unknown")
        key = (scope, type_, str(row.get("category", "unknown")))
        if key not in groups:
            groups[key] = []
        groups[key].append(row)

    consolidated = []
    merges = []

    for (scope, type_, _category), group in groups.items():
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
                dt = _parse_ts(r.get("created") or r.get("date"))
                return dt.timestamp() if dt else float("inf")

            # Canonical = most content-complete claim first (largest token
            # set), then highest confidence, then earliest created. Subset
            # merges are allowed because the longer claim carries strictly
            # more information — picking the shorter one on confidence alone
            # would silently drop its qualifiers.
            canonical = max(
                cluster,
                key=lambda r: (
                    len(tokenize(_comparable_text(r))),
                    _confidence_value(r.get("confidence", 1.0)),
                    -_created_ts(r),
                ),
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

            # Confidence: min(0.95, max_conf + 0.05 * (cluster_size - 1)).
            # When members used the categorical fact schema the merged value
            # is written back as a category label (the schema field stays a
            # label); the precise numeric score lands in merged_confidence.
            max_conf = max(_confidence_value(r.get("confidence", 1.0)) for r in cluster)
            merged_conf = max(max_conf, min(0.95, max_conf + 0.05 * (len(cluster) - 1)))
            if all(isinstance(r.get("confidence"), str) for r in cluster):
                canonical["confidence"] = _confidence_label(merged_conf)
                canonical["merged_confidence"] = round(merged_conf, 3)
            else:
                canonical["confidence"] = merged_conf

            # last_seen = most recent actual member sighting. A merge is
            # bookkeeping, not an observation — stamping `now` would reset the
            # decay clock on exactly the stale entries consolidation handles.
            last_seen_candidates = []
            for row in cluster:
                ts = row.get("last_seen") or row.get("created") or row.get("date")
                if ts:
                    parsed = _parse_ts(ts)
                    if parsed:
                        last_seen_candidates.append(parsed)
            if last_seen_candidates:
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

    # Compute default file path. Under a plugin install the script lives in
    # the plugin dir, so resolve the target project first: CLAUDE_PROJECT_DIR
    # when set, else cwd when it looks like a project root, else the
    # script's own repo root (in-tree invocation).
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    cwd = Path.cwd()
    if project_dir:
        repo_root = Path(project_dir).resolve()
    elif (cwd / ".ai" / "memory").is_dir() or (cwd / ".git").exists():
        repo_root = cwd
    else:
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

    if not 0.0 <= args.threshold <= 1.0:
        parser.error(f"--threshold must be within [0, 1], got {args.threshold}")
    if not 0.0 <= args.rate <= 1.0:
        parser.error(f"--rate must be within [0, 1], got {args.rate}")
    if not 0.0 <= args.floor <= 1.0:
        parser.error(f"--floor must be within [0, 1], got {args.floor}")

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
