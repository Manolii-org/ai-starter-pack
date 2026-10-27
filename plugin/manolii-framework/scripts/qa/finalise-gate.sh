#!/bin/sh
# Host-neutral launcher for scripts/qa/finalise-gate.py.
#   usage: finalise-gate.sh claude|codex|cursor|git-pre-push
# Several hosts read exit 2 as "block", so this launcher never produces it
# itself: a missing interpreter or gate fails open (CI stays authoritative).
# Cursor permission hooks also need valid JSON on every success path.
ADAPTER="${1:-git-pre-push}"
DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
if [ -z "$DIR" ] || [ ! -f "$DIR/finalise-gate.py" ] || ! command -v python3 >/dev/null 2>&1; then
  [ "$ADAPTER" = "cursor" ] && printf '{"permission":"allow"}\n'
  exit 0
fi
shift 2>/dev/null || true
exec python3 "$DIR/finalise-gate.py" --hook "$ADAPTER" "$@"
