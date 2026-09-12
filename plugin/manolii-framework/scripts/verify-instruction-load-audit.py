#!/usr/bin/env python3
"""Verify Claude instruction-load observations against the current checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECEIPT = ROOT / ".ai" / "memory" / "instruction-loads.jsonl"
DEFAULT_CANARY = ROOT / "config" / "instruction-load-canary.json"


def default_requirements(root: Path = ROOT) -> list[tuple[str, str]]:
    canary = root / "config" / "instruction-load-canary.json"
    if canary.is_file():
        data = json.loads(canary.read_text(encoding="utf-8"))
        rows = []
        for item in data.get("requirements") or []:
            path = str(item.get("path") or "")
            reason = str(item.get("reason") or "")
            if path and reason:
                rows.append((path, reason))
        if rows:
            return rows
    return [("CLAUDE.md", "session_start")]


def receipt_sources(path: Path) -> list[Path]:
    """Return the active receipt plus any rotated archives of the same stream.

    ``scripts/rotate-jsonl-receipt.py`` size-rotates the receipt into
    ``.ai/memory/archive/<YYYY-MM>/<stem>.<timestamp>.jsonl`` and recreates an
    empty active file. Reading only the active path would report a
    current-session observation as missing purely because rotation ran, so the
    archives for the same stem are searched too.
    """
    sources = [path]
    archive_root = path.parent / "archive"
    if archive_root.is_dir():
        sources.extend(sorted(archive_root.glob(f"*/{path.stem}.*{path.suffix}")))
    return sources


def load_rows(path: Path, session_id: str) -> list[dict]:
    rows = []
    for source in receipt_sources(path):
        if not source.is_file():
            continue
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {source.name} on line {number}: {exc}") from exc
            if row.get("schema") != "manolii.instruction-load/v1":
                continue
            if row.get("session_id") != session_id:
                continue
            rows.append(row)
    return rows


def verify(rows: list[dict], requirements: list[tuple[str, str]]) -> list[str]:
    failures = []
    for relative, reason in requirements:
        path = ROOT / relative
        expected = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        matches = [
            row for row in rows
            if row.get("file_path") == relative and row.get("load_reason") == reason
        ]
        if not matches:
            failures.append(f"missing {relative}:{reason}")
        elif expected is None:
            failures.append(f"expected file missing from checkout: {relative}")
        elif not any(row.get("observed_sha256") == expected for row in matches):
            failures.append(f"digest mismatch {relative}:{reason}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument(
        "--session-id", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        help="Session whose observations must satisfy the requirements. "
             "Defaults to $CLAUDE_CODE_SESSION_ID; the canary fails without one, "
             "because an unfiltered scan can pass on a stale session's receipt.",
    )
    parser.add_argument(
        "--require", action="append", default=None,
        metavar="PATH:REASON",
    )
    args = parser.parse_args(argv)
    if not args.session_id:
        print(
            "INSTRUCTION-AUDIT-FAIL: no session id (pass --session-id or set "
            "CLAUDE_CODE_SESSION_ID); an unfiltered scan can pass on a stale receipt",
            file=sys.stderr,
        )
        return 1
    requirements = []
    if args.require:
        for value in args.require:
            if ":" not in value:
                parser.error("--require must be PATH:REASON")
            requirements.append(tuple(value.rsplit(":", 1)))
    else:
        requirements = default_requirements()
    try:
        rows = load_rows(args.receipt, args.session_id)
        failures = verify(rows, requirements)
    except (OSError, ValueError) as exc:
        print(f"INSTRUCTION-AUDIT-FAIL: {exc}", file=sys.stderr)
        return 1
    if failures:
        print("INSTRUCTION-AUDIT-FAIL: " + "; ".join(failures), file=sys.stderr)
        return 1
    print(f"INSTRUCTION-AUDIT-PASS observations={len(rows)} requirements={len(requirements)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
