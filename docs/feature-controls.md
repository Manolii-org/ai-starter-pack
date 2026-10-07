# Feature controls core

This public core wraps immutable GrowthBook payloads; it does not authorize requests, host providers, create flags, analyze experiments, or supply production stores. Applications must authorize first and derive every context from trusted server data.

## Immutable installation

Use a full 40-character source commit SHA, not a branch/tag:

```text
npm install "git+https://github.com/Manolii-org/ai-starter-pack.git#<FULL_COMMIT_SHA>"
pip install "git+https://github.com/Manolii-org/ai-starter-pack.git@<FULL_COMMIT_SHA>#subdirectory=packages/feature-controls/python"
```

The root npm bridge exports `@manolii/feature-controls` and `@manolii/feature-controls/client`. Its explicit files allowlist contains compiled/source SDK, schema and this document (npm also includes mandatory metadata, README and license). Safe `prepare` only builds strict TypeScript. GrowthBook is pinned to JS `1.8.0` / Python `3.2.0`; package semver `0.0.0` is independent of wire version. There is no registry publication. A verified SHA-256 `npm pack` tarball is another handoff artifact, not a registry version. npm cannot install from a git subdirectory; committed consumers must not use local `file:` dependencies.

## Server contract

- `FeatureRuntime.evaluate(key, surface, context, options)` returns `resolved/baseline/denied`; `snapshot(keys, surface, context, options)` projects client-safe decisions. Python uses keyword `preview/now`.
- `activate(bundle, expectedRevision, now)` verifies complete UTF-8 payload bytes, SHA-256, scope/catalog/provider binding, approval, expiry and replay order, then atomically swaps state.
- `updateKills(kill, approvalRef, now)` requires increasing generations independently of activation. Known kills and ancestor/local/context exclusions dominate provider absence, failure, and ordinary-bundle expiry, even for `failure: "baseline"` with `baseline: true`.
- `approvalMessage/killMessage` validate metadata and produce UTF-8 JSON arrays in fixed field order without whitespace. Integer fields normalize to decimal integers (including integral Python floats decoded from JSON); strings are schema-restricted ASCII. Only payload bytes themselves are hashed, never reconstructed canonical JSON. Python exports snake_case equivalents.

Runtime scope is `ecosystem_id/deployment_id/environment_id/feature_namespace`. Contexts additionally bind `schema_version: 1`, `application_id`, `surface_id`, `context_scope`, and `projection_source: "trusted-server"`. The opaque context scope binds the authenticated subject/session without exposing an assignment key. The source marker is not authentication: adapters must build trusted projections and secure transport themselves.

Catalog application allowlists restrict features. Assignment boundaries isolate applications by default. Explicit experiment `assignment_boundary: {key, applications}` permits only declared apps to share cohorts. Store keys include runtime scope, boundary mode/key, experiment, allocation epoch and opaque unit key. Existing app-bound layouts require a separately verified migration. No identity-link API or cluster inference is supplied.

Features require typed `disabled_value`; boolean disabled values must be false, including values declared as JSON. Optional `allowed_values/value_schema` validate baseline, disabled, native default, forced, variation and resolved values. Bounded keywords: `type/enum/minimum/maximum/minLength/maxLength/minItems/maxItems/items/properties/required/additionalProperties`; type-inapplicable keywords reject. Objects must be closed; references, remote schemas, patterns and arbitrary composition are rejected. Schema recursion is capped at 8; JSON value nesting at 20. Integers outside ±9,007,199,254,740,991 reject in catalog/provider/client JSON. Unicode value strings/object keys are allowed; numeric 1.0/1 are semantically equal, not canonically reserialized for hashing. TS privately clones/freezes the catalog; Python exposes a defensive copy. Trusted catalog-revision governance is still external; there is no signed catalog-manifest system.

## Provider-backed targeting

Payloads retain exactly `features`, empty `experiments`, empty `savedGroups`, and empty `contextualBandits`; all catalog features must exist. GrowthBook owns forced/hash-v2 allocation, conditions and sticky selection. Identity keys/seeds are bounded opaque ASCII, with control/Unicode strings rejected because non-BMP provider hashes are not cross-language equivalent.

Native conditions allow `groups/roles: $in/$nin` and `tenant: $eq/$ne`. Trusted context carries `groups/group_ancestors/excluded_groups/roles/tenant_key`; bounded complete acyclic graphs expand memberships deterministically. Missing nodes, cycles and depth beyond 32 reject. Exclusions dominate positive targeting. Group eligibility is not cluster assignment/analysis. Richer targeting, prerequisites, saved groups, segments, URLs, bandits, remote refresh and arbitrary callbacks remain rejected capability gaps.

## Stores, clock and bounded snapshots

Production activation requires a trusted approval verifier and durable atomic `DurableControlStore`. Randomized live evaluation requires a durable atomic `AssignmentStore`. `durable: true` declares a trust boundary, not certification. Local fixtures require `trust_policy: "local-test"` and explicitly `test_only` stores.

