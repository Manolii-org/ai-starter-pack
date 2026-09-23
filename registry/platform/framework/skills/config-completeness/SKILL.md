---
name: config-completeness
version: 2.0.0
description: "Checks config.yaml, fly.toml, and routing JSON diffs for missing sibling parameters — when one entry in a group gets a param, all siblings must too."
type: skill
disable-model-invocation: true  # slash/CI-invoked checklist — removed from model-facing catalogue to cut per-session tokens (2026-07-06); delete this line to restore auto-invocation
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
  - "check the config"
  - "is this configuration complete"
  - "verify the config file"
  - "missing config"
---

# Skill: Config Completeness

Narrow specialist for `config.yaml`, `fly.toml`, and routing JSON diffs. Invoked by `pr-classifier` when these files are changed with additions or modifications to sibling entries.

## Input

- Full diff of `config.yaml`, `fly.toml`, or routing JSON files (NOT just changed lines — fetch the full file via `Read` to evaluate siblings)

## Core Rule

> When a parameter is applied to one entry in a list or map (e.g. `timeout: 600` on one model entry), verify ALL sibling entries in the same logical group have it. List every sibling that is missing the parameter.

**Do not flag** if the omission is intentional based on an adjacent comment (e.g. `# no timeout needed — synchronous`).

## Checks

1. **Sibling parameter drift in YAML lists** — When a new key is added to one item in a YAML sequence, check every other item in the same sequence for the same key. Report each missing one as a separate finding.

2. **Sibling parameter drift in TOML tables** — When a new key is added under one `[[services]]` or `[[mounts]]` block in `fly.toml`, verify the same key (if applicable) appears in all sibling blocks.

3. **Routing JSON completeness** — When a new field is added to one entry in a JSON array/object, verify all sibling entries have the same field.

## Important

- Use `Read` to fetch the **full** config file, not just the diff. You need the full sibling list to detect omissions.
- Only flag meaningful configuration keys (timeouts, resource limits, retry counts, sensitivity labels). Do not flag cosmetic fields like `label`, `description`, or `comment`.

## Output Schema

```json
{
  "source": "config-completeness",
  "findings": [
    {
      "file": "config.yaml",
      "line": null,
      "severity": "ERROR|WARNING",
      "message": "model 'gpt-4o' added timeout: 600 but sibling entries 'gpt-4o-mini', 'gpt-3.5-turbo' are missing timeout",
      "fix": "Add timeout: 600 to gpt-4o-mini and gpt-3.5-turbo entries"
    }
  ]
}
```

Return `{"source": "config-completeness", "findings": []}` if no issues found. Never return prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | Missing parameter would cause inconsistent runtime behaviour (e.g. one model has a timeout guard, others don't — request to unguarded model hangs forever) |
| WARNING | Inconsistency is cosmetic or low-risk |

## Phase 1: Executor

For each configuration file change:
- Identify siblings in the same logical group
- Verify all siblings have the new parameter added in this PR
- Draft a finding entry for each missing sibling (file, line, severity, message, fix)
- Rate self-confidence: `high` | `medium` | `low`

## Phase 2: Advisor

Review each draft finding:
1. **Intent check:** Is the omission intentional per an adjacent comment?
2. **Significance check:** Is this a meaningful configuration key or cosmetic?
3. **Severity check:** Does the inconsistency actually cause runtime behaviour divergence?

Escalation trigger: missing parameter causes inconsistent timeout/security behaviour across siblings.

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Phase 3: Final Output

Keep only findings confirmed by advisor. Sort: escalation findings first, then ERROR before WARNING.
