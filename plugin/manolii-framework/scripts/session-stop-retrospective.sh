#!/bin/bash
# WS3 Stop-hook wrapper — synchronous-with-timeout (inner 8s < outer 10s).
# Reads the Stop-hook JSON payload from stdin (session_id + transcript_path)
# fail-open, forwards to the collector. On non-zero: writes failure marker and
# still exits 0 so Stop never blocks. KL network leg backgrounded when
# MCP_API_KEY + KL_ENTITY|RETROSPECTIVE_ENTITY are set.
set -euo pipefail
_R=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
cd "$_R"
_RETRO="$_R/scripts/session-retrospective.py"
[ -f "$_RETRO" ] || exit 0

# Parse Stop-hook JSON payload (fail-open; empty vars if missing/malformed).
_PAYLOAD=""
if [ -t 0 ]; then :; else _PAYLOAD=$(cat 2>/dev/null || true); fi
_SID=""; _TP=""
if [ -n "$_PAYLOAD" ]; then
    # Coalesce None → '' so a payload with explicit "session_id": null
    # doesn't leak the literal string "None" into the collector args.
    _SID=$(printf '%s' "$_PAYLOAD" | python3 -c "import sys,json
try:
    d=json.load(sys.stdin); print((d.get('session_id') or '') if isinstance(d,dict) else '')
except Exception: pass" 2>/dev/null || true)
    _TP=$(printf '%s' "$_PAYLOAD" | python3 -c "import sys,json
try:
    d=json.load(sys.stdin); print((d.get('transcript_path') or '') if isinstance(d,dict) else '')
except Exception: pass" 2>/dev/null || true)
fi

_ARGS=(--mode stop --local-only)
[ -n "$_SID" ] && _ARGS+=(--session-id "$_SID")
[ -n "$_TP" ] && _ARGS+=(--transcript "$_TP")

rc=0
if command -v timeout >/dev/null 2>&1; then
    timeout 8s python3 "$_RETRO" "${_ARGS[@]}" >/dev/null 2>&1 || rc=$?
else
    # Portable fallback for images without coreutils `timeout` (macOS default,
    # some minimal Alpine): use Python's subprocess.run(timeout=…) so Stop
    # is still bounded. rc=124 mirrors coreutils' timeout convention.
    python3 - "$_RETRO" "${_ARGS[@]}" <<'PY' >/dev/null 2>&1 || rc=$?
import sys, subprocess
retro, *args = sys.argv[1:]
try:
    r = subprocess.run(["python3", retro, *args], timeout=8)
    sys.exit(r.returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
PY
fi
if [ "$rc" -ne 0 ]; then
    mkdir -p .ai/memory/retrospectives
    printf '{"timestamp":"%s","exit_code":%s,"event":"session-retrospective-capture-failed"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" \
        > .ai/memory/retrospectives/.last-capture-failed
else
    rm -f .ai/memory/retrospectives/.last-capture-failed 2>/dev/null || true
fi


# KL network leg — backgrounded, never blocks Stop. Skipped if:
#  - local capture failed (avoids uploading a STALE prior snapshot labelled with
#    the current session_id via mode_kl_only's newest-snapshot lookup)
#  - no creds/entity in env
# The wrapper deliberately does NOT trigger bootstrap_env or read
# .ai/config/retrospective.json here — bootstrap_env is a bigger surface (would
# block Stop), and file-based entity config is picked up when kl-only runs from
# a session where SessionStart populated env. Missing-cred case fails-closed.
if [ "$rc" -eq 0 ] && [ -n "${MCP_API_KEY:-}" ] && { [ -n "${KL_ENTITY:-}" ] || [ -n "${RETROSPECTIVE_ENTITY:-}" ]; }; then
    _KL_ARGS=(--mode kl-only)
    [ -n "$_SID" ] && _KL_ARGS+=(--session-id "$_SID")
    ( python3 "$_RETRO" "${_KL_ARGS[@]}" >/dev/null 2>&1 ) &
    disown $! 2>/dev/null || true
fi

exit 0
