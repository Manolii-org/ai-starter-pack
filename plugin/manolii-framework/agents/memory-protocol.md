---
name: memory-protocol
version: 1.0.0
description: "Shared instructions for sub-agents to contribute discoveries back to persistent memory"
type: agent
model: haiku
tier: tier-1-fast
data_sensitivity: internal
requires_mcp: []
required_entities: []
safety_tier: n/a
tags:
  - memory
  - claude-memory
eval_cases: null
---

# Agent Memory Protocol

Shared instructions so sub-agents can push discoveries back to persistent memory.

## Model Routing
| Install | Model | Cost per 1M tokens (in/out) |
|---|---|---|
| Claude-only | haiku → claude-haiku-4-5-20251001 | $0.30 / $1.20 |
| Claude + OSS | tier-1-fast → DeepSeek V3.2 via Fireworks | $0.56 / $1.68 |

## Sub-agent output format

When a sub-agent finds a fact/decision/gotcha/preference/pattern worth persisting, append this block to its output:

````markdown
## Discoveries

```json
[
  {"type": "fact",       "content": "...", "tags": [...], "confidence": 0.5},
  {"type": "gotcha",     "content": "...", "tags": [...], "confidence": 0.5},
  {"type": "decision",   "content": "...", "tags": [...], "confidence": 0.5},
  {"type": "preference", "content": "...", "tags": [...], "confidence": 0.5},
  {"type": "pattern",    "problem": "...", "solution": "...", "rule": "...", "tags": [...], "confidence": 0.5}
]
```
````

Allowed `type` values for `facts.jsonl`: `fact`, `gotcha`, `decision`, `preference`. `patterns.jsonl` uses `pattern`.

Rules:
1. Confidence always starts at 0.5 (parent may boost on reinforcement).
2. Never include secrets, credentials, or PII.
3. Only flag items worth surviving across sessions. Skip: routine successes, content already in CLAUDE.md / persistent-instructions, one-off debug steps, raw errors without analysis.
4. Tag with domain keywords that aid retrieval.

## Parent orchestrator flow

On receiving a `## Discoveries` block:

1. Parse the JSON array.
2. **Validate `type` against the allowed sets and reject any entry whose `type` is not one of the allowed values below.**
   - facts: `fact`, `gotcha`, `decision`, `preference`
   - patterns: `pattern`
   Only validated entries proceed past this step.
3. Add `"source": "agent:{agent_type}"` and `"created": "{ISO_TIMESTAMP}"` to each entry.
4. Deduplicate against `.ai/memory/facts.jsonl` and `.ai/memory/patterns.jsonl`.
5. Append:
   - `fact` / `gotcha` / `decision` / `preference` → `facts.jsonl` (keep original `type`)
   - `pattern` → `patterns.jsonl`
6. Report what was saved to the user (and what was rejected, if any).
