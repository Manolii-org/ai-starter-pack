#!/bin/bash
# WS3 SessionStart hook wrapper — surfaces the recent-navigation-warning
# file produced by `session-retrospective.py --mode inject` so high-
# dysfunction prior sessions produce an agent-visible warning at the
# start of the next Claude session.
#
# Codex P2 2026-07-19 (impaktful#1695 line 1577): `mode_inject` was
# defined but no hook invoked it, so the retrospective feedback loop
# was inert. This wrapper closes the loop.
#
# Wiring (in `.claude/settings.json`):
#     "hooks": {
#         "SessionStart": [
#             { "matcher": "*", "hooks": [ {
#                 "type": "command",
#                 "command": "bash $CLAUDE_PROJECT_DIR/scripts/session-start-inject-warning.sh"
#             } ] }
#         ]
#     }
#
# Behaviour:
#   1. Refresh `.ai/recent-navigation-warning.md` from the local
#      retrospective log by running the collector in `inject` mode.
#      Bounded to 4s — always exits 0 so SessionStart never blocks.
#   2. If a warning file is present, print it to stdout. Claude Code
#      surfaces SessionStart hook stdout to the session context.
set -euo pipefail
_R=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
cd "$_R"
_RETRO="${CLAUDE_PLUGIN_ROOT:-$_R}/scripts/session-retrospective.py"
[ -f "$_RETRO" ] || _RETRO="$_R/scripts/session-retrospective.py"
[ -f "$_RETRO" ] || exit 0

# 1) Refresh the warning file — bounded, fail-open.
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

# 2) Surface the warning file to the session, if produced.
_WARN=".ai/recent-navigation-warning.md"
if [ -s "$_WARN" ]; then
    cat "$_WARN"
fi
exit 0
