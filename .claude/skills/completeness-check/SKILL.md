---
name: completeness-check
version: 1.0.0
description: "Structured invariant verification for active multi-phase projects. Runs four independent checks: phase gates, datastore write health, required files, checkpoint freshness. Always advisory."
type: skill
model: claude-sonnet-4-6
data_sensitivity: internal
max_tokens: 800
safety_tier: green
tools:
  - Read
  - Bash
tags:
  - advisory
  - migration
  - project-gates
  - health-check
---

# /completeness-check

**Trigger:** `/completeness-check <project-slug>`
**Example:** `/completeness-check myproject`

Structured invariant verification for active multi-phase projects.
Runs four independent checks and emits a PASS/FAIL report. Always advisory.

## Differentiation

- `/diagnose` — searches memory and traces for past errors (memory-oriented, backward-looking)
- `/investigate` — open-ended codebase investigation
- `/phase-gate` — evaluates invariants for ONE specific phase transition
- `/completeness-check` — broad structured check: all phases + datastore health + file presence + checkpoint freshness

## Input

- `<project-slug>` — project slug (e.g. `myproject`)

## Procedure

Run all four checks in sequence. Report PASS/FAIL for each.

### Check 1: Phase Gate Status

For each phase defined in `.ai/project-checkpoints/<slug>-phases.yaml` (or defaults),
evaluate phase gate invariants from `.ai/sessions/active-task.json`. Report which phases are PASS/FAIL.

If unavailable, fall back to `.ai/project-checkpoints/latest-session-facts.json`.

### Check 2: Datastore Write Health

Attempt a test write to confirm the project's datastore (file, database, or API) is accessible.

- PASS: write succeeded and was rolled back or marked for cleanup
- FAIL: error returned — record the error message

### Check 3: Required Files Present

Verify these files exist in the repo for the project:

```
tools/<slug>/PROJECT-RECORD.md
tools/<slug>/audit-template.py
tools/<slug>/orchestrator.py     (if multi-phase project)
```

PASS = all expected files present in git.
WARN = file missing (note which one).

### Check 4: Local Checkpoint Freshness

Check `.ai/project-checkpoints/latest-session-facts.json`:
- PASS: exists and modified within the last 7 days
- WARN: older than 7 days — facts may be stale
- FAIL: file missing — no local fallback for datastore outages

## Output format

```
COMPLETENESS CHECK: <slug>
Timestamp: <ISO>

CHECK 1 — Phase Gates:
  Phase 1: ✅ PASS (all invariants met)
  Phase 2: ❌ FAIL — invariant X unmet

CHECK 2 — Datastore Write Health:
  ✅ PASS — write test succeeded

CHECK 3 — Required Files:
  ✅ tools/myproject/PROJECT-RECORD.md
  ✅ tools/myproject/audit_template.py
  ⚠️  tools/myproject/orchestrator.py — NOT FOUND

CHECK 4 — Checkpoint Freshness:
  ✅ latest-session-facts.json (updated 2h ago)

SUMMARY: 2 PASS, 1 WARN, 1 FAIL
Action required: Resolve FAIL items before proceeding.
```

## Scope limitations

- Checks 1–4 are scoped to local git tree and checkpoint files only
- Does NOT verify remote deployments or external integrations
- Does NOT verify that counts in markdown match actual source-of-truth counts
- Check 3 "files present" means committed to git, not deployed to a remote host
