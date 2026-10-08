# Bounded command (Linux only)

Run `python3 bin/bounded-command.py TIMEOUT_SECONDS COMMAND ARGS...`. Finite positive timeout: monotonic clock from before setup.
No shell; command stdin/stderr go to `/dev/null`. Only stdlib and Linux `/proc`/`prctl` needed.

Root cause: shell substitution removes raw NUL; process-group cleanup misses detached processes; lost waits hide child failure.
Subreaper ownership precedes spawn; each cleanup kill/reap wave adopts `setsid`/double-fork descendants. All adopted children are awaited; command-reaped statuses are unobservable.

Stdout: **4096 publishable bytes** in memory (+one overflow-probe byte), frozen after cleanup. One immutable buffer is checked for actual raw NUL and strict UTF-8.
Publication requires native success and complete cleanup. Not a JSON/schema validator: escaped `\u0000`, duplicate keys, exact enums/schema are callers' responsibility.
Check exit status; never shell-normalize raw output.

Wrapper stderr is sanitized JSON only (never argv/environment/raw command output).
`native_parent` is native status (null if unavailable); `native_descendants` counts observed adopted statuses. Signals use 128 + signal.
`reason`, `cleanup`, `signal` are context; `selected` is not necessarily native. Precedence: nonzero native parent, smallest nonzero native descendant, then 125 incomplete cleanup, 128 + own TERM/INT, 124 deadline, 125 setup/validation/publication failure.
Native 124/137/143/17 stay distinct; a deadline killing the root usually selects native 137, **not** 124.

TERM/INT handlers cover setup through finalization, requesting cleanup without raising through ownership setup.
Cleanup: independent **2-second budget**, SIGKILL, no grace. Total: timeout + 2 seconds plus startup/scheduling/finalization.
No zero exit/output on incomplete cleanup. Wrapper SIGKILL cannot be handled; uninterruptible kernel tasks may survive and cause failure.
Not a hostile-process sandbox: nested subreapers, external supervisors or different PID namespaces can break ownership.
Use only commands within this trust boundary.

The pending-signal snapshot commits publication after cleanup/validation: TERM/INT are deferred only for bounded nonblocking writes and immediate exit; descriptor flags are restored.
Use a draining pipe: 4096 bytes is atomic; full/closed pipes fail without payload bytes. Other sinks can partially write irreversibly. Concurrent users of shared descriptors may briefly observe nonblocking flags. No temp files or consumer/workflow wiring.
