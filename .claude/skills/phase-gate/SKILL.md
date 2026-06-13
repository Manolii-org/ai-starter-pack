---
name: phase-gate
version: 1.0.0
description: "Evaluate phase gate invariants before advancing a multi-phase project. Advisory only; returns PASS/FAIL checklist for one specific phase transition."
type: skill
model: claude-sonnet-4-6
data_sensitivity: internal
max_tokens: 600
safety_tier: green
tools:
  - Read
  - Bash
tags:
  - advisory
  - migration
  - project-gates
disallowed-tools:
  - Edit
  - Write
  - NotebookEdit
---

# /phase-gate

**Trigger:** `/phase-gate <project-slug> <phase-number>`
**Example:** `/phase-gate myproject 2`

Advisory skill — evaluates phase gate invariants before advancing a multi-phase project.
Never blocks tool execution; returns a structured PASS/FAIL checklist.

## Differentiation

- `/diagnose` — searches memory and traces for past errors (memory-oriented, backward-looking)
- `/investigate` — open-ended codebase investigation
- `/completeness-check` — broad structured invariant check across all phases + datastore health + file presence
- `/phase-gate` — evaluates invariants for ONE specific phase transition (forward-looking gate)

## Input

- `<project-slug>` — project slug (e.g. `myproject`)
- `<phase-number>` — the phase you are about to START (invariants from phase N must pass before starting N+1)

## Procedure

1. **Load phase definitions** from `.ai/project-checkpoints/<slug>-phases.yaml` if it exists.
   Fall back to hardcoded defaults for known project types (see defaults below).

2. **Fetch current facts** via `.ai/sessions/active-task.json` or `.ai/project-checkpoints/latest-session-facts.json`.
   Note the source in the output.

3. **Evaluate each invariant** for the given phase:
   - `gte` — value >= threshold
   - `eq` — value == expected
   - `exists` — key is present with any non-null value

4. **Emit structured checklist:**

```
PHASE GATE: <slug> → Phase <N>
Source: active-task.json | As of: <timestamp>

✅ PASS: <invariant> (<value>)
❌ FAIL: <invariant> <value> < <threshold> threshold
...
VERDICT: PASS — all invariants met. Safe to proceed to Phase <N>.
         or
VERDICT: BLOCKED — <N> invariant(s) unmet. Do not proceed to Phase <N>.
```

## Default invariants (multi-phase projects)

Phase 1 → Phase 2 gate (completeness):
- `phase_1_complete == "true"`
- `audit_run == "true"`

Phase 2 → Phase 3 gate (acceptance):
- `phase_2_complete == "true"`
- `stakeholder_sign_off == "true"`

## Fallback behavior

If no fact source is available AND `.ai/project-checkpoints/latest-session-facts.json` is missing
or older than 7 days, emit:

```
PHASE GATE: <slug> → Phase <N>
⚠️  WARNING: No fact source available (no active checkpoint).
Cannot evaluate invariants. Do not proceed without manual verification.
VERDICT: INDETERMINATE
```

## Notes

- This skill is **advisory only** — it does not prevent tool calls
- Phase gate results should be captured as a note if applicable
- For projects without a `<slug>-phases.yaml`, the operator should create one based on
  `tools/templates/multi-phase-project/phases.yaml` before the next phase begins
