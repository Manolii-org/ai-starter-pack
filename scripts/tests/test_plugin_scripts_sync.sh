#!/usr/bin/env bash
# Fail if paired scripts/ ↔ plugin/manolii-framework/scripts/ copies drift.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PAIRS=(
  session-retrospective.py
  session-stop-checklist.sh
  migrate-memory-path.sh
  session-cost-logger.py
  lib/failure_class.py
)
fail=0
for rel in "${PAIRS[@]}"; do
  a="$ROOT/scripts/$rel"
  b="$ROOT/plugin/manolii-framework/scripts/$rel"
  if [[ ! -f "$a" ]]; then
    echo "FAIL missing scripts/$rel"
    fail=1
    continue
  fi
  if [[ ! -f "$b" ]]; then
    echo "FAIL missing plugin/.../scripts/$rel"
    fail=1
    continue
  fi
  if ! diff -q "$a" "$b" >/dev/null; then
    echo "FAIL drift: scripts/$rel != plugin/manolii-framework/scripts/$rel"
    fail=1
  else
    echo "OK  $rel"
  fi
done
# Hook pairs that must stay in sync (template ↔ plugin)
HOOK_PAIRS=(
  session-start.sh
  pre-compact.sh
)
for rel in "${HOOK_PAIRS[@]}"; do
  a="$ROOT/.claude/hooks/$rel"
  b="$ROOT/plugin/manolii-framework/hooks/$rel"
  if ! diff -q "$a" "$b" >/dev/null; then
    echo "FAIL drift: .claude/hooks/$rel != plugin/.../hooks/$rel"
    fail=1
  else
    echo "OK  hooks/$rel"
  fi
done
[[ "$fail" -eq 0 ]]
