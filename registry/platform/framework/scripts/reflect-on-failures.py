#!/usr/bin/env python3
"""
Reflect on eval failures — categorize by resolution status, skill gaps, redundancy, and regressions.

Reads .ai/evals/failures.jsonl and outputs categorized analysis JSON + summary table.
Usage: python3 scripts/reflect-on-failures.py [--failures PATH] [--output PATH]
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set


def load_failures(failures_path: Path) -> List[Dict[str, Any]]:
    """Load failure records from JSONL file."""
    if not failures_path.exists():
        print(f"Warning: {failures_path} not found — no evals run yet")
        return []

    records = []
    with open(failures_path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"Warning: skipping malformed JSON on line {i}: {exc}", file=sys.stderr)
    return records


def jaccard_words(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity on word tokens."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a and not words_b:
        return 1.0
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return intersection / union if union > 0 else 0.0


def classify_failures(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Classify failures into buckets A, B, C, D."""
    if not records:
        return {
            "buckets": {"A": [], "B": [], "C": [], "D": []},
            "summary": {"total": 0, "passing": 0, "skill_gap": 0, "redundant": 0, "regression": 0},
            "patch_candidates": []
        }

    # Group by eval_id, sorted by timestamp
    by_eval = defaultdict(list)
    for record in records:
        by_eval[record["eval_id"]].append(record)

    for key in by_eval:
        by_eval[key].sort(key=lambda r: r["timestamp"])

    buckets: Dict[str, List[Dict[str, Any]]] = {"A": [], "B": [], "C": [], "D": []}
    processed_eval_ids: Set[str] = set()

    # Bucket A (passing) & D (regression)
    for eval_id, history in by_eval.items():
        latest = history[-1]

        if latest["passed"]:
            buckets["A"].append(latest)
            processed_eval_ids.add(eval_id)
        else:
            # Check for passed → failed transition (regression)
            was_passing = any(r.get("passed", False) for r in history[:-1])
            if was_passing:
                buckets["D"].append(latest)
                processed_eval_ids.add(eval_id)

    # Collect remaining failures (candidates for B and C)
    remaining = [
        by_eval[eval_id][-1]
        for eval_id in by_eval
        if eval_id not in processed_eval_ids and not by_eval[eval_id][-1]["passed"]
    ]

    # Bucket C (redundant) — Jaccard >= 0.85 on actual output
    redundant_ids: Set[str] = set()
    for i, rec_a in enumerate(remaining):
        if rec_a["eval_id"] in redundant_ids:
            continue
        for rec_b in remaining[i + 1:]:
            if rec_b["eval_id"] in redundant_ids:
                continue
            similarity = jaccard_words(rec_a.get("actual", ""), rec_b.get("actual", ""))
            if similarity >= 0.85:
                # Mark the later one as redundant
                redundant_ids.add(rec_b["eval_id"])

    # Bucket B (skill-gap) and C (redundant)
    for record in remaining:
        if record["eval_id"] in redundant_ids:
            buckets["C"].append(record)
        else:
            buckets["B"].append(record)

    # Patch candidates: unique skills from bucket B
    patch_candidates = sorted(set(r["skill"] for r in buckets["B"]))

    summary = {
        "total": len(by_eval),
        "passing": len(buckets["A"]),
        "skill_gap": len(buckets["B"]),
        "redundant": len(buckets["C"]),
        "regression": len(buckets["D"])
    }

    return {
        "buckets": buckets,
        "summary": summary,
        "patch_candidates": patch_candidates
    }


def print_summary(result: Dict[str, Any]) -> None:
    """Print summary table to stdout."""
    summary = result["summary"]
    print("\nEval Failure Reflection Summary")
    print("=" * 60)
    print(f"Total failures analyzed:  {summary['total']}")
    print(f"  A (Passing):            {summary['passing']}")
    print(f"  B (Skill-gap):          {summary['skill_gap']}")
    print(f"  C (Redundant):          {summary['redundant']}")
    print(f"  D (Regression):         {summary['regression']}")
    print()

    if result["patch_candidates"]:
        print("Patch candidates (skills to improve):")
        for skill in result["patch_candidates"]:
            print("  - " + skill)
    else:
        print("No skill-gap failures to address.")
    print()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Reflect on eval failures and categorize by resolution status."
    )
    parser.add_argument(
        "--failures",
        default=".ai/evals/failures.jsonl",
        help="Path to failures JSONL file"
    )
    parser.add_argument(
        "--output",
        default=".ai/evals/reflection.json",
        help="Path to output reflection JSON"
    )

    args = parser.parse_args()
    failures_path = Path(args.failures)
    output_path = Path(args.output)

    # Load and classify
    records = load_failures(failures_path)
    result = classify_failures(records)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    # Print summary
    print_summary(result)
    print("Reflection written to " + str(output_path))


if __name__ == "__main__":
    main()
