#!/usr/bin/env python3
"""Linux CLI: TIMEOUT_SECONDS COMMAND ARGS. See docs/contracts/bounded-command.md."""

import ctypes
import json
import math
import os
import signal
import sys
import time

LIMIT = 4096
CLEANUP_SECONDS = 2.0
STOP = {signal.SIGTERM, signal.SIGINT}
received = 0


def interrupted(signum, _frame):
    global received
    received = received or signum


def reap(root, parent, descendants, end):
    while time.monotonic() < end:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return parent, True
        if not pid:
            return parent, False
        code = os.waitstatus_to_exitcode(status)
        code = 128 - code if code < 0 else code
        if pid == root and parent is None:
            parent = code
        else:
            descendants[code] = descendants.get(code, 0) + 1
    return parent, False


def cleanup(root, parent, descendants):
    end = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < end:
        parent, complete = reap(root, parent, descendants, end)
        if complete:
            return parent, True
        # Unreaped direct children cannot recycle PIDs. Killing each wave
        # adopts its children, including double-forked/new-session processes.
        with open(f"/proc/self/task/{os.getpid()}/children") as children:
            for pid in children.read(65536).rsplit(" ", 1)[0].split():
                if time.monotonic() >= end:
                    break
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(0.005)
    return parent, False


def execute(timeout, command):
    root, parent, descendants = None, None, {}
    reason, complete, data = "success", False, bytearray()
    read_fd = write_fd = null_fd = None
    end = time.monotonic() + timeout
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError
        if received:
            reason = "signal"
        else:
            read_fd, write_fd = os.pipe()
            null_fd = os.open(os.devnull, os.O_RDWR)
            root = os.fork()
            if root == 0:
                try:
                    for sig in STOP:
                        signal.signal(sig, signal.SIG_DFL)
                    os.dup2(null_fd, 0)
                    os.dup2(write_fd, 1)
                    os.dup2(null_fd, 2)
                    for fd in (read_fd, write_fd, null_fd):
                        if fd > 2:
                            os.close(fd)
                    os.execvp(command[0], command)
                except (OSError, ValueError):
                    os._exit(127)
            os.close(write_fd)
            write_fd = None
            os.set_blocking(read_fd, False)
            while True:
                parent, complete = reap(root, parent, descendants, end)
                if received:
                    reason = "signal"
                elif time.monotonic() >= end:
                    reason = "deadline"
                elif parent or any(descendants):
                    reason = "native_failure"
                if reason != "success":
                    break
                chunk = None
                try:
                    chunk = os.read(read_fd, LIMIT + 1 - len(data))
                except BlockingIOError:
                    pass
                if chunk:
                    data.extend(chunk)
                if len(data) > LIMIT:
                    reason = "stdout_limit"
                    break
                if complete and chunk == b"":
                    break
                time.sleep(0.005)
    except (OSError, ValueError):
        reason = "setup_failure" if root is None else "runtime_failure"
    finally:
        try:
            parent, complete = cleanup(root, parent, descendants)
        except OSError:
            complete = False
        for fd in (read_fd, write_fd, null_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    reason = "runtime_failure"
    payload = bytes(data)
    try:
        payload.decode("utf-8", errors="strict")
        if b"\0" in payload:
            reason = "raw_nul"
    except UnicodeDecodeError:
        reason = "invalid_utf8"
    return parent, descendants, reason, complete, payload


def emit(fd, payload):
    blocking = os.get_blocking(fd)
    try:
        os.set_blocking(fd, False)
        return os.write(fd, payload)
    finally:
        os.set_blocking(fd, blocking)


def finish(parent, descendants, reason, complete, payload):
    # The pending-signal snapshot commits publication: defer signals for this
    # nonblocking write and exit, never during execution or cleanup.
    signal.pthread_sigmask(signal.SIG_BLOCK, STOP)
    pending = signal.sigpending() & STOP
    own_signal = received or (min(pending) if pending else 0)
    if own_signal:
        reason = "signal"
    nonzero = sorted(code for code in descendants if code)
    selected = parent or (nonzero[0] if nonzero else 0)
    if not selected:
        if not complete:
            selected, reason = 125, "cleanup_incomplete"
        elif own_signal:
            selected = 128 + own_signal
        elif reason == "deadline":
            selected = 124
        elif reason != "success" or parent != 0:
            selected = 125
    if selected == 0:
        try:
            if emit(1, payload) != len(payload):
                selected, reason = 125, "publication_failure"
        except OSError:
            selected, reason = 125, "publication_failure"
    report = {"reason": reason, "native_parent": parent,
              "native_descendants": descendants, "selected": selected,
              "cleanup": "complete" if complete else "incomplete",
              "signal": own_signal}
    try:
        emit(2, (json.dumps(report, sort_keys=True) + "\n").encode("ascii"))
    except OSError:
        pass
    os._exit(selected)


def main():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, STOP)
    for sig in STOP:
        signal.signal(sig, interrupted)
    signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    try:
        timeout = float(sys.argv[1])
        if not math.isfinite(timeout) or timeout <= 0 or len(sys.argv) < 3:
            raise ValueError
    except (ValueError, IndexError):
        finish(None, {}, "usage", True, b"")
    if sys.platform != "linux":
        finish(None, {}, "unsupported", True, b"")
    finish(*execute(timeout, sys.argv[2:]))


if __name__ == "__main__":
    main()
