---
name: guard
version: 1.0.0
description: Manage path-level edit guards in .ai/guards.json
type: command
requires_mcp: []
required_entities: []
safety_tier: amber
tags: ['safety', 'guards', 'tooling']
blast_radius: medium
---

# /guard — manage path-level edit guards

Based on proven patterns from the Manolii ecosystem.

Edits the manifest at `.ai/guards.json`. Guards block edits to specific paths/regions until explicitly unfrozen. Enforced by `scripts/guard_check.py` via the PreToolUse hook.

## Subcommands

### `/guard list`
Read `.ai/guards.json` and print each guard as: `<id> | <paths> | <reason> | default=<bool>`.

### `/guard add <id> <path-or-glob> --reason "<text>"`
Append a new guard entry to `.ai/guards.json` with:
- `id`: kebab-case, unique
- `paths`: array containing the user-supplied glob (single string converted to array)
- `reason`: the text the user supplied
- `default`: `false` for operator-added guards (session-time, not pre-shipped). Include this field in all new guard entries.

Refuse if `id` already exists; tell the operator to pick a different id.

### `/guard remove <id>`
Find the guard by id and remove it from the array. If `default: true`, REFUSE — pre-shipped guards cannot be removed via this command (require manual edit + advisor sign-off).

## Implementation notes for Claude

- Always read the current `.ai/guards.json` first; never assume schema.
- Validate JSON before writing (use Python's `json.dumps(obj, indent=2)`).
- After any modification, print a one-line confirmation: `Guard <id> added/removed.` and a 3-line summary of remaining guards.
- Consider adding a `guards-config` guard to protect `.ai/guards.json` from accidental modification. Editing a guarded file requires `/unfreeze <id> --reason "..."` first.

## See also
- `/freeze <id>` — thin alias for `/guard add`
- `/unfreeze <id> --reason "..."` — temporarily permit edits to a guarded path
- `.ai/guards.json` — manifest source of truth
