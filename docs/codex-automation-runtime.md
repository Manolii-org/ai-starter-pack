# Subscription automation runtime

This feature train is incomplete and has not activated a consumer. Existing
code/security review behavior and provider defaults remain unchanged. The first
component supplies explicit role admission and a tool-free transport over the fixed Codex
subscription broker. Assessment, triage, critic and judge callers can share this
transport once a separately reviewed trusted controller admits their execution.
Implementation/autofix and all-role deployment are not implemented by this component.

## Boundaries

The unprivileged `codex-automation-request.yml` producer is opt-in and runs only
for nondraft same-repository PRs. It checks out no source and uploads only
repository/PR/base/head/run/attempt metadata, never prompt, executable or credential
data. The pure `codex_automation_request.admit` reader authenticates actual API
run/workflow/repository/PR/artifact identities, current full revisions, approved
base branches and holds before reading that single bounded inert JSON file.
Ambiguous, stale, forked or unreadable evidence is refused. The artifact cannot
select its own operation, endpoint, account or executable. This reader starts
no account/model work and supplies no custody, sensitivity or funding grant.

`scripts/ci/codex_subscription_text.py` runs inside the credential-free,
networkless guest. It opens only `/broker/model.sock` and makes one stateless
request. It accepts no credential, endpoint or alternate-provider configuration,
does not import an ambient API client and does not retry. Unknown outcomes,
quota/auth refusal and incomplete/malformed responses remain failed execution.

The trusted host opts its existing broker into `text_only=True`. That mode
rejects the review protocol's local tools and opaque history before reading host
authentication. It verifies an actual completed provider response, exact served
model, tool-free final text and nonnegative usage. Stream deltas, a requested
model name, partial text and a green client process are insufficient. The host
audit records provider-observed model/usage without prompt or response text.
Legacy review mode is unchanged and still supports its existing local tools.

The endpoint remains the broker's fixed Codex subscription Responses operation.
Real subscription authentication stays in host memory; the guest receives no
refresh token, access token, publisher credential or Doppler/GitHub credential.
The guest ignores ambient provider URLs and API keys. Billing mode describes the
fixed transport; it does not prove included capacity or zero allocated cost.
Reported `cost_usd` remains unknown. The existing protected profile capacity and
purchased-credit controls must qualify the account before the host admits work.

The client accepts only public/internal text and bounded standard/heavy reasoning.
Its role and sensitivity checks are defense in depth, not a security boundary
against untrusted guest code. The trusted controller must establish entity,
repository, role, sensitivity and model policy before creating the broker.
It must also set broker request/time bounds and bound the entire guest process.
The client socket timeout does not replace those total execution deadlines.
An output-token instruction is guidance, not a hard API token limit; byte bounds
and actual provider usage remain separate.

## Explicit role consent and one session

The host registry remains `/etc/codex-review/profiles.json`. New automation
roles use a separate root-protected document at
`/etc/codex-review/automation-consents/<profile>.json`, with schema
`codex-automation-consent/v1` and an explicit `roles` list: `assessment`,
`triage`, `critic`, `judge`, `implementation`, `autofix`. Owner, account hash,
generation, exact repositories, expiry, revocation and verified disabled
purchased-credit/automatic-purchase controls retain the review consent contract.
The owner supplies only the roles actually approved; no wildcard or inferred
grant exists. Review-only consent in `consents/` cannot authorize automation.

`select_automation` also requires the administratively verified runner-access
list to admit the exact `codex-automation-worker.yml` controller workflow ref.
Its assignment binds the role and workflow, and is revalidated before and
under the existing canonical account lock. `automation_session` uses the same
account home and `review.lock` as code/security review, the same official CLI
refresh owner, and the same account/capacity/expiry checks. It creates no new
credential chain, host, consent file or runner registration. Shipping this API
does not establish deployed selected-workflow restrictions or owner consent.

## Durable execution custody

