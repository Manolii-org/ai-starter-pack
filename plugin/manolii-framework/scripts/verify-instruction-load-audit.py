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


def runtime_root() -> Path:
    override = os.environ.get("INSTRUCTION_LOAD_ROOT")
    if override:
        return Path(override)
    project = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    if project:
        path = Path(project)
        if path.is_dir():
            return path
    return ROOT


def default_receipt_path(root: Path | None = None) -> Path:
    explicit = os.environ.get("INSTRUCTION_LOAD_AUDIT_PATH")
    if explicit:
        return Path(explicit)
    return (root or runtime_root()) / ".ai" / "memory" / "instruction-loads.jsonl"


def load_requirements(root: Path) -> list[tuple[str, str]]:
    canary = root / "config" / "instruction-load-canary.json"
    if not canary.is_file():
        return [("CLAUDE.md", "session_start")]
    try:
        data = json.loads(canary.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid canary JSON in {canary}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"canary must be an object: {canary}")
    items = data.get("requirements")
    if not isinstance(items, list):
        raise ValueError(f"canary requirements must be a list: {canary}")
    rows: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"canary requirement must be an object: {canary}")
        path = str(item.get("path") or "")
        reason = str(item.get("reason") or "")
        if not path or not reason:
            raise ValueError(f"canary requirement needs path and reason: {canary}")
        rows.append((path, reason))
    if not rows:
        raise ValueError(f"canary has no requirements: {canary}")
    return rows


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


def verify(rows: list[dict], requirements: list[tuple[str, str]], *, root: Path | None = None) -> list[str]:
    checkout = root if root is not None else runtime_root()
    failures = []
    for relative, reason in requirements:
        path = checkout / relative
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
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
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
    checkout = args.root if args.root is not None else runtime_root()
    receipt = args.receipt if args.receipt is not None else default_receipt_path(checkout)
    if not args.session_id:
        print(
            "INSTRUCTION-AUDIT-FAIL: no session id (pass --session-id or set "
            "CLAUDE_CODE_SESSION_ID); an unfiltered scan can pass on a stale receipt",
            file=sys.stderr,
        )
        return 1
    try:
        requirements = []
        if args.require:
            for value in args.require:
                if ":" not in value:
                    parser.error("--require must be PATH:REASON")
                requirements.append(tuple(value.rsplit(":", 1)))
        else:
            requirements = load_requirements(checkout)
        rows = load_rows(receipt, args.session_id)
        failures = verify(rows, requirements, root=checkout)
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
