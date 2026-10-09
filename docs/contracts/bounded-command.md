# Bounded command (Linux only)

Run `python3 bin/bounded-command.py TIMEOUT_SECONDS COMMAND ARGS...`. Any other platform exits 125 with reason `unsupported` and no traceback, before POSIX signal APIs. Finite positive timeout: monotonic clock from before setup. If that deadline is already expired immediately before fork, the helper does not fork or exec and reports `deadline` (selected 124) with complete cleanup and no native child. If any direct child exists immediately before fork, the helper does not fork, exec, wait, or signal, and reports `setup_failure` (selected 125). That refusal covers grandchildren reparented afterward; a direct-pid snapshot is not ownership. When no such child exists, only the forked root and descendants observed afterward are owned. Waits are per owned pid, never `waitpid(-1)`.
No shell; command stdin/stderr go to `/dev/null`. Only stdlib and Linux `/proc`/`prctl` needed.

Root cause: shell substitution removes raw NUL; process-group cleanup misses detached processes; lost waits hide child failure.
Subreaper ownership and a readable `/proc/self/task/<pid>/children` interface precede spawn; unavailable enumeration fails setup without launching the command. Each cleanup kill/reap wave adopts `setsid`/double-fork descendants. All adopted children are awaited; command-reaped statuses are unobservable.
Inherited SIGCHLD disposition/auto-reaping flags are reset before fork so native waits remain observable, including when SIGCHLD is blocked. Before exec, Python-ignored SIGPIPE/SIGXFZ/SIGXFSZ are restored to default, matching subprocess-style native launch; unrelated inherited dispositions and mask bits are preserved.

Stdout: **4096 publishable bytes** in memory (+one overflow-probe byte), frozen after cleanup. Successful execution's immutable buffer is checked for actual raw NUL and strict UTF-8; an earlier failure reason is preserved.
Publication requires native success and complete cleanup. Not a JSON/schema validator: escaped `\u0000`, duplicate keys, exact enums/schema are callers' responsibility.
Check exit status; never shell-normalize raw output.

Wrapper stderr is sanitized JSON only (never argv/environment/raw command output).
`native_parent` is native status (null if unavailable); `native_descendants` counts observed adopted statuses. Signals use 128 + signal.
`reason`, `cleanup`, `signal` are context; `selected` is not necessarily native. Precedence: nonzero native parent, smallest nonzero native descendant, then 125 incomplete cleanup, 128 + own TERM/INT, 124 deadline, 125 setup/validation/publication failure.
Native 124/137/143/17 stay distinct; a deadline killing the root usually selects native 137, **not** 124.

TERM/INT handlers cover setup through finalization, requesting cleanup without raising through ownership setup. TERM/INT are blocked across the final cancellation check and fork; a pending stop signal does not spawn. The child restores the prior mask only after stop dispositions are defaulted.
TERM/INT are unblocked after handler installation even when inherited blocked from a launcher; other inherited signal-mask bits are preserved.
Cleanup: independent **2-second budget**, SIGKILL, no grace. Total: timeout + 2 seconds plus startup/scheduling/finalization.
If enumeration becomes unreadable after spawn, the known unreaped root is killed and awaited within that same budget; cleanup is still reported incomplete. Unknown live descendants cannot be safely enumerated under that platform fault, so there is no success/publication or claim of complete cleanup.
No zero exit/output on incomplete cleanup. Wrapper SIGKILL cannot be handled; uninterruptible kernel tasks may survive and cause failure.
Not a hostile-process sandbox: nested subreapers, external supervisors or different PID namespaces can break ownership.
Use only commands within this trust boundary.

The pending-signal snapshot commits publication after cleanup/validation: TERM/INT are deferred only for bounded nonblocking writes and immediate exit; descriptor flags are restored.
Use a draining pipe: 4096 bytes is atomic; full/closed pipes fail without payload bytes. Other sinks can partially write irreversibly. Concurrent users of shared descriptors may briefly observe nonblocking flags. No temp files or consumer/workflow wiring.
