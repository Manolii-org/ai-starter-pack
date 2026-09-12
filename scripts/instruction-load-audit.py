#!/usr/bin/env python3
"""Record what Claude Code actually loaded as instructions, without their content.

The ``InstructionsLoaded`` event is observational and asynchronous.  This handler
therefore writes an append-only receipt rather than pretending it can block a bad
session. Receipts contain an observed content hash for correlation with the working tree
without copying potentially sensitive instructions into telemetry. Because the event is
asynchronous and does not carry the loaded content, the digest is the file's value when
this handler reads it; it is not cryptographic proof of the model's hidden context.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECEIPT = ROOT / ".ai" / "memory" / "instruction-loads.jsonl"


def runtime_root() -> Path:
    override = os.environ.get("INSTRUCTION_LOAD_ROOT")
    return Path(override) if override else ROOT


def sanitise_path(raw_path: object, *, root: Path = ROOT) -> str:
    """Return an auditable path without leaking arbitrary absolute directories."""
    value = str(raw_path or "")
    if not value:
        return ""
    path = Path(value).expanduser().resolve()
    for base, prefix in ((root.resolve(), ""), (Path.home().resolve(), "~/")):
        try:
            return prefix + str(path.relative_to(base))
        except ValueError:
            continue
    path_fingerprint = hashlib.sha256(str(path.parent).encode()).hexdigest()[:12]
    return f"<external:{path_fingerprint}>/{path.name}"


def _observed_digest(raw_path: str, *, root: Path) -> str | None:
    """Hash checkout files only. Out-of-repo targets are fingerprinted by path, not content."""
    if not raw_path:
        return None
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def build_receipt(payload: dict, *, root: Path = ROOT) -> dict:
    raw_path = str(payload.get("file_path") or "")
    digest = _observed_digest(raw_path, root=root)
    relative_path = sanitise_path(raw_path, root=root)
    return {
        "schema": "manolii.instruction-load/v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "session_id": str(payload.get("session_id") or ""),
        "file_path": relative_path,
        "observed_sha256": digest,
        "memory_type": str(payload.get("memory_type") or ""),
        "load_reason": str(payload.get("load_reason") or ""),
        "trigger_file_path": sanitise_path(payload.get("trigger_file_path"), root=root),
        "parent_file_path": sanitise_path(payload.get("parent_file_path"), root=root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the receipt that would be appended without modifying the filesystem.",
    )
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        if payload.get("hook_event_name") != "InstructionsLoaded":
            return 0
        receipt = build_receipt(payload, root=runtime_root())
        rendered = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        if args.dry_run:
            print(rendered)
            return 0
        destination = Path(os.environ.get("INSTRUCTION_LOAD_AUDIT_PATH", DEFAULT_RECEIPT))
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Lock a stable sidecar rather than the receipt inode. Rotation replaces
        # the receipt, so locking the receipt itself can let an appender retain
        # and write through a descriptor for the archived inode.
        lock_path = destination.with_name(destination.name + ".lock")
        with lock_path.open("a", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            with destination.open("a", encoding="utf-8") as handle:
                handle.write(rendered + "\n")
                handle.flush()
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        # Observability must not make Claude Code unusable, but a failed receipt
        # must be visible rather than silently certifying instruction delivery.
        print(f"[INSTRUCTION-LOAD-AUDIT] receipt not written: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
