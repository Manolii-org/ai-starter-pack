#!/usr/bin/env bash
# generate-knowledge-index.sh — rebuild .ai/knowledge-index.md from memory files.
#
# Scans .ai/memory/*.jsonl and produces a structured Markdown index of:
#   - High-confidence facts (confidence >= 0.7)
#   - Active patterns (not archived)
#   - Recent sessions (last 10)
#
# Usage:
#   bash scripts/generate-knowledge-index.sh
#   bash scripts/generate-knowledge-index.sh --all   # include low-confidence facts
#
# Run automatically by /wrap-up. Safe to run at any time — read-only on memory files.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AI_DIR="$REPO_ROOT/.ai"
MEMORY_DIR="$AI_DIR/memory"
OUTPUT="$AI_DIR/knowledge-index.md"
MIN_CONFIDENCE="${MIN_CONFIDENCE:-0.7}"

for arg in "$@"; do
  [[ "$arg" == "--all" ]] && MIN_CONFIDENCE=0
done

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

timeout 20s python3 - <<PYEOF
import json, sys, os
from pathlib import Path
from datetime import datetime

repo_root  = Path("$REPO_ROOT")
memory_dir = Path("$MEMORY_DIR")
output     = Path("$OUTPUT")
min_conf   = float("$MIN_CONFIDENCE")
ts         = "$TS"

output.parent.mkdir(parents=True, exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out

facts    = load_jsonl(memory_dir / "facts.jsonl")
patterns = load_jsonl(memory_dir / "patterns.jsonl")
sessions = load_jsonl(memory_dir / "sessions.jsonl")

# Filter
facts_filtered = [
    f for f in facts
    if f.get("confidence", 0) >= min_conf and not f.get("archived", False)
]
facts_filtered.sort(key=lambda x: x.get("confidence", 0), reverse=True)

patterns_active = [p for p in patterns if not p.get("archived", False)]
patterns_active.sort(key=lambda x: x.get("confidence", 0), reverse=True)

def _session_sort_key(s):
    return s.get("ts") or s.get("session_date") or s.get("created") or ""

sessions_recent = sorted(sessions, key=_session_sort_key, reverse=True)[:10]

lines = [
    "# Knowledge Index",
    "",
    f"_Auto-generated {ts} by \`scripts/generate-knowledge-index.sh\`. Do not edit manually._",
    f"_Min confidence: {min_conf}  |  Facts: {len(facts_filtered)}  |  Patterns: {len(patterns_active)}  |  Sessions shown: {len(sessions_recent)}_",
    "",
]

# Facts section
lines += ["## Facts & Decisions", ""]
if facts_filtered:
    lines.append("| Confidence | Type | Content | Tags |")
    lines.append("|------------|------|---------|------|")
    for f in facts_filtered[:50]:  # cap at 50 rows
        conf  = f"**{f.get('confidence',0):.2f}**" if f.get('confidence',0) >= 0.8 else f"{f.get('confidence',0):.2f}"
        ftype = f.get("type", "fact")
        content = str(f.get("content", f.get("fact", ""))).replace("|", "\\|").replace("\n", " ")[:120]
        tags  = ", ".join(f.get("tags", []))
        lines.append(f"| {conf} | {ftype} | {content} | {tags} |")
else:
    lines.append("_No facts yet. Use \`/remember\` to start capturing decisions._")

lines += [""]

# Patterns section
lines += ["## Patterns", ""]
if patterns_active:
    lines.append("| Confidence | Pattern | Tags |")
    lines.append("|------------|---------|------|")
    for p in patterns_active[:30]:
        conf    = f"{p.get('confidence',0):.2f}"
        content = str(p.get("content", p.get("pattern", ""))).replace("|", "\\|").replace("\n", " ")[:120]
        tags    = ", ".join(p.get("tags", []))
        lines.append(f"| {conf} | {content} | {tags} |")
else:
    lines.append("_No patterns yet. Use \`/learn\` after solving a tricky problem._")

lines += [""]

# Sessions section
lines += ["## Recent Sessions", ""]
if sessions_recent:
    for s in sessions_recent:
        branch  = s.get("branch", "unknown")
        date    = (s.get("ts") or s.get("session_date") or "")[:10]
        summary = s.get("summary") or s.get("description") or "; ".join(s.get("decisions", [])[:2])
        files   = s.get("files_changed") or s.get("files_modified") or []
        lines.append(f"- **{date}** \`{branch}\` — {summary}")
        if files:
            lines.append(f"  Files: {', '.join(str(f) for f in files[:5])}")
else:
    lines.append("_No sessions yet. Use \`/session-summary\` at the end of each session._")

lines.append("")

output.write_text("\n".join(lines))
print(f"Knowledge index written to {output}")
print(f"  {len(facts_filtered)} facts  |  {len(patterns_active)} patterns  |  {len(sessions_recent)} sessions")
PYEOF
