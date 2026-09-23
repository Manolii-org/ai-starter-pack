#!/usr/bin/env python3
"""Size-rotate a local JSONL receipt using a stable sidecar lock.

Portable retention for instruction-load observations. This is not master's
KL memory rollup: it does not classify failures or write capture buffers.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAX_BYTES = 256 * 1024


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


def default_receipt_path() -> Path:
    explicit = os.environ.get("INSTRUCTION_LOAD_AUDIT_PATH")
    if explicit:
        return Path(explicit)
    return runtime_root() / ".ai" / "memory" / "instruction-loads.jsonl"


def _archive_destination(source: Path) -> Path:
    now = datetime.now(timezone.utc)
    dest_dir = source.parent / "archive" / now.strftime("%Y-%m")
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"{source.stem}.{ts}{source.suffix}"
    if not dest.exists():
        return dest
    suffix = 1
    while True:
        candidate = dest_dir / f"{source.stem}.{ts}.{suffix}{source.suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


def rotate_file(source: Path, *, max_bytes: int, dry_run: bool) -> Path | None:
    if not source.exists():
        return None
    size = source.stat().st_size
    if size <= max_bytes:
        return None
    dest = _archive_destination(source)
    if dry_run:
        print(f"dry-run: would move {source} ({size} B) -> {dest}", file=sys.stderr)
        return dest
    lock_path = source.with_name(source.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if not source.exists() or source.stat().st_size <= max_bytes:
                return None
            dest = _archive_destination(source)
            source.replace(dest)
            source.touch()
            return dest
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=None)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    source = args.path if args.path is not None else default_receipt_path()
    try:
        rotate_file(source, max_bytes=args.max_bytes, dry_run=args.dry_run)
    except OSError as exc:
        print(f"[rotate-jsonl-receipt] {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
