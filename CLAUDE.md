# {PROJECT_NAME}

> **Auto-generated from [AI Starter Pack](./README-STARTER-PACK.md)** — customize all `{PLACEHOLDER}` sections below.

## Stack

{List frameworks, database, ORM, and key dependencies with versions. Example:}

- Runtime: Node.js 20
- Framework: {Next.js 15 | Express | Fastify | etc.}
- Language: TypeScript 5.x
- Database: {PostgreSQL (Neon) | Supabase | SQLite | etc.}
- ORM: {Drizzle | Prisma | Knex | etc.}
- Testing: {Vitest | Jest}
- Package Manager: {pnpm | npm}

## First Session Setup

If `.ai/setup-complete` does not exist in this project, run `python3 scripts/first-run-setup.py` before doing anything else.

Setup configures four optional features:
- **OSS model routing** — routes haiku/sonnet tasks to open-source models (saves 60–80% on API costs)
- **Langfuse OTEL telemetry** — cost/latency tracking dashboard
- **Browserbase cloud browser automation** — external-site scraping and form automation
- **Remote memory backend** — KL-compatible MCP server for persistent cross-session memory (v3.x multi-tenant model: tenant_id + project closure tables + mcp_apps registry)

After setup completes, re-read this file — your configuration choices may add steps to this document.

## Architecture

{Describe directory structure, key patterns, and data flow. Example:}

```text
src/
  app/           # Next.js app router
  lib/           # Shared utilities and business logic
  components/    # React components
  types/         # TypeScript type definitions
  db/            # Database schema, migrations, queries
```

**Key patterns:**
- {Server actions for mutations}
- {API routes for webhooks and external integrations}
- {Background jobs via Inngest/BullMQ/etc.}

## Commands

```bash
# Development
{pnpm dev}                    # Start dev server

# Validation
{pnpm typecheck}              # TypeScript check
{pnpm lint}                   # ESLint
{pnpm test}                   # Run tests
{pnpm build}                  # Production build

# Database
{pnpm db:generate}            # Generate migrations
{pnpm db:migrate}             # Apply migrations
{pnpm db:studio}              # Database UI
```

## Critical Rules

> These rules are non-negotiable. See `.claude/persistent-instructions.md` for the full list.

1. Never hardcode credentials — all secrets from environment variables
2. Validate all input at API boundaries with Zod
3. Run full validation before committing (`{pnpm typecheck && pnpm lint && pnpm test}`)
4. Use `AbortSignal.timeout()` for all external HTTP calls
5. Fix root causes, not symptoms — every fix needs a `Root cause:` explanation
6. Every API route needs a security marker: `// PUBLIC:`, `// USER:`, `// ADMIN:`, `// WEBHOOK:`
7. {Add project-specific rules here}

## Framework Rules

{Framework-specific conventions. Examples:}

- {Next.js: Use server components by default, client only when needed}
- {Next.js: Server actions for mutations, API routes for webhooks}
- {Drizzle: Explicit column selection, no `SELECT *` in user-facing queries}
- {Add your framework rules here}

## Known Gotchas

{Framework interaction issues, deployment constraints, common pitfalls. Examples:}

- {Neon: Connection pool limit is 10 — use pooled connection string}
- {Vercel: Serverless function timeout is 300s on Pro plan}
- {Next.js 15: `headers()` and `cookies()` are now async}
- {Add your gotchas here}

## Quality Gate

Before every PR: follow `docs/pre-pr-quality-gate.md`. Six checks: diff self-review, doc↔code verification, tests for changed files, adversarial security tests, rewrite diffing, and config source-of-truth validation. Skipping this gate is the largest source of avoidable review cycles.

## Skills

**Always invoke:**
- `conventions` — project coding standards
- `self-code-review` — required before every commit

**PR Assessment (invoked automatically by pr-classifier):**
- `test-adequacy` — flags changed functions with no test update
- `docs-fact-check` — verifies doc claims against actual code
- `migration-safety` — SQL migration safety (NOT NULL, transactions, rollbacks)
- `security-boundary-test` — adversarial test requirements for auth/isolation changes
- `shell-security` — curl timeouts, errexit guards, secret-passing patterns
- `scope-adherence` — flags off-scope files and single-call abstractions
- `config-completeness` — sibling parameter consistency in config/fly.toml/routing JSON
- `operational-readiness` — pre-ship checklist for infra/cron/alerting completeness
- `oss-model-compat` — validates OSS model compatibility for config changes

