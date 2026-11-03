#!/usr/bin/env bash
# Stop hook — outputs session-ending reminder checklist.
# Plain text stdout becomes additionalContext for Claude.

# ── Stop payload ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Consume stdin once. The sentinel preserves trailing newlines in string fields;
# arrays below preserve each decoded argument without word splitting or eval.
_HOOK_INPUT=$(cat)
_read_hook_string() {
    printf '%s' "$_HOOK_INPUT" | python3 -c '
import json, sys
value = json.load(sys.stdin).get(sys.argv[1], "")
if isinstance(value, str) and "\0" not in value:
    sys.stdout.write(value)
' "$1" 2>/dev/null || true
    printf '.'
}
_SESSION_ID=$(_read_hook_string session_id)
_TRANSCRIPT_PATH=$(_read_hook_string transcript_path)
_SESSION_ID_FOR_RETRO="${_SESSION_ID%.}"
_TRANSCRIPT_PATH_FOR_RETRO="${_TRANSCRIPT_PATH%.}"
unset _HOOK_INPUT _SESSION_ID _TRANSCRIPT_PATH

# ── Session retrospective (local durable write; KL optional/background) ───────
# Synchronous-with-timeout for the local JSONL write (--local-only). Inner 8s is
# below the Stop-hook timeout (10s; settings.json.jinja + plugin hooks.json).
# On timeout/non-zero: write failure marker and still exit 0. KL network leg is
# backgrounded below when MCP_API_KEY and KL_ENTITY/RETROSPECTIVE_ENTITY are set.
_run_session_retrospective() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local retro="${script_dir}/session-retrospective.py"
    [ -f "$retro" ] || return 0

    local sid="${_RETRO_SESSION_ID:-}"
    # Capture session_id once if not already set (stdin may already be consumed).
    if [ -z "$sid" ] && [ -n "${_SESSION_ID_FOR_RETRO:-}" ]; then
        sid="$_SESSION_ID_FOR_RETRO"
    fi

    local tpath="${_TRANSCRIPT_PATH_FOR_RETRO:-}"
    local args=(--mode stop --local-only)
    [ -z "$sid" ] || args+=(--session-id "$sid")
    [ -z "$tpath" ] || args+=(--transcript "$tpath")
    local rc=0
    # LOCAL durable write only (sync-with-timeout). Inner 8s < outer Stop 10s.
    # --local-only guarantees we do not burn the budget on KL HTTP.
    if command -v timeout >/dev/null 2>&1; then
        timeout 8s python3 "$retro" "${args[@]}" \
            >/dev/null 2>&1 || rc=$?
    else
        # Keep a parent alive to deliver the collector's rollback signal.
        # An alarm inherited across exec sends SIGALRM, which skips rollback.
        perl -e '
            use Errno qw(EINTR);
            use POSIX qw(:signal_h :sys_wait_h);
            my $old = POSIX::SigSet->new;
            sigprocmask(SIG_BLOCK, POSIX::SigSet->new(SIGALRM, SIGTERM, SIGINT), $old)
                or exit 125;
            my $pid = fork();
            defined $pid or exit 125;
            if ($pid == 0) {
                $SIG{ALRM} = $SIG{TERM} = $SIG{INT} = "DEFAULT";
                sigprocmask(SIG_SETMASK, $old) or exit 125;
                exec @ARGV;
                exit 127;
            }
            my ($reaped, $status, $timed_out, $cancelled) = (0, 0, 0, 0);
            my $signal_child = sub {
                local ($!, $?);
                return 0 if $reaped;
                my $done = waitpid($pid, WNOHANG);
                if ($done == $pid) { $status = $?; $reaped = 1; return 0; }
                return 0 if $done < 0;
                return kill $_[0], $pid;
            };
            $SIG{ALRM} = sub {
                if ($timed_out || $cancelled) { $signal_child->("KILL"); }
                elsif ($signal_child->("TERM")) { $timed_out = 1; alarm 1; }
            };
            for my $sig ("TERM", "INT") {
                my $name = $sig;
                $SIG{$name} = sub {
                    $cancelled = $name eq "TERM" ? 143 : 130;
                    # The collector rolls back on TERM, including INT cancellation.
                    alarm 1 if $signal_child->("TERM");
                };
            }
            alarm 8;
            sigprocmask(SIG_SETMASK, $old) or exit 125;
            while (!$reaped) {
                my $done = waitpid($pid, 0);
                if ($done == $pid) { $status = $?; $reaped = 1; last; }
                next if $done < 0 && $! == EINTR;
                last;
            }
            alarm 0;
            exit 124 if $timed_out;
            exit $cancelled if $cancelled;
            exit 125 unless $reaped;
            exit(($status & 127) ? 128 + ($status & 127) : $status >> 8);
        ' python3 "$retro" "${args[@]}" \
            >/dev/null 2>&1 || rc=$?
    fi

    if [ "$rc" -ne 0 ]; then
        mkdir -p .ai/memory/retrospectives
        printf '{"timestamp":"%s","exit_code":%s,"event":"session-retrospective-capture-failed"}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" \
            > .ai/memory/retrospectives/.last-capture-failed
        echo "[session-stop] retrospective capture failed (rc=$rc); marker written" >&2
    else
        rm -f .ai/memory/retrospectives/.last-capture-failed 2>/dev/null || true
    fi

    # Drain only after the local step exits cleanly. Failed capture keeps its
    # marker and pending records for a later retry. An unchanged-transcript
    # rc=0 no-op may retry an older pending record without claiming fresh data.
    if [ "$rc" -eq 0 ] && [ -n "${MCP_API_KEY:-}" ] \
        && { [ -n "${KL_ENTITY:-}" ] || [ -n "${RETROSPECTIVE_ENTITY:-}" ]; }; then
        local kl_args=(--mode kl-only)
        [ -z "$sid" ] || kl_args+=(--session-id "$sid")
        ( python3 "$retro" "${kl_args[@]}" ) </dev/null >/dev/null 2>&1 &
        disown $! 2>/dev/null || true
    fi
    return 0
}
# Preserve the session id parsed from the Stop payload; else use the caller fallback.
_RETRO_SESSION_ID="${_SESSION_ID_FOR_RETRO:-${_RETRO_SESSION_ID:-}}"
_run_session_retrospective

