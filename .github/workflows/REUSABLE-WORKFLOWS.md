# Reusable Workflows

Canonical home for AI Starter Pack reusable GitHub Actions workflows. Consumed via:

```yaml
uses: manolii-org/ai-starter-pack/.github/workflows/<name>-reusable.yml@v1.14.0
```

Frozen at v1 (additive=non-breaking; rename/remove/default-change=v2).

Pin pack releases (`vX.Y.Z`, full commit SHA preferred). Component tags
(`email-capture-vX.Y.Z`, `feature-controls-vX.Y.Z`) are not pack releases and
never move `v1`; see README § Tag namespaces.

## Runner selection — opt into `ubuntu-slim` for light jobs

Every `runs_on` input defaults to `ubuntu-latest` and changing that default is a
v2 (breaking) change, so callers opt in explicitly. For jobs that are short
(<10 min), run no Docker daemon workload (`container:`, `services:`, `docker
build/run`), and need no `sudo` or heavyweight toolchains, pass
`runs_on: ubuntu-slim` — it bills at ~1/3 of ubuntu-latest per minute. The slim
image (>= 20260120) ships `gh`, `jq`, `yq`, Node 24 and cloud CLIs; it runs as
root with no `sudo` binary and enforces a 15-minute execution cap. Keep
`ubuntu-latest` (or a `CI_RUNNER_OVERRIDE`-aware expression) for docker,
sudo, or >10-minute jobs, and for anything that must keep full-toolchain
compatibility without re-verification.

## Deployment control-flow actions

Two step-level composite actions keep product-owned provider commands local while
sharing the receipt and pin-SHA controls introduced in v1.10.0:

```yaml
- id: resolve
  uses: Manolii-org/ai-starter-pack/.github/actions/resolve-held-promote-sha@v1.14.0
  with:
    sha: ${{ inputs.sha }}

- uses: Manolii-org/ai-starter-pack/.github/actions/emit-deployment-receipt@v1.13.1
  with:
    emit_when: alias-confirmed
    target_sha: ${{ steps.resolve.outputs.sha }}
    canonical_url: https://held.example.com
    migration_evidence_file: ${{ steps.migrations.outputs.receipt_evidence_file }}
    verify_workflow: .github/workflows/smoke-prod.yml
    verify_result: ${{ steps.smoke.outcome }}
    promote_run_url: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
    repo: ${{ github.repository }}
    rollback_mode: alias-promote
```

`resolve-held-promote-sha` rejects empty/non-40-hex pins and accepts only
`identical` or `behind` from `default-branch...sha`. It never substitutes
`GITHUB_SHA`. `emit-deployment-receipt` requires a final verification enum,
schema-validates against its vendored receipt schema, uploads the artifact for
30 days, then clobbers the `production-receipt` Release asset. Callers need
`permissions: contents: write`; a Release failure fails the calling job.
When known, callers may preserve the non-negative migration backlog count with
`migration_pending`; omitting it leaves the optional receipt field absent.

Database-backed adapters should instead pass `migration_evidence_file`, pointing
to a canonical v2 `migration` object produced by the adapter's existing ledger
check. The action then emits and validates a v2 receipt. It never queries a
database or starts another workflow, so adoption adds no deployment dispatch,
approval, runner job, or duplicate smoke. `migration_pending` and
`migration_evidence_file` are mutually exclusive.

The caller owns ordering: alias/apex confirm, non-job-killing smoke, receipt
action under `always() && !cancelled()`, then the final smoke-failure guard.
For `deploy-succeeded`, call the action only when the real deploy step succeeded;
never emit on a dry run. Complete examples for both modes live at
[`docs/examples/held-promote-receipts.yml`](../../docs/examples/held-promote-receipts.yml).

The `production-receipt` tag and canonical asset are a shared mutable pointer.
**Callers must serialize every invocation for a product** with their existing
promotion `concurrency` group and `cancel-in-progress: false`; a composite action
cannot declare job/workflow concurrency on the caller's behalf. Concurrent
receipt writers are unsupported and may not invoke this action.

## Two Forms

**Standalone:** Self-triggering (`on: push`, `on: pull_request`) — includes full trigger config. Use in a single repository.

**Reusable (`workflow_call`):** Caller owns triggers and path filters. Declare `on: workflow_call:` with explicit inputs/secrets. Caller provides `on:` block + `permissions:` + `secrets:` passing. Zero coupling to caller's trigger shape.

## Provider Wiring

Default: **Anthropic** (`ANTHROPIC_API_KEY` direct). Opt-in: **Proxy mode** (`ANTHROPIC_BASE_URL` + `LITELLM_MASTER_KEY`).

Guard-then-wire: Always validate secrets exist before wiring. Use this guard step FIRST:

```yaml
- name: Validate provider secrets
  env:
    PM: ${{ inputs.provider_mode }}
    AK: ${{ secrets.ANTHROPIC_API_KEY }}
    LK: ${{ secrets.LITELLM_MASTER_KEY }}
    PURL: ${{ inputs.litellm_proxy_url }}
  run: |
    if [[ "$PM" == "proxy" ]]; then
      if [[ -z "$LK" ]]; then echo "::error::proxy requires LITELLM_MASTER_KEY"; exit 1; fi
      if [[ -z "$PURL" ]]; then echo "::error::proxy requires litellm_proxy_url"; exit 1; fi
    else
      if [[ -z "$AK" ]]; then echo "::error::anthropic requires ANTHROPIC_API_KEY"; exit 1; fi
    fi
```

Post-guard env wiring:

```yaml
env:
  ANTHROPIC_API_KEY: ${{ inputs.provider_mode == 'proxy' && secrets.LITELLM_MASTER_KEY || secrets.ANTHROPIC_API_KEY }}
  ANTHROPIC_BASE_URL: ${{ inputs.provider_mode == 'proxy' && inputs.litellm_proxy_url || '' }}
```

