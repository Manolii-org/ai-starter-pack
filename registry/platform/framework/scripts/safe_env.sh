#!/usr/bin/env bash
# Sanctioned helpers for inspecting secret-bearing environment variables WITHOUT
# printing their values. Use these instead of echo/printf/printenv/declare on any
# token/key/secret-named variable. The PreToolUse token-leak guard
# (scripts/pre-tool-use.py) blocks the unsafe forms; these are the safe ones.
#
#   source scripts/safe_env.sh
#   safe_summary GITHUB_TOKEN      # -> "GITHUB_TOKEN: set (len=40)"
#
# See docs/token-leak-hygiene.md.

# Enable strict mode only when executed directly (not when sourced), so sourcing
# this library never changes the caller's shell options.
if ! (return 0 2>/dev/null); then
    set -euo pipefail
fi

# is_set VAR_NAME -> "set" or "unset" (never the value)
is_set() {
  if [ -n "${!1:-}" ]; then echo "set"; else echo "unset"; fi
}

# safe_length VAR_NAME -> character length of the value (never the value)
safe_length() {
  local val="${!1:-}"
  echo "${#val}"
}

# safe_prefix VAR_NAME [N] -> first N chars (default 4, usually just a provider
# prefix) followed by a redaction marker. Never prints the full value.
safe_prefix() {
  local val="${!1:-}" n="${2:-4}"
  # Hard-cap the prefix length so a large or invalid N can never dump the secret
  # (the sanctioned helper must honour the redaction guarantee in token-leak-hygiene.md).
  case "$n" in (*[!0-9]*|'') n=4 ;; esac
  if [ "$n" -gt 8 ]; then n=8; fi
  if [ -z "$val" ]; then echo "(unset)"; return; fi
  printf '%s…[REDACTED:len=%s]\n' "${val:0:$n}" "${#val}"
}

# safe_summary VAR_NAME -> one-line status: set/unset + length, never the value
safe_summary() {
  local val="${!1:-}"
  if [ -z "$val" ]; then echo "$1: unset"; else echo "$1: set (len=${#val})"; fi
}
