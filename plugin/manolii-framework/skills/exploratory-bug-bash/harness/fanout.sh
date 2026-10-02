#!/usr/bin/env bash
# usage: fanout.sh <charters-file> <outdir> [parallel]
# Charter line: slug|target|agent|charter. "Sign in as session <name>" in the
# charter selects the saved session. Exit 0 when every charter exited 0 or 1
# (1 = candidate reported); nonzero when any charter errored (setup, auth, crash).
set -u
cd "$(dirname "$0")" || exit 1
mkdir -p "$2"
while IFS='|' read -r slug target agent charter; do
  case "$slug" in ''|'#'*) continue ;; esac
  printf '%s\0%s\0%s\0%s\0' "$slug" "$target" "$agent" "$charter"
done < "$1" | OUT="$2" xargs -0 -n 4 -P "${3:-4}" sh -c \
  'sess=$(printf %s "$4" | sed -n "s/^Sign in as session \([a-z0-9-]*\).*/\1/p"); ./run.py explore "$4" --target "$2" --agent "$3" ${sess:+--session "$sess"} --output "$OUT/$1" --max-steps 6 --timeout 600000 --video --reporter list,markdown < /dev/null > "$OUT/$1.log" 2>&1; rc=$?; echo "$rc" > "$OUT/$1.exit"; [ "$rc" -le 1 ]' _
status=$?
for f in "$2"/*.exit; do
  [ -e "$f" ] || continue
  printf '%s exit=%s\n' "$(basename "$f" .exit)" "$(cat "$f")"
done > "$2/exits.txt"
exit "$status"
