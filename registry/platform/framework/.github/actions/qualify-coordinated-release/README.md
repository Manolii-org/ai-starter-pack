# Coordinated release qualification

One protected production decision covers the exact release plan: compatible
database preparation, named app artifacts, feature activation, verification and
the named safe recovery. This action validates evidence; it never supplies that
decision, credentials, provider commands or a second approval service.

The owning trusted adapter authenticates the gate artifact's repository,
workflow, event, source, run and attempt before calling this offline action.
It constructs `expected_subject` from fresh observations, including current
supported-client policy and production generation. Arbitrary JSON is not an
attestation. Classification is reviewed policy, not an SQL effect detector.

Use an immutable reviewed pack commit. Run `qualify` before presenting approval;
recheck `prepare` immediately before database writes and `activate` immediately
before enabling behavior. Keep all production mutation/verification in the
existing protected job, or propagate an authenticated authorization for exactly
the same release identity. A typed confirm or auto-dispatch is not approval.

## Entity-local bundle contract, version 1

`subject` binds `entity`, `release_group`, `target`, exact component `sources`
(40 hex), immutable `artifacts` (SHA-256), `migration_digest`, `policy_digest`,
`client_policy_digest`, the observed `baseline`, and `recovery` (mode and named
artifact/migration digests). Explicit empty artifact/baseline maps support
database-only/no-database services. All digests are raw lowercase SHA-256.

`supported_clients` identifies every protected installed cohort, including the
deployed web/worker source. `required_checks` lists executable checks. Evidence
records the identical subject, successful check IDs and their covered clients,
completion time, exact run/attempt and `ready_components` bound to source SHAs.
`evidence.release_id` must equal the hash of the entire plan excluding evidence,
including classifications, features, required checks and supported cohorts.
Execution phases also require `approved_release_id` from the trusted protected
decision. Editing the plan cannot reuse either the tested or approved identity.
Evidence expires after 24 hours; a changed plan/policy requires requalification
and a new approval. Retries of the same plan retain `release_id`.

Each `pending_migrations` entry has `id`, content `sha256`, `effect`, and
`feature` (null for inert preparation). Effects are:

| Effect | Rule |
| --- | --- |
| `schema-preparation` | Compatible and dormant; may precede any dependent app. Review actual SQL, triggers, jobs and configuration writes. |
| `behavior-change` | Must name a feature and pass its behavioral checks for every supported cohort; app-dependent behavior cannot run in preparation. |
| `feature-activation` | Must be explicit release intent. An all-pending preparation applier must hold until activation SQL is separated. |

Each feature records `id`, observed `active`, intended `activate`, dependent
component names in `requires`, `client_policy`, and `required_checks`. Already
active features require behavioral coverage too. Checks must cover every
supported client, not only the candidate. Publishing a mobile version or
uploading to a store never proves adoption by installed devices.

`all-supported` preserves every installed cohort. `verified-targeting` and
`verified-minimum` additionally require executable `<feature>:version-policy`
and `<feature>:unknown-client-safe` checks covering all protected cohorts.
The adapter must prove the actual server/device enforcement, mixed old/new
installs, missing/stale version metadata and existing sessions/queues. A source
manifest or claimed minimum alone is insufficient. Do not automatically retire
an old cohort. Activation waits for exact dependent component readiness.

See `scripts/tests/test_coordinated_release.py` for complete generic bundles and
negative cases, including web/mobile, database-only and container-revision
variations. Test fixtures are not deployment evidence.

## Adapter obligations

Inventory every writer: Git integrations, build/startup SQL, seeds, migrations,
config flags, triggers, jobs, manual paths and feature-control services. No merge
or alternate path may bypass production approval. Verify target identity before
the first write. Keep provider secrets and evidence in their owning entity.

Keep behavioral activation dormant until readiness. If the existing applier
cannot separate activation safely, fail closed and author an inert expansion
plus a separately named activation action. Do not regex-edit historical SQL or
silently skip a migration dependency. Upload/store review and user installation
remain asynchronous; dependency safety does not promise simultaneous release.

Exercise approval/rejection, changed head/policy/baseline, duplicate events,
partial apply, failed app promotion/upload, recovery and mixed clients before
declaring adoption. Preserve applied additive schema after a later failure;
fix forward or use the exact named safe recovery. A destructive restore needs
its own authority. Keep existing receipt formats/status names and record the
new release identity alongside them.

Source distribution does not install this boundary in a consumer. Adopt via the
existing tagged pack/registry and owner-specific overlay, canary first. Never
replace provider commands, branch topology, SQL engines or entity credentials
with a generic central execution service.
