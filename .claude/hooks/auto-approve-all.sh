#!/usr/bin/env bash
# auto-approve-all.sh — PreToolUse hook: auto-approve every tool call so Claude
# Code runs prompt-free ("yolo" / effective bypass) on the WEB platform — UNLESS
# the repo's OSS-guard (scripts/pre-tool-use.py) blocks the call, in which case
# its block is honoured instead.
#
# WHY THIS EXISTS
#   defaultMode "bypassPermissions" / "dontAsk" / "auto" are silently ignored in
#   Claude Code web/cloud sessions — a repo cannot grant itself those modes. A
#   committed PreToolUse hook, however, IS honoured in cloud sessions (protected
#   paths included), and returning permissionDecision "allow" skips the
#   interactive prompt. This is the only checked-in way to get zero-prompt
#   behaviour on web, our primary platform.
#
# WHY IT CONSULTS THE GUARD ITSELF (single PreToolUse hook)
#   This is the SOLE PreToolUse hook. It runs the OSS-guard (scripts/pre-tool-use.py)
#   exactly once and honours any block it returns, then auto-approves otherwise.
#   Running the guard here (instead of ALSO registering it as a separate hook)
#   means the guard executes once — no double-counted side effects (audit log /
#   telemetry) — and there is no reliance on multi-hook merge precedence.
#
#   - The guard is resolved from THIS hook's own location (BASH_SOURCE), not cwd
#     / git root, so it always comes from this repo even in a multi-repo session.
#   - Decisions use the modern PreToolUse schema (hookSpecificOutput.
#     permissionDecision); the guard's legacy {"decision":"block"} is translated
#     to "deny" so it is honoured regardless of legacy support.
#   - A MISSING guard fails open (auto-approve); a PRESENT guard that crashes
#     fails CLOSED (deny) — a broken guard must not silently grant bypass.
#   - Non-blocking guard output (e.g. an additionalContext advisory) does NOT
#     suppress auto-approve — it is preserved alongside the allow.
#
# WHAT STILL APPLIES (this hook cannot loosen these)
#   - The OSS-guard's blocks pass through: Bash token-leak patterns, Agent
#     model-routing / scope-budget, and PR repo-targeting.
#   - rm -rf / and rm -rf ~ still hit Claude Code's hardcoded circuit breaker.
#   - Any deny/ask permission rule still wins over allow (deny-first precedence).
#
# SECURITY NOTE (owner-enabled, deliberate)
#   Auto-approves writes to .git/, .claude/, .mcp.json and shell rc files with
#   no prompt, in repos that ingest untrusted external content (PR comments, web
#   fetches, issue text), so there is no last line of defence against prompt
#   injection here. Owner-authorised exception — see docs/zero-prompt-playbook.md
#   (Buro-Built/master).
set -uo pipefail

_payload="$(cat)"

# Resolve this hook's own repo root (…/.claude/hooks/ → repo root), so the guard
# is always this repo's, never whatever directory the session happens to be in.
_hook_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
_R="$(cd -- "$_hook_dir/../.." >/dev/null 2>&1 && pwd -P || echo "$PWD")"
_guard="$_R/scripts/pre-tool-use.py"

_out=""
if [ -f "$_guard" ]; then
  # Check the guard's exit status directly (avoids masking it via $?). With
  # pipefail the substitution reflects python3's status; a present guard that
  # crashes (non-zero) fails CLOSED below.
  # NOTE: guard stderr is intentionally NOT suppressed — its diagnostics
  # ([OSS-GUARD WARN], [ROUTING-CONFIG-ERROR], …) must stay visible in the hook
  # transcript/debug log, especially on degraded fail-open paths. stdout (the
  # decision JSON) is captured into _out; stderr passes through to the hook.
  if ! _out="$(printf '%s' "$_payload" | python3 "$_guard")"; then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"OSS-guard (scripts/pre-tool-use.py) failed to run; refusing to auto-approve."}}\n'
    exit 0
  fi
fi

# Decide: deny on a real block/deny/ask; otherwise allow, preserving any
# non-blocking additionalContext the guard supplied.
printf '%s' "$_out" | python3 -c '
import sys, json
raw = sys.stdin.read().strip()
REASON = "owner-enabled web yolo auto-approve (OSS-guard passed)"

def allow(ctx=None):
    hso = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": REASON}
    if ctx is not None:
        hso["additionalContext"] = ctx
    print(json.dumps({"hookSpecificOutput": hso}))

if not raw:
    allow(); sys.exit(0)
try:
    d = json.loads(raw)
except Exception:
    sys.stdout.write(raw); sys.exit(0)   # opaque guard output: pass through, do not auto-approve

hso = d.get("hookSpecificOutput") if isinstance(d, dict) else None
pd = hso.get("permissionDecision") if isinstance(hso, dict) else None
if (isinstance(d, dict) and d.get("decision") == "block") or pd in ("deny", "ask"):
    reason = d.get("reason") or (hso or {}).get("permissionDecisionReason") or "Blocked by OSS-guard (scripts/pre-tool-use.py)."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    sys.exit(0)
allow((hso or {}).get("additionalContext") if isinstance(hso, dict) else None)
'
exit 0
