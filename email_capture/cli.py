"""Command-line interface for hermetic email capture."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .core import CaptureError, Profile, allocate, assert_messages, atomic_write_json, await_messages, backend, receipt, release_allocation


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
            allocation = read_json(arguments.allocation)
            messages, cursor = await_messages(selected_backend, allocation, arguments.timeout, arguments.count, arguments.not_before)
            atomic_write_json(Path(arguments.output), messages)
            if allocation_path:
                atomic_write_json(allocation_path, allocation)
            emit(receipt("await", "passed", started, mode=profile.mode, message_count=len(messages), cursor=cursor))
        elif arguments.command == "assert":
            emit(assert_messages(read_json(arguments.messages), read_json(arguments.rules)))
        elif arguments.command == "release":
            _private_input_path(arguments.allocation)
            release_allocation(selected_backend, read_json(arguments.allocation))
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
