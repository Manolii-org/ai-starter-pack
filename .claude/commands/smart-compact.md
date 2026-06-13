# /smart-compact — Intelligent Context Compaction

Prepare a structured session summary, then compact context to preserve quality while reducing token usage. Run this at task boundaries, after a git push/PR, or when the compact-trigger hook recommends it.

## Before compacting

Check: are there any open todos or incomplete steps in the current plan?
- **If yes:** Complete the current task first, then run /smart-compact.
- **If no:** Proceed immediately.

## Step 1: Generate session state summary

Produce a structured summary in this exact format (be terse — each section max 5 bullet points):

```
## ACTIVE TASK
[Current task, branch, PR number/URL/status]

## REMAINING PLAN
[Any steps not yet done — list only, no detail]

## DECISIONS + WHY
[Decision 1: <what> — <why in one sentence>]
[Decision 2: ...]

## MODIFIED FILES
[file/path: what changed — not the content]

## BLOCKERS & DEFERRED
[Any unresolved errors, open questions, deferred work]

## PATTERNS DISCOVERED
[Any gotchas, constraints, or patterns learned this session]
```

## Step 2: Compact

After outputting the summary above, tell the user:

> Summary prepared. Run `/compact` now — the summary above is in context and will be preserved during compaction. After compacting, the session will continue with a lean context. To resume a loop: `/loop 30m /smart-compact`
