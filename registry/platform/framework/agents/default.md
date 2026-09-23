---
name: default
version: 1.0.0
description: "General-purpose sub-agent for research, search, file exploration, and simple multi-step tasks"
type: agent
model: haiku
tier: tier-1-fast
data_sensitivity: public
safety_tier: green
tags:
  - general
  - research
  - search
---

# Default Agent

General-purpose sub-agent for research, exploration, and lookup tasks. Uses `haiku` for fast, low-cost execution of search, grep, glob, and simple exploration tasks.

Excludes infrastructure MCP servers (database, secrets managers) to reduce startup time and avoid timeouts. If you need database or infrastructure access, use the specialized agents instead.

## Model Routing
| Install | Model | Cost per 1M tokens (in/out) |
|---|---|---|
| Claude-only | haiku → claude-haiku-4-5-20251001 | $0.30 / $1.20 |
| Claude + OSS | tier-1-fast → DeepSeek V3.2 via Fireworks | $0.56 / $1.68 |

## Best For

- File and codebase search (glob, grep, read)
- Answering questions about existing code
- Summarizing a module, file, or function
- Simple one-shot lookups
- Generating boilerplate from clear specs

## Not For

- Complex reasoning or architecture decisions → use `deep-analyse` or spawn an `opus` agent
- Security-sensitive code (auth, crypto, payment flows) → use `review` or `sonnet` directly
- Infrastructure changes (deploys, secrets, migrations) → use the appropriate infra agent

## Output Constraints

Always include explicit output constraints in prompts to this agent:
- Word/line limit: `"Report in under 200 words"` or `"List max 10 items"`
- Format: `"Return as a Markdown table"` or `"List one file per line"`

Without these, `haiku` produces 2–3× more output tokens than `sonnet`/`opus` for the same task.
