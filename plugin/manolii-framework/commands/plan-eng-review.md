---
name: plan-eng-review
version: 1.0.0
description: Engineering review of a plan — haiku checklist by default; escalates to architecture-impact + systems-consistency on triggers
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: ['plan-review', 'engineering']
blast_radius: low
---

# /plan-eng-review — engineering plan review

Based on proven patterns from the Manolii ecosystem.

Two-tier review: haiku checklist on most plans; sonnet escalation (`architecture-impact` + `systems-consistency` agents) only when triggers fire.

## Trigger conditions for sonnet escalation

ANY of:
- Plan touches >5 files OR >300 lines
- Plan modifies `model-routing.json`, security policies, database migrations, deployment config
- Plan adds a new MCP server or hook
- Plan crosses repo boundaries (multi-repo)
- Operator passed `--escalate` flag

If none triggered: haiku checklist only.

## Haiku checklist

Default route. Single haiku call (~2k tokens) reads the plan and emits findings under:

- **Type safety:** non-nullable types matched by NOT NULL constraints? Defensive parsing at external boundaries?
- **Error handling:** try/except blocks specific, not bare? Errors logged with PII redacted?
- **Idempotency:** retries safe? Migrations rerunnable?
- **Test coverage:** new functions paired with test files? Existing critical paths still covered?
- **Routing compliance:** restricted-tier agents stay on Anthropic? OSS-tier choices match data sensitivity?
- **Hooks impact:** changes to PreToolUse/PostToolUse/Stop maintain fail-open semantics?

Output: markdown findings to `.ai/sprints/<sprint-id>/plan-eng-review.md` (or `.ai/reviews/<topic>-<date>.md` outside a sprint).

## Sonnet escalation

When triggered, dispatch in parallel:
1. `architecture-impact` agent — checks downstream caller counts, god-node status on changed public symbols
2. `systems-consistency` agent — reads full deployment surface and verifies cross-file invariants

Both run on sonnet. Combined findings appended to the same review file.

## Implementation notes for Claude

1. Read the plan file (or sprint manifest tasks).
2. Compute trigger metrics: file count, line count, paths-touched.
3. If no triggers → dispatch generate(haiku) with the checklist prompt.
4. If triggers → dispatch architecture-impact + systems-consistency in parallel; THEN dispatch generate(haiku) for the standard checklist; merge findings.
5. Write the combined output. Print 1-line summary + path to file.

## What this is NOT

- Not the pre-PR quality gate (that's `docs/pre-pr-quality-gate.md`, runs against the diff, not the plan).
- Not architecture review at the strategic level — that's `/plan-review` or `/office-hours`.

## See also
- `/plan-design-review` — UI/UX review
- `/plan-devex-review` — operator-experience checklist
- `docs/pre-pr-quality-gate.md` — diff-stage gate (after implementation)
