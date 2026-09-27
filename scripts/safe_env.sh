#!/usr/bin/env bash
# scripts/safe_env.sh — sanctioned env-var probe helpers
#
# Purpose: structurally prevent token-in-transcript leaks. Replaces fragile
# `echo "${VAR:+SET}${VAR:-UNSET}"` patterns (the 2026-05-16 incident pattern)
# with safe wrappers that NEVER echo the value of a token-named variable.
#
# Source this file from ~/.bashrc (added automatically by SessionStart hook).
#
# Usage:
#   is_set DOPPLER_TOKEN_PRD           → "yes" | "no"
#   safe_prefix GH_TOKEN 6             → first 6 chars (e.g. "ghp_aB") or ""
#   safe_length ANTHROPIC_API_KEY      → numeric length or 0
#   safe_summary MCP_API_KEY           → "absent" | "present prefix=... length=N"
#
# The summary form is the recommended diagnostic — it answers the three
# real questions (is it set, does it look right, is the length plausible)
# without ever exposing the value or a continuous substring long enough to
# brute-force against a service.
#
# SECURITY NOTES:
#   - `safe_prefix` caps at 8 chars max regardless of caller request; this
#     ensures even a misuse cannot dump a full token.
#   - All helpers reject names with non-portable characters (defense against
#     command injection via crafted var names).
#   - These run with `set -u` safe — every helper uses `${!1:-}` defaulted
#     reference, so nounset never trips. We do NOT call `set +u` because this
#     file is sourced into the caller's interactive shell and would silently
#     weaken nounset for every subsequent command.

# ── CREDENTIAL CHECK PATTERN ─────────────────────────────────────────────────
# Use these helpers instead of reading credential files or echoing token vars:
#   safe_summary VAR_NAME   → "present prefix=... length=N" or "absent" — never the value
#   is_set VAR_NAME         → "yes" | "no"
#   safe_prefix VAR_NAME N  → first N chars (hard-capped at 8) for log correlation
#   safe_length VAR_NAME    → numeric length
#
# DO NOT USE — all of these have caused PAT leaks to stdout/transcript:
#   cat ~/.netrc                              ← raw PAT in plaintext (2026-05-19 incident)
#   cat ~/.git-credentials                    ← same
#   python3 -c "import netrc; print(...)"     ← raw PAT (2026-05-17 incident, fp=f673bb4c)
#   echo "${GH_TOKEN}"                        ← raw token (2026-05-16 incident)
#   echo "${GH_TOKEN:-default}"               ← expands to VALUE when set (incident pattern)
#   printenv GH_TOKEN                         ← prints value
#   env | grep TOKEN                          ← dumps value
#
# All banned patterns above are blocked by pre-tool-use.py §GUARD:netrc-read
# and §_token_leak_check. See docs/token-leak-hygiene.md.
_safe_env__valid_name() {
    # Reject anything that isn't a portable shell identifier
    [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

is_set() {
    if ! _safe_env__valid_name "${1:-}"; then echo "invalid-name"; return 2; fi
    if [[ -n "${!1:-}" ]]; then echo "yes"; else echo "no"; fi
}

safe_prefix() {
    # safe_prefix VAR_NAME [chars]
    # Reveals at most len-4 chars (and the requested cap) — for a value at or
    # below 4 chars the prefix IS the value, so emit length-only then.
    if ! _safe_env__valid_name "${1:-}"; then echo ""; return 2; fi
    local req="${2:-6}"
    # Validate decimal BEFORE any arithmetic — `(( req > 8 ))` evaluates a
    # crafted string as an arithmetic expression (an array subscript can run
    # $(…) against the caller's variables), and `08` is an invalid octal that
    # leaves the cap unset (Codex round-3 P1 / Devin Review SEC).
    if [[ ! "$req" =~ ^[0-9]+$ ]]; then req=6; fi
    # Strip leading zeros so `001` still means a one-char request (all-zero
    # becomes 0, and the <1 floor then emits one char like an explicit `0`).
    req="${req#"${req%%[!0]*}"}"
    [[ -z "$req" ]] && req=0
    # An overflowing decimal wraps int64 arithmetic to a negative BEFORE the
    # 8-char cap, and the <1 floor then shrinks the prefix to one char —
    # clamp on digit count first (Devin Review BUG on #5852).
    if (( ${#req} > 2 )); then req=99; fi
    req=$((10#$req))
    # Hard cap at 8 — never expose more than 8 chars of any variable
    if (( req > 8 )); then req=8; fi
    if (( req < 1 )); then req=1; fi
    local val="${!1:-}"
    if [[ -z "$val" ]]; then echo ""; return 0; fi
    local len=${#val}
    # Short values: a prefix is a meaningful fraction of the secret — emit
    # length only (Devin Review SEC on #1398 round-41).
    if (( len < 12 )); then echo "(len=$len)"; return 0; fi
    local show=$(( len - 4 ))
    if (( show > req )); then show=$req; fi
    if (( show < 1 )); then echo "(len=$len)"; else echo "${val:0:$show}"; fi
}

safe_length() {
    if ! _safe_env__valid_name "${1:-}"; then echo 0; return 2; fi
    local val="${!1:-}"
    echo "${#val}"
}

safe_summary() {
    # safe_summary VAR_NAME
    # → "absent"
    # → "present prefix=<up-to-6-chars> length=<N>"
    if ! _safe_env__valid_name "${1:-}"; then echo "invalid-name"; return 2; fi
    local val="${!1:-}"
    if [[ -z "$val" ]]; then echo "absent"; return 0; fi
    local prefix="${val:0:6}"
    local n="${#val}"
    # Length-only output if the value is too short to safely show a prefix
    # — a 6-char prefix of a 12-char credential exposes HALF of it
    # (Devin Review round-34 SEC_0002). Under 16 chars a prefix would be
    # a material fraction; at 16+ it reveals at most ~37% and still
    # leaves >=10 chars hidden.
    if (( n < 16 )); then
        echo "present length=$n"
    else
        echo "present prefix=$prefix length=$n"
    fi
}

# Convenience: bulk presence summary for diagnostic scripts.
# Usage: safe_env_report VAR1 VAR2 VAR3
safe_env_report() {
    local v i=0
    for v in "$@"; do
        i=$((i + 1))
        # "$v" is the LABEL — a caller passing "$GH_TOKEN" expands to the
        # VALUE, which printf would then print verbatim (Codex round-44 P1).
        # Even an identifier-shaped value echoes as a label (Devin Review
        # SEC on #5852) — so only the name of a variable that is actually
        # set may print; unset/invalid args report by position.
        # `-v` evaluates indexed-variable syntax arithmetically — a crafted
        # name like 'x[$(cmd)]' would execute. Validate before probing.
        if _safe_env__valid_name "$v" && [[ -v $v ]]; then
            printf '%-40s %s\n' "$v" "$(safe_summary "$v")"
        else
            printf 'arg%-37s %s\n' "$i" "absent-or-invalid"
        fi
    done
}

# _safe_env__valid_name must ride along — an exported helper in a child shell
# calls it and otherwise reports "invalid-name" for every probe (Devin Review
# BUG on #1398).
export -f _safe_env__valid_name is_set safe_prefix safe_length safe_summary safe_env_report 2>/dev/null || true
