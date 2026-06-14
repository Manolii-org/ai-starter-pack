---
description: "Expire stale low-confidence patterns that were only seen once and never re-confirmed"
---

# /prune

Expire stale low-confidence patterns that were only seen once and never re-confirmed.

1. Read `.ai/memory/patterns.jsonl`
2. Identify patterns where:
   - `last_seen` is more than 60 days ago, AND
   - `reinforced` count is 1 (never confirmed), AND
   - `confidence` < 0.7
3. Move expired patterns to `.ai/memory/archive/patterns-expired-{YYYY-MM-DD}.jsonl`
4. Rewrite `.ai/memory/patterns.jsonl` with only the retained patterns
5. Report: how many patterns reviewed, how many expired, how many retained
6. Do NOT delete — archive only. Patterns can be recovered.
