# Prompt 05 — ai-starter-pack: extract promotion/gate/smoke reusable workflows (v1)

> **Ecosystem:** manolii — GH token key `GH_TOKEN`
> **Repo scope:** Manolii-org/ai-starter-pack → `feat/promotion-reusables` → implementation; Buro-Built/bcp-core (read-only source of the proven implementations)
> **Runs from:** Manolii-org/ai-starter-pack
> **Bootstrap token(s):** MANOLII_MASTER_DOPPLER_TOKEN_PRD → GH_TOKEN; if bcp-core is not in session scope, STATUS: INCOMPLETE naming it (do not route around scope)
> **Parallel with:** 04
> **Depends on:** 02 and 03 merged in bcp-core (their workflows are the extraction source). Verify at pre-flight; if 03 is unmerged, extract gate+smoke only and mark promote as follow-up.
> **Output:** one PR to ai-starter-pack: `.github/workflows/{pre-production-gate,promote-vercel,smoke-prod}-reusable.yml` + `REUSABLE-WORKFLOWS.md` section + adapter-contract doc
> **Session type:** Web or CI

## Reference Manifest

| Reference | Repo | Path | How resolved | Verified @ commit |
|---|---|---|---|---|
| Existing reusable conventions | Manolii-org/ai-starter-pack | REUSABLE-WORKFLOWS.md, .github/workflows/*-reusable.yml | exists in origin repo | 34a75f6 |
| Extraction source: gate | Buro-Built/bcp-core | .github/workflows/pre-production-gate.yml | fetched at pre-flight (clone command below) | post-02 |
| Extraction source: promote | Buro-Built/bcp-core | .github/workflows/promote-production.yml | fetched at pre-flight | post-03 |
| Extraction source: smoke | Buro-Built/bcp-core | .github/workflows/smoke-prod.yml | fetched at pre-flight | ed596be |
| Design constraints + adapter contract | (inlined) | §Context below | inlined — source was design session | n/a |

## Context (inlined)

Productisation decision (2026-07-16): centralise the app-agnostic ~70% of the deploy/test
framework as **versioned reusable workflows** consumers pin by tag
(`uses: Manolii-org/ai-starter-pack/.github/workflows/<x>-reusable.yml@vX.Y.Z`; Renovate preset
`github>Manolii-org/ai-starter-pack` delivers bumps as PRs). Keep per-app: DB branching/migration
internals, seeds, suites, path filters. **Adapter contract**: consumers supply four hooks the gate
calls — `create-ephemeral-branch` / `apply-migrations` / `verify-schema` / `cleanup` — as either
repo-local composite actions (`.github/actions/db-adapter-*/action.yml`, preferred) or script
paths passed as inputs. Reusables hold NO credentials: everything via `secrets:` blocks declared
`required: false` with explicit fail-with-message when a needed one is absent.

Workflows to deliver (inputs → behaviour):
1. **`smoke-prod-reusable.yml`** — inputs: `base_url`, `routes_json` (list of {path, expect[]}),
   `seo_routes` (optional); secret `protection_bypass` (optional header). Any status outside a
   route's expect list fails. Port from bcp-core smoke-prod verbatim semantics.
2. **`pre-production-gate-reusable.yml`** — inputs: adapter action path/prefix, test command(s),
   server start command + readiness URL, `gate_artifact_name` (default `gate-sha`), timeouts;
   uploads gate SHA artifact from a job named `production-gate`. The e2e/test execution is a
   consumer-supplied command — the reusable owns orchestration (branch lifecycle via adapter,
   bounded timeouts, artifact conventions, JUnit upload path), not the suites.
3. **`promote-vercel-reusable.yml`** — inputs: `vercel_project_id`, `team_id`, `prod_health_url`,
   `gate_workflow_name`, `ordering` (**default `migrate-first`**; `promote-first` allowed as
   explicit opt-out), `migrate_workflow_name` (required when migrate-first), `auto_promote_var_name`
   (default `AUTO_PROD_PROMOTE`); secrets `vercel_token`, `protection_bypass` (optional). Behaviour
   ported from bcp-core promote-production: hold job (`promotion-hold`, exit 1, step summary,
   distinct held-vs-failed), gate-ancestry via `gate-sha` artifact + `git merge-base
   --is-ancestor`, pending-migrations halt semantics, READY-deployment match by `gitSource.sha`
   with post-gate timestamp fallback, promote API, alias poll (timeout=FAIL), health check with
   optional bypass header, blocking smoke dispatch (input `smoke_workflow_name`, optional).
Guardrails baked in (non-configurable): pass IDs between jobs never masked values; all polls
bounded; `concurrency` groups parameterised with `cancel-in-progress: false` for promote.
Versioning: this lands as a MINOR release tag per the repo's release conventions (check
REUSABLE-WORKFLOWS.md for the tagging process; if release tagging is maintainer-only, note it in
the PR body instead of tagging).

## Session Bootstrap & Pre-Flight

```bash
export DOPPLER_TOKEN_PRD="${DOPPLER_TOKEN_PRD:-${MANOLII_MASTER_DOPPLER_TOKEN_PRD:-${BURO_BUILT_MASTER_DOPPLER_TOKEN_PRD:-${DOPPLER_TOKEN:-}}}}"
# manolii org: GH_TOKEN is the key; lighter bootstrap if scripts absent in this repo — token comes from the session env
git status && git branch && git checkout -b feat/promotion-reusables origin/main
git clone https://x-access-token:${GH_TOKEN}@github.com/Buro-Built/bcp-core.git /tmp-src/bcp-core 2>/dev/null || git clone https://x-access-token:${GH_TOKEN}@github.com/Buro-Built/bcp-core.git ../bcp-core
```
NOTE: cross-ORG clone (Manolii session → Buro repo) may be denied by session scope or token reach — the two orgs have separate tokens. If the clone 403s, fetch the three source files read-only via `mcp__github__get_file_contents` (owner Buro-Built, repo bcp-core); if that is also out of scope, STATUS: INCOMPLETE naming the scope gap. Verify manifest paths; read REUSABLE-WORKFLOWS.md conventions (naming, inputs style, pinning policy: third-party actions SHA-pinned + `# vX.Y.Z` trailer; org-internal reusables `@v*` tag).