`codex_automation_custody.claim` checks complete native commit-status history
before admitting a new execution. The default-source controller must serialize
that check-and-append transaction per repository/PR/operation; this API is not a
distributed compare-and-swap or a lease. It validates the actual running
default-branch `workflow_run` controller identity, appends a pending status and
reads back its acknowledged ID before model work may begin.

Any prior claim for the operation/head holds automatic re-execution. A purported
success, another source policy, artifact expiry, cancellation, missing response,
or rollback cannot clear custody. Completed-result reuse and terminal publication
require their own authenticated proof; this helper supplies neither a clean
review nor permission to rerun. It writes no automatic success or release. This
keeps the execution fence durable while the full controller and reconciler are
being implemented. A lost acknowledgement or competing claim never admits work.

## Trusted assessment controller

`.github/workflows/codex-automation-worker.yml` runs only after the producer
completes, only when `vars.CODEX_AUTOMATION_ENABLED == 'true'`, and only from
the accepted `workflow_sha` checkout (no PR source, no secrets, no OIDC).
`scripts/ci/codex_automation_worker.py` then:

1. `prepare`: admits the exact producer run and inert request against the
   reviewed policy in `scripts/ci/codex-automation-worker.json`
   (`codex-automation-worker/v1`: allowed bases, hold labels, fixed role
   roster, sensitivity, effort, request/time bounds). The policy file is
   instance-owned in consumer renders.
2. `run`: reads explicit automation consent for every role it needs
   (`assessment`, `judge`) without spending; a missing consent or runner
   admission is `blocked_credentials` and claims nothing. It then appends and
   reads back durable custody before any model work. Each guest pass runs under
   `automation_session` (same account lock and official CLI refresh owner as
   review; no second refresh chain) through a text-only broker, inside the
   networkless, read-only `codex-automation-runtime` image built only from the
   accepted closure. The assessment pass returns candidates; the host writes
   them into a read-only hand-off for the judge pass. A held assessment never
   reaches the Judge; quota exhaustion stops without retry.
3. `publish`: posts the Judge outcome captured by the guest as an advisory
   comment (`codex-automation-assessment:v1`). The guest runs the Judge's
   unchanged decision logic; only its GitHub writes are captured.

Each validated broker completion becomes one `automation.inference/v2` receipt
(`scripts/ci/codex_automation_receipts.py`), retained as
`inference-receipts.jsonl` in the result artifact: provider-reported model and
usage, actual admitted input size, trusted run/attempt/base/head/workflow
coordinates and a per-completion attempt ID. `cost_usd` stays unknown. The
contract mirrors the orchestrating repository's reader; spent completions are
measured even when a later stage holds.

Hardening from independent review: the image includes the shipped secret-shape
catalog (`.ai/security/token-shapes.json`) required by Judge evidence
redaction, and refuses to build without it; the advisory comment neutralises
Markdown and @mentions and accepts only known Judge events; spent completions
are measured even when a guest pass fails, and requests without a verified
completion are reported as `unmeasured_requests` (never as receipts) on every
exit after broker execution, including held, failed and quota-blocked passes; the
admitted PR title and body (bounded, read at admission and bound to the admitted
head) reach the guest as a read-only `/input/pr-meta.json` and the stage runners
(classifier and specialists) as untrusted PR metadata, so scope comparison
sees the stated intent; guest
stages share one budget below the host deadline; publication re-checks the
current PR head and posts once per head (trusted-author marker).

