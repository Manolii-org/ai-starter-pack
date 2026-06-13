# /compact-review — Compaction System Health Report

Read `.ai/compact-metrics.jsonl` and generate a health report on how the smart compaction system is performing. Run this periodically (weekly, or when token usage feels high) to detect drift and tune thresholds.

## Steps

### 1. Read metrics file

```bash
cat .ai/compact-metrics.jsonl 2>/dev/null || echo "(no metrics yet)"
```

### 2. Analyse and report

Compute from the JSONL entries:

**Trigger distribution** — count each `trigger` value:
- `auto-95pct` (auto-compact fired = milestone/counter missed it — this should be rare)
- `git-push`, `pr-created` (Tier-1 milestones)
- `counter-40` (bulk fallback)
- `manual` (user ran /smart-compact directly)

**Auto-compact miss rate** — `auto-95pct` count ÷ total compactions × 100%
- Target: < 10%. Higher means milestone detection is missing real boundaries.

**Average calls/cycle** — mean of `calls_since_compact` across all `compact-recommended` events
- Target: 25–55 calls. Below 25 = over-compacting. Above 55 = under-compacting.

**Summary size** — mean and max `summary_chars` from `compaction-complete` events
- Target: 3000–6000 chars. Above 8000 = summary bloat (tighten PreCompact instructions).

### 3. Output health report

Format:
```
COMPACT SYSTEM HEALTH REPORT
Last N events | Date range: X to Y

Trigger breakdown:
  git-push:       N (X%) [target: majority]
  counter-40:     N (X%)
  auto-95pct:     N (X%) ⚠️ if > 10%
  manual:         N (X%)

Avg calls/cycle:   N (target: 25–55) [✓ / ⚠️ too low / ⚠️ too high]
Auto-compact miss: X% (target: <10%) [✓ / ⚠️]
Avg summary size:  N chars (target: 3–6k) [✓ / ⚠️]

Recommendations:
[Only include if there's something to tune — be specific]
```

### 4. Tune if needed

- **Miss rate > 10%**: Review which tool calls preceded auto-compact misses. Add those patterns to `.claude/hooks/compact-trigger.py` TIER1 tools or the git push detection.
- **Calls/cycle < 20**: Raise `MIN_CALLS_BEFORE_TRIGGER` in `.claude/hooks/compact-trigger.py`.
- **Calls/cycle > 60**: Lower `COUNTER_TRIGGER_THRESHOLD` (e.g., 30 instead of 40).
- **Summary size > 8000 chars**: Tighten the DISCARD section in `.claude/hooks/pre-compact.sh`.
