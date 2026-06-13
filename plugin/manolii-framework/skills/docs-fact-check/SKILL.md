---
name: docs-fact-check
version: 2.0.0
description: "Checks .md file diffs for prose claims about system behaviour and verifies each claim against the actual code or config."
type: skill
model: haiku
advisor_model: claude-sonnet-4-6
data_sensitivity: internal
max_tokens: 800
safety_tier: green
requires_mcp: []
required_entities: []
tools:
  - Read
  - Grep
  - Bash
tags:
  - pr-assessment
  - specialist
intent_phrases:
  - "check the docs"
  - "verify this documentation"
  - "are these claims accurate"
  - "fact-check this"
---

# Skill: Docs Fact Check

Narrow specialist for `.md` file diffs. Invoked by `pr-classifier` when Markdown files contain prose claims about system behaviour.

## Input

- Diff of `.md` files from the PR

## Core Rule

> For every prose claim about system behaviour in the changed Markdown, grep the actual code/config to verify the claim is true. Flag mismatches as findings.

## What Counts as a "Prose Claim About System Behaviour"

Examples of claims to verify:
- "model X routes to provider Y" → check model routing config
- "timeout is set to N seconds" → grep for timeout config value
- "the pipeline runs at :15 past each hour" → check cron schedule
- "tool A accepts parameter B" → grep function/schema definition
- "migration 00009 creates table X" → read the migration file

Do NOT flag:
- Conceptual/architectural descriptions that are not falsifiable against code
- Marketing or aspirational language ("designed to", "aims to")
- Version history or changelog entries
- Hypothetical examples in code blocks

## Verification Method

For each identified claim:
1. Identify the canonical source file (config, agent definition, migration, cron schedule)
2. Use `Grep` or `Read` to verify the claim against that source
3. If the claim is TRUE → no finding
4. If the claim is FALSE or UNVERIFIABLE → add finding

Use `Bash` with `git grep` for broad searches: `git grep -r "timeout" --include="*.yaml"`.

## Output Schema

```json
{
  "source": "docs-fact-check",
  "findings": [
    {
      "file": "docs/architecture.md",
      "line": 87,
      "severity": "ERROR|WARNING",
      "message": "Docs claim 'timeout is 30s' but config sets it to 60s — verify against config/defaults.yaml",
      "fix": "Update claim to match the canonical value in config/defaults.yaml"
    }
  ]
}
```

Return `{"source": "docs-fact-check", "findings": []}` if no verifiable claims are present or all claims check out. Never return prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | Claim directly contradicts the code/config — would mislead developers into wrong assumptions |
| WARNING | Claim is outdated or imprecise but not actively harmful |

## Phase 1: Executor

For each identified prose claim:
- Apply detection logic exactly as specified
- Draft a finding entry with file, line, severity, and message
- Rate self-confidence: `high` | `medium` | `low`

## Phase 2: Advisor

Review each draft finding:
1. **Validity check:** Is this genuinely a prose claim about system behaviour?
2. **Evidence check:** Is the verification method sound? Was the right canonical source checked?
3. **Severity check:** Does the misleading statement magnitude match the rating?

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Phase 3: Final Output

Keep only findings confirmed by advisor. Sort: ERROR before WARNING, then by advisor_confidence (high first).
