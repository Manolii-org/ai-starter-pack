#!/usr/bin/env bash
# PreCompact hook — archives session transcript before compaction.
# Receives hook JSON on stdin with transcript_path and trigger fields.
# Copies transcript to .ai/session-logs/ with timestamp, rotates to keep last 30.
# Logs auto-compact-fired metric when trigger == "auto" (not manual /compact).
# NOTE: PreCompact stdout is NOT injected into Claude's compaction context.
#       Compaction instructions live in .claude/persistent-instructions.md.
# Always exits 0 — never blocks Claude.
set -o pipefail
trap 'exit 0' ERR

# .claude/hooks/ is 2 levels below repo root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
LOGS_DIR="$REPO_ROOT/.ai/session-logs"

INPUT=$(cat)
TRANSCRIPT=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")
TRIGGER=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('trigger',''))" 2>/dev/null || echo "")

# Archive transcript if it exists
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    mkdir -p "$LOGS_DIR"
    DEST="$LOGS_DIR/session_$(date -u +%Y%m%d_%H%M%S).jsonl"
    cp "$TRANSCRIPT" "$DEST" 2>/dev/null || true
    # Rotate: keep last 30 transcripts
    ls -1t "$LOGS_DIR"/session_*.jsonl 2>/dev/null | tail -n +31 | while read -r file; do rm -f "$file"; done
fi


# Stage retrospective signals from the archived transcript (best-effort).
_retro_precompact() {
  local dest="${DEST:-}"
  [ -n "$dest" ] && [ -f "$dest" ] || return 0
  local candidate=""
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/session-retrospective.py" ]; then
    candidate="${CLAUDE_PLUGIN_ROOT}/scripts/session-retrospective.py"
  elif [ -f "$REPO_ROOT/scripts/session-retrospective.py" ]; then
    candidate="$REPO_ROOT/scripts/session-retrospective.py"
  elif [ -f "$SCRIPT_DIR/../scripts/session-retrospective.py" ]; then
    candidate="$SCRIPT_DIR/../scripts/session-retrospective.py"
  fi
  [ -n "$candidate" ] || return 0
  python3 "$candidate" --mode precompact --transcript "$dest" >/dev/null 2>&1 || true
}
_retro_precompact

# Log auto-compact-fired only when trigger is "auto" (manual /compact is not a miss)
if [ "$TRIGGER" = "auto" ]; then
    METRICS="$REPO_ROOT/.ai/compact-metrics.jsonl"
    TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    STATE_FILE="$REPO_ROOT/.ai/compact-state.json"
    CALLS=$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1])).get('calls_since_compact', 0))
except Exception:
    print(0)
" "$STATE_FILE" 2>/dev/null || echo "0")
    mkdir -p "$(dirname "$METRICS")"
    echo "{\"ts\":\"$TS\",\"event\":\"auto-compact-fired\",\"trigger\":\"auto-95pct\",\"calls_since_compact\":$CALLS}" >> "$METRICS" 2>/dev/null || true
fi

exit 0
