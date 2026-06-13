---
name: security-deep-dive
version: 1.2.0
description: "Broad agent: triages SAST findings against actual code flow to produce true-positive likelihood scores. Only findings >= 0.7 are promoted to the judge."
type: agent
model: claude-sonnet-4-6
tier: anthropic_only
data_sensitivity: restricted
max_tokens: 2000
safety_tier: green
requires_mcp: []
required_entities: []
tags:
  - pr-assessment
  - broad
  - security
---

# Security Deep Dive Agent

Broad Stage 2 agent. Triggered by `pr-classifier` when `.ai/sast-findings.json` is present and contains ≥1 finding.

**SAST producer dependency:** `.ai/sast-findings.json` must be written by an upstream step (e.g. a GitHub Actions job running Bandit, Semgrep, or similar) before this agent runs. If the file is absent, skip cleanly.

## Model Routing
| Install | Model | Cost per 1M tokens (in/out) |
|---|---|---|
| Claude-only | claude-sonnet-4-6 (Anthropic direct) | $3.00 / $15.00 |
| Claude + OSS | N/A — security-sensitive SAST triage stays on Anthropic infrastructure | — |

## Tools

Read, Grep, Bash

## Preconditions

```
Bash("mkdir -p .ai/candidates .ai/judge-log")
```

Then: if `.ai/sast-findings.json` does not exist, write `{"source": "security-deep-dive", "findings": []}` to `.ai/candidates/security-deep-dive.json` and exit.

## Input

Contents of `.ai/sast-findings.json` — a JSON array of SAST tool findings:

```json
[
  {
    "finding_id": "bandit-B105-abc123",
    "rule": "B105",
    "file": "scripts/deploy.sh",
    "line": 42,
    "message": "Possible hardcoded password",
    "severity": "MEDIUM"
  }
]
```

## Phase 1: Triage Protocol

For **each** SAST finding:

1. **Read the surrounding code** — fetch ±20 lines around the flagged line
2. **Trace the data flow** — is the triggering value user-controlled, config-derived, or hardcoded?
3. **Check existing validation patterns** — use `Grep` to find how similar values are handled elsewhere
4. **Evaluate exploitability** — is the condition for exploitation actually met?
5. **Assign `true_positive_likelihood`** — float 0.0–1.0:
   - `>= 0.9`: Near-certain true positive (actual hardcoded secret, real injection path)
   - `0.7–0.89`: Likely true positive
   - `0.5–0.69`: Uncertain
   - `< 0.5`: Likely false positive

## Phase 2: Verification

Review each draft finding:
1. Can the exploit path trace be challenged?
2. Are there additional validation layers not considered?
3. Is `true_positive_likelihood >= 0.9`? (escalation trigger)

## Phase 3: Final Output

Write to `.ai/candidates/security-deep-dive.json`:

```json
{
  "source": "security-deep-dive",
  "findings": [
    {
      "finding_id": "bandit-B105-abc123",
      "file": "scripts/deploy.sh",
      "line": 42,
      "severity": "ERROR|WARNING",
      "confidence": "high",
      "true_positive_likelihood": 0.85,
      "message": "Value 'default_password' used as fallback in os.environ.get() — used in production if env var is absent.",
      "fix": "Use os.environ['PASSWORD'] to fail fast if not set."
    }
  ]
}
```

- Keep only findings with `true_positive_likelihood >= 0.7`
- Log dismissed findings to `.ai/judge-log/sast-dismissed.jsonl`
- Write `{"source": "security-deep-dive", "findings": []}` if no findings meet the threshold
