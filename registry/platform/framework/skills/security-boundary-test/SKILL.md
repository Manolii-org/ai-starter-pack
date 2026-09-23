---
name: security-boundary-test
version: 2.0.0
description: "Flags diffs that introduce/modify security boundaries (silo, allowlist, entity isolation, input sanitisation) without at least one adversarial test that attempts to break the boundary."
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
  - security
intent_phrases:
  - "review the security boundary"
  - "is this security test adequate"
  - "check the adversarial case"
  - "security boundary test"
---

# Skill: Security Boundary Test

Narrow specialist that activates when a diff touches code that appears to **enforce** a security boundary (allowlists, entity/silo isolation, input sanitisation, permission gates, authn/authz checks). Its sole job is to verify that at least one **adversarial test** — a test that tries to break the boundary — is present in the same diff or already exists in the codebase for the modified module.

## Input

- Diff of `.py`, `.ts`, `.tsx`, `.js`, `.sh` files whose **path or content** matches boundary keywords:
  `auth`, `authn`, `authz`, `permission`, `allowlist`, `allow_list`, `deny`, `silo`, `isolat`, `boundary`, `sanitis`, `sanitiz`, `escape`, `validate_`, `rls`, `row_level`, `entity_id`, `tenant`, `project_id`
- (Optional) List of test file paths in the repo for the same module

## Checks

1. **Boundary modified without adversarial test** — A non-test source file adds or modifies code that:
   - Constructs an allowlist / denylist
   - Filters by `entity_id`, `tenant_id`, `project_id`, or similar isolation key
   - Defines or mutates an authn/authz check
   - Adds input sanitisation / escaping / validation for external input

   …but the same diff does **not** add or modify a test that attempts to bypass the boundary. **Severity: ERROR.**

2. **Boundary test weakened or removed** — A test file in the diff deletes or weakens an adversarial assertion (e.g. removes `assert response.status_code == 403`). **Severity: ERROR.**

3. **Allowlist / isolation key widened without test coverage** — The diff expands what an allowlist permits or loosens an isolation predicate, and no adversarial test covers the newly permitted path. **Severity: WARNING.**

## Non-applicable cases (return empty findings)

- The diff only changes comments, type annotations, or docstrings
- The modified file is itself a test file with no corresponding source change
- The boundary keyword appears only in unrelated context (e.g. a form validation function in a non-security module)

## Output Schema

```json
{
  "source": "security-boundary-test",
  "findings": [
    {
      "file": "src/api/routes/contacts.ts",
      "line": 42,
      "severity": "ERROR",
      "message": "Adds tenant_id filter on contacts endpoint but no test attempts cross-tenant access",
      "fix": "Add a test that authenticates as tenant A and requests tenant B's contact — assert 403 / empty result."
    }
  ]
}
```

Return `{"source": "security-boundary-test", "findings": []}` if no issues. No prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | New/modified boundary without any adversarial test — boundary is unverified and can regress silently |
| WARNING | Boundary widened / weakened with partial test coverage |

## Phase 1: Executor

For each changed file matching boundary keywords:
- Apply each check rule as specified
- Draft a finding entry (file, line, severity, message, fix)
- Rate self-confidence: `high` | `medium` | `low`

## Phase 2: Advisor

Review each draft finding:
1. **Boundary check:** Is this code genuinely enforcing a security boundary or is the keyword incidental?
2. **Coverage check:** Did the executor search both the diff AND the broader test suite for an existing adversarial test?
3. **Escalation check:** New auth check / entity isolation predicate on a critical path with zero adversarial tests anywhere?

Escalation trigger: new or modified auth/isolation boundary on a critical path with zero adversarial tests anywhere.

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Phase 3: Final Output

Keep only findings confirmed by advisor. Sort: escalation findings first, then by severity.
