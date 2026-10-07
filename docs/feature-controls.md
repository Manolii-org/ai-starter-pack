# Feature controls core

This public package is a small, server-side contract around immutable GrowthBook payloads. It does not authorize requests, host a provider, create flags, analyze experiments, or supply production stores. Applications must authorize first and derive `Context` only from trusted server data.

## Install by immutable revision

Use a full 40-character commit SHA, never a branch or tag:

```text
npm install "git+https://github.com/Manolii-org/ai-starter-pack.git#<FULL_COMMIT_SHA>"
pip install "git+https://github.com/Manolii-org/ai-starter-pack.git@<FULL_COMMIT_SHA>#subdirectory=packages/feature-controls/python"
```

The root npm package exports `@manolii/feature-controls` and `@manolii/feature-controls/client`. `files` is an explicit allowlist for compiled output, source, schema, and this document (npm also includes mandatory package metadata, README, and license); `prepare` reproducibly compiles strict TypeScript with the lockfile-pinned GrowthBook `1.8.0`. The Python subpackage pins GrowthBook `3.2.0` and includes the same JSON Schema as package data.

There is no approved registry publication. Until one exists, consumers may use the tested full-SHA root git install above, or consume the exact `npm pack` tarball produced from that checkout after independently verifying its published SHA-256 provenance. npm cannot install this package from a git subdirectory, and committed consumers must not use machine-local `file:` dependencies. The tarball is a handoff artifact, not a registry version.

## Server API

- `FeatureRuntime.evaluate(key, surface, context, options)` produces a typed `resolved` / `baseline` / `denied` decision. It intentionally exposes no authorization-like `allowed` field. `snapshot(keys, surface, context, options)` produces the client wire projection.
- `activate(bundle, expectedRevision, now)` validates the complete exact UTF-8 payload, its SHA-256 digest, catalog/scope/provider bindings, approval bytes, expiry, replay order, then delegates one atomic compare-and-swap.
- `updateKills(kill, approvalRef, now)` is independent from release activation and accepts only increasing generations.
- `recordEvent(event, now)` accepts an explicitly attributed exposure or authoritative outcome after a randomized decision. Evaluation alone emits nothing.
- `approvalMessage` and `killMessage` return UTF-8 JSON arrays in fixed field order with no insignificant whitespace. All fields are integers or bounded ASCII, so Python and JavaScript generate identical bytes; payload bytes themselves are never canonicalized, only digested.

Constructing a production runtime requires a trusted approval verifier and a durable atomic `DurableControlStore`. Randomized live decisions additionally require a durable atomic `AssignmentStore`; events require a durable idempotent `EventSink`. Interfaces tagged `durable: true` are trust boundaries, not runtime implementations. `trust_policy: "local-test"` is only for explicit synthetic/local fixtures; it refuses a supplied control store unless that store is explicitly marked `test_only: true`. Preview never creates assignments/events, calls tracking callbacks, or sends telemetry.

Provider payloads must contain exactly `features`, empty `experiments`, empty `savedGroups`, and empty `contextualBandits`. Every catalog feature must be present. The adapter permits provider-native forced values and hash-v2 variation rules only; arbitrary conditions, segments, prerequisites, URLs, bandits, and remote refresh are rejected. Identity and seeds are opaque bounded ASCII. This intentionally removes unsupported transitive/live dependency behavior instead of reimplementing a rules engine.

## Client API and wire

`@manolii/feature-controls/client` has no GrowthBook, Node crypto, telemetry, or server imports. `validateSnapshot`, `decisionValue`, and `DecisionClient` accept only schema version 1 and exact `application` / `environment` / `surface` binding. Snapshots use `schema_version`, `generated_at`, `expires_at`, and a `decisions` map. Each decision contains only value, reason, expiry, configuration revision, and kill generation—never context, groups, assignment IDs, or targeting traces.

Unknown, missing, expired, wrong-scope, wrong-version, extra-field, or type-incompatible data returns the caller's baseline. JSON object/array values require an application-supplied type predicate. Call `bindSession` on login/account changes and `bindSession(null)` or `clear()` on logout; changing the session clears the in-memory snapshot.

## Safety and conformance status

The checked-in tests are offline L0/L1 evidence: shared Python/TypeScript golden outcomes, 1,000 ASCII assignments, Unicode rejection, exact approval bytes, payload completeness, scope separation, monotonic kills, expiry/skew boundaries, local-disable/ancestor precedence, preview purity, sticky rebucketing resistance, event attribution/idempotence, and client session/expiry behavior. They block network access in the Python provider test and install no provider callback.

They are **not** production L2 datastore certification. Target adapters must separately prove subprocess restart durability, persisted activation/kill/time high-water marks (including clock rollback), crash-boundary atomicity, real CAS races, identity-alias migration/conflict handling, transition-plus-outbox transactions, and late-outcome quarantine. The core deliberately has no identity-link API: applications issue verified opaque keys and migrate aliases in their durable assignment adapter.

Statistics remain provider/warehouse concerns. This package does not compute SRM, denominators, winners, cluster estimators, ICC, power, or telemetry-gap policy. Assignment/exposure/outcome events stay separate so a validated connector can build those inputs. No analysis capability may be advertised until the external connector passes its registered unit, exclusion, denominator, cluster-minimum, and suspension gates.

Run the verified local profiles:

```text
npm ci --ignore-scripts && npm test && npm run typecheck
python -m venv packages/feature-controls/python/.venv
packages/feature-controls/python/.venv/bin/pip install -e packages/feature-controls/python pytest==8.3.5
packages/feature-controls/python/.venv/bin/python -m pytest -q tests/feature_controls/provider_conformance.py
```

The Python SDK suite is invoked explicitly after its isolated dependency installation, rather than collected by dependency-light pack-only pytest. Root TypeScript configuration extends the strict SDK compiler configuration; root ESLint applies type-aware rules to the SDK source. Adapters normalize availability failures to `RuntimeError` or `OSError`; other unexpected exceptions propagate rather than being silently swallowed.
