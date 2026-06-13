---
name: shell-security
version: 2.0.0
description: "Checks shell script diffs for missing timeouts, secret-passing anti-patterns, missing errexit guards, and soft-fail dependency checks."
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
  - "check this shell script"
  - "review this bash script"
  - "is this shell safe"
  - "hook gotcha"
---

# Skill: Shell Security

Narrow specialist for `.sh`/`.bash` file diffs. Invoked by the `pr-classifier` when changed files include shell scripts.

## Input

- Diff of `.sh`/`.bash` files from the PR

## Checks

1. **Missing HTTP timeouts** — `curl` calls without both `--max-time` and `--connect-timeout`. A curl with no timeout will hang indefinitely in a deploy script, blocking the entire pipeline.

2. **Secrets via process arguments** — Credentials passed as CLI positional arguments (visible in `ps aux`). Pattern: a variable containing a key/token/secret/password passed directly to a command. Suggest reading from stdin (`--password-stdin`, `< <(echo "$SECRET")`) or a file instead.

3. **Missing `set -euo pipefail`** — Shell scripts without this guard silently continue on error, swallowing failures. Check the first non-comment, non-shebang line of each changed script.

4. **Soft-fail on required dependency** — A `command -v`/`which` check for a required tool that WARNs and continues rather than `exit 1`ing. Pattern: `if ! command -v foo; then echo "WARNING: foo not found"; fi` without a following `exit 1`. This is a WARN-level finding unless the script would produce incorrect output without the tool.

## Output Schema

```json
{
  "source": "shell-security",
  "findings": [
    {
      "file": "deploy/deploy.sh",
      "line": 42,
      "severity": "ERROR|WARNING",
      "message": "curl called without --max-time or --connect-timeout",
      "fix": "Add --max-time 30 --connect-timeout 10 to the curl invocation"
    }
  ]
}
```

Return `{"source": "shell-security", "findings": []}` if no issues found. Never return prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | Will cause a hang, data leak, or silently incorrect result in production |
| WARNING | Bad practice that increases risk but doesn't guarantee failure |

## Phase 1: Executor

For each shell script change:
- Apply each check rule as specified (timeouts, secrets, errexit, dependency checks)
- Draft a finding entry (file, line, severity, message, fix)
- Rate self-confidence: `high` | `medium` | `low`

## Phase 2: Advisor

Review each draft finding:
1. **Validity check:** Is this pattern actually a security/reliability risk in this context?
2. **Severity check:** Does the rating match the actual production impact?
3. **Context check:** Are there mitigating factors (timeout set elsewhere, errexit enabled at caller level)?

Escalation trigger: secrets passed via process arguments or `curl` with no timeout in a deploy script.

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Phase 3: Final Output

Keep only findings confirmed by advisor. Sort: escalation findings first, then ERROR before WARNING.
