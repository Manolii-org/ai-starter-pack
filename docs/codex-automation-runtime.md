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

## Completion of the train

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
