"""Offline Linux source-only execution; never a v1 PASS or admission receipt.

Trusted owner preparation must independently review/pin the profile digest,
source/base/policy IDs and runtime bundle. A candidate's own digest is not trust.
Only the fixed bubblewrap adapter below may report closure. No input directory
is mounted: verified source objects/runtime files become sealed memory files.
"""
from __future__ import annotations

from contextlib import ExitStack
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Mapping, TypedDict

from . import source_projection as projection

SCHEMA = "source-execution-profile/v1"
MAX_PROFILE = 65536
MAX_RUNTIME = 128 * 1024 * 1024
ADAPTER = Path("/usr/bin/bwrap")


class ExecutionError(ValueError):
    """Fixed categories only; never include command output or private paths."""


def _require(condition: object, code: str) -> None:
    if not condition:
        raise ExecutionError(code)


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _path(value: object, prefixes: tuple[str, ...]) -> str:
    _require(isinstance(value, str), "invalid_runtime_path")
    assert isinstance(value, str)
    _require(len(value) <= 500 and value.startswith(prefixes)
             and all(part not in {"", ".", ".."} for part in value.split("/"))
             and re.fullmatch(r"[A-Za-z0-9_./+-]+", value), "invalid_runtime_path")
    return value


def validate_profile(raw: bytes, *, expected_sha256: str, selection: projection.Selection,
                     repository: str, policy_sha256: str) -> Mapping:
    """The expected digest is a trusted preparation input, never candidate trust."""
    _require(type(raw) is bytes and len(raw) <= MAX_PROFILE and _digest(expected_sha256)
             and hashlib.sha256(raw).hexdigest() == expected_sha256, "profile_digest_changed")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "invalid_profile")
            result[key] = value
        return result
    try:
        profile = json.loads(raw, object_pairs_hook=unique)
        _require(isinstance(profile, dict) and set(profile) == {
            "schema", "id", "repository", "source_sha", "source_tree", "base_sha", "base_tree",
            "compiled_policy_sha256", "adapter_sha256", "argv", "runtime_files",
            "timeout_seconds", "max_output_bytes"}, "invalid_profile")
        _require(profile["schema"] == SCHEMA and isinstance(profile["id"], str)
                 and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", profile["id"]), "invalid_profile")
        _require(isinstance(selection, projection.Selection), "invalid_selection")
        selection.algorithm()
        expected = (repository, selection.source, selection.tree, selection.base,
                    selection.base_tree, policy_sha256)
        actual = tuple(profile[key] for key in ("repository", "source_sha", "source_tree", "base_sha",
                                                "base_tree", "compiled_policy_sha256"))
        _require(actual == expected and _digest(policy_sha256), "profile_source_binding_changed")
        _require(_digest(profile["adapter_sha256"]), "invalid_adapter_pin")
        argv = profile["argv"]
        _require(isinstance(argv, list) and 1 <= len(argv) <= 64
                 and all(isinstance(arg, str) and 0 < len(arg.encode()) <= 4096
                         and not any(ord(char) < 32 or ord(char) == 127 for char in arg) for arg in argv),
                 "invalid_argv")
        runtime = profile["runtime_files"]
        _require(isinstance(runtime, list) and 1 <= len(runtime) <= 128, "invalid_runtime")
        names = set()
        for item in runtime:
            _require(isinstance(item, dict) and set(item) == {"path", "sha256", "executable"}
                     and type(item["executable"]) is bool and _digest(item["sha256"]), "invalid_runtime")
            name = _path(item["path"], ("bin/", "lib/", "lib64/"))
            _require(name not in names, "duplicate_runtime_path")
            names.add(name)
        _require(any(argv[0] == "/" + item["path"] and item["executable"] for item in runtime),
                 "command_not_pinned_runtime")
        _require(type(profile["timeout_seconds"]) in {float, int}
                 and math.isfinite(profile["timeout_seconds"]) and 0 < profile["timeout_seconds"] <= 300
                 and type(profile["max_output_bytes"]) is int and 1 <= profile["max_output_bytes"] <= 1048576,
                 "invalid_execution_budget")
        return profile
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, OverflowError, projection.ProjectionError) as exc:
        raise ExecutionError("invalid_profile") from exc