Landing review (#347) added: every subscription completion passes the same
review-content admission (`admit_review_wire`) as the HTTP lanes before it is
sent, and a refusal holds (without the HTTP broad lane's local identifier proofs,
so it is stricter there and can hold where HTTP would not; it never fails open); the classifier sees the admitted head's changed
workflow files (data only, bounded) so its deployment-review floor still
applies; concurrent subscription calls are capped at the broker's two active
turns; account-lock waits and guest runs share one deadline inside the
execute job's limit, and receipts are checkpointed after each pass so a
cancelled job keeps them; the broker audit is taken after shutdown under its
lock; `attempt_id` is `<run>.<attempt>:<role>:<n>`, unique across role
batches; the controller policy admits only `internal` sensitivity (the Judge's
input class); autofix patches refuse legacy `rename old/new` headers, accept
only added or modified files in the staged result, and treat `---`/`+++` hunk
content as content.

### Guarded mutation and publication (`autofix`, default off)

Enabled only by `"autofix": true` in the instance-owned controller policy and
`vars.CODEX_AUTOMATION_PUBLISH == 'true'` (no editor runs, no quota is spent and
no custody is claimed when nothing could be published). Each published repair
creates a new head, so `max_autofix_rounds` (default 1, at most 4) caps the
`[autofix]` commits already on the PR from native history. The accepted
`autofix-custody.sh` gate (open PR at the exact head, no fork, no human or
security hold, no session owner, no held/uncertain publication) runs before
any editor work, not only before publication. After
the Judge, only surviving findings from a captured `COMMENT`/`REQUEST_CHANGES`
review are repairable. The controller then needs explicit `autofix` consent
(a refusal claims nothing) and appends its own `autofix` custody claim before
one guest pass. The guest makes a single tool-free `autofix` text call whose
output must be `{"summary", "patch"}` (`codex_automation_patch.py`): a plain
unified diff limited to modifications and new regular files, at most 20 paths,
never deletions, renames, mode/binary/symlink changes (including retargeting an
existing symlink), traversal, any dot-path segment, `conftest.py`, `Makefile`,
`CODEOWNERS` or agent instruction files (case-insensitive, a strict superset of
the publisher's `INSTRUCTIONS` rule, test-enforced). The summary is plain text
without issue-closing keywords, `#` or mentions.
It is applied with `git apply` to a hook-free clone of the exact head and
committed once with an `[autofix]` subject; the result is bundled.

The editor job holds no publication credential. It writes exactly the accepted
publisher's packet (`receipt.json` plus `implementation.bundle`; a test imports
it with `autofix-publish.py`'s own `import_packet`). A separate `publish_fix`
job, enabled only by `vars.CODEX_AUTOMATION_PUBLISH == 'true'`, runs the
accepted `autofix-custody.sh` gate and then `autofix-publish.py` with
`secrets.CODEX_AUTOMATION_PUBLISH_TOKEN`. The publisher re-checks ancestry,
subjects, instruction surfaces, native PR state, holds, owner registration and
head protection, sends once with an expected-ref lease, reads back and records
its outcome marker; the pushed head then runs ordinary CI. Autofix completions
are `automation.inference/v2` receipts with role `autofix`.

Not covered by this controller: triage/critic/implementation roles, SAST
execution inside the guest, and any live
host, account or funding qualification. Security roles remain held.

## Completion of the train

The in-progress pipeline integration adds an explicit `CODEX_AUTOMATION_MODE=required`
guest adapter. It reads a separate immutable `/input/codex-automation.json` with
exact repository/PR/base/head, role, sensitivity, reasoning context, actual
controller run/attempt coordinates and mandatory review-role roster. Context
is inert input, not host authorization or an execution receipt. The qualified
controller still owns real API admission, consent, custody and the accepted
runtime image. Do not enable it by setting a flag on a legacy secret-bearing job.

The classifier, specialist, broad-agent and Judge integrations bypass legacy credential guards and HTTP resolution
only inside the isolated guest. It sends complete supplied diff, path inventory
and metadata, or holds when the bounded transport cannot accept them. Unknown
outcomes, invalid manifests and unsupported policies do not become the legacy
fallback manifest. Ordinary default consumers retain their existing transport
and limits. Explicit direct/client-model policies and no-AI sensitivity remain
holds until a separately accepted eligible contract exists.
Required security roles (`first_party`, `required_security_review`, and the
named security specialists) are held before transport even though the governed
default lane clears their legacy `first_party` flag: the fixed subscription
route is not an independent-family security review. They surface as first-party
missing coverage, so the Judge fails closed.

Specialist and broad-agent candidates retain their existing findings parsers
and required-role coverage. Judge retains immutable Git evidence, the existing
decision parser and incomplete-adjudication behavior. Controller coordinates
bind the existing candidate provenance shape; the guest does not pretend to be
a GitHub Actions runner. The host must independently verify those coordinates,
actual execution, SAST completion and final publication. Neither a guest's
candidate nor its model response supplies that authority.

The guest loader compiles the fixed accepted source closure without importing
repository package initializers or candidate bytecode. Candidate Git remains
inert and separate from that closure. A loader or context file cannot prove
that deployment boundary by itself. Local critic, full controller, SAST
integration and mutation/publication integration remain unfinished. These
optional text-phase seams are not an all-agent rollout or activation.

Before a default-branch landing or consumer activation, implement and independently
review the credential-free request producer, exact-head trusted controller,
deployment of explicit role consent and one canonical serialized login lifecycle,
guest pipeline/schema adapters and isolated mutation/publisher paths. Preserve
SAST completion, execution provenance, budgets, deduplication, fork/guarded-path
and production/human holds, independent-review policy and native merge controls.
Review-only consent must not implicitly grant new roles. No account rotation or
alternate-provider fallback is permitted.

Installed files or mocked transports do not establish live activation. Qualify
the actual selected-workflow host boundary, intended account/workspace, healthy
authentication, subscription funding controls and served model, then exercise
useful assessment, triage, critique and repair through real authenticated
publication, new-head CI and protected review/merge. Unsupported sensitivity or
required independent-review contracts hold; do not erase them to obtain a pass.

The generic pack contains no consumer account or infrastructure bindings. Each
consumer owns reviewed adoption and its private policy/customizations. Rollback
retains all existing execution custody and never enables an alternate provider
for a consumer whose policy forbids it.

## GitHub-hosted mode (no dedicated host)

Consumers can run the controller on GitHub-hosted runners by calling the pack's
reusable workflows pinned to one reviewed pack commit. The account login stays in
the consumer entity's own Doppler, so no self-hosted runner, host registry or
root-owned consent files are needed. What changes, and what does not:

| Guarantee | Host mode | Hosted mode |
|---|---|---|
| Whose account | Host registry `account_sha256` | Policy `consent.account_sha256`; the Doppler login must match it |
| Owner consent | Root-owned `/etc/codex-review/automation-consents/` | `consent` in the consumer's default-branch policy (lands only through that repo's review) |
| Trusted controller | Selected-workflow runner group | The caller `codex-automation-worker.yml` on the default branch, pinned to a pack SHA; controller code comes from that pack commit |
| One refresh writer | Host flock + one session directory | The CLI refreshes in-run, only near expiry; a rotated login is written back only while Doppler still holds the login the run started from (best-effort lineage guard: the read and write are not atomic), and is read back |
| Funding | Consent flags + live capacity probe | Unchanged |
| PR source | Credential-free networkless container | Unchanged |

Consumer files (replace `<PACK_SHA>` with the full SHA of the pinned release):

```yaml
# .github/workflows/codex-automation-request.yml
name: Codex Automation Request
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
permissions:
  contents: read
concurrency:
  group: codex-automation-request-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  request:
    if: vars.CODEX_AUTOMATION_ENABLED != 'false'
    uses: Manolii-org/ai-starter-pack/.github/workflows/codex-automation-request-reusable.yml@<PACK_SHA>
```

```yaml
# .github/workflows/codex-automation-worker.yml
name: Codex Automation Worker
on:
  workflow_run: # zizmor: ignore[dangerous-triggers]
    workflows: ["Codex Automation Request"]
    types: [completed]
permissions: {}
concurrency:
  group: codex-automation-${{ github.event.workflow_run.pull_requests[0].number || github.event.workflow_run.id }}-${{ github.event.workflow_run.head_sha }}
  cancel-in-progress: false
jobs:
  worker:
    if: vars.CODEX_AUTOMATION_ENABLED != 'false'
    permissions:
      contents: read
      actions: read
      pull-requests: write
      issues: write
      statuses: write
    uses: Manolii-org/ai-starter-pack/.github/workflows/codex-automation-worker-reusable.yml@<PACK_SHA>
    with:
      pack_sha: <PACK_SHA>
      enabled: ${{ vars.CODEX_AUTOMATION_ENABLED != 'false' }}
      publish: ${{ vars.CODEX_AUTOMATION_PUBLISH == 'true' }}
    secrets:
      CODEX_AUTOMATION_DOPPLER_TOKEN: ${{ secrets.DOPPLER_TOKEN_CODEX }}
      CODEX_AUTOMATION_PUBLISH_TOKEN: ${{ secrets.CODEX_AUTOMATION_PUBLISH_TOKEN }}
```

> **Migration note.** A called workflow cannot receive more permissions than
> its caller grants. The worker `execute` and `publish_fix` jobs now request
> `pull-requests: write`. Consumers that copied an earlier version of this
> example granted the caller `pull-requests: read`; if you update only the
> pinned `<PACK_SHA>` on the `uses:` line, the called jobs cannot run with the
> requested permission and the PR comment will fail to post. Update the
> caller's `permissions:` block to `pull-requests: write` (and keep
> `contents: read`, `actions: read`, `issues: write`, `statuses: write`) on
> the worker job BEFORE bumping the pin.