## Input/Secret Matrix

Generated in the pack repository from each reusable's `workflow_call` schema
by `python3 scripts/gen-reusable-matrix.py`, a maintainer tool that is not
rendered into instances; `tests/test_reusable_matrix.py` fails the pack's CI
when it drifts. Pack maintainers edit the workflow, then regenerate — never this
table.


<!-- reusable-matrix:begin (generated by scripts/gen-reusable-matrix.py; do not edit) -->

| Workflow | Inputs (`name=default: type`; type shown when not string; `(req)` = required) | Secrets | Outputs |
|----------|--------|---------|---------|
| **backup-kernel-validate-reusable** | `manifest_path` (req), `pack_ref=''` | none | none |
| **ci-reusable** | `runs_on=ubuntu-latest`, `node_version=24`, `package_manager=npm`, `pnpm_version=''`, `install_command=''`, `lint_command=npx eslint .`, `typecheck_command=npx tsc --noEmit`, `test_command=npm test -- --coverage`, `test_timeout_minutes=15: number`, `build_command=npm run build`, `build_timeout_minutes=15: number`, `audit_command=''`, `skip_lint=false: boolean`, `skip_typecheck=false: boolean`, `skip_test=false: boolean`, `skip_build=false: boolean`, `skip_security_audit=false: boolean` | none | none |
| **claude-md-contract-reusable** | `runs_on=ubuntu-latest`, `python_version=3.12`, `require_contract=false: boolean` | none | none |
| **coverage-ratchet-reusable** | `runs_on=ubuntu-latest`, `node_version=''`, `python_version=''`, `setup_command=''`, `install_command=npm ci`, `coverage_command` (req), `metric_command` (req), `baseline_file=.ci/coverage-baseline.txt`, `mode=enforce`, `require_baseline=false: boolean`, `fail_on_inert=false: boolean`, `auto_commit_baseline=false: boolean`, `cache_path=''`, `cache_key=''`, `timeout_minutes=30: number`, `job_timeout_minutes=65: number` | `GH_PAT` | `activation` |
| **e2e-playwright-reusable** | `runs_on=ubuntu-latest`, `node_version=22`, `workdir=.`, `install_command=npm ci`, `build_command=''`, `browsers=chromium`, `playwright_args_json=[]`, `test_env=''`, `artifact_name=''`, `artifact_paths=playwright-report/\ntest-results/\ne2e-artifacts/`, `retention_days=14: number`, `timeout_minutes=30: number`, `job_timeout_minutes=90: number`, `pack_ref=v1`, `classify_results=test-results/`, `fail_on_inert=false: boolean` | `E2E_TEST_USER_EMAIL`, `E2E_TEST_USER_PASSWORD`, `VERCEL_BYPASS_SECRET` | `activation`, `test_failure_class`, `autofix_eligible`, `retryable` |
| **email-quality-reusable** | `pack_ref` (req), `profile_path=config/email-quality.json`, `sender_path` (req) | `UNSPAM_API_TOKEN` (req), `SENDER_TOKEN` | none |
| **fast-tier-reusable** | `gates` (req), `budget_minutes=5: number`, `job_timeout_minutes=15: number`, `runs_on=ubuntu-latest`, `max_parallel=10: number`, `checkout_fetch_depth=0: number` | none | `verdict`, `breakdown` |
| **integration-admission-reusable** | `config_path=config/integration-admission.json`, `base_sha` (req), `head_sha` (req), `installation_id` (req), `accepted_producer` (req), `pack_ref=v1`, `evidence_bundle_json=[]`, `runs_on=ubuntu-latest`, `shadow=true: boolean`, `timeout_minutes=15: number` | none | `plan`, `verdict` |
| **migration-run-gate-reusable** | `db_image` (req), `db_ports_json` (req), `db_env_json` (req), `db_options=''`, `migrations_env_json` (req), `workdir=.`, `adapter=alembic`, `python_version=3.12`, `runs_on=ubuntu-latest`, `install_command` (req), `setup_command=''`, `run_prefix=''`, `downgrade_steps=1`, `alembic_check=false: boolean`, `url_env_var=ALEMBIC_DATABASE_URL` | none | none |
| **outcome-check-reusable** | `outcome` (req), `operation` (req), `summary` (req), `head_sha=''` | none | none |
| **pr-assessment-reusable** | `provider_mode=anthropic`, `litellm_proxy_url=''`, `model=''`, `runs_on=ubuntu-latest`, `pack_ref=v1`, `trusted_sync_author_id=215220684: number`, `jev_judge_shadow=off`, `jev_entity=''` | `ANTHROPIC_API_KEY`, `LITELLM_MASTER_KEY`, `JEV_TYPESAFE_API_KEY` | none |
| **pr-autofix-loop-reusable** | `provider_mode=anthropic`, `litellm_proxy_url=''`, `model=''`, `runs_on=ubuntu-latest`, `max_successful_fixes=1` | `ANTHROPIC_API_KEY`, `LITELLM_MASTER_KEY`, `GH_PAT` | `activation` |
| **pre-production-tier-reusable** | `gates` (req), `budget_minutes=45: number`, `job_timeout_minutes=60: number`, `runs_on=ubuntu-latest`, `max_parallel=4: number`, `environment=''`, `checkout_fetch_depth=0: number`, `open_issue_on_failure=false: boolean` | `GATE_SECRETS` | `verdict`, `breakdown` |
| **restore-drill-reusable** | `resource_group` (req), `sql_server` (req), `source_database` (req), `sanity_queries=SELECT COUNT(*) AS table_count FROM INFORMATION_SCHEMA.TABLES`, `db_auth=sql-auth`, `sqlcmd_version=1.9.0`, `sqlcmd_sha256=b896bac71cf6ad8feca125bb42be61999116d8566a078ed475893372ccf8c0f2`, `timeout_minutes=45: number`, `runs_on=ubuntu-latest` | `DRILL_SQL_USER`, `DRILL_SQL_PASSWORD`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | none |
| **secret-scan-reusable** | `runs_on=ubuntu-latest`, `node_version=22`, `python_version=3.12`, `require_gitleaks_license=false: boolean`, `npm_audit_omit_dev=false: boolean`, `gitleaks_version=8.30.0` | `GITLEAKS_LICENSE` | none |
| **static-review-reusable** | `runs_on=ubuntu-latest`, `node_version=24`, `python_version=3.14` | none | none |
| **tier-gate-summary-reusable** | `gate_name` (req), `tier=fast`, `applies: boolean` (req), `skip_reason=no files in scope changed`, `command=''`, `runs_on=ubuntu-latest`, `working_directory=.`, `timeout_minutes=10: number`, `setup_command=''`, `checkout_fetch_depth=0: number` | none | `verdict`, `applied` |
| **ui-smoke-reusable** | `base_url` (req), `routes=''`, `profile=default`, `config_path=config/qa.json`, `pack_ref=v1`, `playwright_version=1.63.0`, `axe_core_version=4.10.3`, `node_version=22`, `runs_on=ubuntu-latest`, `timeout_minutes=10: number`, `artifact_name=''` | `VERCEL_BYPASS_SECRET` | `status` |

