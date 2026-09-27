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

Dispatch a critic agent on the current work. The pack ships `quick-critic`
(haiku — routine checks) and `work-critic` (sonnet — adversarial, for
high-stakes diffs and plans) under `.claude/agents/`; eval cases live under
`.ai/evals/session-critic/`.

Expected input: optional `--deep` flag for work-critic (default: quick-critic).

Required behavior:

1. Gather the target: `git diff HEAD` (or the staged diff / plan under review).
2. Default: `Agent(subagent_type="quick-critic", model="haiku", description="Verify current work", prompt="<the diff/plan>")`.
3. For `--deep`: `Agent(subagent_type="work-critic", model="sonnet", description="Deep work-critic review", prompt="<the diff/plan>")`.
   Always pass `model=` explicitly — an omitted model inherits the parent tier.
4. Return findings verbatim with specific file paths and line numbers; do not apply fixes unless explicitly asked.
5. Do not commit, push, or make assumptions about intent beyond the visible diff/plan.
