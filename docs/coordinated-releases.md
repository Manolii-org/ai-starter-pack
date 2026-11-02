# Coordinated release capability

The version 1 capability prepares and validates immutable release proposals and
activation-only continuation proposals. It does not execute migrations, activate
traffic or certify human approval. Existing product workflows remain authoritative.

## Owner boundaries

The control repo owns policy, adoption declarations and readers. This pack owns
the generic manifest schema, validator and admission-state primitives. The product
owns database/provider commands, qualification, backup storage/keys, live-state and
approval observers, locks, smoke, recovery authority and alerts. Publish only
generic code and synthetic fixtures here. Private overlays consume an immutable
reviewed pack revision. Do not copy product workflows into the public package.

The control repo's optional deployment-contract `release` block defaults to legacy.
The standalone `schemas/release-adoption.v1.schema.json` describes that block.
The pack's existing `schemas/deployment-contract.schema.json` describes a different
lane-model envelope and remains unchanged; never replace one with the other.
Shadow mode
compares evidence without another production operation or human decision.
Coordinated mode requires a qualified owner profile; a static declaration does not
establish live protection. Preserve installed decision scopes, actors and branch
rules. Combining distinct schema/activation decisions requires explicit owner
policy adoption. Automatic profiles retain their existing production policy.

## Manifest validation

`schemas/release-manifest.v1.schema.json` binds the candidate, reviewed control and
capability revisions, policy and configuration digests, qualification run/attempt
and actual synchronized schema/fixture context, per-destination artifact/recovery
identities, engine-native ledger baseline/pending/after-state, timing and recovery
owners. It contains metadata, never SQL, database URLs, credentials or application
data. Arrays retain their engine-native order; duplicate identifiers are invalid.

`release_id` is SHA-256 of the subject encoded as UTF-8 JSON with sorted object
keys, compact separators, literal Unicode and no non-finite numbers. The top-level
schema version and ID are not part of their own hash. The trusted preparation job
retains the expected ID separately from the downloaded manifest. The validator
checks that independent expectation; matching it still does not attest what a
human approved or what is currently deployed.

The schema itself requires activation-only proposals to link predecessor/schema
evidence, select a new protected decision and carry empty pending migration lists.
Normal releases cannot claim continuation metadata. The portable validator also
checks identity, ledger and cross-field consistency. A synthetic complete normal
manifest is in `scripts/tests/fixtures/release-manifest.v1.json`; it is test data,
not a current approval or a deployable release.

Use the composite at `.github/actions/validate-release-manifest` with `manifest`
and `expected_release_id` from the trusted preparation dependency. It installs the
same pinned JSON Schema validator as the receipt action, validates, and returns
only `release_id`. Candidate checkout must not overwrite this trusted action or
the preparation/upload/output code. The action uses no production credential.

For local read-only verification:

```bash
python3 .github/actions/validate-release-manifest/release_manifest.py \
  --manifest /path/to/manifest.json --expected-release-id "$EXPECTED_RELEASE_ID" --dry-run
```

The validator rejects changed candidate/qualification context, duplicate targets,
unknown database dependencies, inconsistent ledger sets, tampering, invalid
deadlines, extra fields and pending SQL hidden in an activation-only proposal.
Version 1 qualifies one common candidate migration graph: one database or mirrors
of that same graph. Every database's migration digest must equal the tested
qualification digest. Heterogeneous migration graphs require a separately
qualified version/profile rather than an invented aggregate hash.
The owning adapter separately verifies real artifact/configuration provenance,
latest qualification, backup policy and current before-state.

## Approval capability observation

`github_approval.py` reads the documented workflow-run review-history endpoint
using the owning `GH_TOKEN`; it refuses redirects and never retries with another
identity. Its machine-readable result is `unsupported`, `blocked` or
`unavailable`, always with `execution_authorized: false` and exit code 3.

