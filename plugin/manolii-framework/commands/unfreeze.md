---
name: unfreeze
version: 1.0.0
description: Temporarily permit edits to a guarded path (auto-clears at next SessionStart)
type: command
requires_mcp: []
required_entities: []
safety_tier: amber
tags: ['safety', 'guards', 'session']
blast_radius: medium
---

# /unfreeze — temporarily permit edits to a guarded path

Based on proven patterns from the Manolii ecosystem.

Adds a guard id to the `session_unfreezes` array in `.ai/guards.json`. The PreToolUse hook will allow edits to that guard's paths and log each event to `.ai/bypass-log.jsonl` (scope: `pretool/guard-unfreeze`). SessionStart clears the array before a new session begins, so the bypass remains usable across turns but never carries into the next session.

## Usage

```
/unfreeze <guard-id> --reason "<text>"
```

The reason is mandatory. Future Edits/Writes/Bash commands targeting paths under that guard will be permitted — every permitted edit appends to the bypass log.

## Implementation notes for Claude

1. Read `.ai/guards.json`.
2. Verify a guard with that id exists. If not, list all guard ids and tell the operator.
3. Append `<guard-id>` to `session_unfreezes` array if not already present.
4. Refuse if `--reason` is empty or missing.
5. Append a marker entry to `.ai/bypass-log.jsonl` immediately:
    ```
    {"ts": "<utc-iso>", "scope": "command/unfreeze", "head": "<git head>", "user": "operator", "reason": "unfreeze:<id>:<operator-reason>"}
    ```
6. Print: `Unfrozen: <id>. Edits permitted for this session; the next SessionStart clears it. All edits will be logged.`

## What this is NOT

- Not a permanent guard removal — use `/guard remove` for that (pre-shipped guards cannot be removed via that path).
- Not a global bypass — only the named guard's paths are unfrozen; other guards still enforce.
- Not silent — every permitted edit while unfrozen creates a bypass-log entry.

## SessionStart auto-clear

`.claude/hooks/session-start.sh` resets `session_unfreezes` to `[]` at the start of every session. If a later session needs the same bypass, run the command again.

## See also
- `/guard list` — view all guards
- `/freeze <path>` — add a session-time guard
- `.ai/bypass-log.jsonl` — audit trail of permitted edits
