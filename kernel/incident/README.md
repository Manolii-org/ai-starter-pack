# Incident kernel: Sentry incident sweep

A portable, knowledge-layer-free producer of **sanitized production-incident
candidates** from one entity's Sentry organization. It is the shared building
block every org uses to turn Sentry issues into automated-fix candidates. The
pack holds a single copy; each org binds it with its own routes manifest and
credential.

## What it does

1. For each explicitly routed **(project id, environment)**, lists unresolved
   `error`/`fatal` issue groups seen in the lookback window, with complete
   bounded pagination.
2. Reads each group's latest event **once**, up to `max_enrichments` (default
   60), oldest `lastSeen` first. Groups over the budget are the newest; they
   are deferred to the next sweep rather than emitted unclassified.
3. Classifies deterministically with `signatures.json`, without a model call,
   using the candidate-schema enum `code_defect | migration | configuration |
   provider | quota | security | unknown`. `code_defect` requires an in-app
   stack frame **and** a commit-SHA release.
4. Prints one candidate per group (JSON Lines) in the sanitized
   production-incident candidate shape documented in `candidate-shape.md`.

It never writes to Sentry, GitHub or any store. The consumer owns admission,
attestation, per-entity switches, caps, deduplication and execution
(typically an orchestrator's production-incident intake).

## Why these choices

| Choice | Reason |
|---|---|
| Route by (project, environment) | One Sentry project is often shared by several deployments, e.g. an app and a scraper on one DSN. |
| Fingerprint = `sha256("sentry-group:<id>")` | Stable across deploys and days, so an unfixed error is one incident, not one per release. |
| `delivery_id` per group per UTC day | A recurrence stays visible without writing a row on every poll. |
| Message matched in memory, never emitted | Provider messages carry customer data and secrets. |
| Fixed `https://sentry.io` origin | The bearer token can never be sent to another host; pagination rebuilds URLs from a validated cursor. |
| Fail closed on unrouted projects / broken pagination | A partial sweep is never reported as complete coverage. |

## Use

```bash
SENTRY_INCIDENT_SWEEP_TOKEN=… python3 kernel/incident/bin/sentry_incident_sweep.py \
  --routes routes.json --stats-out stats.json > candidates.jsonl
```

`routes.json` follows `manifest/routes.schema.json`; see
`manifest/examples/example.json`. Bind the token from the entity's own secret
store. Never pass it as an argument.

## Parity

`classification-cases.jsonl` is the shared parity corpus. Any consumer that
re-implements classification in another language must mirror
`signatures.json` and pass every corpus case. When a signature changes, update
every mirror and extend the corpus in the same change.

Tests: `python3 -m pytest kernel/incident/tests`.
