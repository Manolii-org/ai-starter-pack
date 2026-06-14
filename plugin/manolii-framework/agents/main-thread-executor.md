---
name: main-thread-executor
version: 1.0.0
description: >
  Main-thread executor for Claude Code sessions.
  Sonnet executor with optional Opus advisor on escalation triggers.
  Handles Claude Code CLI/Desktop/Web sessions.
type: agent
model: claude-sonnet-4-6
tier: anthropic_only
data_sensitivity: anthropic_only
requires_mcp: []
required_entities: []
safety_tier: green
tags:
  - system
  - claude-code
  - main-thread
---

# Main-Thread Executor

**Current model:** `claude-sonnet-4-6` (executor) with `claude-opus-4-7` advisor on escalation triggers.

**Tier:** `heavy-main` — Sonnet executor + Opus advisor via `advisor_pairing` in `.claude/model-routing.json`.

**Escalation:** Advisor invocation is triggered by deterministic rules (file-pattern matching, operation type, size thresholds). Tier classification runs via `scripts/model-routing-suggester.py`.

## Model Routing
| Install | Model | Cost per 1M tokens (in/out) |
|---|---|---|
| Claude-only | claude-sonnet-4-6 (Anthropic direct) | $3.00 / $15.00 |
| Claude + OSS | N/A — main-thread executor requires Anthropic infrastructure | — |

## Tier Routing (Main Thread Only)

| Tier | Model | Advisor | Triggers |
|------|-------|---------|----------|
| `light-main` | Haiku | None | <500 chars, single-file/search |
| `medium-main` | Sonnet | None | 500–2000 chars, no escalation patterns |
| `heavy-main` | Sonnet | Opus | >2000 chars or escalation pattern match |
| `fast-escape` | Opus | None | `/fast` user command only |

### Heavy-main escalation patterns

Eight regex classes promote a prompt to `heavy-main` (Sonnet+Opus advisor):
`multi_repo`, `security_boundary`, `cross_cutting_refactor`, `schema_migration`,
`rollback_incident`, `cost_perf_tradeoff`, `policy_contract`, `architecture_decision`,
plus `governance_judgment` (guards/unfreeze, blast-radius reasoning, auto-merge or
CI gates, data-sensitivity/safety-tier topics).

## Scope

- **Applies:** Claude Code CLI, Claude Code Desktop, Claude Code Web
- **Does NOT apply:** Cursor (drives its own API), Codex CLI, GitHub Copilot, Gemini CLI

## Fallback Discipline

When a step has a conditional precondition (`if X is set, do Y; else mark n/a`), do not
skip on missing precondition. Instead:

1. Try the primary path. If it errors, capture the error verbatim.
2. Try the documented alternative path (or the next-most-likely fallback inferable from
   the script's docstring / `--help`). If it also errors, capture that error too.
3. Only mark `n/a` after documented alternatives are exhausted — include every error
   message so the gap is debuggable.

Prefer over-trying with logged errors over under-trying with silent `n/a`.
