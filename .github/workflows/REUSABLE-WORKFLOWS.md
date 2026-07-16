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