# Emit primary hook context before optional work can consume the host deadline.
cat <<'EOF'
Session ending checklist:
- Run /extract-insights if decisions were made
- Run /session-summary for significant sessions
- Flag any unresolved issues for next session
- Check if .ai/memory/ changes should be committed
- Check .ai/reflections/ for any pending-review reflection proposals (or run /reflect)
EOF

# Local capture has first use of the host's 10-second budget. Optional receipt
# retention, reflection and doctor work may be interrupted afterward without
# starving the durable collector's own bounded attempt.
if [ -f "${SCRIPT_DIR}/rotate-jsonl-receipt.py" ]; then
  if command -v timeout >/dev/null 2>&1; then
    timeout 1 python3 "${SCRIPT_DIR}/rotate-jsonl-receipt.py" >/dev/null 2>&1 || true
  else
    perl -e 'alarm 1; exec @ARGV' python3 "${SCRIPT_DIR}/rotate-jsonl-receipt.py" >/dev/null 2>&1 || true
  fi
fi

# Redirect the entire background group: redirecting only Python leaves its
# waiting shell holding Stop's output pipes. Host termination remains best effort.
_cost_args=()
[ -z "$_SESSION_ID_FOR_RETRO" ] || _cost_args+=(--session "$_SESSION_ID_FOR_RETRO")
( python3 "${SCRIPT_DIR}/session-cost-logger.py" "${_cost_args[@]}" || true ) </dev/null >/dev/null 2>&1 &
disown $! 2>/dev/null || true
unset _RETRO_SESSION_ID _SESSION_ID_FOR_RETRO _TRANSCRIPT_PATH_FOR_RETRO _cost_args

# Opt-in reflection still writes staged proposals for /reflect, after capture.
if [ "${CLAUDE_AUTO_REFLECT:-0}" = "1" ]; then
  python3 "${SCRIPT_DIR}/reflect-on-failures.py" >/dev/null 2>&1 || true
fi