That endpoint records reviewer, environment, state and comment. It does not bind
a manifest digest, exact job/attempt and authenticated decision timestamp.
Adding similarly named fields to a client JSON file does not repair that API
contract. A qualified provider-retained immutable linkage still needs a reviewed
adapter and actual transport evidence. Until then, retain legacy execution and
hold all coordinated paths depending on this capability. Do not use typed
confirmation, a dispatch actor or a mutable summary as a replacement.

## Interrupted activation and admission

After successful schema application, record the original release ID, successful
schema stage, completion time and per-target after-state in immutable evidence.
The trusted observer rechecks current live artifacts, ledger and policy. The
`activation_only` function prepares a new subject with empty SQL, the same pinned
application/configuration, new preparation identity and a new protected decision. A combined-release
profile preserves its `release` decision scope; separate schema/activation
profiles request the remaining `activation` decision. Policy digests do not
silently change meaning during continuation preparation.
It links the predecessor and expected schema-evidence digest. Preparation never
reserves a release or reuses an expired approval.

The continuation deadline must remain within the predecessor's declared maximum
schema-to-activation interval. Changed schema, artifact, policy, missing evidence
or expired compatibility requires the named recovery owner and a reviewed
forward-fix/incident decision. A destructive restore that loses later writes is
not an automatic rollback. Backup decryption-key availability and isolated restore
qualification are enablement requirements.

`release_state.py` supplies a shared Git admission store under
`refs/release-state/<normalized-environment>`, outside application branches.
Qualify the custom-ref transport and its owning contents-write permission before
adoption; source fetch success is not proof that writes work. A state read denied
or concealed by the provider never becomes empty state, including HTTP 404.
Runtime reads require an initialized revision. `initialize_empty_for_adoption`
is a distinct bootstrap after writer closure and verified legacy drain; it can
only create an empty custom ref and cannot overwrite an existing ref. Never call
it as a runtime read-error fallback. The store retains exact parent
revisions and creates a unique child commit, then updates without force. Two
concurrent children cannot both advance one revision, including identical
proposals created in the same second. An unknown write result holds for readback;
it is not permission to execute or blindly repeat the mutation.

`reserve` rejects an expired execution window using a timezone-aware admission
clock, then constructs a proposed state update; the caller must successfully
`compare_and_swap` the expected retained revision before claiming reservation.
It blocks new normal releases during every unresolved prior phase and enforces
one continuation per predecessor. Only `success`, `failed-before-writes` and
owner-qualified `recovered` close a normal admission. Record running phases
before mutation so runner loss remains unresolved rather than falsely complete.
Production workflow overlap and cross-repository database fencing are still
required; this metadata store does not replace them. Retention maintenance must
preserve active/recovery-held identities and prevent replay of expired scopes.

## Rollout and compatibility

Publish readers/schemas before producers; keep strict receipt v1/v2 unchanged.
Companion records join through the receipt's explicit `promote_run_url`, exact
attempt and immutable artifact ID. They do not infer identity from run titles or
workflow `head_sha`. Keep workflow names, required checks, holds, monitors and
legacy sessions compatible until their writers drain.

Shadow exit requires parity for both migration and no-migration cases and tested
tamper, interrupted activation, duplicate admission, backup retrieval/restore and
provider failure scenarios. Measure stage budgets; duration alone is insufficient.
Verify existing alerts cover held, overflow, interrupted and continuation states.
Define the legacy drain and exact canary abort/recovery owner before enablement.
The admission stop affects future requests and never cancels an active writer.

Native GitHub `queue: max` is bounded to 100 pending requests; ordering is by
wait-start, not guaranteed dispatch order. Overflow cancels new requests. Declare
`bounded-native` with its reconciliation route, rather than claiming unlimited
durability, and keep `cancel-in-progress: false` on the production writer.

From a pack checkout, run the owning shared-core, real-Git CAS, generated-drift
and consumer-render checks with:

```bash
python3 scripts/agent-check.py --provider codex --provision
```

This provisions the recipe's isolated Python 3.12 environment with pinned
JSON Schema/PyYAML and pytest/Copier dependencies. The recipe covers its configuration and
legacy lane-model schema inputs as well as the release capability.

The final public candidate must also pass the existing secret scan. No model
call, additional business database or new approval service is added to releases.
