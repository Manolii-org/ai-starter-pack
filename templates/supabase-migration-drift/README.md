# Supabase Migration Drift Detection

Automated detection of "merged but not applied" migrations across Supabase projects.

## Problem Solved

Supabase projects have no built-in `schema_migrations` tracking table. When you merge a migration PR, it enters the repository — but it may never be applied to the live database. This drift can go unnoticed for months, breaking application features silently.

**This template solves that** by embedding `@assert-applied:` predicates in each migration and automatically verifying them against your target Supabase projects via the Management API.

See [ADR-0029](https://github.com/manolii-org/manolii-knowledge-layer/blob/main/docs/decisions/ADR-0029-migration-drift-invariant.md) (in the source repo) for the full reasoning.

## Setup (3 Steps)

### 1. Copy Template Files

Copy the three directories into your repo:

```bash
# From ai-starter-pack/templates/supabase-migration-drift/
cp -r scripts/.github templates/supabase-migration-drift/.github <your-repo>/
cp templates/supabase-migration-drift/scripts/check-migration-drift-mgmt.py <your-repo>/scripts/
```

### 2. Set Repository Variable

Create a repo variable `MIGRATION_DRIFT_PROJECTS` in your GitHub repository settings:

**Format:** `entity:supabase_ref,entity:supabase_ref,...`

**Example:**
```
prod:wccgdisnrbvstnnzppld,staging:xyz789
```

Find your Supabase project refs in the Supabase console (Settings → General).

### 3. Set Repository Secret

Create a repo secret `SUPABASE_ACCESS_TOKEN` with a Supabase Personal Access Token:

- Generate at: [Supabase Console → Account Settings → Access Tokens](https://app.supabase.com/account/tokens)
- OR: Fetch from Doppler (your org's Supabase secrets project)
- Paste into GitHub repo secrets

## Writing `@assert-applied` Predicates

Every new migration must include a predicate that verifies it was applied. Add a comment to your migration file:

```sql
-- @assert-applied: SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'my_new_table' AND table_schema = 'public'

CREATE TABLE public.my_new_table (
  id UUID PRIMARY KEY,
  created_at TIMESTAMP DEFAULT NOW()
);
```

**More examples:**

```sql
-- Column addition
-- @assert-applied: SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'email_verified'

-- Index creation
-- @assert-applied: SELECT COUNT(*) FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_users_email'

-- RLS policy
-- @assert-applied: SELECT COUNT(*) FROM pg_policies WHERE tablename = 'posts' AND policyname = 'users_see_own_posts'

-- Function
-- @assert-applied: SELECT COUNT(*) FROM pg_proc WHERE proname = 'my_function' AND pronamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
```

**Rule:** The predicate must return a truthy result (row count > 0, boolean true, or similar) when the migration is applied.

## How It Works

1. **CI Triggers:**
   - Every PR touching `supabase/migrations/` → run drift check
   - Every push to `main` → run drift check
   - Daily at 07:00 UTC → catch out-of-band applies/rollbacks
   - Manual `workflow_dispatch` → on-demand recheck

2. **Execution:**
   - The Python script reads all migrations in `supabase/migrations/`
   - Extracts `@assert-applied:` predicates
   - Connects to each configured Supabase project via Management API
   - Wraps each predicate in `SELECT EXISTS (...)` and checks the result

3. **Exit Codes:**
   - **0** = no drift; all annotated migrations applied on all projects
   - **1** = drift detected; at least one predicate returned false
   - **2** = operational error (auth failure, endpoint unreachable, invalid SQL)

4. **Reports:**
   - JSON artifacts: `drift-<entity>.json` with structured results
   - Markdown summary: `DRIFT.md` with human-readable findings
   - GitHub Actions summary annotation (visible on PR/workflow)

## Known-Good Consumers

- **[manolii-knowledge-layer](https://github.com/manolii-org/manolii-knowledge-layer)** — Source repo; 3-entity setup (manolii, personal, impaktful)
- *(Your project here)* — Add yours after successful adoption

## Troubleshooting

**"MIGRATION_DRIFT_PROJECTS repo variable not set"**
- Add the variable to your GitHub repo settings (Settings → Variables)
- Format must be: `entity:ref,entity:ref,...`

**"SUPABASE_ACCESS_TOKEN not set"**
- Add the secret to your GitHub repo settings (Settings → Secrets and variables → Actions)
- Token must be a Supabase Personal Access Token with database access

**"predicate returned no ok column"**
- Your SQL predicate is invalid (syntax error, reference to nonexistent object)
- Test locally: `SELECT EXISTS (<your-predicate>)` against a target database

**"HTTP 401"**
- Token is expired or invalid
- Regenerate from Supabase console or Doppler

**"URLError: \[Errno -2\] Name or service not known"**
- Network connectivity issue (unlikely in GitHub Actions)
- Check if `api.supabase.com` is reachable

## Local Testing

Test the drift check locally before relying on CI:

```bash
export SUPABASE_ACCESS_TOKEN="<your-pat>"
export MIGRATION_DRIFT_PROJECTS="prod:wccgdisnrbvstnnzppld"

python3 scripts/check-migration-drift-mgmt.py \
  --projects "$MIGRATION_DRIFT_PROJECTS" \
  --migrations-dir supabase/migrations \
  --json \
  --out ./reports/drift-test/
```

Exit codes:
- `0` = success, no drift
- `1` = drift found
- `2` = error (check stderr)

## Files Included

- `scripts/check-migration-drift-mgmt.py` — Python drift detector
- `.github/workflows/migration-drift.yml` — CI workflow
- `README.md` — This file

## Further Reading

- [ADR-0029: Migration drift as an ongoing invariant](https://github.com/manolii-org/manolii-knowledge-layer/blob/main/docs/decisions/ADR-0029-migration-drift-invariant.md) — Full architectural reasoning
- [KL Migration Workflow](https://github.com/manolii-org/manolii-knowledge-layer/blob/main/.github/workflows/kl-migration-drift.yml) — Live production example
