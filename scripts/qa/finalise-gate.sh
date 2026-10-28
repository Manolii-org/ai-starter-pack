#!/bin/sh
# Host-neutral launcher for scripts/qa/finalise-gate.py.
#   usage: finalise-gate.sh claude|codex|cursor|git-pre-push
# Several hosts read exit 2 as "block", so this launcher never produces it
# itself: a missing interpreter or gate fails open (CI stays authoritative).
# Cursor permission hooks also need valid JSON on every success path.
ADAPTER="${1:-git-pre-push}"
DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
allow() {
  [ "$ADAPTER" = "cursor" ] && printf '{"permission":"allow"}\n'
  exit 0
}
if [ -z "$DIR" ] || [ ! -f "$DIR/finalise-gate.py" ] || ! command -v python3 >/dev/null 2>&1; then
  allow
fi
shift 2>/dev/null || true
if [ "$ADAPTER" = "git-pre-push" ]; then
  exec python3 "$DIR/finalise-gate.py" --hook "$ADAPTER" "$@"
fi
# Agent hosts fire this on every shell call. Skip the interpreter unless the
# payload could be a hand-off; the Python classifier makes the real decision.
PAYLOAD=$(cat)
case "$PAYLOAD" in
  *push*|*pr*create*|*create_pull_request*) ;;
  *) allow ;;
esac
printf '%s' "$PAYLOAD" | exec python3 "$DIR/finalise-gate.py" --hook "$ADAPTER" "$@"
