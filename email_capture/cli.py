"""Command-line interface for hermetic email capture."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .core import CaptureError, Profile, _read_registry, advance_registry_cursor, allocate, assert_messages, atomic_write_json, await_messages, backend, receipt, release_allocation


def read_json(value: str) -> dict | list:
    """Read inline JSON or an @file reference."""
    return json.loads(Path(value[1:]).read_text() if value.startswith("@") else value)


def emit(value: object) -> None:
    """Write a compact metadata-only JSON record to stdout."""
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def _private_input_path(reference: str) -> Path | None:
    """Return and validate an @file path when mutation is required."""
    if not reference.startswith("@"):
        return None
    path = Path(reference[1:])
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise CaptureError("AUTHORIZATION_DENIED", "allocation file must be mode 0600 and not a symlink")
    return path


def _verified_allocation(reference: str, *, releasing: bool = False) -> dict:
    """Read an --allocation payload and require an exact live registry record.

    A caller holding the capture token could otherwise name another
    allocation's recipient and read or purge its mail — the registry is the
    authority on which allocations exist and what they look like.
    """
    allocation = read_json(reference)
    if not isinstance(allocation, dict):
        raise CaptureError("CONFIG_INVALID", "allocation must be a JSON object")
    live = None
    for record in _read_registry().values():
        if isinstance(record, dict) and record.get("allocation_id") == allocation.get("allocation_id"):
            live = record
            break
    if live is None or live.get("recipient") != allocation.get("recipient") or live.get("scope") != allocation.get("scope"):
        raise CaptureError("AUTHORIZATION_DENIED", "allocation does not match a live registry record")
    if live.get("released"):
        # A released tombstone is verifiable proof the earlier release's
        # purge completed — a retry is a no-op success, never a second purge
        # of caller-supplied fields.
        if releasing:
            raise CaptureError("ALLOCATION_RETIRED", "allocation already released")
        raise CaptureError("AUTHORIZATION_DENIED", "allocation does not match a live registry record")
    # Release stays reachable on expired or pending_purge records — an expired
    # allocation still holds mail to purge, and pending_purge marks a release
    # that must be retried, not a live claim on the mailbox.
    if not releasing and (float(live.get("expires_at", 0)) <= time.time() or live.get("pending_purge")):
        raise CaptureError("AUTHORIZATION_DENIED", "allocation does not match a live registry record")
    # Backends trust every field on the returned object (recipient, cursor,
    # allocation_id) — return the registry record itself so caller-controlled
    # extras on the submitted payload cannot ride along on a verified identity.
    # The registry's cursor is also authoritative: a forged payload cursor
    # would otherwise rewind or skip replay position.
    return live


def main(argv: list[str] | None = None) -> int:
    """Dispatch one email-capture operation."""
    parser = argparse.ArgumentParser(prog="email-capture")
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)
    allocate_parser = commands.add_parser("allocate")
    allocate_parser.add_argument("--request", required=True)
    allocate_parser.add_argument("--output", required=True)
    await_parser = commands.add_parser("await")
    await_parser.add_argument("--allocation", required=True)
    await_parser.add_argument("--timeout", type=float, default=30)
    await_parser.add_argument("--count", type=int, default=1)
    await_parser.add_argument("--not-before")
    await_parser.add_argument("--output", required=True)
    assert_parser = commands.add_parser("assert")
    assert_parser.add_argument("--messages", required=True)
    assert_parser.add_argument("--rules", required=True)
    release_parser = commands.add_parser("release")
    release_parser.add_argument("--allocation", required=True)
    commands.add_parser("capabilities")
    commands.add_parser("doctor")
    commands.add_parser("canary")
    arguments = parser.parse_args(argv)
    started = time.monotonic()
    try:
        profile = Profile.load(arguments.profile)
        selected_backend = backend(profile)
        if arguments.command == "allocate":
            allocation = allocate(read_json(arguments.request), profile)
            atomic_write_json(Path(arguments.output), allocation)
            emit(receipt("allocate", "passed", started, mode=profile.mode))
        elif arguments.command == "await":
            allocation_path = _private_input_path(arguments.allocation)
            allocation = _verified_allocation(arguments.allocation)
            messages, cursor = await_messages(selected_backend, allocation, arguments.timeout, arguments.count, arguments.not_before)
            atomic_write_json(Path(arguments.output), messages)
            advance_registry_cursor(str(allocation.get("allocation_id", "")), cursor)
            if allocation_path:
                atomic_write_json(allocation_path, allocation)
            emit(receipt("await", "passed", started, mode=profile.mode, message_count=len(messages), cursor=cursor))
        elif arguments.command == "assert":
            emit(assert_messages(read_json(arguments.messages), read_json(arguments.rules)))
        elif arguments.command == "release":
            _private_input_path(arguments.allocation)
            try:
                verified = _verified_allocation(arguments.allocation, releasing=True)
            except CaptureError as error:
                if error.code == "ALLOCATION_RETIRED":
                    emit(receipt("release", "passed", started, mode=profile.mode, cleanup_state="already_released"))
                    return 0
                raise
            release_allocation(selected_backend, verified)
            emit(receipt("release", "passed", started, mode=profile.mode, cleanup_state="complete"))
        elif arguments.command == "capabilities":
            emit(selected_backend.capabilities())
        elif arguments.command == "canary":
            if profile.mode != "hosted":
                raise CaptureError("CAPABILITY_UNSUPPORTED", "canary requires hosted mode")
            healthy = selected_backend.health()
            emit(receipt("canary", "passed" if healthy else "failed", started, mode=profile.mode, fidelity=profile.fidelity))
            return 0 if healthy else 2
        else:
            healthy = selected_backend.health()
            emit(receipt("doctor", "passed" if healthy else "failed", started, mode=profile.mode, fidelity=profile.fidelity))
            return 0 if healthy else 2
        return 0
    except CaptureError as error:
        emit(error.as_dict())
        return 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        emit(CaptureError("CONFIG_INVALID", type(error).__name__).as_dict())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
