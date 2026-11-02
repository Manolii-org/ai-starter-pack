# Automatic feature releases

An unfinished feature stays off. A feature marked ready in reviewed source
defaults on through its normal approved deployment. An intentional disable wins
and survives deployments, renewals and rollback. A passing build cannot infer
functional completion.

The owning repository commits `.feature-controls/releases.json`:

```json
{"schema_version":1,"features":{"search.export":{
  "state":"development","owner":"team.search",
  "applications":["web"],"surfaces":["search"],"acceptance":[]
}}}
```

The change that finishes the feature sets `state` to `ready` and supplies
repository-relative acceptance-test references. CI must run the relevant tests;
file existence alone is not acceptance. Normal review establishes completion.
Gate every affected server, UI and worker entry point. Registration cannot find
or fence arbitrary unregistered code. Authorization and operational holds remain
independent.

## Runtime and publication

`releaseCatalogFeatures` / `release_catalog_features` generates strict boolean
definitions. Add them to the owning catalogue, preserving legacy controls.
Construct `FeatureRuntime` with the reviewed `release_manifest` and actual build
`release_source_sha`. It independently disables unfinished source features.

The normal owning deployment publisher verifies the deployed artifact and
reads `previousForDeployment()` / `previous_for_deployment()` in its private
operations process. `prepareDeploymentPayload` / `prepare_deployment_payload`
combines source defaults with the unchanged approved provider policy. It rejects
provider drift, ownership collisions, retirement, readiness regression and
changed application/surface/ancestor boundaries. Existing provider false values
need explicit classification and migration; they are never guessed safe to enable.

The publisher signs fresh immutable payload bytes with
`deployment: {source_sha, manifest}` and activates against the previous revision
through the existing CAS store. It never clears kills. This is part of normal
approved deployment, not an additional manual feature-enable step. Initial
baseline approval, durable authority, existing identities and normal deployment
approval are prerequisites; the helper bootstraps none of them.

Keep the existing provider-catalogue revision for additive source-owned flags;
the signed source policy versions those additions. Changing the legacy provider
contract still needs its owning migration process. Each shared feature has one
canonical policy owner. Client repositories consume authenticated decisions and
apply their own source guard. Cross-component work retains normal end-to-end
acceptance and coordinated release rules where adopted. Never join org data,
secrets or user identities to share the framework.

## Automatic and intentional off

Absence from the durable disabled set means Automatic. Off is an independent,
higher-generation kill, optionally carrying signed `exceptions` metadata:

```json
{"checkout":{"owner":"team.incident","reason":"Investigating an incident","review_at":1800000000000}}
```

Metadata is bound by the v2 kill message. Without metadata the v1 message remains
byte-compatible. Reasons are bounded printable ASCII; never store secrets or
personal data. Emergency off works immediately without metadata. Qualification
reports `needs_review` until follow-up. An overdue review never re-enables a flag.
Valid exceptions appear in every release summary and need no repeated approval.
Renewals preserve disables, metadata and deployment policy.

## Qualification and clients

An owning operator probe calls `releaseEvidence` / `release_evidence` with trusted
eligible contexts. This batches bounded snapshots and inspects actual approved
controls. Its private stream is captured by the release checker; never return
control state to browsers, logs or artifact uploads.

Verified inspection additionally requires the owning verifier's
`verifyKill(kill, scope)` / `verify_kill(kill)` port. It looks up and authenticates
the exact scoped kill message's durable receipt, including exception metadata.
Missing or invalid receipt verification fails qualification while the runtime
continues to enforce the off setting immediately. Legacy ordinary reads need
no new verifier port.

`assessRelease` / `assess_release` checks expected versus independently observed
source, policy, scope, revisions, kill generations, freshness, coverage and values.
Normal deployment requires the signed approval's source SHA to match that exact
artifact, including when its readiness declaration is unchanged. Publish a fresh
approval for the new source without clearing any disables.
Ready unexpectedly off fails. Unfinished unexpectedly on fails. Missing, mixed
or fallback evidence is not green. The normal deployment also verifies actual
feature journeys, not only boolean delivery.

Browsers import only `@manolii/feature-controls/client`. At build/server time,
`releaseClientStates(manifest, application_id)` projects IDs and readiness
booleans without owners, acceptance paths or rules. Wrap the existing
`DecisionClient` in `ReleaseClient`; `enabled` requires both source readiness and
an authenticated true server decision. Unknown and unfinished keys stay off.

Pin `.github/actions/feature-release-policy` to the same immutable component
release as the SDK. Put source mode inside an existing required guard/test job
after checkout. It reports `source_validated` and `runtime_status: not_requested`.
An empty policy reports `no_features`, not a verified live service.

Runtime mode needs the owning protected deployment job, a reviewed probe script
and exact expected source SHA. The probe independently verifies the deployment
and returns `{schema_version:1,source_sha,scope,controls,snapshots}` privately.
The checker suppresses raw probe streams and writes a release summary. It refuses
PR and workflow-run events: a downstream protected ref cannot establish an
upstream run's trust. It supplies no cloud credentials,
approvals or authority to ignore HOLD/PARK.

## Rolling versions and adoption

Deploy SDK/publisher support before writing v2 control metadata. SDK 0.1 readers
reject new fields restrictively; retain the new SDK floor in recovery images and
renewal jobs. Stripping metadata or resetting watermarks is not rollback.

Compatible new policies work with old and new source simultaneously. Only signed,
declared future source keys are projected from an older catalogue's provider
view; approved bytes and signatures are retained. Each version still disables
its own unfinished code. Unknown provider additions reject. A normal publication
cannot remove a flag or turn ready back into development. Use a separate flag
for unfinished replacements or an explicit disable for incidents.

An explicitly authorized owning rollback can qualify compatible older source
without overwriting the newer control policy or clearing disables. The owning
rollback gate and artifact verification remain required.

Track source tooling, SDK integration, owning adapters, nonproduction acceptance
and production activation separately. A manifest check, empty policy or synthetic
test establishes only that step. Keep runtime evidence within its org/environment.
