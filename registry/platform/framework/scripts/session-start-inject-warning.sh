#!/bin/bash
# WS3 SessionStart helper — refreshes `.ai/recent-navigation-warning.md`
# by running the retrospective collector in `inject` mode. Bounded to 4s,
# always exits 0.
#
# Codex P2 2026-07-19 (impaktful#1695 line 1577 / manolii-platform#430
# line 36): `mode_inject` was defined but no hook invoked it, so the
# retrospective feedback loop was inert. This script is a building
# block: existing SessionStart hooks call it to refresh the file, then
# surface the file's contents in whatever format their hook contract
# uses (raw stdout for plain-text hooks; folded into MEMORY_CONTEXT /
# systemMessage for JSON-emitting hooks).
#
# The script only refreshes the file. It intentionally does NOT print
# the warning itself — that would break JSON-emitting hooks whose
# stdout must be a single valid JSON envelope. Callers surface the
# file with `cat "$_R/.ai/recent-navigation-warning.md" 2>/dev/null`
# (plain-text) or by reading its contents into their message payload.
set -euo pipefail
_R=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
cd "$_R"
_RETRO="${CLAUDE_PLUGIN_ROOT:-$_R}/scripts/session-retrospective.py"
[ -f "$_RETRO" ] || _RETRO="$_R/scripts/session-retrospective.py"
[ -f "$_RETRO" ] || exit 0

if command -v timeout >/dev/null 2>&1; then
    timeout 4s python3 "$_RETRO" --mode inject >/dev/null 2>&1 || true
else
    python3 - "$_RETRO" <<'PY' >/dev/null 2>&1 || true
import sys, subprocess
retro = sys.argv[1]
try:
    subprocess.run(["python3", retro, "--mode", "inject"], timeout=4)
except Exception:
    pass
PY
fi

exit 0
