---
name: verify-work
version: 1.0.0
description: Run work-critic or quick-critic to verify current work quality. Use --deep for full work-critic.
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags:
  - system
  - workflow
eval_cases: session-critic/
---

# /verify-work

Use `.claude/skills/verify-work.md`. Do not dispatch `quick-critic`/`work-critic` via `Agent()` on the default plane.

eval_cases live under `.ai/evals/session-critic/`.

Expected input: optional `--deep` flag for work-critic (default: quick-critic).

Required behavior:
1. Dispatch `Agent(subagent_type="quick-critic")` on the diff (`git diff HEAD`)
2. For `--deep`: dispatch `Agent(subagent_type="work-critic")` on the same diff
3. For default: use the quick-critic dispatch
4. Return findings with specific file paths and line numbers; do not apply fixes unless explicitly asked.
5. Do not commit, push, or make assumptions about intent beyond the visible diff/plan.