**Available:**
- `defense-in-depth` — security review patterns
- `database-optimization` — query performance patterns
- `test-driven-development` — TDD workflow
- `verification-before-completion` — task completion gates
- `browser-qa` — browser-based QA testing
- `python-error-handling` — Python diff review: bare-except, version compat, argparse
- `analytics` — weekly telemetry aggregation: tool usage, error rates, activity
- `curator` — skill lifecycle management: stale detection, overlap analysis
- `assess-model` — Model Change Protocol: 15-item checklist before adding/removing/re-routing any AI model
- `completeness-check` — invariant verification for multi-phase projects (advisory PASS/FAIL)
- `phase-gate` — gate before advancing a project phase (advisory PASS/FAIL)
- {Add project-specific skills}

## Sub-Agents

**PR Assessment Pipeline:**
- `pr-classifier` — triages PR diff → routing manifest (haiku)
- `diff-reflex` — lightning CRITICAL-only pre-commit check (sonnet)
- `review-internal` — full code review for own-repo PRs (haiku)
- `architecture-impact` — downstream caller count + breaking change risk (sonnet)
- `ci-fixer` — CI failure diagnosis, propose-only (sonnet)
- `security-deep-dive` — SAST triage with true-positive scoring (claude-sonnet-4-6)
- `systems-consistency` — cross-file deployment invariants (sonnet)
- `judge` — 3-gate filter, only agent that posts to GitHub (sonnet)
- `orchestrator` — multi-step DAG coordinator (sonnet)

**Available:**
- `performance-auditor` — bundle and query performance
- `deployment-verifier` — post-deploy health checks
- `incident-diagnostician` — production error triage
- `main-thread-executor` — Sonnet executor with quick-critic / work-critic sub-agent dispatch for quality gating (the native `advisor()` tool is disabled)
- `codex-adversarial` — cross-provider adversarial review via OpenAI Codex
- `context-loader` — deep project memory synthesis from local .ai/memory/
- `infra` — infrastructure deploys, secrets health, worker management
- `prompt-hardener` — eval loop → winning prompt variant promotion → GitHub PR
- `test-hardener` — mutation-testing survivor elimination → test generation → PR
- `secrets-handler` — restricted-tier router for credential-bearing tool/MCP responses; returns sanitised summaries (sonnet, restricted_us_oss_ok clearance)
- `memory-keeper` — maintains the committed `.ai/memory/` knowledge base after significant sessions (haiku)
- {Add project-specific agents}

## Commands

**Development:**
- `/tdd` — test-driven development loop (red → green → refactor)
- `/doctor` — session dysfunction detector, proposes CLAUDE.md rules
- `/drift-check` — agent routing lint + hook health + stale checkpoint detection
- `/reflect` — eval failure reflection and skill patch proposals
- `/watch-pr` — persistent PR monitoring loop (CI + reviews + conflicts)
- `/sprint-fan-out` — decompose goal into parallel worktree-isolated tasks
- `/investigate` — read-only multi-repo diagnosis, never mutates
- `/retro` — sprint retrospective from events.jsonl
- `/freeze` / `/guard` / `/unfreeze` — path guard management
- `/careful` — pre-action risk checklist for irreversible operations
- `/design-shotgun` — generate 4 UI mockup variants
- `/browse` — browser automation (Browserbase external, Playwright localhost)
- `/graphify` — codebase graph queries (call chains, god nodes)

## Knowledge Layer v3.x — Multi-Tenant Model

If your project uses the KL remote memory backend, these patterns apply (P0 Foundation, 2026-05-17):

- **`entity` parameter** — required on every `kl_*` call; maps to a Supabase project. Never use `tenant_id` directly.
- **`project_slug`** — logical isolation key for realm hierarchy (closure tables, migration 00059). Projects may have parent-child relationships traversable via `kl_get_project_tree`.
- **`mcp_app_id`** — your app should register in `mcp_apps` (migration 00065) with a namespace + capability manifest. Controls which data your app may read/write.
- **Bi-temporal facts** — use `kl_get_facts_at(entity, project_slug, as_of)` for point-in-time snapshots; `kl_assert_fact` for current truth.
- **New tools**: `kl_get_project_tree`, `kl_get_unclassified`, `kl_describe_schema`, `kl_list_tables`.
- **Immutable audit** — regulated entity writes use `audit_log_immutable` (append-only RLS, migration 00066).

See your platform's knowledge-layer access guide for full setup instructions.

## Persistent Instructions

See `.claude/persistent-instructions.md` for constraints that must survive context compression.
