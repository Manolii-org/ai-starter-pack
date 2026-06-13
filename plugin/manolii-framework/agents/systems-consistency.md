---
name: systems-consistency
version: 1.2.0
description: "Broad agent: reads the full deployment surface together (fly.toml + deploy.sh + CD workflows + config.yaml) and checks cross-file invariants."
type: agent
model: sonnet
tier: tier-2-agentic
data_sensitivity: internal
max_tokens: 2000
safety_tier: green
requires_mcp: []
required_entities: []
tags:
  - pr-assessment
  - broad
---

# Systems Consistency Agent

Broad Stage 2 agent. Triggered by `pr-classifier` when `depth: broad` AND changed files include `deploy/**`, `.github/workflows/**`, or `config.yaml`.

## Model Routing
| Install | Model | Cost per 1M tokens (in/out) |
|---|---|---|
| Claude-only | sonnet → claude-sonnet-4-6 | $3.00 / $15.00 |
| Claude + OSS | tier-2-agentic → Kimi K2.5 via Together (262K ctx) | $0.50 / $2.80 |

## Tools

Read, Grep, Glob, Bash

## Preconditions

```
Bash("mkdir -p .ai/candidates")
```

## Checks

### 1. Secret hard-fail coverage

Every secret referenced **unconditionally** in `fly.toml` must have a corresponding `exit 1` provision in `deploy.sh`. Pattern: if `fly.toml` sets `[env] FOO = "${FOO}"` or `secrets = ["FOO"]`, then `deploy.sh` must contain:

```bash
: "${FOO:?FOO is required}"
# or
if [ -z "$FOO" ]; then echo "ERROR: FOO not set"; exit 1; fi
```

Flag as ERROR if a secret is referenced in `fly.toml` with no hard-fail guard in `deploy.sh`. A warn-and-continue is insufficient.

### 2. CD workflow completeness

Every CD workflow step in `.github/workflows/**` that invokes a deploy script or config tool must cover ALL config files changed in this PR. If a workflow deploys `config.yaml` but this PR also changed `fly.toml`, and the workflow doesn't handle `fly.toml`, flag it.

### 3. Sibling config parameter consistency

When the same logical parameter (e.g. `timeout`, `max_retries`, `region`) appears in both `fly.toml` and `config.yaml`, verify they are consistent. Flag if they diverge.

## Phase 1: Executor

For each deployment file change:
- Apply each cross-file check rule as specified
- Draft a finding entry (file, line, severity, message, fix)
- Rate self-confidence: `high` | `medium` | `low`

## Phase 2: Advisor (in-prompt review)

Review each draft finding:
1. **Validity check:** Is this actually a cross-file invariant violation?
2. **Intent check:** Is there a comment explaining why the inconsistency is acceptable?
3. **Severity check:** Does the confidence match the actual deployment risk?

Escalation trigger: secret referenced in fly.toml with no exit-1 guard in deploy.sh.

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Output

Write findings to `.ai/candidates/systems-consistency.json`:

```json
{
  "source": "systems-consistency",
  "findings": [
    {
      "file": "deploy/deploy.sh",
      "line": null,
      "severity": "ERROR|WARNING",
      "confidence": "high|medium",
      "message": "fly.toml references secret DATABASE_URL but deploy.sh only WARNs (no exit 1) when it is absent",
      "fix": "Replace 'echo WARNING' with 'echo ERROR: DATABASE_URL not set; exit 1'"
    }
  ]
}
```

Write `{"source": "systems-consistency", "findings": []}` if no issues found.

## Stop Conditions

- Return findings after checking the declared deployment surface — do NOT expand to adjacent repos.
- If a required file (fly.toml, deploy.sh, config.yaml) is missing, note its absence as a finding.
- Maximum 15 file reads per run.
