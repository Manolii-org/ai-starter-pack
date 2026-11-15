"""Bounded observations of public adapter subjects; never qualification authority."""
from __future__ import annotations

from contextlib import ExitStack
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time

ADAPTER = Path("/usr/bin/bwrap")
MAX_ADAPTER_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 8192
TIMEOUT_SECONDS = 5
PUBLIC_ARGV = (
    "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
    "--new-session", "--cap-drop", "ALL", "--clearenv",
    "--ro-bind", "/usr/bin/true", "/bin/true",
    "--ro-bind", "/lib/x86_64-linux-gnu/libc.so.6", "/lib/x86_64-linux-gnu/libc.so.6",
    "--ro-bind", "/lib64/ld-linux-x86-64.so.2", "/lib64/ld-linux-x86-64.so.2",
    "--", "/bin/true",
)


class ProbeError(ValueError):
    """Fixed categories only; caller must not serialize exception text."""


def _details(fd):
    value = os.fstat(fd)
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
            value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _read_adapter(fd):
    before = _details(fd)
    if (not stat.S_ISREG(before[2]) or before[3] != 0 or before[5] != 1
            or before[2] & 0o6022 or before[2] & 0o005 != 0o005
            or not 0 < before[6] <= MAX_ADAPTER_BYTES):
        raise ProbeError("unsafe_adapter")
    try:
        capability = os.getxattr(fd, "security.capability")
    except OSError as exc:
        if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
            raise ProbeError("adapter_capabilities_unavailable") from exc
    else:
        if capability:
            raise ProbeError("privileged_adapter")
    raw = bytearray()
    while len(raw) <= before[6]:
        part = os.pread(fd, min(65536, before[6] + 1 - len(raw)), len(raw))
        if not part:
            break
        raw.extend(part)
    if len(raw) != before[6] or _details(fd) != before:
        raise ProbeError("adapter_changed")
    return before, bytes(raw)


def _capture_adapter(stack):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open("/", flags)
    stack.callback(os.close, directory)
    for component in ("usr", "bin"):
        directory = os.open(component, flags, dir_fd=directory)
        stack.callback(os.close, directory)
        info = os.fstat(directory)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ProbeError("unsafe_adapter_directory")
    fd = os.open("bwrap", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory)
    stack.callback(os.close, fd)
    before, raw = _read_adapter(fd)
    return fd, before, raw


def _sealed(stack, raw):
    fd = os.memfd_create("public-adapter-observation", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    stack.callback(os.close, fd)
    offset = 0
    while offset < len(raw):
        written = os.write(fd, raw[offset:])
        if written <= 0:
            raise ProbeError("sealed_copy_incomplete")
        offset += written
    fcntl.fcntl(fd, getattr(fcntl, "F_ADD_SEALS", 1033), 0x0F)
    if fcntl.fcntl(fd, getattr(fcntl, "F_GET_SEALS", 1034)) & 0x0F != 0x0F:
        raise ProbeError("sealed_copy_unavailable")
    if os.pread(fd, len(raw) + 1, 0) != raw:
        raise ProbeError("sealed_copy_changed")
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _category(sample):
    if not sample:
        return "none"
    if b"Permission denied" in sample:
        return "permission_denied"
    if b"Operation not permitted" in sample:
        return "operation_not_permitted"
    return "other_unretained"


def _observe(argv, fds=(), *, timeout=TIMEOUT_SECONDS, output_limit=MAX_OUTPUT_BYTES):
    """Discard output after classification; reserve leader PID through group cleanup."""
    if (type(timeout) not in (float, int) or not math.isfinite(timeout)
            or not 0 < timeout <= TIMEOUT_SECONDS or type(output_limit) is not int
            or not 1 <= output_limit <= MAX_OUTPUT_BYTES):
        raise ProbeError("invalid_observation_budget")
    result = {"returncode": None, "stdout_bytes": 0, "stderr_bytes": 0,
              "error_category": "none", "raw_retained_bytes": 0,
              "timeout": False, "output_limit": False}
    start = time.monotonic()
    try:
        process = subprocess.Popen(argv, pass_fds=fds, env={}, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, close_fds=True)
    except OSError as exc:
        result["error_category"] = ("launch_permission_denied" if exc.errno in (errno.EACCES, errno.EPERM)
                                    else "launch_unavailable")
        return result
    sample = bytearray()
    exited = None
    try:
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "stdout_bytes"), (process.stderr, "stderr_bytes")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or exited is None:
                if exited is None:
                    exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if not selector.get_map() and exited is not None:
                    break
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    result["timeout"] = True
                    break
                for key, _ in selector.select(min(remaining, 0.05)):
                    available = output_limit + 1 - result["stdout_bytes"] - result["stderr_bytes"]
                    if available <= 0:
                        break
                    data = os.read(key.fd, min(4096, available))
                    if not data:
                        selector.unregister(key.fileobj)
                    result[key.data] += len(data)
                    if key.data == "stderr_bytes" and len(sample) < 4096:
                        sample.extend(data[:4096 - len(sample)])
                if result["stdout_bytes"] + result["stderr_bytes"] > output_limit:
                    result["output_limit"] = True
                    break
        if exited is not None:
            result["returncode"] = exited.si_status if exited.si_code == os.CLD_EXITED else -exited.si_status
        result["error_category"] = _category(sample)
    finally:
        sample.clear()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            result["group_already_exited"] = True
        except OSError as exc:
            raise ProbeError("owned_probe_cleanup_failed") from exc
        finally:
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ProbeError("owned_probe_cleanup_failed") from exc
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
    return result


def _record(status):
    return {"schema": "source-adapter-observations/v1", "scope": "synthetic-public-elf-only",
            "status": status, "subjects": [], "source_execution_closure_verified": False,
            "full_repository_qualified": False, "native_gate_qualified": False,
            "admission_authorized": False, "production_authorized": False, "cache_reusable": False}


def observations():
    """Three observations; a negative result never selects another execution route."""
    if sys.platform != "linux" or not hasattr(os, "memfd_create") or os.uname().machine != "x86_64":
        raise ProbeError("unsupported_observation_platform")
    result = _record("observations_incomplete")
    with ExitStack() as stack:
        fd, before, raw = _capture_adapter(stack)
        digest = hashlib.sha256(raw).hexdigest()
        sealed = _sealed(stack, raw)
        result["adapter_sha256"] = digest
        result["sealed_adapter_mode"] = stat.S_IMODE(os.fstat(sealed).st_mode)
        result["sealed_adapter_seals"] = fcntl.fcntl(sealed, getattr(fcntl, "F_GET_SEALS", 1034))
        for label, executable, fds in (("installed_path", str(ADAPTER), ()),
                                      ("original_opened_fd", f"/proc/self/fd/{fd}", (fd,)),
                                      ("sealed_copy_fd", f"/proc/self/fd/{sealed}", (sealed,))):
            observed = _observe([executable, *PUBLIC_ARGV], fds)
            result["subjects"].append({"subject": label, **observed})
        after, observed_raw = _read_adapter(fd)
        _, named_before, named_raw = _capture_adapter(stack)
        if before != after or before != named_before or observed_raw != raw or named_raw != raw:
            raise ProbeError("adapter_changed")
        result["adapter_identity_reobserved"] = True
        result["status"] = "observations_complete"
    return result


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print(json.dumps(_record("unsupported_probe_arguments")))
        return 2
    try:
        result = observations()
    except (ProbeError, OSError, ValueError):
        print(json.dumps(_record("capture_or_platform_unavailable")))
        return 2
    # Completion describes observations, including denial. It is not a check
    # PASS or source qualification. The existing 17 executor assertions follow.
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