<!-- reusable-matrix:end -->

**Notes:**
- `GITHUB_TOKEN` auto-injected by workflow_call (never declare).
- Secrets are optional unless the matrix marks them `(req)` (callers gate optional ones with conditionals).
- `node_version` / `python_version` parameterized; defaults are CI best-practice.
- Path filtering is the caller's: put `paths` / `paths-ignore` under the caller's event, e.g. `on.pull_request.paths` or `on.push.paths-ignore` (static-review has no `paths_ignore` input). Exception: `pr-assessment-reusable` callers must not path-filter, or a `converted_to_draft` event can fail to cancel an in-flight assessment.
- Proxy mode accepts any Anthropic-compatible endpoint (e.g. an OpenRouter base URL); `LITELLM_MASTER_KEY` is just the proxy key slot — see `docs/autofix-cost-alarms.md` § Proxy mode without LiteLLM.
- Concurrency is the **caller's** responsibility — reusables declare none (a reusable-level concurrency block resolves the caller's github.workflow and self-cancels the caller run).

## Caller Examples

### Anthropic Mode

```yaml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]

permissions:
  contents: read

jobs:
  ci:
    uses: manolii-org/ai-starter-pack/.github/workflows/ci-reusable.yml@v1.14.0
    with:
      runs_on: ubuntu-latest
      node_version: '24'
```

### Proxy Mode

Callers of `pr-assessment-reusable` must include `ready_for_review`,
`converted_to_draft`, and `edited` in `on.pull_request.types`. `classify` runs only when
`github.event_name == 'pull_request'` and `draft != true`. Without
`ready_for_review`, a PR opened as a draft never starts assessment when it
is marked ready. `converted_to_draft` starts a skip run; `edited` re-runs
assessment when the title/body changes without a push — metadata-based
checks (scope-adherence under-delivery) would otherwise keep evaluating
the stale description. Callers must also
set workflow-level `concurrency` with `cancel-in-progress: true` so an
in-flight LLM/SAST run is cancelled. Do not add `paths` or `paths-ignore`
on the caller: GitHub applies those filters to `converted_to_draft`, so a
revert that leaves only ignored files plus convert-to-draft never occupies
the concurrency group. Downstream jobs require classify success so SAST
does not start when classify is skipped.
`workflow_dispatch` is not a supported assessment path: `classify` does
not run, and later jobs would otherwise read empty `github.base_ref` and
`github.event.pull_request.number`.

```yaml
name: PR Assessment

on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, ready_for_review, converted_to_draft, edited]

concurrency:
  group: pr-assessment-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read
  pull-requests: write

jobs:
  assessment:
    uses: manolii-org/ai-starter-pack/.github/workflows/pr-assessment-reusable.yml@v1.14.0
    with:
      provider_mode: proxy
      litellm_proxy_url: ${{ vars.LITELLM_PROXY_URL }}
    secrets:
      LITELLM_MASTER_KEY: ${{ secrets.LITELLM_MASTER_KEY }}
```

### pr-assessment: adopting in a repo with no `.claude/` payload

The four runner scripts read prompts off disk — `run-pr-classifier.py` reads
`.claude/agents/pr-classifier.md`, `run-broad-agents.py` reads the three broad
agents, `run-specialists.py` reads one `SKILL.md` per routed specialist.
`run-judge.py` needs no prompt file (its system prompt is inline).

You do **not** have to vendor any of that. Each runner job probes the workspace
first and hydrates only what is missing, from this pack at `pack_ref`:

| Caller state | What happens |
|---|---|
| Ships all 15 runtime files (e.g. a Copier-rendered instance) | Nothing is fetched — probe reports "no hydration" and the pack is never checked out |
| Ships some (a customised agent prompt, say) | Only the absent files are taken from the pack; **caller-owned copies always win** |
| Ships none (a repo adopting the pipeline cold) | All 15 are hydrated |

So adopting the pipeline in a repo that has no `.claude/` tree at all is a
**one-file change** — just the caller above. Because the pack checkout is
conditional on the probe, an unreachable or mistyped `pack_ref` cannot break a
repo that needs no hydration.

`pack_ref` defaults to the moving `v1` alias. If you pin the reusable to an
immutable tag and want prompts and workflow to move together, pass the same tag:

```yaml
    with:
      pack_ref: v1.14.0
```

