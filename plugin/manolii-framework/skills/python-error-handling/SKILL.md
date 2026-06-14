---
name: python-error-handling
version: 2.0.0
description: "Checks Python diffs for bare-except patterns, Python version compat issues, silent argparse flags, and missing mutually-exclusive groups."
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
  - "review this exception handling"
  - "check the error messages"
  - "is this error case handled"
  - "trace the exception"
---

# Skill: Python Error Handling

Narrow specialist for `.py` file diffs containing `try`/`except` blocks. Invoked by `pr-classifier` when Python files with exception handling are changed.

## Data Sensitivity Note

This skill receives diff snippets from the orchestration layer (`pr-assessment` orchestrator). The orchestrator is responsible for stripping client-identifiable content before passing diffs to this skill. `data_sensitivity: internal` assumes sanitised input.

## Input

- Diff of `.py` files containing `try`/`except`
- (Optional) Subset of Bandit SAST findings for the changed files

## Checks

1. **Bare except with no logging** — `except: pass` or `except Exception: pass` (or `except Exception as e: pass`) that swallows the error entirely without logging. These hide infrastructure failures masquerading as normal flow. Require at minimum `logger.exception(...)` or `print(..., file=sys.stderr)`.

2. **Python 3.10+ type union syntax on older runtimes** — Use of `X | Y` type union syntax in function signatures or type annotations in a project whose `pyproject.toml` / `setup.cfg` declares `python_requires < "3.10"`. This is a runtime error on older Pythons, not just a style issue.

3. **`--strict` / boolean argparse flag with None-safe check missing** — An argparse `add_argument("--strict", action="store_true")` where the downstream code checks `if args.strict:` but the argument is only added conditionally (e.g., inside an `if` block), meaning `args.strict` may be `None` rather than `False`. Flag when `args.<flag>` is accessed without a `or False` / `is True` guard and the argument is not always registered.

4. **Mutually exclusive flags without `add_mutually_exclusive_group`** — Two or more `add_argument` calls whose help text or names suggest mutual exclusion (e.g., `--json`/`--csv`, `--verbose`/`--quiet`, `--dry-run`/`--apply`) but no `add_mutually_exclusive_group` is used. Flag as WARNING with a suggested refactor.

## Output Schema

```json
{
  "source": "python-error-handling",
  "findings": [
    {
      "file": "scripts/sync.py",
      "line": 87,
      "severity": "ERROR|WARNING",
      "message": "except Exception: pass silently swallows errors with no logging",
      "fix": "Replace with: except Exception: logger.exception('sync failed'); raise"
    }
  ]
}
```

The `source` field is required — the judge uses it for attribution and Gate 3 deduplication. Return `{"source": "python-error-handling", "findings": []}` if no issues found. Never return prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | Silent failure, runtime crash on supported Python versions, data-loss risk |
| WARNING | Bad practice that increases debugging difficulty but doesn't guarantee failure |

## Phase 1: Executor (Haiku)

Run all checks from the Checks section above.

For each Python file change with exception handling:
- Apply each check rule exactly as specified
- Draft a finding entry (file, line, severity, message, fix)
- Rate self-confidence: `high` (pattern is clearly problematic) | `medium` (pattern matches but context is unclear) | `low` (may have mitigating factors)

Emit a draft findings list with confidence ratings.

## Phase 2: Advisor (Sonnet)

Review each draft finding from Phase 1:
1. **Validity check:** Is this actually a problem, or is there a mitigating factor (logging elsewhere, error is intentionally suppressed)?
2. **Severity check:** Does the confidence rating match the actual impact (silent failure vs. debug difficulty)?
3. **Version check:** For Python version issues, does the executor verify the runtime version requirement?
4. **Escalation check:** Is this a critical-path bare-except on a critical path with no logging?

Escalation trigger for this skill: silent `except: pass` on a critical path with no logging

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Phase 3: Final Output

Merge Phase 1 and Phase 2 results:
- Keep only findings confirmed by advisor
- Sort: escalation findings first, then by severity (ERROR before WARNING), then by advisor_confidence (high first)
- Return the final JSON output schema defined above
