---
name: plan-to-codex
version: 1.0.0
description: "Create a structured Codex execution plan and print the handoff prompt."
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: [workflow, orchestration]
eval_cases: null
---

# /plan-to-codex

Use the `plan-to-codex` skill in `.claude/skills/plan-to-codex/SKILL.md`.

Write the current design discussion into `reports/plans/<slug>-<YYYY-MM-DD>.md` using `reports/plans/_template.md`, then print the copy-paste Codex bootstrap prompt. Do not invoke Codex.
