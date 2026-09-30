# CI cost conventions (GitHub Actions minutes)

Portable rules for Manolii, Buro, and Impaktful consumers of this pack.
Product staging hosts, Vercel project IDs, and recovery-path lists stay in
the product repo. Do not copy those into pack workflows.

## 1. Skip drafts on expensive PR automation

`pull_request`-triggered LLM jobs (PR Assessment) skip
`github.event.pull_request.draft`. Always list `ready_for_review` and
`converted_to_draft` in `on.pull_request.types`. Without `ready_for_review`,
marking ready never starts the job. `converted_to_draft` starts a skip run;
pair it with workflow-level `concurrency` and `cancel-in-progress: true` so
an in-flight LLM/SAST run is cancelled. Do **not** add `paths` /
`paths-ignore` on that workflow: GitHub evaluates those filters for
`converted_to_draft` too, so a revert to ignored-only files plus convert-to-
draft never occupies the concurrency group. Put the draft skip on the
**job** `if:` so no runner starts (`$0`). Reusable callers must also require
`github.event_name == 'pull_request'` on `classify` — a missing PR payload
is not a supported assessment path.

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

Prefer GitHub `deployment_status` (or equivalent Ready on the preview URL)
before installing a local app stack / `pnpm dev` for PR browser jobs. Readonly
jobs must not `needs` a lock-holding job. Do not put product hostnames or
mutex **names** (for example `e2e-preview`) in this pack file.

`cancel-in-progress:` splits by role. SHA-bound verifiers — workflows that
test a commit (CI gates, assessments) — keep `cancel-in-progress: true`:
results on a superseded SHA are worthless and queued verifications replace
each other anyway. Consumers/watchers — `workflow_run` and event-driven
responders (autofix, fleet wake, review relays, notifiers) — use
`cancel-in-progress: false`. Killing a consumer mid-flight loses real work
(posted fixes, relayed comments, arm/revoke state) and the trigger that
killed it re-fires the whole fan-out anyway. The default single-slot queue
already bounds pile-up: at most one pending run per group, and a newer event
replaces the pending one, so bursts collapse into one pass over the latest
state. This also fixes the self-cancel class of bug: an autofix push raises
`pull_request:synchronize`, which must not kill the run that pushed it.
Group keys stay PR/branch-scoped; a repo-wide group on `pull_request_target`
leaves a cancelled check attached to that PR's head and destabilises
`mergeable_state`.


## Cost policy (R1–R6, adopted 2026-09-30)

House rules for hosted-minute spend, verified against the
`ci-cost-framework-verification-2026-09-30` report. Each rule has a guard in
`scripts/tests/test_gha_fly_cost_guards.py (master repo)`; new workflows that break a rule
fail that suite.

- **R1 — no sub-30-second solo jobs.** A job whose typical run is under ~30s
  still bills a full minute; fold micro-jobs into a shared aggregator job,
  unless the job is a required check by name (GitHub reports check names per
  job) or needs privilege isolation (different `permissions:` scope).
- **R2 — heavy advisory checks never per-push on hosted.** Any job ≥~2 min or
  ≥$0.01/run that is not a required check runs on the Fly pool via the
  fork-safe selector, on a settle-time (workflow_run/debounce), or nightly —
  never inline on every `pull_request`.
- **R3 — required checks are never path-filtered.** `on.pull_request.paths`
  on a required workflow leaves the check 'Expected' forever. Relevance
  gating is job-level instead: a slim detect job emits a scope, the gated
  job carries `always() && (<non-PR events> || detect failed/cancelled ||
  scope != 'reduced')` — uncertainty always runs the real gate (fail-open).
  The required check name belongs to the *gated* job itself, and a skipped
  job reports `Success` for the required context. The detect job needs
  `permissions: { pull-requests: read }` — under a least-privilege
  `contents: read`-only job the PR-files API 403s and the gate falls back
  to scope=full on every PR. See
  `.github/actions/relevance-gate/` for the reusable component and its
  contract tests.
- **R4 — every job has `timeout-minutes`; every PR-scoped verifier lane has
  `concurrency` + `cancel-in-progress: true`.** Cancel-on-new applies to
  SHA-bound verifiers only — lanes that mutate state (autofix, sync,
  consumers of prior runs) keep `cancel-in-progress: false` per the
  consumer exception above. Main-line groups stay SHA-unique so queued
  merge runs never cancel each other.
- **R5 — runner choice via existing variables only.** `CI_RUNNER_OVERRIDE`
  (global drain), `LIGHT_RUNNER`, `QUALITY_BASE_RUNNER`, `AUTO_MERGE_RUNNER`,
  and quality-base `runner`/`coverage_runner` inputs are the complete set —
  do not invent new variable names for lane selection.
- **R6 — cost-per-push is the KPI.** Claimed savings only after ≥7 days and
  ≥150 pushes of matched telemetry (run-census sampler
  `scripts/gha-cost-sample.py` cross-checked against the billing API in
  `scripts/gha-billing-assert.py` (master repo)); estimates in design docs
  are not claims.
