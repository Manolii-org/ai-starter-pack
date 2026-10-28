# Fly.io Self-Hosted GitHub Actions Runners

Standard GitHub-hosted `ubuntu-latest` jobs are capped at 6 hours. Runner minutes
are free for public repositories; private repositories have plan allowances and
usage charges. This does not make provider calls or all storage free. See
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
Fly.io self-hosted runners are useful for **heavy CI** (long test matrices, large
builds). They are **not** the default for Autofix control-plane jobs.

## Autofix control-plane (required hosted)

`pr-autofix-loop.yml` / `pr-autofix-loop-reusable.yml` must keep their Claude
Code Action / control-plane jobs on **`ubuntu-latest`** (or another
GitHub-hosted label with a ≥20-min execution cap — ubuntu-slim's 15-min cap is
below the Autofix job timeouts, so latest is required there).

Do **not** point Autofix at `[self-hosted, fly]`. Putting Autofix on the same
Fly pool as heavy CI caused queue starvation across Manolii consumers
(2026-08 remediation: master #3601 / #3602). The portable pack contract matches
that posture:

- `runs-on: ubuntu-latest` (hard default)
- `cancel-in-progress: true` on the caller concurrency group
- Optional Fly runners remain available for **CI** jobs only

`pr-autofix-loop-reusable.yml` accepts a `runs_on` input for emergency
experiments; leave it at the default unless you have an explicit operator
override and a separate runner pool that will not compete with CI.

## Why Fly.io runners (heavy CI only)

- Sessions up to 24h (vs 6h on GitHub-hosted)
- Persistent volume for credential caching between jobs
- Lower per-job cost than GitHub Actions minutes for long builds
- Runner stays warm between jobs (no cold-start penalty)

## Setup (heavy CI)

### 1. Prerequisites

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Authenticate
flyctl auth login
```

### 2. Deploy a runner app

```bash
# Create the runner app
flyctl apps create {YOUR_ORG}-gh-runner --org {YOUR_FLY_ORG}

# Set required secrets
# For a repository-scoped runner:
flyctl secrets set \
  ACCESS_TOKEN={YOUR_GH_PAT_WITH_REPO_SCOPE} \
  REPO_URL=https://github.com/{YOUR_GITHUB_ORG}/{YOUR_REPO_NAME} \
  --app {YOUR_ORG}-gh-runner

# OR for an org-scoped runner:
# flyctl secrets set ACCESS_TOKEN={YOUR_GH_PAT} ORG_NAME={YOUR_GITHUB_ORG} --app {YOUR_ORG}-gh-runner
```

### 3. Create fly.toml for the runner

Create `deploy/fly-runner/fly.toml`:

```toml
app = "{YOUR_ORG}-gh-runner"
primary_region = "lax"

[build]
  image = "myoung34/github-runner:latest"

[env]
  RUNNER_NAME_PREFIX = "{your-org}"
  EPHEMERAL = "true"
  LABELS = "fly,self-hosted,linux,x64"

[[vm]]
  memory = "2048mb"
  cpu_kind = "shared"
  cpus = 2
```

### 4. Point heavy CI jobs at Fly (house-guard — not Autofix)

For fleet tenants (Manolii / Impaktful / Buro), heavy **product CI** must use the
house-guard expression — **not** a static label list. Static labels have no
kill-switch; inverting the fallback to `ubuntu-latest` while
`CI_RUNNER_OVERRIDE` is deleted is the Impaktful #1737 regression
(warm Fly idle + hosted burn).

```yaml
runs-on: >-
  ${{ (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)
      && fromJSON(vars.CI_RUNNER_OVERRIDE || '["self-hosted","fly"]')
      || 'ubuntu-latest' }}
```

**Kill-switch:** set `CI_RUNNER_OVERRIDE=["ubuntu-latest"]`. Never invert the
fallback for fleet product CI. Do **not** set override to Fly repo-wide (quality/autofix often prefer
OVERRIDE first). Register paths in `manolii-org/master`
`config/fleet-consumer-product-ci.yaml` for the scheduled canary.

The standalone `pr-assessment.yml` has one public Pack maintenance exception
(see `docs/ci-cost-conventions.md` R2). Only repository ID `1268261727`, full name
`Manolii-org/ai-starter-pack`, and explicit `public` visibility together select
`["ubuntu-latest"]` when `CI_RUNNER_OVERRIDE` is unset/empty. The explicit override
and outer fork guard retain precedence. Copier copies this workflow verbatim:
copied consumers, a different ID, renamed/transferred or private repositories,
and missing/uncertain identity or visibility retain `["self-hosted","fly"]`.
This exception does not change heavy product CI, review/provider/data policy,
permissions, budgets or required checks. It does not read or alter the override.
A source-policy revert rolls back this exception without changing settings or
retargeting already-created jobs; existing PRs need normal qualified base
integration and a new event, not an old-event rerun, to adopt the workflow.

`ci-reusable.yml` still defaults `runs_on: ubuntu-latest` for non-fleet pack
consumers — fleet tenants must pass a Fly house-guard expression (or migrate to
a fleet-aware caller). Labels registered by the fleet image are `self-hosted`
+ `fly` (no extra `linux` required).

Keep Autofix control-plane jobs on `ubuntu-latest` (their Claude Code Action
legs run up to ~20 min — past the ubuntu-slim 15-min cap). Of the other
control-plane lanes, only the seconds-long legs take `ubuntu-slim`
(billing-floor sweep): `bot-review-relay` and the auto-merge `arm` leg.
The bulk kill-switch revoker stays on `ubuntu-latest` — its 30-min timeout
exceeds the slim cap; moving it would truncate bulk disables. Slim is a
GitHub-hosted label, so the not-Fly invariant is preserved either way.
Pack Autofix also enforces `max_successful_fixes` (default 1) and the dual-ownership
XOR with master `auto-address-review.yml` — see `docs/autofix-cost-alarms.md` and
`scripts/lint-autofix-xor.py`.

### 5. Register and start

```bash
flyctl deploy --app {YOUR_ORG}-gh-runner
```

The runner will register with GitHub automatically and appear under Settings → Actions → Runners.

## Runner version policy

GitHub stops dispatching jobs to a self-hosted runner that has not installed a release
within 30 days of its publication, and refuses registration below a minimum version
(check GitHub's current runner documentation: the enforcement dates have moved before).
The image above keeps the runner's own auto-update on, which satisfies this by default.
A fleet that **disables** auto-update (to keep the baked runner authoritative) takes
over the clock and needs three things, none of which is optional:

1. **Pin the version and its tarball hash together**, changed by one tool, so a
   half-bump cannot ship. Check the pin against GitHub's current registration minimum as
   well, separately from release age: a pin can be young enough and still too old to
   register a new or recreated runner. Keep the minimum in one named constant so it is
   easy to update when GitHub changes it.
2. **Measure the right age**: the age of the first release *newer than the pin*, not the
   age of the pin. Warn well before the limit (for example 10 days) and fail loudly
   with margin for a review and a rollout (for example 21 days). Treat "could not
   decide" as a failure, never as healthy. These thresholds model the normal 30-day
   window only; GitHub may require a critical security release sooner, which an age
   check cannot detect, so read the runner release notes as well.
3. **Track the rollout, not just the merge**: a tenant that cannot take the new image
   (busy pool, provider account block) must stay visible and be retried automatically
   (`docs/issue-lifecycle.md` § Pending state as a retry queue). A pin that is current
   in git but not rolled is still out of date where it matters.

## Scaling

To handle parallel CI jobs, scale to multiple machines:

```bash
flyctl scale count 3 --app {YOUR_ORG}-gh-runner
```

## Cost estimate

At ~10 long CI jobs/day × 5 min average: ~$0.30–1.50/day depending on machine size.