The hydration list lives in the workflow-level `ASSESSMENT_RUNTIME_FILES` env.
`tests/test_hydrate_assessment.py` derives the required set from the runners
themselves (`BROAD_AGENTS`, `_VALID_SKILLS`) and fails if the two drift apart.

## Renovate Config

Auto-bump reusable-workflow pins via this custom manager:

```json
{
  "customManagers": [
    {
      "customType": "regex",
      "description": "Bump manolii-org/ai-starter-pack reusable-workflow pins",
      "managerFilePatterns": ["/^\\.github/workflows/.*\\.ya?ml$/"],
      "matchStrings": ["manolii-org/ai-starter-pack/\\.github/workflows/[\\w-]+\\.yml@(?<currentValue>[\\w.-]+)"],
      "depNameTemplate": "manolii-org/ai-starter-pack",
      "datasourceTemplate": "github-tags"
    }
  ]
}
```

Tag strategy: immutable vX.Y.Z + moving `v1` major alias (regex tracks the moving tag).
The alias is moved by `release-tag.yml` on every release. It was documented as
moving long before anything moved it — `v1` sat on v1.7.2 while releases ran to
v1.8.0, so anyone extending the alias silently got stale content. Treat a `v1`
that lags the newest vX.Y.Z as a release-workflow bug, not as intended pinning.

## Decision Log

| Decision | Rationale |
|----------|-----------|
| **Explicit secrets over `secrets: inherit`** | Self-documenting; callers explicitly pass only required secrets. Reduces risk of unintended exposure. |
| **Conditionally-required secrets** | `required: false` + guard step. Allows optional features (e.g., Gitleaks premium) without forcing secrets everywhere. |
| **Provider mode omitted on non-AI workflows** | ci, secret-scan, static-review have no AI calls. Additive for future. |
| **`litellm_proxy_url` as input, not secret** | URL is non-sensitive (endpoint address); secret is the key. Simplifies config. |
| **Path filtering belongs to the caller** | Callers own event-level path filters (`on.pull_request.paths`, `on.push.paths-ignore`, …); no reusable takes a `paths_ignore` input. |
| **Tag v1 alias** | Major version signals API stability. Patch releases (v1.0.1, v1.1.0) are additive non-breaking. Rename/remove/default-change requires v2. |

## CI Tiering (fast / pre-production)

