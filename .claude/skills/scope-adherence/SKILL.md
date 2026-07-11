---
name: scope-adherence
version: 1.0.0
description: "Checks if a PR diff stays within the stated scope. Flags files modified that appear unrelated to the PR title/description, and new abstractions with only one call site."
type: skill
disable-model-invocation: true  # slash/CI-invoked checklist — removed from model-facing catalogue to cut per-session tokens (2026-07-06); delete this line to restore auto-invocation
model: haiku
data_sensitivity: internal
safety_tier: green
requires_mcp: []
required_entities: []
tags:
  - scope
  - simplicity
  - pr-assessment
intent_phrases:
  - "check the scope"
  - "is this work in scope"
  - "verify the requirements"
  - "scope violation"
---

# Scope Adherence Skill

You receive a PR diff and its title/description. Identify scope drift — changes that appear unrelated to the stated purpose.

## Checks

### 1. Out-of-scope files
For each modified file: does its path and the nature of the change relate to the PR title/description? Flag any file whose changes are in a completely different domain from the stated purpose (e.g., a UI component modified in a "fix database migration timeout" PR with no explanation in the description).

### 2. One-call abstractions
For each new function, class, or module introduced: is it called from more than one location in the diff? If a new abstraction has only one call site, flag it as a candidate for inlining.

### 3. Formatting creep
In files directly related to the stated task: are there formatting-only changes (whitespace, comment rewording, import reordering) that account for >20% of the changed lines in that file but are unrelated to the functional change? Flag these sections.

## Output

Write findings to `.ai/candidates/scope-adherence.json`:

```json
{
  "source": "scope-adherence",
  "findings": [
    {
      "file": "src/components/Modal.tsx",
      "line": null,
      "severity": "WARNING",
      "confidence": "medium",
      "message": "File appears unrelated to PR description 'fix email validation in contact form'. Modal changes not mentioned.",
      "fix": "Remove Modal.tsx changes from this PR or update the PR description to explain the coupling."
    }
  ]
}
```

If no scope issues are found, emit `{"source": "scope-adherence", "findings": []}`.

## Constraints

- Do NOT flag test files for changed modules — those are always in scope.
- Do NOT flag import updates strictly required by the main change.
- Use `"confidence": "high"` only when the file change is clearly unrelated. Use `"medium"` for ambiguous cases.
- Never post findings directly to GitHub — output to the candidates file only. The judge agent handles posting.
