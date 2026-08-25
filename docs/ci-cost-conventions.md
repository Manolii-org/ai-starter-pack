# CI cost conventions (GitHub Actions minutes)

Portable rules for Manolii, Buro, and Impaktful consumers of this pack.
Product staging hosts, Vercel project IDs, and recovery-path lists stay in
the product repo. Do not copy those into pack workflows.

## 1. Skip drafts on expensive PR automation

`pull_request`-triggered LLM jobs (PR Assessment) skip
`github.event.pull_request.draft`. Always list `ready_for_review` in
`on.pull_request.types` or marking ready never starts the job. Put the skip
on the **job** `if:` so no runner starts (`$0`).

Autofix (`pr-autofix-loop.yml`) is **not** draft-gated. It runs on
`issue_comment`, `pull_request_review`, `pull_request_review_comment`, and
failing `workflow_run`. Review payloads can include `pull_request.draft`;
the shipped job `if:` still does not inspect it. Do not tell consumers
Autofix is `$0` on drafts.

When classify can be skipped, every downstream job must also require
`needs.classify.result == 'success'`. An empty output is **not** the string
`'none'`, so `outputs.depth != 'none'` alone still starts Semgrep/Bandit on
drafts.

Required fast-tier names and secret-scan stay unconditional on drafts.

## 2. Do not add unfiltered `pull_request` browser / local-stack jobs

A `pull_request` workflow that installs Playwright browsers or runs
`supabase start` (or an equivalent local stack) without `paths:` / internal
detect-gating bills minutes on every docs-only push. Path-filter non-required
jobs. Copier template model: `mutation-testing-diff.yml`
(`on.pull_request.paths`). Internal detect without `on.paths`:
`static-review.yml` `changed-files` job (`git diff --name-only` against the
PR base). Do **not** cite `ci.yml` `detect` for this — it only tests whether
a package manifest or lockfile exists in the tree, so docs-only PRs in a
Node repo still run install/test. Cite only workflows that ship in this
template. Semgrep in `static-review.yml` still runs on every PR; Bandit /
Shellcheck / ESLint are the jobs gated on those path outputs.

Do **not** add `on.paths` to a Playwright (or similar) workflow that is
already internally detect-gated if that check might become required. GitHub
omits the check context when no path matches, and a required check that never
reports hangs the PR. Internal detect + SKIPPED suite is the safe pattern.

## 3. Wait loops that poll `gh run list`

Put them in `scripts/ci/` with a fake-`gh` test. Capture `gh` exit status
before consuming output — process substitution hides failures from `set -e`
and looks like zero runs. Wait on the SHA you will deploy, including in-flight
**ancestor** runs of the same workflow. Do not add an always-on workflow just
to enforce this.

`workflow_run` waiters that assume an open PR must skip (or cancel) when that
SHA has no open PR. Otherwise they idle-wait on hosted minutes after merge.
Inspect the triggering `workflow_run.head_sha` / open PR heads, not the waiter
run's default-branch `head_sha`.

## 4. Shared concurrency groups

Put `concurrency:` on the job that holds the scarce resource, not the whole
workflow, when the workflow also has a cheap detect/skip job. Workflow-level
concurrency makes a detect-only skip take the lock.

Production-gate MUST NOT share a cancel/supersede queue with PR ephemeral e2e.
See the deployment-framework gate-concurrency rule in the orchestrator repo
(`docs/deployment-framework.md` in `manolii-org/master`).

## 5. Leftover runs after merge

Cancel leftover PR / merge-queue runs after close rather than waiting them
out. Orchestrator example: `merged-pr-run-sweeper`. Do not add a dense GHA
poller.

## What this pack will not absorb

- Product staging hostnames or Vercel project IDs
- Product-only migration-preview locks
- Product recovery / auth path lists
