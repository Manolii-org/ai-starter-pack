# CI cost conventions (GitHub Actions minutes)

Portable rules for Manolii, Buro, and Impaktful consumers of this pack.
Product staging hosts, Vercel project IDs, and recovery-path lists stay in
the product repo. Do not copy those into pack workflows.

## 1. Skip drafts on expensive PR automation

`Classify PR` performs deterministic source classification on every
`pull_request`, including drafts, without model or deployment credentials. It
reads the trusted classifier's existing literal manifests without executing
model code. Every observed nonempty diff, including unknown paths, selects the
existing full SAST scan; an empty diff does not. Optional `review-routing`,
specialist, broad-agent and Judge jobs each require a non-draft PR. Provider
validation and model credentials remain in those optional jobs. AI-review skip
labels, mechanical titles and trusted sync exclusions apply to model routing,
not deterministic source qualification.

Always list `ready_for_review`, `converted_to_draft` and `edited` in
`on.pull_request.types`. Ready events enable optional model review; converting
to draft keeps source qualification automatic while disabling model jobs. Pair
these events with workflow-level `concurrency` and `cancel-in-progress: true`
so older assessment runs are superseded. Do **not** add `paths` /
`paths-ignore`: those filters also apply to `converted_to_draft` and can prevent
the latest event from occupying the concurrency group. Put model draft guards
on the **job** `if:` so no model runner starts. Reusable callers must require
`github.event_name == 'pull_request'` on `classify`; a missing PR payload is not
a supported assessment path.

Autofix (`pr-autofix-loop.yml`) is **not** draft-gated. It runs on
`issue_comment`, `pull_request_review`, `pull_request_review_comment`, and
failing `workflow_run`. Review payloads can include `pull_request.draft`;
the shipped job `if:` still does not inspect it. Do not tell consumers
Autofix is `$0` on drafts.

SAST and every optional model job require `needs.classify.result == 'success'`.
SAST additionally requires the explicit deterministic `sast_required == 'true'`
output, so skipped, failed or incomplete classification cannot start it.
Specialists, broad agents and Judge also require optional review-routing
success. A routing failure blocks model review without suppressing source scans.

Reusing an unchanged verdict on `ready_for_review` is not implemented here.
That needs source, runtime, tool and configuration provenance before it can
avoid another billable run; this eligibility repair makes no savings claim.

Required fast-tier names and secret-scan stay unconditional on drafts.

SAST completion must be real before specialist or Judge work can use its
findings. Both Assessment forms use pinned Semgrep and Bandit CLI reports;
Semgrep 1.185.0 restores supported shell parsing, with a 30-second per-rule
ceiling and no early file abandonment after a timeout. Timeouts remain failures.
GitHub expressions enter shell steps through environment variables; reusable
command callbacks execute through their existing native shell, retaining each
step's errexit, nounset and pipefail settings.
`--config auto` and the existing whole-repository scan scope remain. The old
Semgrep action accepted only `config`/`publishToken`, so its `output`/`json`
arguments did not establish a report. Missing, malformed, scanner-error or
scope-less reports, skipped rules and inconsistent file metrics now fail the SAST job, preserving its existing name.
Semgrep 1.185.0 labels `time.fixpoint_timeouts` experimental profiling/debug
metadata. The [provider source](https://github.com/semgrep/semgrep/blob/9240f708f8d7e2fbd38b67aa6cd17704b1f81e14/src/core/Core_error.ml#L383)
separates these limits from native errors. They are retained as diagnostic counts
when available; a missing or unrecognized field stays unknown. They do
not replace the provider's native exit/error contract or prove exhaustive taint
analysis. Native top-level timeouts and parsing errors still fail, including
warning-level errors alongside findings. The 30-second rule/file ceiling,
no-early-abandonment setting, provider versions, rule selection and job deadline
are unchanged.
Raw scanner reports are retained as a separate owner-local diagnostic artifact
for seven days, including failed jobs. Those reports never authorize findings
publication, specialist work or a release.

Native exit 1 with valid findings remains a completed scan; other native errors
fail. Bandit may be inapplicable only when the checkout has no Python files.
Empty findings from a complete scan remain valid. This is failure propagation,
not a measured saving or a pinned remote Semgrep rule snapshot. Roll out the
workflow repair through owning reviewed pins; it grants no release authority.

## 2. Do not add unfiltered `pull_request` browser / local-stack jobs

A `pull_request` workflow that installs Playwright browsers or runs
`supabase start` (or an equivalent local stack) without `paths:` / internal
detect-gating bills minutes on every docs-only push. Path-filter non-required
jobs. Copier template model: `claude-md-contract.yml`
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

## 6. Maintainer source-contract execution budget

The 2026-11-12 E3 policy migration sets the existing
`assessment-sast-failure-contract` in `config/agent-checks.json` to **180 seconds
per command**, replacing its 120-second limit. This applies only to the Pack's
maintainer contract: the same 22 pytest modules and the existing pinned-reference
check. Its check identity, selectors, complete test scope and provider remain
unchanged. The check newly declares the `release-contract-python` environment,
which the base recipe did not declare. The contract uses the already provisioned
`release-contract-python` interpreter; its Jinja2 prerequisite must not depend on
ambient Python packages.

The old limit had little execution margin. Local source-head observations were:

| Observation | Result |
| --- | --- |
| Old 120-second whole-recipe attempts at `037d549a` and `57c1c1e3` | Contract timed out at 120.005 and 120.007 seconds; both remain failed. |
| Related full-scope run at `902bf546` | 1,435 cases passed in 119.41 seconds, close to the old ceiling. |
| Controlled full-scope diagnostic, before changing this policy | 1,435 cases passed, zero skips, in 113.67 seconds; runner wall time 114.378 seconds and CPU time 111.918 seconds. |

The diagnostic used a fresh normal declared release environment and verified
actionlint 1.7.12. Its 180-second diagnostic cap was not a passing receipt under
the old policy. No individual test exceeded 0.95 seconds; the budget accounts for
the cumulative deterministic contract workload and execution variation. Relative
to that observation, 180 seconds leaves 65.622 seconds (about 36%) of headroom.
Source heads and execution conditions differ across these observations; they
are not matched billing samples or native enforcement proof. Invoice savings
remain unmeasured and are not claimed.

Nonzero exits, missing prerequisites and deadline expiration still fail the
contract. The native missing-Jinja2 collection error is a separate prerequisite
failure, addressed by the declared interpreter binding rather than this budget.
All product scanner rule limits, workflow job deadlines, source-executor limits
and release/production authority deadlines retain their owning policies. Apply
this migration only through reviewed source and actual new-head owning gates;
there is no routine operator task or manual enable switch.

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
  **Public Pack maintenance exception:** the four LLM jobs in the standalone
  `pr-assessment.yml` may use standard `ubuntu-latest` on normal qualified PR
  events only for repository ID `1268261727`, full name
  `Manolii-org/ai-starter-pack`, and explicit `public` visibility together.
  An unset/empty `CI_RUNNER_OVERRIDE` enables that fallback; an explicit override
  keeps precedence. Fork isolation remains outermost. Copied, private, renamed,
  transferred or uncertain identities keep the Fly fallback. This is not an
  exemption for fleet product CI or other heavy advisory checks, and does not
  establish that Assessment is a required check. GitHub documents no incremental
  standard-hosted runner-minute charge for public repositories; provider calls
  and storage remain subject to their existing charges and limits. Draft skips,
  concurrency cancellation, review coverage, timeouts, model/data routes and
  cost acceptance rules remain unchanged. No savings are claimed.
  See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
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
