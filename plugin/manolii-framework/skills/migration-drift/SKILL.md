---
name: migration-drift
description: Detect Supabase migrations that are merged but not applied to live databases
author: Manolii-org
version: 1.0
---

# Migration Drift Detection

Embedded guardrail for Supabase schema management: automatically verify that every merged migration is actually applied on all target databases, using `@assert-applied:` predicates and the Management API.

## Quick Reference

| Task | Command |
|------|---------|
| Test locally | `SUPABASE_ACCESS_TOKEN=… python3 scripts/check-migration-drift-mgmt.py --projects prod:ref --json --out ./reports/` |
| CI setup | Set repo variable `MIGRATION_DRIFT_PROJECTS=prod:ref,staging:ref` + secret `SUPABASE_ACCESS_TOKEN` |
| Write assertion | Add `-- @assert-applied: <SQL predicate>` to every new migration |
| Debug failing assertion | Run the predicate locally: `SELECT EXISTS (<predicate>)` |

## 1. Why This Exists

Supabase projects have no built-in `schema_migrations` tracking. Migrations can merge into `main` without being applied to the live database. This drift — "merged ≠ applied" — has historically gone unnoticed for months, silently breaking features.

This skill embeds a continuous-verification system: every migration carries a small SQL predicate (`@assert-applied`) that checks whether it was applied. A CI workflow runs these predicates daily on all target databases, failing fast if any drift is detected.

## 2. Setup

### Copy Template to Your Repo

```bash
cp -r ai-starter-pack/templates/supabase-migration-drift/.github <your-repo>/
cp ai-starter-pack/templates/supabase-migration-drift/scripts/check-migration-drift-mgmt.py <your-repo>/scripts/
```

### Set Repository Configuration

1. **Variable** `MIGRATION_DRIFT_PROJECTS` (GitHub repo settings → Variables):
   - Format: `entity:supabase_ref,entity:supabase_ref,...`
   - Example: `prod:wccgdisnrbvstnnzppld,staging:xyz789`
   - Find refs in Supabase console (Settings → General)

2. **Secret** `SUPABASE_ACCESS_TOKEN` (GitHub repo settings → Secrets):
   - Generate at [Supabase console → Account Settings → Access Tokens](https://app.supabase.com/account/tokens)
   - OR: Fetch from your Doppler secrets project

## 3. Write Assertions

Add `-- @assert-applied:` to every new migration:

```sql
-- @assert-applied: SELECT 1 FROM information_schema.tables WHERE table_name = 'orders' AND table_schema = 'public'

CREATE TABLE public.orders (
  id UUID PRIMARY KEY,
  user_id UUID REFERENCES public.users(id),
  created_at TIMESTAMP DEFAULT NOW()
);
```

**Pattern:** the predicate should be `SELECT 1 FROM <catalog> WHERE <condition>` — the driver wraps it as `SELECT EXISTS (<predicate>)`, so it must return **≥1 row** iff the migration is applied and **0 rows** otherwise.

- **Table addition:** `SELECT 1 FROM information_schema.tables WHERE …`
- **Column addition:** `SELECT 1 FROM information_schema.columns WHERE …`
- **Index creation:** `SELECT 1 FROM pg_indexes WHERE …`
- **RLS policy:** `SELECT 1 FROM pg_policies WHERE …`
- **Function:** `SELECT 1 FROM pg_proc WHERE …`

> ⚠️ **Do not use `SELECT COUNT(*) FROM …`.** Aggregate queries always return one row (containing the count), so `EXISTS(SELECT COUNT(*) …)` is always `true` — the drift check silently reports "applied" even when the object is missing. Use `SELECT 1 FROM … WHERE …` instead.

## 4. How the Workflow Runs

- **Triggers:** push to main (post-merge), daily 07:00 UTC, manual dispatch. Deliberately NOT `pull_request` — running this on PR-controlled code would expose the Supabase PAT and would false-fail every migration PR by definition. See the workflow header + README for details.
- **Steps:**
  1. Parse all migrations in `supabase/migrations/`
  2. Extract `@assert-applied:` predicates
  3. Connect to each target project via Supabase Management API
  4. Wrap each predicate in `SELECT EXISTS (...)` and check result
  5. Fail the workflow if any predicate returns false

- **Exit codes:**
  - `0` = no drift
  - `1` = drift detected (at least one predicate false)
  - `2` = operational error (auth, HTTP, SQL syntax)

## 5. Interpret Results

**Green workflow (exit 0):**
All annotated migrations are applied on all configured projects. Safe to merge.

**Red workflow (exit 1):**
At least one migration is merged but not applied. Check the job summary annotations for which entity/version is missing. Investigate manually:
```bash
# Verify locally
psql $DATABASE_URL -c "SELECT EXISTS (<predicate>);"
```

**Orange workflow (exit 2):**
Operational issue: PAT expired, endpoint unreachable, predicate SQL invalid. Check stderr in the workflow job. Fix the underlying issue and re-run.

## 6. Local Testing

Before relying on CI:

```bash
export SUPABASE_ACCESS_TOKEN="<your-pat>"
export MIGRATION_DRIFT_PROJECTS="prod:wccgdisnrbvstnnzppld"

python3 scripts/check-migration-drift-mgmt.py \
  --projects "$MIGRATION_DRIFT_PROJECTS" \
  --migrations-dir supabase/migrations \
  --json \
  --out ./reports/drift-test/

echo "Exit code: $?"  # 0 = success, 1 = drift, 2 = error
```

## 7. Known Issues & Gotchas

- **Predicate becomes stale:** If you rename or drop an object, the old `@assert-applied` predicate silently returns false. Solution: update the predicate in the same PR that changes the schema.
- **Missing annotations:** If a migration has no `@assert-applied:`, it's not verified. The workflow logs it as "unverified" but doesn't fail (as of v1.0). Future: add a pre-commit check to enforce annotations.
- **Transient network errors:** The script retries API calls 3 times with exponential backoff. Transient 5xx errors from `api.supabase.com` are handled gracefully.

## 8. Architecture Reference

Full ADR-0029 rationale and enforcement surface:
[manolii-knowledge-layer ADR-0029](https://github.com/manolii-org/manolii-knowledge-layer/blob/main/docs/decisions/ADR-0029-migration-drift-invariant.md)

Known-good implementations:
- [manolii-knowledge-layer](https://github.com/manolii-org/manolii-knowledge-layer) — 3-entity setup (manolii, personal, impaktful)

## 9. Troubleshooting

| Error | Fix |
|-------|-----|
| `MIGRATION_DRIFT_PROJECTS repo variable not set` | Add to repo settings (Settings → Variables) |
| `SUPABASE_ACCESS_TOKEN not set` | Add to repo secrets (Settings → Secrets) |
| `predicate returned no ok column` | Test predicate syntax locally: `SELECT EXISTS (<your-predicate>)` |
| `HTTP 401` | Regenerate PAT in Supabase console or Doppler |
| Job hangs to timeout | Unlikely in GitHub Actions; check `api.supabase.com` status |
