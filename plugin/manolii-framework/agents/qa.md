---
name: qa
version: 1.0.0
description: "QA sub-agent for visual testing, UI verification, and end-to-end browser flows"
type: agent
model: haiku
tier: tier-1-fast
data_sensitivity: internal
requires_mcp:
  - playwright
required_entities: []
safety_tier: green
tags:
  - browser
  - playwright
  - automation
eval_cases: null  # TODO: add eval cases
---

# QA Agent

Sub-agent with access to Playwright for browser-based QA testing.
Use this agent for visual testing, UI verification, end-to-end browser flows.

## Model Routing
| Install | Model | Cost per 1M tokens (in/out) |
|---|---|---|
| Claude-only | haiku → claude-haiku-4-5-20251001 | $0.30 / $1.20 |
| Claude + OSS | tier-1-fast → DeepSeek V3.2 via Fireworks | $0.56 / $1.68 |
