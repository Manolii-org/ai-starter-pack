# Fly.io Self-Hosted GitHub Actions Runners

GitHub-hosted `ubuntu-latest` runners are capped at 6 hours and charged per minute. For the PR autofix loop (`pr-autofix-loop.yml`) to run long Claude Code sessions reliably, Fly.io self-hosted runners are recommended.

## Why Fly.io Runners

- Sessions up to 24h (vs 6h on GitHub-hosted)
- Persistent volume for credential caching between jobs
- ~$0.01-0.03/job vs GitHub Actions minutes billing
- Runner stays warm between jobs (no cold-start penalty)

## Setup

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

### 4. Update pr-autofix-loop.yml to use the runner

In `.github/workflows/pr-autofix-loop.yml`, change:
```yaml
runs-on: ubuntu-latest
```
to:
```yaml
runs-on: [self-hosted, fly, linux]
```

### 5. Register and start

```bash
flyctl deploy --app {YOUR_ORG}-gh-runner
```

The runner will register with GitHub automatically and appear under Settings → Actions → Runners.

## Scaling

To handle parallel PRs, scale to multiple machines:

```bash
flyctl scale count 3 --app {YOUR_ORG}-gh-runner
```

## Cost estimate

At ~10 PR fix jobs/day x 5 min average: ~$0.30-1.50/day depending on machine size.
