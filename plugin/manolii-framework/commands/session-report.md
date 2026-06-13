---
description: "Generate a session retrospective and save it"
---

# Session Report — Retrospective

Generate a session retrospective and save it.

## Gather

1. Read `.git/.session-state.json` for edit count, modified files, session duration
2. Review recent git log for this session's commits
3. Check for any unresolved issues or TODO comments added

## Report Structure

```markdown
# Session Report — {DATE}

## Summary

{1-2 sentence overview of what was accomplished}

## Changes Made

{List of files modified with brief description of each change}

## Decisions

{Key decisions made and their rationale}

## Issues Encountered

{Problems hit and how they were resolved}

## Patterns Learned

{Any new patterns or insights worth remembering}

## Open Items

{Unfinished work, known issues, next steps}
```

## Save

Save to `.claude/reports/session-{YYYY-MM-DD-HHMMSS}.md`
