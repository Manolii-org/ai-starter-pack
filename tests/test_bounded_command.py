"""Native fork/exec/wait/signal tests; the observer records real kernel waits."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin/bounded-command.py"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper")


def _children_interface_readable():
    try:
        fd = os.open(f"/proc/self/task/{os.getpid()}/children", os.O_RDONLY)
    except OSError:
        return False
    os.close(fd)
    return True


@pytest.fixture(autouse=True)
def _require_children_interface(request):
    # Injected setup failures do not need the kernel file. Every other case
    # here asserts a spawned command, which this helper refuses without it.
    if request.node.originalname == "test_unavailable_child_enumeration_prevents_spawn":
        return
    if not _children_interface_readable():
        pytest.skip("Linux /proc/<pid>/task/<pid>/children is not readable")
OBSERVER = r'''
import atexit, builtins, json, os, runpy, signal, sys, time
log, phase, script, *args = sys.argv[1:]
if phase.startswith("blocked_exec"):
    blocked = {int(sig) for sig in phase.split(":")[1].split(",")}
    signal.pthread_sigmask(signal.SIG_BLOCK, blocked | {signal.SIGUSR1})
def record(row):
    with open(log, "a") as f: f.write(json.dumps(row) + "\n")
fork, wait, install, read = os.fork, os.waitpid, signal.signal, os.read
original_open, original_exit = builtins.open, os._exit
observer_pid = os.getpid()
owned = set()
pending = signal.sigpending
sent = False
def observed_fork():
    pid = fork()
    if pid:
        owned.add(pid)
        record(["root", pid])
    return pid
def observed_wait(*args):
    pid, status = wait(*args)
    if pid:
        owned.discard(pid)
        record(["wait", pid, status])
    return pid, status
def observed_open(path, *args, **kwargs):
    if str(path) == f"/proc/self/task/{os.getpid()}/children":
        if phase == "pre_fork_expire":
            time.sleep(0.2)
        if phase.startswith("proc_setup") or (phase.startswith("proc_runtime") and owned):
            raise OSError(int(phase.split(":")[1]), "injected proc failure")
    return original_open(path, *args, **kwargs)
def final_owned_cleanup():
    for pid in owned:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
    end = time.monotonic() + .5
    while owned and time.monotonic() < end:
        observed_wait(-1, os.WNOHANG)
        time.sleep(.005)
if phase.startswith("proc_") or phase == "pre_fork_expire":
    builtins.open = observed_open
    atexit.register(final_owned_cleanup)
    def observed_exit(status):
        if os.getpid() == observer_pid: final_owned_cleanup()
        original_exit(status)
    os._exit = observed_exit
def observed_install(sig, handler):
    result = install(sig, handler)
    if phase.startswith("early") and sig == signal.SIGINT:
        os.kill(os.getpid(), int(phase.split(":")[1]))
    return result
def observed_read(*args):
    global sent
    data = read(*args)
    if phase.startswith(("exec", "blocked_exec")) and data and not sent:
        sent = True
        record(["signal", time.monotonic()])
        os.kill(os.getpid(), int(phase.split(":")[-1]))
    if phase == "deadline" and data and not sent:
        sent = True
        time.sleep(1)
    if phase == "native_failure" and data and not sent:
        sent = True
        with open(log + ".read", "w") as f: f.write("read")
    return data
def observed_pending():
    result = pending()
    if phase.startswith("commit"): os.kill(os.getpid(), int(phase.split(":")[1]))
    return result
lineage = {"child": 0}
def trace(frame, event, arg):
    if phase == "budget_zero" and event == "call" and frame.f_code.co_name == "cleanup": frame.f_globals["CLEANUP_SECONDS"] = 0
    if (phase.split(":")[0] in {"final", "cleanup"} and event == "call"
            and frame.f_code.co_name == ("finish" if phase.startswith("final") else "cleanup")):
        os.kill(os.getpid(), int(phase.split(":")[1]))
    if (phase == "foreign_lineage" and event == "call" and lineage["child"]
            and frame.f_code.co_name == "cleanup"):
        os.kill(lineage["child"], signal.SIGTERM)
        wait(lineage["child"], 0)
        lineage["child"] = 0
    return trace
if phase == "foreign_lineage":
    lineage["child"] = fork()
    if lineage["child"] == 0:
        grand = fork()
        if grand == 0:
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0); os.dup2(devnull, 1); os.dup2(devnull, 2)
            os.closerange(3, 256)
            time.sleep(30)
            os._exit(0)
        record(["grandchild", grand])
        time.sleep(30)
        os._exit(0)
    record(["foreign", lineage["child"]])
if phase == "foreign_child":
    foreign_pid = fork()
    if foreign_pid == 0:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0); os.dup2(devnull, 1); os.dup2(devnull, 2)
        os.closerange(3, 256)
        time.sleep(30)
        os._exit(0)
    record(["foreign", foreign_pid])
os.fork, os.waitpid, signal.signal = observed_fork, observed_wait, observed_install
os.read = observed_read
signal.sigpending = observed_pending
sys.settrace(trace)
sys.argv = [script, *args]
runpy.run_path(script, run_name="__main__")
'''
FIXTURE = r'''
import os, signal, sys, time
path, mode, status = sys.argv[1:]
def mark():
    with open(path, "a") as f: f.write(str(os.getpid()) + "\n")
mark()
if mode != "root":
    child = os.fork()
    if child == 0:
        os.setsid()
        mark()
        if mode == "double":
            if os.fork(): os._exit(0)
            mark()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if mode == "natural":
            time.sleep(.1)
            os._exit(int(status))
        time.sleep(60)
        os._exit(0)
    # Synchronise recording of detached descendants before root exit.
    target = 3 if mode == "double" else 2
    while len(open(path).read().split()) < target: time.sleep(.001)
    if mode != "live": os._exit(0 if mode != "precedence" else int(status))
if mode == "root":
    os.write(1, b"unpublished")
    os._exit(int(status))
os.write(1, b"running")
time.sleep(60)
'''


def run(tmp_path, code, args=(), timeout=".4", phase="none"):
    log = tmp_path / "waits"
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", OBSERVER, str(log), phase, str(SCRIPT),
         timeout, sys.executable, "-c", code, *map(str, args)],
        capture_output=True, timeout=float(timeout) + 2.5, env={"PATH": os.defpath}, check=False,
    )
    assert time.monotonic() - start < float(timeout) + 2.5
    report = json.loads(result.stderr)
    assert result.returncode == report["selected"]
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    roots = [row[1] for row in rows if row[0] == "root"]
    native = {row[1]: os.waitstatus_to_exitcode(row[2]) for row in rows if row[0] == "wait"}
    mapped = {pid: 128 - status if status < 0 else status for pid, status in native.items()}
    assert report["native_parent"] == (mapped.get(roots[0]) if roots else None)
    counts = {}
    for pid, status in mapped.items():
        if pid not in roots: counts[str(status)] = counts.get(str(status), 0) + 1
    assert report["native_descendants"] == counts
    incomplete = phase == "budget_zero" or phase.startswith("proc_runtime")
    assert report["cleanup"] == ("incomplete" if incomplete else "complete")
    if result.returncode: assert result.stdout == b""
    for pid in native: assert not Path(f"/proc/{pid}").exists()
    return result, report, native


@pytest.mark.parametrize("status", [17, 124, 137, 143])
def test_native_parent_precedence(tmp_path, status):
    pids = tmp_path / "pids"
    result, report, native = run(tmp_path, FIXTURE, [pids, "root", status])
    assert result.returncode == status
    assert list(native.values()) == [status]
    assert report["reason"] == "native_failure"


@pytest.mark.parametrize("mode,status,expected", [
    ("natural", 17, 17), ("natural", 0, 0), ("double", 0, 137),
    ("orphan", 0, 137), ("precedence", 17, 17), ("live", 0, 137),
])
def test_detached_descendants(tmp_path, mode, status, expected):
    pids = tmp_path / "pids"
    result, report, native = run(tmp_path, FIXTURE, [pids, mode, status])
    assert result.returncode == expected
    assert report["native_parent"] == (137 if mode == "live" else 17 if mode == "precedence" else 0)
    descendant = -9 if mode != "natural" else status
    assert descendant in native.values()
    assert len(native) == (3 if mode == "double" else 2)
    for pid in pids.read_text().split(): assert not Path(f"/proc/{pid}").exists()


@pytest.mark.parametrize("payload,expected,reason", [
    (b"valid\n", 0, "success"), (b"x" * 4096, 0, "success"),
    (b"real\0nul", 125, "raw_nul"), (b"\xff", 125, "invalid_utf8"),
    (b"x" * 4097, 125, "stdout_limit"),
    (b'{"v":"\\u0000","v":"other"}', 0, "success"),
])
def test_immutable_bytes(tmp_path, payload, expected, reason):
    result, report, native = run(tmp_path, f"import os; os.write(1, {payload!r})")
    if reason == "stdout_limit" and list(native.values()) == [-9]:
        expected = 137  # Independently observed SIGKILL before native root exit.
        assert report["native_parent"] == 137
    else:
        assert list(native.values()) == [0]
    assert result.returncode == expected
    assert report["reason"] == reason
    if not expected: assert result.stdout == payload


@pytest.mark.parametrize("phase", ["early", "final"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_setup_and_finalizer_signals(tmp_path, phase, sig):
    result, report, native = run(tmp_path, "print('withheld')", phase=f"{phase}:{int(sig)}")
    assert result.returncode == 128 + sig
    assert report["reason"] == "signal"
    assert report["signal"] == sig
    assert list(native.values()) == ([] if phase == "early" else [0])


def test_stderr_discard_and_no_argv_diagnostics(tmp_path):
    result, report, _ = run(tmp_path, "import os; os.write(2,b'RAW-DIAGNOSTIC'); os._exit(17)")
    assert result.returncode == 17
    assert b"RAW-DIAGNOSTIC" not in result.stderr
    assert set(report) == {"reason", "native_parent", "native_descendants", "selected", "cleanup", "signal"}


@pytest.mark.parametrize("blocking,status", [(True, 0), (False, 0), (True, 17), (False, 17)])
def test_direct_cli(tmp_path, blocking, status):
    with (tmp_path / "out").open("w+b") as out, (tmp_path / "err").open("w+b") as err:
        for f in (out, err): os.set_blocking(f.fileno(), blocking)
        result = subprocess.run([sys.executable, str(SCRIPT), "1", sys.executable,
                                 "-c", f"import sys; print('ok'); sys.exit({status})"], stdout=out, stderr=err,
                                timeout=3.5, env={"PATH": os.defpath}, check=False)
        assert [os.get_blocking(f.fileno()) for f in (out, err)] == [blocking, blocking]
    assert result.returncode == status
    assert (tmp_path / "out").read_bytes() == (b"ok\n" if status == 0 else b"")
    assert json.loads((tmp_path / "err").read_text())["native_parent"] == status


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_during_descendant_finalization(tmp_path, sig):
    pids = tmp_path / "pids"
    result, report, native = run(tmp_path, FIXTURE, [pids, "double", 0], phase=f"cleanup:{int(sig)}")
    assert result.returncode == 137
    assert report["native_parent"] == 0 and report["signal"] == sig
    assert -9 in native.values()
    for pid in pids.read_text().split(): assert not Path(f"/proc/{pid}").exists()


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM])
def test_actual_native_parent_signal(tmp_path, sig):
    result, report, native = run(tmp_path, f"import os; os.kill(os.getpid(), {int(sig)})")
    assert list(native.values()) == [-sig]
    assert result.returncode == report["native_parent"] == 128 + sig


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_own_signal_with_live_tree(tmp_path, sig):
    pids = tmp_path / "pids"
    result, report, native = run(tmp_path, FIXTURE, [pids, "live", 0], phase=f"exec:{int(sig)}")
    assert result.returncode == 137 and report["signal"] == sig
    assert sorted(native.values()) == [-9, -9]
    for pid in pids.read_text().split(): assert not Path(f"/proc/{pid}").exists()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_after_commit_snapshot(tmp_path, sig):
    result, report, native = run(tmp_path, "print('committed')", phase=f"commit:{int(sig)}")
    assert result.returncode == 0 and result.stdout == b"committed\n"
    assert report["signal"] == 0 and list(native.values()) == [0]


def test_incomplete_cleanup_fault_injection(tmp_path):
    result, report, native = run(tmp_path, "print('withheld')", phase="budget_zero")
    assert result.returncode == 125 and report["reason"] == "cleanup_incomplete"
    assert list(native.values()) == [0]


def _reap_survivor(pid):
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 1
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{pid}").exists()


def test_preexisting_child_is_not_reaped_or_signaled(tmp_path):
    result, report, native = run(
        tmp_path, "import os; os.write(1, b'ok')", timeout="1", phase="foreign_child",
    )
    assert result.returncode == 125 and report["reason"] == "setup_failure"
    assert report["native_parent"] is None and native == {} and result.stdout == b""
    rows = [json.loads(line) for line in (tmp_path / "waits").read_text().splitlines()]
    foreign = next(row[1] for row in rows if row[0] == "foreign")
    assert Path(f"/proc/{foreign}").exists()
    _reap_survivor(foreign)


def test_reparented_foreign_grandchild_is_not_signaled(tmp_path):
    result, report, native = run(
        tmp_path, "import os; os.write(1, b'ok')", timeout="1", phase="foreign_lineage",
    )
    assert result.returncode == 125 and report["reason"] == "setup_failure"
    assert report["native_parent"] is None and report["native_descendants"] == {}
    assert native == {} and result.stdout == b""
    rows = [json.loads(line) for line in (tmp_path / "waits").read_text().splitlines()]
    grand = next(row[1] for row in rows if row[0] == "grandchild")
    assert Path(f"/proc/{grand}").exists()
    _reap_survivor(grand)


def test_expired_deadline_before_fork_does_not_spawn(tmp_path):
    marker = tmp_path / "launched"
    code = "import sys; open(sys.argv[1], 'w').write('launched')"
    result, report, native = run(
        tmp_path, code, [marker], timeout="0.05", phase="pre_fork_expire",
    )
    assert result.returncode == 124 and report["reason"] == "deadline"
    assert report["selected"] == 124 and report["cleanup"] == "complete"
    assert report["native_parent"] is None and native == {}
    assert not marker.exists()
    log = tmp_path / "waits"
    assert not log.exists()


@pytest.mark.parametrize("error", [2, 13])
def test_unavailable_child_enumeration_prevents_spawn(tmp_path, error):
    marker = tmp_path / "launched"
    code = "import sys, time; open(sys.argv[1], 'w').write('launched'); time.sleep(60)"
    result, report, native = run(tmp_path, code, [marker], phase=f"proc_setup:{error}")
    assert result.returncode == 125 and report["reason"] == "setup_failure"
    assert report["native_parent"] is None and native == {}
    assert not marker.exists()


@pytest.mark.parametrize("error", [2, 13])
def test_lost_child_enumeration_kills_and_reaps_known_root(tmp_path, error):
    result, report, native = run(
        tmp_path, "import os,time; os.write(1,b'running'); time.sleep(60)",
        phase=f"proc_runtime:{error}",
    )
    assert result.returncode == report["native_parent"] == 137
    assert report["reason"] == "deadline"
    assert list(native.values()) == [-9]


@pytest.mark.parametrize("mode", ["root", "natural"])
@pytest.mark.parametrize("status", [0, 17, 124, 137, 143])
def test_inherited_ignored_sigchld_preserves_native_status(tmp_path, mode, status):
    launcher = (
        "import os,signal,sys; signal.signal(signal.SIGCHLD,signal.SIG_IGN); "
        "signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGCHLD,signal.SIGUSR1}); "
        "os.execv(sys.executable,[sys.executable,*sys.argv[1:]])"
    )
    pids = tmp_path / "pids"
    result = subprocess.run(
        [sys.executable, "-c", launcher, str(SCRIPT), "1", sys.executable,
         "-c", FIXTURE, str(pids), mode, str(status)],
        capture_output=True, timeout=3.5, env={"PATH": os.defpath}, check=False,
    )
    report = json.loads(result.stderr)
    assert result.returncode == report["selected"] == status
    assert report["native_parent"] == (status if mode == "root" else 0)
    assert report["native_descendants"] == ({str(status): 1} if mode == "natural" else {})
    assert report["reason"] == ("native_failure" if status else "success")
    assert report["cleanup"] == "complete"
    assert result.stdout == (b"unpublished" if mode == "root" and status == 0 else b"")
    for pid in pids.read_text().split(): assert not Path(f"/proc/{pid}").exists()


@pytest.mark.parametrize("sig", [signal.SIGPIPE, signal.SIGXFSZ])
def test_exec_restores_python_ignored_native_signals(sig):
    command = ["/bin/sh", "-c", f"kill -{int(sig)} $$; exit 17"]
    direct = subprocess.run(command, capture_output=True, timeout=3.5, check=False)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "1", *command],
        capture_output=True, timeout=3.5, env={"PATH": os.defpath}, check=False,
    )
    report = json.loads(result.stderr)
    assert direct.returncode == -sig
    assert result.returncode == report["native_parent"] == 128 + sig
    assert report["reason"] == "native_failure" and report["cleanup"] == "complete"
    assert result.stdout == b""


@pytest.mark.parametrize("payload", [b"\0", b"\xff"])
def test_deadline_reason_survives_invalid_output(tmp_path, payload):
    result, report, native = run(
        tmp_path, f"import os; os.write(1, {payload!r})", timeout=".5", phase="deadline"
    )
    assert result.returncode == 124
    assert report["reason"] == "deadline"
    assert report["native_parent"] == 0
    assert list(native.values()) == [0]


@pytest.mark.parametrize("payload", [b"\0", b"\xff"])
def test_native_failure_reason_survives_invalid_output(tmp_path, payload):
    code = (
        f"import os, sys, time; os.write(1, {payload!r}); "
        "\nwhile not os.path.exists(sys.argv[1]): time.sleep(.001)"
        "\nos._exit(17)"
    )
    result, report, native = run(
        tmp_path, code, [tmp_path / "waits.read"], timeout="1", phase="native_failure"
    )
    assert result.returncode == 17
    assert report["reason"] == "native_failure"
    assert list(native.values()) == [17]


@pytest.mark.parametrize("blocked", [
    (signal.SIGTERM,), (signal.SIGINT,), (signal.SIGTERM, signal.SIGINT),
])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_inherited_stop_mask_is_cleared_before_execution(tmp_path, blocked, sig):
    mask = tmp_path / "mask"
    code = (
        "import json, os, signal, sys, time; "
        "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set()); "
        "open(sys.argv[1], 'w').write(json.dumps(sorted(map(int, blocked)))); "
        "os.write(1, b'running'); time.sleep(60)"
    )
    initial = ",".join(str(int(item)) for item in blocked)
    result, report, native = run(
        tmp_path, code, [mask], timeout="5", phase=f"blocked_exec:{initial}:{int(sig)}"
    )
    assert json.loads(mask.read_text()) == [int(signal.SIGUSR1)]
    assert result.returncode == 137
    assert report["reason"] == "signal" and report["signal"] == int(sig)
    assert list(native.values()) == [-9]
    sent = next(
        json.loads(line)[1]
        for line in (tmp_path / "waits").read_text().splitlines()
        if json.loads(line)[0] == "signal"
    )
    assert time.monotonic() - sent < 2.5
