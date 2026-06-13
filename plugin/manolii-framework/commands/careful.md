---
name: careful
version: 1.0.0
description: Toggle session-scoped slow-mode (smaller diffs, more confirmation)
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: ['session', 'modal', 'safety']
blast_radius: low
---

# /careful — session-scoped slow mode

Based on proven patterns from the Manolii ecosystem.

Sets a flag in `.ai/session-flags.json` that signals to subsequent agent actions: this session is in elevated-caution mode. Operators invoke this when:
- Editing critical infrastructure (schema, model-routing, security policies)
- Working in an unfamiliar codebase mid-session
- Recovering from a botched edit
- Coaching mode (operator wants to review every diff before it lands)

## Usage

```
/careful on            # enable slow mode
/careful off           # disable
/careful status        # show current state
```

## Effects when enabled

The flag is read by:
1. **PreToolUse hook** (`scripts/pre-tool-use.py`): may surface a warning before Edit/Write/Bash, encouraging the agent to state the intent first.
2. **Agent operation**: agents in this session should:
   - Limit Edit operations to ≤5 lines each (split larger changes)
   - State the next action plus expected file path BEFORE invoking Edit/Write/Bash
   - Call `advisor()` before any SQL, security policy, or `model-routing.json` change (lower threshold than standard)
   - Skip OSS-delegation patterns; stay on main thread for transparency
3. **Stop hook**: invoked on demand each time. To disable, run `/careful off` explicitly. Does NOT persist across sessions.

## Implementation notes for Claude

When operator runs `/careful on`:
1. Read or create `.ai/session-flags.json`.
2. Set `careful_mode: true` and `careful_set_at: <utc-iso>`.
3. Write the JSON back, indented.
4. Confirm: "Slow mode ENABLED. Edits limited to 5 lines; advisor lowered for SQL/security/routing. Disable via /careful off."

When operator runs `/careful off`:
1. Read `.ai/session-flags.json`.
2. Set `careful_mode: false`.
3. Confirm: "Slow mode DISABLED. Standard operation resumed."

When operator runs `/careful status`:
1. Read `.ai/session-flags.json`.
2. Print: "Slow mode: <ON|OFF>. Set at: <ts | not set>."

## What this is NOT

- Not a permission system. Doesn't actually block Edit operations larger than 5 lines — agent self-discipline is the mechanism.
- Not persisted across sessions. The flag file is per-repo but typically gitignored; new sessions start in standard mode.
- Not a substitute for `/freeze`/`/guard` on truly protected paths.

## See also
- `/freeze`, `/guard`, `/unfreeze` — hard path-level enforcement
- CLAUDE.md — project rules and constraints