# ── Branch fingerprint ───────────────────────────────────────────────────────
# One-line visibility of what this branch has done relative to main: files
# touched, lines changed, new files created. Surfaces the "bloats existing
# files, never creates new ones" pattern without enforcing anything.
# Scoped to the branch (not the session) — accurate framing because
# session boundaries are not reliably knowable from inside a hook.
_branch_fingerprint() {
    git rev-parse --git-dir >/dev/null 2>&1 || return 0
    # Pick the first ref that actually exists: BASE_BRANCH (if set), then
    # origin/main, then origin/master. Only fall through to origin/master
    # when origin/main is absent — never when merge-base fails against an
    # existing configured branch (that would silently switch the baseline).
    local target
    if [ -n "${BASE_BRANCH:-}" ] \
        && git rev-parse --verify --quiet "$BASE_BRANCH" >/dev/null; then
        target="$BASE_BRANCH"
    elif git rev-parse --verify --quiet origin/main >/dev/null; then
        target="origin/main"
    elif git rev-parse --verify --quiet origin/master >/dev/null; then
        target="origin/master"
    else
        return 0
    fi
    local base
    base=$(git merge-base HEAD "$target" 2>/dev/null) || return 0
    local stat
    stat=$(git diff --shortstat "$base" 2>/dev/null) || return 0
    [ -n "$stat" ] || return 0
    local new_files
    new_files=$(git diff --name-status --diff-filter=A "$base" 2>/dev/null | wc -l | tr -d ' ')
    echo "Branch fingerprint (vs ${target}): ${stat# }, ${new_files} new file(s)."
}
_branch_fingerprint

# ── Doctor: surface dysfunction signals from this session ────────────────────
# Runs the existing doctor-analyse.py over the last hour's transcripts and
# surfaces only warn-or-higher findings. Signals covered: edit-thrashing,
# error-loop, repeated-instructions, correction-heavy, rapid-corrections.
# Silent on clean sessions. Synchronous because doctor runs in <0.2s; capped
# at 2s after the primary capture attempt; the host may interrupt optional work —
# backgrounding with disown would lose stdout once the parent hook exits.
_doctor_silent() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "${script_dir}/doctor-analyse.py" ] || return 0
    # Pass path via env var (no heredoc interpolation into Python source) —
    # mirrors the safe pattern used by _run_intent_drift below.
    export _DOCTOR_SCRIPT="${script_dir}/doctor-analyse.py"
    python3 - <<'PYEOF' 2>/dev/null || true
import json, os, subprocess, sys
try:
    out = subprocess.run(
        ["python3", os.environ["_DOCTOR_SCRIPT"], "--since", "1h", "--json"],
        capture_output=True, text=True, timeout=2,
    )
    data = json.loads(out.stdout or "{}")
    by_sig = data.get("summary", {}).get("by_signal", {})
    if not by_sig:
        sys.exit(0)
    parts = [f"{n}× {sig}" for sig, n in sorted(by_sig.items(), key=lambda x: -x[1])]
    print("Doctor signals this session: " + ", ".join(parts) + ". Run /doctor for detail.")
except Exception:
    pass
PYEOF
    unset _DOCTOR_SCRIPT
}
_doctor_silent

