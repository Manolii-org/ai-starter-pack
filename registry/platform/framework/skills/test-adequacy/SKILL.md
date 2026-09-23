---
name: test-adequacy
version: 2.0.0
description: "Flags functions in src/ or scripts/ that were changed, have observable side effects, and have no corresponding test file updated in the same PR."
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
  - "check the test coverage"
  - "are these tests adequate"
  - "review the test cases"
  - "missing test"
---

# Skill: Test Adequacy

Narrow specialist for `src/` and `scripts/` diffs. Invoked by `pr-classifier` when source files change but no test files are updated.

## Input

- Diff of `src/`/`scripts/` files from the PR
- List of test files NOT touched in the PR (provided by orchestrator, or infer via `Grep`)

## Core Rule

> Flag functions that **changed** AND have **observable side effects** AND no test file was updated in the PR.

## Do NOT Flag

- Private/internal functions with no external callers (prefixed `_`, declared unexported, or only called within the same file)
- Documentation-only changes (only comments/docstrings changed, no logic changed)
- Pure configuration changes (only constant values changed, no conditional logic touched)
- Functions that are already covered by an unchanged test file (use `Grep` to verify)
- Test helper utilities

## Observable Side Effects Definition

A function has observable side effects if it:
- Makes network calls (HTTP, database, MCP tool calls)
- Reads or writes files
- Modifies shared/global state
- Sends messages (email, Slack, webhooks)
- Spawns subprocesses
- Has a return value consumed by external callers

## Verification Steps

1. For each changed function, use `Grep` to find existing test files that reference it
2. If a test file exists but was not changed in this PR, note it as `covered_by_existing_test: true` — do not flag
3. If no test reference exists anywhere, flag as WARNING
4. If a test file exists but its assertions only test the old behaviour (visible in the diff), flag as WARNING with the specific behaviour gap

## Output Schema

```json
{
  "source": "test-adequacy",
  "findings": [
    {
      "file": "src/lib/email.ts",
      "line": 143,
      "severity": "ERROR|WARNING",
      "message": "Function 'sendWelcomeEmail' changed (side effect: sends HTTP request) with no test update in this PR. No existing test found.",
      "fix": "Add or update test in tests/email.test.ts covering the updated send path"
    }
  ]
}
```

Return `{"source": "test-adequacy", "findings": []}` if all changed functions are private, doc-only, or covered. Never return prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | Changed function is a critical path (auth, data write, external API call) with zero test coverage anywhere |
| WARNING | Changed function has side effects but existing tests cover adjacent paths (gap is incremental, not total) |

## Phase 1: Executor

Run all verification steps above. For each changed function with observable side effects:
- Check if it is private, doc-only, or already covered by existing tests
- Draft a finding entry (file, line, severity, message, fix)
- Rate self-confidence: `high` | `medium` | `low`

## Phase 2: Advisor

Review each draft finding:
1. **Criticality check:** Is this really a critical-path function (auth, data write, API call)?
2. **Coverage check:** Is the function covered by adjacent existing tests?
3. **Intent check:** Is the test gap intentional (documentation-only change)?

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Phase 3: Final Output

Keep only findings confirmed by advisor. Sort: ERROR before WARNING, then by advisor_confidence (high first).
