# Fly.io Self-Hosted GitHub Actions Runners

GitHub-hosted `ubuntu-latest` runners are capped at 6 hours and charged per
minute. Fly.io self-hosted runners are useful for **heavy CI** (long test
matrices, large builds). They are **not** the default for Autofix control-plane
jobs.

## Autofix control-plane (required hosted)

`pr-autofix-loop.yml` / `pr-autofix-loop-reusable.yml` must keep their Claude
Code Action / control-plane jobs on **`ubuntu-latest`** (or another
GitHub-hosted label).

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

### 4. Point heavy CI jobs at Fly (not Autofix)

In long-running **CI** workflows only, change:

```yaml
runs-on: ubuntu-latest
```

to:

```yaml
runs-on: [self-hosted, fly, linux]
```

Keep Autofix / auto-merge / bot-relay control-plane jobs on `ubuntu-latest`.

### 5. Register and start

```bash
flyctl deploy --app {YOUR_ORG}-gh-runner
```

The runner will register with GitHub automatically and appear under Settings → Actions → Runners.

## Scaling

To handle parallel CI jobs, scale to multiple machines:

```bash
flyctl scale count 3 --app {YOUR_ORG}-gh-runner
```

## Cost estimate

At ~10 long CI jobs/day × 5 min average: ~$0.30–1.50/day depending on machine size.
