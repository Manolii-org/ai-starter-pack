# Reusable Workflows

Canonical home for AI Starter Pack reusable GitHub Actions workflows. Consumed via:

```yaml
uses: manolii-org/ai-starter-pack/.github/workflows/<name>-reusable.yml@v1
```

Frozen at v1 (additive=non-breaking; rename/remove/default-change=v2).

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

| Workflow | Inputs | Secrets |
|----------|--------|---------|
| **ci-reusable** | `runs_on`, `node_version=24` | none |
| **secret-scan-reusable** | `runs_on`, `node_version=22`, `python_version=3.12`, `require_gitleaks_license=false` | `GITLEAKS_LICENSE` (optional) |
| **static-review-reusable** | `runs_on`, `node_version=24`, `python_version=3.14`, `paths_ignore` | none |
| **mutation-testing-diff-reusable** | `runs_on`, `node_version=24`, `paths_ignore` | none |
| **claude-md-contract-reusable** | `runs_on`, `python_version=3.12`, `require_contract=false` | none |
| **fast-tier-reusable** | `gates` (JSON, required), `budget_minutes=5`, `job_timeout_minutes=15`, `runs_on`, `max_parallel=10`, `checkout_fetch_depth=0` | none |
| **pre-production-tier-reusable** | `gates` (JSON, required), `budget_minutes=45`, `job_timeout_minutes=60`, `runs_on`, `max_parallel=4`, `environment`, `checkout_fetch_depth=0`, `open_issue_on_failure=false` | `GATE_SECRETS` (optional) |
| **tier-gate-summary-reusable** | `gate_name` (required), `applies` (required), `tier=fast`, `command`, `skip_reason`, `setup_command`, `runs_on`, `working_directory`, `timeout_minutes=10`, `checkout_fetch_depth=0` | none |

**Notes:**
- `GITHUB_TOKEN` auto-injected by workflow_call (never declare).
- All declared secrets have `required: false` (caller gates with conditionals).
- `node_version` / `python_version` parameterized; defaults are CI best-practice.
- `paths_ignore` input gates internal changed-files detection; caller still owns top-level `on.paths-ignore`.
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
    uses: manolii-org/ai-starter-pack/.github/workflows/ci-reusable.yml@v1
    with:
      runs_on: ubuntu-latest
      node_version: '24'
```

### Proxy Mode

```yaml
name: PR Assessment

on:
  pull_request:
    branches: [main]

permissions:
  contents: read
  pull-requests: write

jobs:
  assessment:
    uses: manolii-org/ai-starter-pack/.github/workflows/pr-assessment-reusable.yml@v1
    with:
      provider_mode: proxy
      litellm_proxy_url: ${{ vars.LITELLM_PROXY_URL }}
    secrets:
      LITELLM_MASTER_KEY: ${{ secrets.LITELLM_MASTER_KEY }}
```

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

## Decision Log

| Decision | Rationale |
|----------|-----------|
| **Explicit secrets over `secrets: inherit`** | Self-documenting; callers explicitly pass only required secrets. Reduces risk of unintended exposure. |
| **Conditionally-required secrets** | `required: false` + guard step. Allows optional features (e.g., Gitleaks premium) without forcing secrets everywhere. |
| **Provider mode omitted on non-AI workflows** | ci, secret-scan, static-review, mutation-testing-diff have no AI calls. Additive for future. |
| **`litellm_proxy_url` as input, not secret** | URL is non-sensitive (endpoint address); secret is the key. Simplifies config. |
| **`paths_ignore` input is internal-only** | Caller owns top-level `on.paths-ignore` for trigger-level filtering. Input gates internal changed-files detection (Semgrep, ESLint, etc.). Decouples concerns. |
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
    uses: manolii-org/ai-starter-pack/.github/workflows/fast-tier-reusable.yml@v1
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
    uses: manolii-org/ai-starter-pack/.github/workflows/pre-production-tier-reusable.yml@v1
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
    uses: manolii-org/ai-starter-pack/.github/workflows/backup-kernel-validate-reusable.yml@v1
    with:
      manifest_path: config/backup-tenant.yaml
      pack_ref: v1   # keep identical to the `uses:` pin — the pack cannot discover its own ref
```
