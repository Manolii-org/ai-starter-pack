# Validate database migrations

This composite action performs deterministic migration-tree checks before a database, container,
or preview environment is started. The database provider and migration adapter are independent:
Neon-hosted Postgres repositories may select `drizzle`, `prisma`, `alembic`, or `flyway`; Supabase
repositories normally select `supabase` unless they have a documented custom applier.

```yaml
- uses: Manolii-org/ai-starter-pack/.github/actions/validate-database-migrations@v1.12.0
  with:
    adapter: drizzle
    path: drizzle
```

Supported adapters:

- `drizzle`: unique numeric filename prefixes, a readable journal, contiguous journal indexes,
  unique tags/timestamps, and exact journal-to-SQL correspondence. Rollback/down companions are
  excluded from the forward tree.
- `alembic`: literal revision identifiers, unique revisions, existing parents, and exactly one
  graph head. Intentional branches must be converged with an Alembic merge revision before merge.
- `supabase`: five-digit legacy and fourteen-digit CLI-native timestamp identifiers, with duplicate
  rejection. Rollback/down companions are excluded.
- `prisma`: timestamped migration directories with a `migration.sql` file.
- `flyway`: versioned `V<version>__<name>.sql` files with unique versions.

Repositories with immutable historical Supabase collisions can provide `baseline`, a JSON file
whose `allowed_duplicate_identifiers` object maps each identifier to the exact sorted filenames in
that historical group. Adding another file to a baselined identifier still fails. The baseline is
not a general exemption and must be code-reviewed.

This action validates desired-state structure only. It does not connect to Neon, Supabase, or any
other provider, allocate identifiers, inspect actual database history, or replace clean replay and
deployment receipt checks.
