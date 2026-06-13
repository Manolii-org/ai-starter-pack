---
description: "Chains all session-end cleanup commands in sequence"
---

# /wrap-up — End-of-Session Orchestrator

Chains all session-end cleanup commands in sequence. Run this instead of remembering to invoke each command separately.

## Steps

### Step 1: Extract insights
Run the `/extract-insights` workflow to auto-capture decisions, preferences, and gotchas from this session's conversation. Review the extracted items — correct any misclassifications before they're persisted.

### Step 2: Session summary
Run the `/session-summary` workflow to log session metadata (branch, files modified, decisions, unresolved issues).

### Step 3: Memory consistency check
Quick health check on the memory system:
1. Count total entries in `.ai/memory/facts.jsonl`
2. Count entries with `reviewed: false` (auto-extracted, need manual promotion via `/remember`)
3. List any files in `.ai/memory/retry/` — these are failed write-through envelopes that need manual re-processing (retry processing is not automated; flag them for the user)
4. Report: "{N} facts total, {M} unreviewed, {P} pending retry (manual processing required)"

### Step 4: Regenerate knowledge index (if not already done)
If Step 2 (`/session-summary`) completed and you saw the "Session logged" confirmation, the index was already regenerated. Otherwise, run:
```bash
bash scripts/generate-knowledge-index.sh
```

### Step 5: Flag items for next session
Review and output a "Next session pickup" section:
- Unresolved issues from the session summary just created
- Any pending retries from `.ai/memory/retry/`
- Any entries with `reviewed: false` that need human review
- Any patterns with confidence >= 0.8 and `reinforced_at` within the last 14 days (approaching promotion threshold)

## Output

Present a concise summary:
```
## Session Wrap-Up Complete

**Insights extracted:** {N} items
**Session logged:** {date} on branch {branch}
**Memory health:** {N} facts, {M} unreviewed
**Knowledge index:** regenerated
**Next session:** {list of items to pick up}
```