# ── Intent-drift check ───────────────────────────────────────────────────────
# Compares the session diff against the original user prompt (if available).
# Output goes to .ai/drift-alerts/ for later review — never blocks Stop.
_run_intent_drift() {
    local prompt_file=".ai/session-logs/current/user-prompt.txt"
    [ -f "$prompt_file" ] || return 0

    local diff_summary
    diff_summary=$(git diff "$(git merge-base HEAD origin/main 2>/dev/null || echo HEAD~1)" --stat 2>/dev/null | tail -5 || true)
    [ -n "$diff_summary" ] || return 0

    local original_prompt
    original_prompt=$(head -c 500 "$prompt_file" 2>/dev/null || true)
    [ -n "$original_prompt" ] || return 0

    # Only run if API key available and drift check enabled
    { [ -n "${LLM_API_KEY:-}" ] || [ -n "${LITELLM_MASTER_KEY:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; } || return 0
    [ "${PR_ASSESSMENT_DRIFT_ENABLED:-0}" = "1" ] || return 0

    # Export as env vars — avoids shell injection into Python source (triple-quote termination,
    # backslash sequences, etc. that would occur with unquoted <<PYEOF interpolation).
    export _DRIFT_PROMPT="$original_prompt"
    export _DRIFT_STATS="$diff_summary"

    python3 - <<'PYEOF' 2>/dev/null || true
import json, os, time, signal
from contextlib import contextmanager
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.parse import urlparse
from datetime import datetime, timezone

_original_prompt = os.environ.get("_DRIFT_PROMPT", "")
_diff_stats = os.environ.get("_DRIFT_STATS", "")

# Resolve only the governed LiteLLM endpoint; never clear its compatibility alias.
proxy_url = os.environ.get("LITELLM_PROXY_URL") or os.environ.get("ANTHROPIC_BASE_URL")
parsed = urlparse(proxy_url or "")
if (os.environ.get("CLIENT_AI_POLICY") or parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.hostname.lower().rstrip(".") == "api.anthropic.com"):
    raise RuntimeError("session critic requires the governed LiteLLM proxy")
auth = os.environ.get("LLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("ANTHROPIC_API_KEY")
auth = (auth or "").strip()
if auth.lower().startswith("bearer "):
    auth = auth[7:].strip()
if not auth or auth.lower() == "bearer" or auth.lower().startswith("sk-ant-"):
    raise RuntimeError("session critic requires a LiteLLM proxy credential")
# Send only the validated proxy credential. SDK defaults can independently
# inherit provider authentication, even when an explicit API key is supplied.
class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

@contextmanager
def _wall_budget(seconds):
    # This heredoc runs on the main thread of its own POSIX Python process.
    # A socket timeout alone does not stop a peer trickling response bytes.
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("critic cannot replace an active deadline")
    def expired(signum, frame):
        raise TimeoutError("critic time budget exhausted")
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)

opener = build_opener(_NoRedirect())
base = proxy_url.rstrip("/")
endpoint = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
_prompt_text = (
    "Original intent: " + _original_prompt + "\n\n"
    "Session diff stats:\n" + _diff_stats + "\n\n"
    "Classify scope drift: on-scope | minor-expansion | significant-drift | scope-creep\n"
    'Return JSON only: {"classification": "...", "reason": "one sentence"}'
)

try:
    with _wall_budget(15.0):
        # Proxied reasoning shares the output allowance. Retry only starvation,
        # once, with more headroom and the remainder of the original time budget.
        deadline = time.monotonic() + 15.0
        budget = 100 + 4096
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("critic time budget exhausted")
            payload = {"model": "sonnet", "max_tokens": budget,
                       "messages": [{"role": "user", "content": _prompt_text}]}
            request = Request(endpoint, data=json.dumps(payload).encode(), headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + auth,
                "anthropic-version": "2023-06-01",
            }, method="POST")
            with opener.open(request, timeout=remaining) as response:
                r = json.loads(response.read())
            if time.monotonic() >= deadline:
                raise TimeoutError("critic response exceeded deadline")
            _text = "".join(
                block.get("text", "") for block in r.get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if _text.strip() and r.get("stop_reason") != "max_tokens":
                break
            if attempt:
                raise ValueError("critic output remained incomplete")
            budget *= 2
        _result = json.loads(_text)
    if _result.get("classification") in ("significant-drift", "scope-creep"):
        os.makedirs(".ai/drift-alerts", exist_ok=True)
        _fname = f".ai/drift-alerts/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
        with open(_fname, "w") as _f:
            json.dump({
                "classification": _result["classification"],
                "reason": _result["reason"],
                "diff_stats": _diff_stats,
            }, _f, indent=2)
        print(f"[intent-drift] {_result['classification']}: {_result['reason']}")
        print(f"[intent-drift] Alert written to {_fname}")
except Exception:
    print("[intent-drift] Critic unavailable; no scope verdict recorded.")
PYEOF
}

# Run in detached background — Stop hook timeout is 10s; an API call takes 2-10s.
# Subshell isolates the exported env vars (_DRIFT_PROMPT, _DRIFT_STATS).
# disown prevents SIGHUP when the parent shell exits.
( _run_intent_drift ) &
disown $! 2>/dev/null || true