def _sealed(stack: ExitStack, value: bytes) -> int:
    fd = os.memfd_create("source-execution", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    stack.callback(os.close, fd)
    offset = 0
    while offset < len(value):
        offset += os.write(fd, value[offset:])
    # Linux UAPI fcntl.h values; some supported Python builds omit names.
    # The adapter is Linux-only and an actual failed seal always fails closed.
    fcntl.fcntl(fd, getattr(fcntl, "F_ADD_SEALS", 1033), 0x01 | 0x02 | 0x04 | 0x08)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _capture_file(root_fd: int, name: str, expected: str, *, adapter: bool = False) -> bytes:
    """Read only an explicitly pinned file through no-follow directory anchors."""
    with ExitStack() as stack:
        directory = root_fd
        for component in name.split("/")[:-1]:
            directory = os.open(component, projection._DIRECTORY, dir_fd=directory)
            stack.callback(os.close, directory)
        fd = os.open(name.split("/")[-1], projection._FILE, dir_fd=directory)
        stack.callback(os.close, fd)
        before = projection._details(fd)
        _require(stat.S_ISREG(before[2]) and before[4] == 1 and not before[2] & 0o022
                 and before[3] == (0 if adapter else os.getuid()) and before[5] <= MAX_RUNTIME,
                 "unsafe_runtime_file")
        raw = bytearray()
        while len(raw) <= before[5]:
            part = os.read(fd, min(65536, before[5] + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        _require(len(raw) == before[5] and projection._details(fd) == before
                 and hashlib.sha256(raw).hexdigest() == expected, "runtime_digest_changed")
        return bytes(raw)


class _Outcome(TypedDict):
    outcome: str
    output_bytes: int
    retained_output_bytes: int
    elapsed_ms: int
    returncode: int | None


def _collect(argv: list[str], fds: tuple[int, ...], timeout: float, output_limit: int) -> _Outcome:
    """Count/discard output; kill only this created process group on every exit."""
    start = time.monotonic()
    result: _Outcome = {"outcome": "failed", "output_bytes": 0, "retained_output_bytes": 0,
              "elapsed_ms": 0, "returncode": None}
    process = subprocess.Popen(argv, pass_fds=fds, env={}, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True, close_fds=True)
    try:
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            exited = None
            # Observe without reaping: the reserved leader PID prevents a
            # recycled process group from being signalled during cleanup.
            while selector.get_map() or exited is None:
                if exited is None:
                    exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if not selector.get_map() and exited is not None:
                    break
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    result["outcome"] = "timeout"
                    return result
                for key, _ in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    result["output_bytes"] += len(chunk)
                    if result["output_bytes"] > output_limit:
                        result["outcome"] = "output-limit"
                        return result
            assert exited is not None
            result["returncode"] = exited.si_status if exited.si_code == os.CLD_EXITED else -exited.si_status
            result["outcome"] = "passed" if result["returncode"] == 0 else "command-or-isolation-failed"
            return result
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise ExecutionError("owned_group_cleanup_failed") from exc
        finally:
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ExecutionError("owned_group_cleanup_failed") from exc
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                result["elapsed_ms"] = round((time.monotonic() - start) * 1000)


def execute_committed_source(repository: Path, *, selection: projection.Selection, repository_name: str,
                             compiled_policy_sha256: str, destination_parent: Path,
                             profile_bytes: bytes, profile_sha256: str, runtime_root: Path) -> dict:
    """Execute exactly the trusted pinned profile; never mount the input checkout.

    Linux/bubblewrap/memfd are mandatory. Any unavailable adapter or failed
    command yields no positive closure; no fallback executes on the host.
    Runtime bundle paths are public reviewed tool bytes, not owner data inputs.
    All outputs are bounded aggregate metadata, never command text/payloads.
    """
    profile = validate_profile(profile_bytes, expected_sha256=profile_sha256, selection=selection,
                               repository=repository_name, policy_sha256=compiled_policy_sha256)
    _require(sys.platform == "linux" and hasattr(os, "memfd_create") and Path("/proc/self/fd").is_dir(),
             "isolation_adapter_unavailable")
    try:
        projected = projection.project_committed_source(repository, selection=selection,
            repository_name=repository_name, compiled_policy_sha256=compiled_policy_sha256,
            destination_parent=destination_parent)
        with ExitStack() as stack:
            # Adapter is also captured/sealed before exec, eliminating path swaps.
            adapter_root = os.open("/", projection._DIRECTORY)
            stack.callback(os.close, adapter_root)
            adapter = _sealed(stack, _capture_file(adapter_root, str(ADAPTER)[1:],
                                                   profile["adapter_sha256"], adapter=True))
            root = Path(runtime_root)
            _require(root.is_absolute() and ".." not in root.parts, "invalid_runtime_root")
            runtime_fd = stack.enter_context(projection._directory(root))
            _require(not os.fstat(runtime_fd).st_mode & 0o077, "runtime_root_not_private")
            files = []
            total = 0
            for item in profile["runtime_files"]:
                raw = _capture_file(runtime_fd, item["path"], item["sha256"])
                total += len(raw)
                _require(total <= MAX_RUNTIME, "runtime_budget_exceeded")
                files.append(("/" + item["path"], _sealed(stack, raw), item["executable"]))
            # Read only the materializer's verified, admitted Git object store;
            # never re-open mutable projected working payloads as authority.
            metadata = stack.enter_context(projection._directory(projected.metadata))
            objects = os.open("objects", projection._DIRECTORY, dir_fd=metadata)
            stack.callback(os.close, objects)
            store = projection._LooseObjects(objects, selection.algorithm())
            tree, _ = projection._commit(store, selection.source)
            _require(tree == selection.tree, "projected_tree_changed")
            entries = projection._tree(store, tree)
            policy = projection._policy(store.read(entries[projection.POLICY_PATH][1], "blob"),
                                        compiled_policy_sha256, repository_name)
            admitted, _ = projection._classify(entries, policy)
            for path, (mode, oid) in admitted.items():
                files.append(("/source/" + path, _sealed(stack, store.read(oid, "blob")), mode == "100755"))
            command = [f"/proc/self/fd/{adapter}", "--unshare-all", "--unshare-user", "--die-with-parent", "--new-session",
                       "--disable-userns", "--cap-drop", "ALL", "--clearenv", "--tmpfs", "/", "--tmpfs", "/source"]
            directories = {"/source", "/tmp", "/dev"}
            for path, _, _ in files:
                directories.update(str(parent) for parent in Path(path).parents if str(parent) != "/")
            for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
                command.extend(["--dir", directory])
            command.extend(["--dev", "/dev", "--tmpfs", "/tmp", "--chdir", "/source",
                            "--setenv", "PATH", "/bin", "--setenv", "HOME", "/tmp"])
            for path, fd, executable in files:
                command.extend(["--perms", "0555" if executable else "0444", "--ro-bind-data", str(fd), path])
            command.extend(["--remount-ro", "/source", "--remount-ro", "/", "--", *profile["argv"]])
            outcome = _collect(command, tuple([adapter] + [fd for _, fd, _ in files]),
                               profile["timeout_seconds"], profile["max_output_bytes"])
        return {"schema": "source-execution/v1", "scope": "committed-source-only", **outcome,
                "repository": repository_name, "source_sha": selection.source, "source_tree": selection.tree,
                "base_sha": selection.base, "base_tree": selection.base_tree,
                "compiled_policy_sha256": compiled_policy_sha256, "profile_sha256": profile_sha256,
                "profile_id": profile["id"], "adapter_sha256": profile["adapter_sha256"],
                "source_execution_closure_verified": outcome["outcome"] == "passed",
                "full_repository_qualified": False, "native_gate_qualified": False,
                "admission_authorized": False, "production_authorized": False, "cache_reusable": False}
    except ExecutionError:
        raise
    except (OSError, subprocess.SubprocessError, projection.ProjectionError, KeyError) as exc:
        raise ExecutionError("execution_input_or_adapter_unavailable") from exc