`.github/codex-automation.json` holds the controller policy fields of
`scripts/ci/codex-automation-worker.json` plus:

```json
{
  "consent": {
    "schema": "codex-automation-hosted-consent/v1",
    "owner": "<account owner>",
    "account_sha256": "<sha256 of tokens.account_id>",
    "roles": ["assessment", "judge"],
    "repositories": ["<owner>/<repo>"],
    "expires_at": 1798761600,
    "revoked": false,
    "purchased_credits_disabled": true,
    "automatic_purchases_disabled": true
  },
  "credential": {
    "doppler_project": "<project>",
    "doppler_config": "<config>",
    "secret": "CODEX_SUBSCRIPTION_AUTH_JSON"
  }
}
```

`CODEX_AUTOMATION_DOPPLER_TOKEN` is a Doppler service token scoped to exactly
that config (the example reuses the consumer's existing `DOPPLER_TOKEN_CODEX`).
A read/write token makes the run the refresh writer under the lineage guard; a
read-only token works too, using the refreshed copy in memory and writing
nothing back (the same behaviour as the existing advisory review). Committing
the caller files activates the controller; setting the repository variable
`CODEX_AUTOMATION_ENABLED=false` stops later invocations of both jobs. It does
not cancel a worker run that has already started (that run keeps its own
`enabled` input); cancel the run in Actions to stop it. Guarded repair needs all
three of: `"autofix"` in `consent.roles`, top-level `"autofix": true` in
`.github/codex-automation.json`, and `CODEX_AUTOMATION_PUBLISH=true`. Repair is
skipped unless the top-level `"autofix"` is `true`.

Hosted-mode limits (accepted):

- Logins are refreshed only within an hour of expiry, about once per token
  lifetime, and a rotated login is written back even if a later check refuses
  the run. Two runs that refresh the same login in the same minute can still
  race. The lineage guard is best-effort: it skips the write when Doppler
  already holds a different login (recorded as `superseded`; that run continues
  on its own refreshed copy), but because the read and the write are separate
  requests, two writers that both read the original login can still both
  write, and the slower one wins. Refresh near expiry keeps that window rare.
- Several consumers may read one Doppler login (for example, an account that
  funds more than one repository). Only a read/write token writes back.
- The trusted controller is the caller `codex-automation-worker.yml` run by
  `workflow_run`, which GitHub always resolves on the default branch; custody
  re-authenticates that run. Keep `pack_sha` equal to the `uses:` SHA.