Control states require `{bundle, kill, time_highwater}`. Reads validate metadata/scope; non-preview observations CAS-advance the persisted clock floor with at most four attempts. Activation, kills and evaluation reject earlier time. Never migrate by silently resetting high-water metadata. These mechanisms require real datastore atomicity/durability; synthetic in-memory restart/race probes are not production certification.

Snapshots capture one control state and validate/hash/parse payload once per request. Every decision projects that root revision/kill pair, including restrictive decisions. Requests accept at most **64 keys**. At the current time floor there is one control read; advancing it adds CAS and bounded contention rereads. SDK instances remain per-key/context, assignments serial: serialization/provider setup can still cost up to 128 provider loads for 64 randomized keys at the 1 MiB payload bound. No global mutable evaluator or arbitrary batch-efficiency claim. Context/options are copied before suspension. Preview writes no control/assignment/event records, installs no tracking callback, and uses read-only sticky fixtures.

## Client API and replay contract

`@manolii/feature-controls/client` imports no GrowthBook, Node crypto, telemetry or server module. Wire `schema_version` is authoritatively integer **1**; source metadata may stringify it as **"1"**, never "1.0". Provider semantics and SDK semver are independent.

Snapshots bind all four scope dimensions plus application/surface/context scope. Root fields include `configuration_revision/kill_generation/time_highwater/generated_at/expires_at` and `decisions`. Decisions contain value/status/reason/expiry and consistent control metadata, never groups, unit/assignment IDs or targeting traces. Revision zero/null means no bundle. No catalog/context digest is computed and no generic JSON canonicalization is claimed. Exact payload bytes alone are SHA-256 hashed and approval-bound; Unicode value/key and ordinary finite-fraction semantics remain structural. This is structural validation, not snapshot signature verification; trusted authenticated delivery is an adapter responsibility.

Primary API: `new DecisionClient(scope, minimum)`, `bindSession(session, context_scope)`, `setSnapshot(snapshot, session, now)`, `get(key, baseline, now, accepts?)`. Required minimum is `{configuration_revision, kill_generation, time_highwater}`. Seed/persist `client.watermark` as scope-global, non-identity metadata between client lifetimes; all-zero values are only for a genuinely new scope. Neither revision nor kill component may decrease; a rollback release needs a higher activation revision. Candidates replace decisions/fences atomically. Rejected candidates, including delayed responses from a previous session, retain current decisions and watermarks. Only explicit `bindSession/clear` changes the account boundary. Backward clock observations fail closed.

`bindSession(null, null)` or `clear()` removes decisions on logout; account switching requires the new authenticated context scope. Clearing never resets watermarks. A retained denial cannot become an enabling fallback on snapshot expiry or clock rollback; it requires a valid replacement or explicit session/decision clearing. Denied false dominates boolean fallback and predicates; incompatible non-boolean denied values throw instead of returning a presentation baseline. Predicates and returned JSON values receive copies, not cached objects. Unknown/absent snapshots use the separately approved presentation baseline. For fail-closed boolean controls use false; client values are never authorization.

Stateless `validateSnapshot(value, scope, now, minimum)` and `decisionValue(value, key, baseline, scope, now, minimum, accepts?)` require explicit fences. They cannot remember observed kills or clocks without caller persistence. Missing/expired/wrong-scope/version/extra-field data and incompatible non-denied values return baseline. Within a valid snapshot, denied false dominates boolean fallback and predicates; incompatible non-boolean denied values throw. JSON object/array values require an application type predicate.

## Measurement and proof limits

`recordEvent/record_event` separates exposure from outcome, emits nothing on evaluation, and deduplicates through a sink. It is **local-test only**, even with a durable-tagged sink. Evidence binds the original unit/application/surface/context; synthetic outcomes require `event_id === evidence.transition_key`. Process-local receipts expire with decisions and cannot certify transitions, survive restart or attribute delayed outcomes. Production requires authenticated durable receipt/read ports, business-transition-plus-outbox transactions, delayed attribution and quarantine in separately verified adapters.

Shared offline tests cover 1,000 ASCII parity probes, exact approval bytes, complete payloads, scope/exclusions, schemas/native targeting, immutable catalogs/inherited properties, synthetic clock/restart fences, snapshot atomicity, client replay/session/expiry behavior, preview and local attribution. They are L0/L1 evidence only. Target adapters still need real crash/restart/CAS/migration/outbox and time-floor proofs. No custom statistics, SRM, denominators, winners, ICC, power, cluster estimators or production telemetry connector is provided.

## Validation

```text
npm ci --ignore-scripts
npm run lint
npx tsc --noEmit
npm test
packages/feature-controls/python/.venv/bin/python -m pytest -q tests/feature_controls/provider_conformance.py
ruff check packages/feature-controls/python/feature_controls tests/feature_controls/provider_conformance.py
python3 -m pytest -q tests/
npm audit --audit-level=moderate
```

Create the isolated Python environment with `python -m venv packages/feature-controls/python/.venv` and install `-e packages/feature-controls/python pytest==8.3.5` before its conformance suite. It is intentionally not auto-collected by dependency-light pack pytest. Scoped root ESLint and strict TS configs check the SDK. Python adapters normalize expected availability failures to `RuntimeError/OSError`; unexpected exceptions propagate instead of being silently swallowed.
