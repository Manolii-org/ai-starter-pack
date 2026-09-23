---
name: migration-safety
version: 2.0.0
description: "Checks SQL migration diffs for NOT NULL without DEFAULT, missing IF EXISTS guards, irreversible DDL without down-migration, and missing transaction wrappers."
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
  - "review this migration"
  - "is this SQL safe"
  - "check the migration file"
  - "before I run this migration"
---

# Skill: Migration Safety

Narrow specialist for `migrations/**/*.sql` diffs. Invoked by `pr-classifier` when SQL migration files are changed.

## Input

- Diff of `migrations/**/*.sql` files

## Checks

1. **NOT NULL column without DEFAULT on likely-large table** — `ALTER TABLE ... ADD COLUMN ... NOT NULL` with no `DEFAULT` clause on a table that appears in multiple earlier migrations (proxy for size). This causes a full table rewrite on Postgres < 11 and a lock on large tables. Require either a `DEFAULT` or a two-step approach (add nullable, backfill, add constraint).

2. **Missing `IF NOT EXISTS` / `IF EXISTS` guards on DDL** — `CREATE TABLE`, `CREATE INDEX`, `DROP TABLE`, `DROP COLUMN` without the appropriate guard clause. Without these, re-running the migration (e.g. on rollback/retry) will fail with an error.

3. **Non-reversible operations without a down-migration in the same PR** — `DROP TABLE`, `DROP COLUMN`, or `TRUNCATE` in an `up` migration with no corresponding `down` migration file in the PR. Flag as ERROR if no `down`/`rollback` file is present in the changed file list.

4. **Missing transaction wrapper** — Migration SQL that contains multiple DML/DDL statements but no `BEGIN`/`COMMIT` (or `START TRANSACTION`/`COMMIT`) wrapper. Without a transaction, a mid-migration failure leaves the database in a partially-applied state.

## Output Schema

```json
{
  "source": "migration-safety",
  "findings": [
    {
      "file": "migrations/00015_add_user_col.sql",
      "line": 3,
      "severity": "ERROR|WARNING",
      "message": "NOT NULL column 'verified_at' added to 'users' table without DEFAULT — will lock table on Postgres < 11 or fail on non-empty tables",
      "fix": "Add DEFAULT NULL and make the column nullable, then add NOT NULL constraint in a separate migration after backfill"
    }
  ]
}
```

Return `{"source": "migration-safety", "findings": []}` if no issues found. Never return prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | Will cause data loss, a failed deployment, or a production lock on a large table |
| WARNING | Bad practice that increases rollback complexity or deployment fragility |

## Phase 1: Executor

For each migration change:
- Apply each check rule exactly as specified
- Verify NOT NULL additions, DDL guards, reversibility, and transaction wrapping
- Draft a finding entry (file, line, severity, message, fix)
- Rate self-confidence: `high` | `medium` | `low`

## Phase 2: Advisor

Review each draft finding:
1. **Validity check:** Is this actually a safety issue, or is there legitimate context?
2. **Severity check:** Does the confidence match the actual data loss or deployment risk?
3. **Reversibility check:** If DDL is destructive, is there a corresponding down migration?

Escalation trigger: `DROP TABLE`, `DROP COLUMN`, or `TRUNCATE` without down-migration.

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Phase 3: Final Output

Keep only findings confirmed by advisor. Sort: escalation findings first, then ERROR before WARNING.
