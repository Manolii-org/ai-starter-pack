#!/usr/bin/env bash
# Stop hook — session-end cleanup, self-check, and skill graph rebuild.
# Receives hook JSON on stdin. Always exits 0 — never blocks Claude from stopping.
set -o pipefail
trap 'exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

SESSION_LOGS_DIR="$REPO_ROOT/.ai/session-logs"
mkdir -p "$SESSION_LOGS_DIR"

# ── 1. Write clean-stop sentinel ─────────────────────────────────────────────
STOP_SENTINEL="$REPO_ROOT/.ai/session-stop.json"
timeout 10s python3 -c "
import json, os, sys
ts, branch, out = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w') as f:
    json.dump({'ts': ts, 'branch': branch, 'clean_stop': True}, f)
" "$TS" "$BRANCH" "$STOP_SENTINEL" 2>/dev/null || true

# ── 2. Append session-end record to session-logs ──────────────────────────────
LOG_DATE="${TS%%T*}"
LOG_FILE="$SESSION_LOGS_DIR/${LOG_DATE}.jsonl"
timeout 10s python3 -c "
import json, sys
print(json.dumps({'ts': sys.argv[1], 'event': 'session-stop', 'branch': sys.argv[2]}))
" "$TS" "$BRANCH" >> "$LOG_FILE" 2>/dev/null || true

# ── 3. Rebuild skill graph ────────────────────────────────────────────────────
if [ -f "$SCRIPTS_DIR/build-skill-graph.py" ]; then
    timeout 10s python3 "$SCRIPTS_DIR/build-skill-graph.py" 2>/dev/null || true
fi

# ── 4. System self-check ──────────────────────────────────────────────────────
if [ -f "$SCRIPTS_DIR/system-self-check.py" ]; then
    timeout 10s python3 "$SCRIPTS_DIR/system-self-check.py" 2>/dev/null || true
fi

exit 0
