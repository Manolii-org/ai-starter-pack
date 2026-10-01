#!/usr/bin/env bash
# usage: fanout.sh <charters-file> <outdir> [parallel]
# Charter line: slug|target|agent|charter. "Sign in as session <name>" in the
# charter selects the saved session.
set -u
cd "$(dirname "$0")"
mkdir -p "$2"
while IFS='|' read -r slug target agent charter; do
  case "$slug" in ''|'#'*) continue ;; esac
  printf '%s\0%s\0%s\0%s\0' "$slug" "$target" "$agent" "$charter"
done < "$1" | OUT="$2" xargs -0 -n 4 -P "${3:-4}" sh -c \
  'sess=$(printf %s "$4" | sed -n "s/^Sign in as session \([a-z0-9-]*\).*/\1/p"); ./run.py explore "$4" --target "$2" --agent "$3" ${sess:+--session "$sess"} --output "$OUT/$1" --max-steps 6 --timeout 600000 --video --reporter list,markdown < /dev/null > "$OUT/$1.log" 2>&1; echo "$1 exit=$?" >> "$OUT/exits.txt"' _