## Stream & Scope Protocol

Standard: parallel batching; >30s phases → background sub-agent + keep main thread active; checkpoint `.ai/sessions/active-task.json` (`mkdir -p .ai/sessions`) per phase; ≤4 MCP calls/batch; every dispatch has scope cap + return cap (≤120 words + format) + early-exit clause; same-error-twice → root-cause; ≤3 attempts.

## Advisor Escalation

Manolii ecosystem: call `advisor` if wired in this repo's agent config; else dispatch `work-critic` with the `[ADVISOR BRIEFING]` block. MANDATORY: post-draft review of `promote-vercel-reusable.yml` (Cross-System Impact — a bad central release affects every future consumer's deploys). Confidence ≤6 → escalate. Emit `escalation_decision` JSON pre-commit. Post-edit loop: YAML-parse + re-read each file.

## Sub-Agent Model Routing — OSS-first (source of truth: this repo's `.claude/model-routing.json`; verify aliases exist before dispatch)

Rule 0: credential-bearing steps (clone/API fetch) main-thread only. Boilerplate transforms → `model="haiku"` (word/format caps). Review gate → `model="sonnet"`. Anthropic-direct only with a stated justification; none expected here.

## Phases

**P5.1 — Extract + parameterise.** Author the three reusables per Context, transforming the bcp-core sources: replace bcp-specific literals (project IDs, URLs, secret names, project lists) with inputs/secrets; keep the guardrails non-configurable. Adapter contract doc: `docs/db-adapter-contract.md` — the four hooks, their inputs/outputs, a Supabase-branch example (from bcp-core) and a Neon example (sketch, marked untested). Done when: three YAMLs parse; zero hardcoded org/project literals (`grep -nE "buro|bcp|jpitd|qegs" *.yml` clean except comments naming the reference implementation).
**P5.2 — Consumer docs.** Add a section to `REUSABLE-WORKFLOWS.md`: usage snippets for each workflow (tag-pinned), the ordering flag with migrate-first default + rationale (one line: promote-first has a promoted-code-without-schema window; migrate-first requires an expand/contract guard in the consumer's CI — state it as a documented prerequisite), secret-injection pattern, canary rollout note (bcp-core is the canary consumer). Done when: docs↔inputs greped consistent both ways.
**P5.3 — Validation.** `workflow_call` workflows can't be dispatched standalone: add a minimal `.github/workflows/selftest-promotion-reusables.yml` (`workflow_dispatch` only) that calls `smoke-prod-reusable.yml` against a public stable URL (e.g. the repo's own GitHub Pages or a well-known 200 endpoint) with a trivial routes_json — proving input plumbing end-to-end. Run it on the branch; record the run URL. Gate/promote reusables: validated by YAML parse + `act`-style static review only (no live Vercel creds in this repo) — say so explicitly in the PR body, and note bcp-core conversion (consuming the reusables) as the real integration test, listed as follow-up. Done when: selftest green.

## Execution Protocol

Bootstrap → pre-flight (scope check!) → plan → review gate → P5.1→P5.3 with checkpoints → audit → fix loop (≤3) → pre-PR quality gate (`docs/pre-pr-quality-gate.md` — verify it exists in this repo per manifest rules; if absent, apply the checklist inlined in §Audit below) → push `git push -u origin feat/promotion-reusables` (4× backoff) → PR (template if present; `X-Originating-Agent` trailer) → STATUS report.

## Audit Checklist

Core: no credentials anywhere (reusables must reference only `secrets.*` inputs); feature branch only; doc↔code sync (every documented input exists, every input documented); no org literals. Conditional (scripts): `set -euo pipefail`; bounded polls; `curl --max-time`; timeout-of-poll = FAIL preserved from source. Cross-repo: bcp-core touched read-only; Impaktful not touched at all. Simplicity: no abstraction beyond the three workflows + adapter doc — resist a framework-framework. Remediation loop standard.

## Multi-Repo Map

| Repo | Org | Role | Branch | Credential | Changes land here |
|---|---|---|---|---|---|
| ai-starter-pack | Manolii-org | Implementation | feat/promotion-reusables | GH_TOKEN | reusables + docs |
| bcp-core | Buro-Built | Read-only extraction source | main | none (read via MCP/clone if in scope) | nothing |

## Decision Log (PR body)

| Decision | Alternatives | Why |
|---|---|---|
| ordering default migrate-first | promote-first default | ecosystem standard 2026-07-16 |
| adapter contract for DB internals | abstract Neon+Supabase in the reusable | provider APIs too divergent; leaky abstraction risk |
| selftest = smoke only | mock Vercel API | promote path validated by canary consumer conversion, not mocks |

## Discoveries

Output `## Discoveries` (Facts / Gotchas / Patterns; no secrets).
