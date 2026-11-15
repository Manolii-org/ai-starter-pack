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
   60) and within an enrichment time budget, oldest `lastSeen` first. A group
   over either budget is still emitted, as `unknown`: a storm degrades
   classification but never drops a signal, and `unknown` never reaches an
   implementation lane.
3. Classifies deterministically with `signatures.json`, without a model call,
   using the candidate-schema enum `code_defect | migration | configuration |
   provider | quota | security | unknown`. `code_defect` requires an in-app
   stack frame **and** a commit-SHA release.
4. Applies the admission rule below, then prints one candidate per admitted
   group (JSON Lines) in the sanitized production-incident candidate shape
   documented in `candidate-shape.md`.

## Admission

A group is **transient** when its classification is `provider` or `quota`,
its exception type is one of `Timeout`, `BadGatewayError`,
`ServiceUnavailableError`, `APIError`, `ProxyException` or `RateLimitError`,
or it is a Sentry cron-monitor issue. A cron-monitor issue has
`issueType == "monitor_check_in_failure"`; a title starting with
`Cron failure:` is the fallback.

A transient group becomes a candidate only when it is **sustained**: at
least 100 events, with at least 7 days between its first and last event.
The figures come from the issue-list row's `lifetime` object, so no extra
request is made and the enrichment budget and deadline are unaffected. The
row's top-level `count` and `firstSeen` cover only the query window, so they
are never used. `count` arrives as a string.

A transient group that is not sustained is not emitted. It is counted in the
stats as `filtered_transient`, so coverage stays visible. If its lifetime
data is missing or malformed (no `lifetime`, a non-numeric `count`, or an
unparseable timestamp), the group is admitted rather than dropped, and
counted as `admission_unreadable`. For a producer of incident candidates,
admitting on unreadable data is the safer failure: a spurious candidate costs
one triage, but a dropped one hides an incident. Every other group is
admitted as before.

Why: one-off upstream failures and alerts from flapping cron monitors were
most of the non-incidents in the first labelled shadow sample. Sustained
rate-limit, guardrail or monitor failures are real incidents and stay in.

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
