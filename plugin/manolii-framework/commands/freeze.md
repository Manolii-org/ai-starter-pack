---
name: freeze
version: 1.0.0
description: Add a path-level edit guard (alias for /guard add)
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: ['safety', 'guards', 'alias']
blast_radius: low
---

# /freeze — add a path edit guard

Based on proven patterns from the Manolii ecosystem.

Thin alias for `/guard add`. Adds an entry to `.ai/guards.json` so future Edit/Write/NotebookEdit/Bash operations targeting the path are blocked until `/unfreeze` is invoked.

## Usage

```
/freeze <path-or-glob> --reason "<text>"
```

Auto-derives the guard id from the last path component (e.g. `lib/safety.ts` → id `safety-ts`). Operator can override with `--id <kebab-name>`.

## Implementation notes for Claude

1. Read `.ai/guards.json`.
2. If a guard with the derived/supplied id already exists, append a numeric suffix (`safety-ts-2`, `safety-ts-3`, ...).
3. Append the new guard with `default: false`. Operator-added guards are session-time additions, not pre-shipped policy.
4. Write the updated JSON back, indented.
5. Print: `Frozen: <id> → <path>. Use /unfreeze <id> --reason "..." to temporarily permit edits.`

## When to use

- Stable refactor: freeze the files NOT being changed so an over-eager agent doesn't drift outside scope.
- Critical migration: freeze adjacent migration files to prevent accidental edits.
- WIP work: freeze files you don't want re-touched until reviewed.

## See also

- `/guard list` — view all guards
- `/guard remove <id>` — remove a non-default guard
- `/unfreeze <id> --reason "..."` — temporarily permit edits
