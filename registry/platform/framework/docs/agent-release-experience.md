# Agent release experience

**Contract version:** 1.1.0. **Status:** target contract with an executable qualification validator; consumer adoption requires evidence.
This document does not change a repository's installed approval gates or authorize
production execution. Its owning release contract and platform controls apply.

## One production decision

The normal delivery path is automated preparation, source review, required
checks, integration, preview/staging, release qualification, one human production
approval, execution, verification and outcome recording. Keep the owner's branch
topology: an integration branch is not necessarily `main`.

Development and staging need no routine human acknowledgement. Failed tests,
review findings and missing infrastructure still require repair; they do not
create a fresh consent prompt for already-authorized work. Explicit security,
legal, business-risk and human-merge holds remain real decisions. Track them as
exceptions to the normal path rather than silently bypassing them.

Automatically prepare an exact-candidate production approval when qualification
passes. An approval authorizes only the named repository, target, source SHA,
artifact/deployment digest and migration set. A changed candidate requires a new
decision. Prefer the existing protected environment. If a human dispatch is the
sole approved decision, attest its actor and exact scope in trusted execution;
an automatically dispatched workflow or typed confirmation string is not human
approval.

The approval also names the exact predeclared recovery action, recovery artifact
or deployment digest, and recovery migration set. A change to any of these
recovery fields requires a new decision.

Migrations and application activation belong to the same release plan. Complete
preparation and dry-runs before approval; then execute the approved migration set
and artifact in the declared compatibility order. Reuse that authorization only
for post-deploy smoke and the named recovery action, artifacts and migrations.
Keep those jobs least
privileged and confined to the approved subject; do not trigger another manual
approval merely to verify or recover that release. A destructive or out-of-plan
recovery needs its own decision. Preserve existing independent manual migration
controls until the normal release path has absorbed their protection.

A pre-merge `prod-write-ack` hold can retire only after every relevant production
writer is confined to this boundary, including provider Git integrations,
alternate dispatches, scripts, schema auto-apply and workflow changes. Merging
must not itself bypass the boundary. Do not remove reviewers from a shared
environment as a shortcut. Keep source-policy changes reviewed, protected and
evaluated from trusted code.

## Schema preparation, behavior and activation

Classify the actual database effects. Additive DDL may still change behavior
through a function, trigger, seed, default, scheduler or feature flag. Review
and bind every pending migration; unclassified changes hold the release.
Prepare compatible dormant schema before dependent apps, without an extra
approval. Enable features only as explicitly named in the approved release,
after exact dependent components are ready and their behavior is verified.
An all-pending applier must not mix activation SQL into preparation. Split the
authored change safely; never rewrite historical migrations or skip dependencies.

Check existing behavior as well as newly activated features against the deployed
web/worker source and every protected installed-client cohort. Exercise affected
notification links, badge/count semantics, writes, errors, jobs and queued work;
schema/API/decoder checks alone are insufficient. Keep old clients supported by
default. A minimum-version or targeted policy needs executable enforcement tests,
including mixed installations and missing/stale/unknown client versions. A build
being available, signed, uploaded or store-approved never proves user adoption.

The portable implementation is
`.github/actions/qualify-coordinated-release/validate.py` and its composite action.
It binds the release plan, source/artifact/migration/policy/recovery identities,
fresh executable evidence, supported cohorts and activation readiness. It holds
missing/failed/skipped coverage and rejects activation during an all-pending
preparation phase. Qualification is not approval: the trusted provider adapter
authenticates evidence, observes current state and confines mutations to the
existing single protected production decision. See the action README for its
versioned JSON contract and executable examples/tests.

Keep mobile delivery asynchronous: verify the backend before upload, keep native
features dormant or safely targeted while users update, and recheck prerequisites
before activation. A failed app promote/upload leaves already-committed database
behavior live; preserve data and fix forward. A later smoke failure cannot undo
already delivered notifications or external side effects.

## Ownership through completion

A push, migration rename or requested new head SHA is a progress update within
an ongoing delivery task. Reply and continue review, repair, merge and requested
post-merge checks. Honor an explicit stop, handoff or publication-only scope.

Keep repair, merge, acceptance and production as separate commitments in the
existing task ledger. A completed repair cannot retire the parent. Ordinary
CI/review latency is a monitored wait. Record one actual owner, current PR/head,
remaining commitments, next action and any precise blocker. A working wake or
accepted durable handoff receipt is required to claim background continuation.
An unaccepted local note is not a handoff. Missing wake infrastructure is
`blocked_on_infra`, not a request for renewed consent. A production hold leaves
other authorized work runnable.

## Automatic evidence and cost

Reuse the existing CI decision, deployment and continuation receipts; do not add
a parallel approval service or mandatory model call. The trusted release
controller records candidate/policy/workflow versions, qualifying run and
attempt, approval actor/time/scope, migration and artifact digests, destination
before/after, verification/recovery outcome and next owner. Use existing semantic
outcomes: executed, no changes, deferred, policy held and failed. A held or skipped
job is not a completed deployment.

Derive a stable release identity from repository, target, source, artifact,
migration set, policy and the exact named recovery action, recovery artifact or
deployment digest, and recovery migration set. Bind each approval instance to
that full identity and distinguish reissued approvals in existing receipts.
Key idempotent stage events by release identity, approval instance and stage;
fence concurrent writers and deduplicate retries. A cancelled or expired approval cannot be
replayed for another candidate. Keep raw evidence and credentials entity-local;
share portable contracts and explicitly allowed aggregate metrics.

Use event-driven continuation and existing reconciliation, with bounded backoff.
Do not allocate a runner to await approval or add an extra per-PR poller. Reuse
evidence only when its source, subject, policy and trust context still match.
Preserve required status names. Measure human interventions before production
(target zero), production decisions per release (target one), abandoned open
PRs, approval delay, duplicate jobs, repair rounds and total runner/model spend.
Report measured changes; an architectural saving estimate is not a saving.

## Adoption and rollback

Inventory every production entry point and live protection first. Denied or
missing observations remain unknown. Exercise development, staging, a held exact
candidate, approval/rejection, changed-head invalidation, duplicate events,
verification and recovery. Prove that publishing a repair and reporting its SHA
retain the merge commitment. Verify each agent platform's actual hooks/wake
capability; an instruction file does not install them.

Release portable source through the existing immutable pack/registry route.
Keep provider resources, branch names, credentials and queue ownership in owning
overlays. Canary representative adapters before broad rollout; record the
installed pin and acceptance evidence per repository. Until then mark adoption
pending. Restore the prior source/pin to roll back guidance; revert a release
adapter without reopening an unprotected production writer. Never use a data
restore that discards new activity as a routine rollback.