Canonical convention: [`manolii-org/master:docs/cicd-tiering.md`](https://github.com/manolii-org/master/blob/main/docs/cicd-tiering.md).
These three workflows are its building blocks. Read the convention before wiring them —
the value is in the *classification rule*, not the YAML.

| Workflow | Blocks | Natural triggers |
|---|---|---|
| `fast-tier-reusable.yml` | merge into the integration branch | `pull_request`, `merge_group` |
| `pre-production-tier-reusable.yml` | promotion to production | `push` to integration branch, `schedule`, `deployment_status` |
| `tier-gate-summary-reusable.yml` | — (single-gate primitive) | whatever the caller needs |

**Name exactly one required status check per tier** in branch protection — the
aggregate job. For a *called* workflow the status GitHub reports is
`<your caller job name> / <reusable job name>`, so with the caller example
below (`jobs.fast-tier:`) the string to enter is **`fast-tier / fast-tier`**,
not the bare `fast-tier`. Entering the bare name gives you a required check
that never reports — the exact permanent-pending deadlock this paragraph warns
about.

Never list individual gates as required either: a renamed or removed gate then
leaves a required check that can never report, and every PR blocks forever.

### The skip-vs-pass rule

All three emit one of three verdicts — `pass`, `skip`, `fail` — and a `skip`
is always rendered as a green check whose summary states in words that
**nothing was verified**. That is deliberate. GitHub offers no built-in way
to say "this gate correctly did not apply" that is visually distinct from
"this gate passed", so selectivity silently reads as assurance.

The consequence for callers: compute `applies` with **change detection inside
the workflow** (`dorny/paths-filter`, or `git diff --name-only`), *not* with a
workflow-level `paths:` filter. A `paths:` filter prevents the run from being
created at all, which is the one state these workflows cannot annotate.

### Caller permissions are mandatory, and failing them is silent

A reusable workflow may only **retain or reduce** the caller's `GITHUB_TOKEN`
permissions — never elevate them. `pre-production-tier-reusable.yml`'s
`aggregate` job requests `issues: write` (for `open_issue_on_failure`), so a
caller **must** grant it:

```yaml
permissions:
  contents: read
  issues: write     # required by pre-production-tier-reusable.yml
```

Two things make this a trap rather than an inconvenience:

1. **It is required even with `open_issue_on_failure: false`.** Job
   permissions resolve when the run graph is built, long before any step
   reads the input.
2. **The failure is invisible.** The run dies instantly as
   `startup_failure` — no jobs, no check run, no annotation, and
   `GET /actions/runs/{id}/logs` returns 404. There is nothing to read that
   says why.

`fast-tier-reusable.yml` and `tier-gate-summary-reusable.yml` need only
`contents: read`. If you scope permissions per job rather than workflow-wide,
put the grant on the job that has the `uses:` — `permissions` is one of the
few keys allowed on a reusable-workflow caller job.

This is verified by `tests/test_tier_reusables.py`, which fails if any tier
reusable starts requesting a permission the self-test caller does not grant.

### Fail-closed guarantees

Both tier workflows run a `validate` job **before** any gate, and the aggregate
refuses to report `pass` unless it succeeded. Every rule below closes a path by
which the required check could go green having verified nothing:

| Rejected | Why it would otherwise fail open |
|---|---|
| `gates: []` | 0 expected and 0 actual verdicts — the count check passes and the tier reports `pass` with no gate having run |
| a gate object missing `applies` | absent key is falsy in the matrix expression, so the gate records a green `skip` instead of running |
| `applies` that is not a JSON boolean | `"true"` (quoted) is truthy JSON but not a boolean; silently mis-gates |
| `applies: true` with no `command` | a green "pass" that executed nothing |
| gate names colliding after slugification | verdict artifacts overwrite each other, silently losing a verdict |
| a gate that reported no verdict at all | a cancelled or crashed gate job would be counted as passing |

**One thing is deliberately *not* fail-closed-by-output:** the workflow-level
`verdict` output is **empty when the tier fails**, because GitHub does not
propagate `workflow_call` outputs from a failed job. Branch on
`needs.<job>.result == 'failure'` and treat an empty `verdict` as a failure —
never as a pass. Keeping the job green so the string survived would make the
tier fail *open*, which is worse than an awkward output contract.

Every row in that table is covered by a test. `tests/test_tier_reusables.py`
extracts the shipped `validate` and `aggregate` scripts **out of the workflow
YAML** and runs them against fail-open inputs, so the workflow stays the single
source of truth and a regression cannot pass unnoticed. It is not a
restatement: mutate the aggregate's missing-verdict check or the validator's
empty-spec check and the suite goes red.

`tier-reusables-selftest.yml` covers the other direction — it calls all three
reusables by local path ref on any PR that touches them, and asserts the
verdicts and breakdown counts they actually produce. The negatives cannot live
there: a job calling a reusable workflow may not use `continue-on-error`
(callers are restricted to `name`/`uses`/`with`/`secrets`/`needs`/`if`/
`permissions`), so a deliberately-failing tier would turn the run red with no
way to invert it. Hence the split.

**Known limitation — `environment` also gates skipped legs.** It is set at job
level, so a `pre-production` gate with `applies: false` still waits for that
environment's required reviewers before it can report its skip. With
`environment` set, keep every gate `applies: true` and put change-detection
selectivity in the fast tier.

### Two timeouts, and you must set both

`budget_minutes` is **one deadline shared by setup and the gate command**. A
`Start the budget clock` step stamps `now + budget_minutes` before checkout;
`Setup` and `Run gate` each compute what is LEFT of it and wrap their command
in `timeout`. Exceeding it exits `124` with an error naming the tier
misclassification.

> Earlier revisions put `timeout-minutes: budget_minutes` on each step
> separately, which gave a gate with a setup step **twice** its advertised
> ceiling. If you copy this pattern elsewhere, share one deadline — per-step
> timeouts do not compose into a wall-clock budget.

`job_timeout_minutes` is the **outer job** ceiling. It is the backstop, and it
still bounds checkout, which the budget clock covers but cannot `timeout`-wrap
(checkout is an action, not a shell command).

**`budget_minutes` is per GATE, not per TIER.** The convention's fast-tier
budget is a ceiling on the whole tier's *measured p90*; this input bounds one
gate. They coincide only in a single matrix wave. With more gates than
`max_parallel` the matrix serialises, each wave restarts the per-gate clock,
and worst-case tier settle time is `ceil(gates / max_parallel) x
budget_minutes`. Keep `gates <= max_parallel` for the fast tier, or size
against that product. A tier-wide deadline is deliberately not enforced —
matrix legs cannot share a start timestamp without an extra coordinating job.

**Runner requirement.** Budget enforcement needs GNU `timeout` (or `gtimeout`,
the Homebrew coreutils name on macOS). `runs_on` is a free-form label, so both
tier workflows probe for it and **fail closed** if neither is present — an
unenforced budget presented as enforced is exactly what this convention exists
to prevent. Linux runners have it; on macOS add `brew install coreutils`, and
Windows `timeout` has unrelated semantics and is not accepted.

**Reserved `GATE_SECRETS` keys.** `GATE_DEADLINE`, `GATE_SETUP`,
`GATE_COMMAND`, and anything prefixed `GITHUB_` / `RUNNER_` are rejected. The
export step appends to `$GITHUB_ENV` *after* the budget clock is stamped, so a
`GATE_DEADLINE=` entry would silently replace the shared deadline — a future
value restores the 2×-budget bug, a past value fails every gate instantly.

They are **separate inputs, not derived** — GitHub Actions expressions have no
arithmetic operators, so a computed `budget_minutes + N` fails while evaluating
`timeout-minutes` and breaks every caller before a single gate runs.

**Raising one without the other is the footgun.** A pre-production caller who
sets `budget_minutes: 90` for a long migration or load gate but leaves
`job_timeout_minutes` at its 60 default will have the whole matrix leg killed
at 60 minutes, before the step timeout it configured can ever apply. Raise both:

```yaml
with:
  budget_minutes: 90
  job_timeout_minutes: 100   # budget + headroom for checkout/setup
```

### Caller example — fast tier on PRs

```yaml
name: Fast Tier
on:
  pull_request:
    branches: [main]
  merge_group:
concurrency:
  group: fast-tier-${{ github.ref }}
  cancel-in-progress: true
permissions:
  contents: read
jobs:
  detect:
    runs-on: ubuntu-latest
    outputs:
      gates: ${{ steps.build.outputs.gates }}
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6
        with: { fetch-depth: 0, persist-credentials: false }
      - id: changed
        uses: dorny/paths-filter@de90cc6fb38fc0963ad72b210f1f284cd68cea36 # v3
        with:
          filters: |
            py:  ['**/*.py']
            sql: ['migrations/**']
      - id: build
        env:
          PY:  ${{ steps.changed.outputs.py }}
          SQL: ${{ steps.changed.outputs.sql }}
        run: |
          python3 - <<'EOF' >> "$GITHUB_OUTPUT"
          import json, os
          gates = [
            {"name": "lint", "applies": os.environ["PY"] == "true",
             "command": "ruff check .",
             "skip_reason": "no Python changed"},
            {"name": "migration-safety", "applies": os.environ["SQL"] == "true",
             "command": "python3 scripts/check-migrations.py",
             "skip_reason": "no files under migrations/ changed"},
          ]
          print("gates=" + json.dumps(gates))
          EOF
  fast-tier:
    needs: detect
    uses: manolii-org/ai-starter-pack/.github/workflows/fast-tier-reusable.yml@v1.14.0
    with:
      gates: ${{ needs.detect.outputs.gates }}
      budget_minutes: 5
```

### Caller example — pre-production tier after merge

```yaml
name: Pre-Production Gate
on:
  push:
    branches: [main]
  schedule: [{ cron: '0 9 * * 1' }]
  workflow_dispatch:
permissions:
  contents: read
  issues: write
jobs:
  pre-production-tier:
    uses: manolii-org/ai-starter-pack/.github/workflows/pre-production-tier-reusable.yml@v1.14.0
    with:
      gates: >-
        [{"name":"e2e","applies":true,"command":"pnpm test:e2e"},
         {"name":"mutation","applies":true,"command":"pnpm test:mutation"}]
      environment: staging
      open_issue_on_failure: true
    secrets:
      GATE_SECRETS: ${{ secrets.STAGING_ENV }}
```

Your promotion workflow then gates on this via `workflow_run`, exactly as
`bcp-core` and `impaktful_3.0` already do.

## Backup Kernel (WS-2 scaffold)

`backup-kernel-validate-reusable.yml` — validate-only entry point for the backup
kernel (`kernel/backup/`): kernel script syntax, provenance-hash integrity, and
tenant-manifest validation against `kernel/backup/manifest/backup-tenant.schema.json`.
Requires no secrets and touches no databases or storage. The dump/drill reusable
workflows land with the WS-2 cutover (gated on the Manolii Resilience Platform
Phase-1 exit) — see `kernel/backup/README.md`.

```yaml
jobs:
  validate-backup-manifest:
    uses: manolii-org/ai-starter-pack/.github/workflows/backup-kernel-validate-reusable.yml@v1.14.0
    with:
      manifest_path: config/backup-tenant.yaml
      pack_ref: v1.14.0   # keep identical to the `uses:` pin — the pack cannot discover its own ref
```

## pr-autofix-loop-reusable

Thin caller for review-comment Autofix. **Keep `runs_on` at `ubuntu-latest`**
(hosted control-plane). See `docs/fly-runner-setup.md`.

Caller owns triggers + concurrency. OSS / Buro universes use `provider_mode: proxy`.

**Optional `workflow_run`:** The reusable supports `workflow_run` triggers to retry
failed CI (e.g., when Static Review, PR Assessment, or Secret Scan workflows fail).
Autofix will only run on Pull Requests, not scheduled runs or other workflow_run events.

**Relay acceptance:** Autofix processes comments starting with `**[@` posted by
`github-actions[bot]` (typically relayed by `bot-review-relay.yml`). The pack owns
relay transport — do **not** install `manolii-org/master` `auto-address-review.yml`
on the same repo alongside pack Autofix (creates duplicate processing).
Enforce with `python3 scripts/lint-autofix-xor.py`.

**Per-PR budget:** `max_successful_fixes` (default `1`) skips after that many
successful `[autofix]` commits. Cost alarms: `docs/autofix-cost-alarms.md`.
Action pin: immutable SHA (`# v1.0.185`), not `@beta`.

```yaml
name: PR Autofix Loop
on:
  issue_comment:
    types: [created]
  pull_request_review:
    types: [submitted]
  pull_request_review_comment:
    types: [created]
  workflow_run:
    workflows: [Static Review, PR Assessment, Secret Scan, CI]
    types: [completed]

# workflow_run payloads carry no pull_request/issue context — fall through to
# the completed run's PR number, else its run id, so completions for different
# PRs do not share a group and cancel each other.
concurrency:
  group: pr-autofix-${{ github.event.pull_request.number || github.event.issue.number || github.event.workflow_run.pull_requests[0].number || format('run-{0}', github.event.workflow_run.id) || github.run_id }}
  cancel-in-progress: true

permissions:
  contents: write
  pull-requests: write
  issues: write

jobs:
  autofix:
    uses: Manolii-org/ai-starter-pack/.github/workflows/pr-autofix-loop-reusable.yml@v1.15.0
    with:
      provider_mode: proxy
      litellm_proxy_url: ${{ vars.LITELLM_PROXY_URL }}
      model: sonnet
      runs_on: ubuntu-latest
      max_successful_fixes: "1"
    secrets:
      LITELLM_MASTER_KEY: ${{ secrets.LITELLM_MASTER_KEY }}
      GH_PAT: ${{ secrets.GH_PAT }}
```

Product repos that deliberately keep Autofix off (e.g. Buro `bcp-core` ADR-0006)
should not add this caller.

**Agent-step contract.** The pinned `anthropics/claude-code-action` (v1) takes
`prompt` and `claude_args`; the reusable passes `--allowedTools` and, when the
`model` input is non-empty, `--model`. Releases before this fix passed v0
inputs that the action ignored, so Autofix never ran — after upgrading it
pushes `[autofix]` commits and spends tokens. A dispatched agent step that
produces no `execution_file` fails the job; skips (loop-break, budget,
`skip-ai-review`, bot actors other than the relay, actors without write
access) stay green. `GH_PAT` is optional; pushes made with `github.token` do
not re-trigger PR checks.

**Instruction-surface hardening.** Before the agent runs, the checkout is
pinned to the PR head SHA and `.claude/`, `.mcp.json`, `.claude.json`,
`.gitmodules`, `.ripgreprc`, `.husky/`, `CLAUDE.md`, `CLAUDE.local.md` and
`AGENTS.md` (root and nested) are restored from the PR base SHA, so a PR
cannot change the agent's instructions, settings, hooks configuration or MCP
servers. A trusted pre-commit hook outside the checkout refuses `[autofix]`
commits that touch those paths (it replaces the repository's own git hooks
for agent commits), and a final step fails the job if one was pushed anyway.
Code at the PR head — tests, scripts and anything the base hooks call — still
runs, as it must for Autofix to run the PR's own checks.

**Activation (v1.23.0+).** Workflow output `activation` is `active` when the
agent step ran and its execution file was verified, and `skipped` when a gate declined the event. It is empty when
the job did not run (the event failed the job `if:`) or failed before the
agent step. Autofix has no inert state, so there is no `fail_on_inert` input:
a missing credential, or an agent step with no execution file, fails the job.
See `docs/workflow-outcome-convention.md` § Activation contract.


## bot-review-relay

Portable relay for github-actions[bot] review comments from a master copy or external
source. Hosted control-plane like `pr-autofix-loop-reusable.yml`, but defaults to
**`ubuntu-slim`** — the relay's jobs are seconds-long API calls, so they take the
slim 1-CPU image's ~3x cheaper floor instead of ubuntu-latest's (billing-floor
sweep). See `docs/fly-runner-setup.md`.

Relayed comments are accepted by Autofix when they start with `**[@` — the pack
handles the integration. Do **not** install the master `auto-address-review.yml`
on the same repository.

## migration-run-gate (v1.17.0+)

Reusable job that runs REAL migrations against a throwaway DB service
container — apply, downgrade-N, re-apply, single-head assertion. Pairs with
`validate-database-migrations` (graph check): that one proves the revision
graph is sound, this one proves the migrations actually execute.

```yaml
jobs:
  migration-run:
    uses: Manolii-org/ai-starter-pack/.github/workflows/migration-run-gate-reusable.yml@v1.17.0
    with:
      db_image: mcr.microsoft.com/mssql/server:2022-latest
      db_ports_json: '["1433:1433"]'
      db_env_json: '{"ACCEPT_EULA":"Y","MSSQL_SA_PASSWORD":"<ci-throwaway-pw>"}'
      migrations_env_json: >-
        {"MODE":"UAT","TESTING":"1",
         "ALEMBIC_DATABASE_URL":"mssql+pyodbc://sa:<ci-throwaway-pw>@localhost:1433/master?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"}
      install_command: pip install uv && uv sync --dev
      setup_command: <install msodbcsql18 — see header comment in the reusable>
      run_prefix: uv run
      downgrade_steps: "1"
```

Postgres is identical shape with `postgres:16`, `pg_isready` health options,
and a `postgresql://` URL. The `ALEMBIC_DATABASE_URL` convention is the
recommended env override in each instance's `alembic/env.py` — it makes the
same var usable for local dev without secrets-manager access.

## e2e-playwright (v1.18.0+)

Reusable job that runs a repo's Playwright suite in PR CI and uploads the
HTML report, test-results (traces/failure screenshots), and the conventional
`e2e-artifacts/` journey screenshots — always, pass or fail. The suite's own
`playwright.config.ts` `webServer` block starts the app (or point
`PLAYWRIGHT_BASE_URL` at a remote env via `test_env` — dotenv-style
`KEY=VALUE` lines, e.g. `test_env: PLAYWRIGHT_BASE_URL=https://staging.example.com`).

```yaml
jobs:
  e2e:
    uses: Manolii-org/ai-starter-pack/.github/workflows/e2e-playwright-reusable.yml@v1.18.0
    with:
      playwright_args_json: '["--grep", "@smoke"]'
    secrets:
      E2E_TEST_USER_EMAIL: ${{ secrets.E2E_TEST_USER_EMAIL }}
      E2E_TEST_USER_PASSWORD: ${{ secrets.E2E_TEST_USER_PASSWORD }}
```

Convention: specs write verification screenshots to `e2e-artifacts/*.png`
with `page.screenshot(...)` so every PR carries visual evidence, not just
failures. Credentials travel via `secrets:` (GitHub forbids the secrets
context inside `with:`); `test_env` carries NON-secret env only.
`artifact_paths` are relative to `workdir`. Full input list in the file
header.

Failure classification (v1.20.0+, advisory): every run ends with a
`test_failure_class` — `none`, `product`, `test-defect`, `environment`,
`external-dependency` or `unclassified` — in the step summary,
`test-results/test-failure-class.json` and the workflow outputs
`test_failure_class` / `autofix_eligible` / `retryable`. It reads JSON
reports under `classify_results` (default `test-results/`; add
`['json', { outputFile: 'test-results/results.json' }]` to the config's CI
reporters for per-test precision) and otherwise parses the captured reporter
stdout. The classifier is loaded from the pack at `pack_ref` (default `v1`;
keep identical to your `uses:` pin). It never changes the job conclusion. Downstream automation should
retry `retryable == 'true'`, hand `autofix_eligible == 'true'` to code
fixers, and escalate `environment` to whoever owns the secret/seed/config.

Activation (v1.23.0+): the `activation` output is `active` when Playwright
executed at least one test, `inert` when it executed none (every test skipped,
for example because `E2E_TEST_USER_*` secrets are absent, or no test matched
`--grep`), and `unknown` when no reporter summary is readable (a JSON-only
reporter). An inert run emits `::warning::e2e-playwright INERT …` and still
passes unless the caller sets `fail_on_inert: true` (default `false` in v1).
See `docs/workflow-outcome-convention.md` § Activation contract.

## ui-smoke (v1.23.0+)

Runs the pack's `scripts/qa/ui-smoke.mjs` — the same script the agent
finalise gate runs locally — against a deployed URL: HTTP status,
runtime/console errors, broken same-origin assets, mobile overflow /
missing responsive viewport, and serious/critical axe violations at 375px
and 1280px; `profile: launch` adds a slow-3G + 4x CPU mobile budget. It
installs only `playwright` + `axe-core` into a temp prefix (Chromium cached)
and never installs or builds the app, so a run takes about 2-3 minutes.
Routes come from the caller's `config/qa.json` `ui_smoke` section
(`routes` input overrides). Full model: `docs/qa-framework.md`.

```yaml
on: deployment_status
jobs:
  ui-smoke:
    if: github.event.deployment_status.state == 'success'
    uses: Manolii-org/ai-starter-pack/.github/workflows/ui-smoke-reusable.yml@v1.23.0
    with:
      base_url: ${{ github.event.deployment_status.environment_url }}
      pack_ref: v1.23.0
    secrets:
      VERCEL_BYPASS_SECRET: ${{ secrets.VERCEL_BYPASS_SECRET }}
```

The same caller on `schedule` with the production URL is the production
synthetic monitor. The bypass secret is sent only to the base URL's origin.
Output `status` is `passed`, `failed` or `unavailable`.

## classify-playwright-failures action (v1.20.0+)

The same classifier as a composite action for repos that run Playwright
inline rather than through the reusable. Wire it `if: always()` after the
test step; it always exits 0.

```yaml
      - name: Classify Playwright failures
        if: ${{ always() }}
        uses: Manolii-org/ai-starter-pack/.github/actions/classify-playwright-failures@<sha> # vX.Y.Z
        with:
          results: test-results            # JSON-reporter files/dirs
          test-logs: ${{ runner.temp }}/pw.log   # optional: tee'd stdout fallback
          logs: ${{ runner.temp }}/pre-test.log  # optional: health-check / setup evidence
          job-status: ${{ job.status }}
```

Vocabulary and rules live in `scripts/lib/failure_class.py`
(`TEST_FAILURE_CLASSES`, `classify_test_failure_text`); `environment`,
`external-dependency` and `unclassified` mean the same as in the session
`failure_class` taxonomy. Unmatched evidence stays `unclassified` — the
classifier never guesses.

## coverage-ratchet (v1.19.0+)

Reusable coverage gate: runs the suite, extracts a float metric, compares
against an in-repo baseline file. Below baseline = fail in `enforce` mode,
warn in `report` mode.

**Absent baseline = INERT.** The run passes, emits
`::warning::coverage-ratchet INERT: no baseline at <path>; this required check
cannot fail`, prints the measured number to seed with, and sets the workflow
output `activation=inert` (`active` once a baseline is compared). A required
check bound to an inert ratchet can never fail. Set `require_baseline: true`
(or its activation-contract name `fail_on_inert: true`; opt-in in v1, planned
default in v2) to fail instead; a default-branch push
that seeds the baseline via `auto_commit_baseline` is exempt (and that run
fails if the seeding step cannot write the baseline).

**Check name.** The called job id is `coverage` and has no `name:`, so the
reported check is `<caller job name> / coverage`, where the caller job name
is its `name:` when set and its job id otherwise — `coverage / coverage` with
the caller below. Bind required checks to that full string, not bare
`coverage`.

```yaml
jobs:
  coverage:
    uses: Manolii-org/ai-starter-pack/.github/workflows/coverage-ratchet-reusable.yml@v1.19.0
    with:
      node_version: '22'
      install_command: npm ci --ignore-scripts
      coverage_command: npm run test:coverage -- --coverage.reporter=json-summary
      metric_command: node -e "console.log(require('./coverage/coverage-summary.json').total.lines.pct)"
      baseline_file: .ci/coverage-baseline.txt
```

`setup_command` is the escape hatch for toolchains node/python don't cover
(e.g. Go from the hosted toolcache). `auto_commit_baseline` raises (or first
seeds) the baseline on default-branch pushes via a signed gh-api commit. The
only permission that matters is a write-capable `GH_PAT` secret
(`contents: write` on the repository): the reusable's `permissions: contents:
read` caps `github.token`, a caller's `permissions:` block cannot raise it,
and the step fails without `GH_PAT`.

## restore-drill (v1.19.0+)

Scheduled Azure SQL restore drill: `az sql db restore` (point-in-time) into
a throwaway database on the same server, runs SELECT-only sanity queries,
uploads `results.jsonl` + `report.json` (90d), and deletes the drill DB in
an `if: always()` teardown — pass or fail. Caller owns `on:` (weekly
schedule recommended).

## check-branch-protection.py (v1.19.0+)

Contract-side auditor for `deployment-contracts.yaml`: reads each repo's
`protected_branches` and `required_checks` (repo-level, with per-lane
overrides), queries `repos/{r}/branches/{b}/protection` via `gh api`, and
fails (or warns without a PAT) when the live required contexts don't cover
the contract. `--emit-fixes DIR` writes ready-to-run `gh api -X PUT`
payloads that preserve the branch's existing protection settings — it
never applies changes itself.

## check-guarded-paths (v1.17.0+)

Step-level composite action enforcing `.ai/guards.json` at merge time —
diffs `base...head`, fails when a block-mode guard's paths changed without a
`guard-ok:<id>` PR label or `Guarded-Path: <id>` commit trailer. See
[`docs/guarded-paths.md`](../../docs/guarded-paths.md) for the taxonomy and a
ready-to-paste caller job.

## Registry + contract schemas and validators

- `schemas/automation-registry.schema.json` + `scripts/validate-automation-registry.py`
- `schemas/deployment-contract.schema.json` + `scripts/check-deployment-contract.py`

Instance-owned config validated nightly in the control repo: every scheduled
automation registered with owner + risk tier, and every product repo's lane
model diffed against its real workflows (trigger branch, required jobs,
rollback, concurrency group). Both scripts support `--mode local` (sibling
checkouts) and `--mode github` (`gh api`).
