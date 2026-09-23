# Shared Telemetry Helpers (heartbeat)

Loud-failure heartbeat + canary helpers for the drift-sentinel P3 pattern
(`manolii-org/master` `reports/drift-sentinel-2026-07-12.md` § 3). Sentry Cron
Monitors transport; fail-soft contract (a broken Sentry must never take down
the surface it observes); operator-intent `checkin_margin` conversion built in.

## Canonical source + distribution

- **Canonical:** this directory, distributed with the pack at `@v1.5.0+`.
  `manolii-org/master` `scripts/lib/heartbeat.{ts,py}` is kept byte-identical
  (drift checked daily by master's drift-sentinel `telemetry_mirrors`).
- **Consumer dest conventions** (registered in master
  `config/drift-sentinel.json` → `telemetry_mirrors`):
  - Python repos → `telemetry/heartbeat.py`
  - TypeScript repos → `lib/telemetry/heartbeat.ts`
- Do NOT hand-edit a consumer copy. Fix here (or in master, then sync), bump
  the pack, and let the mirror-drift check verify convergence. The 2026-07-16
  reverse-drift — master's canonical went a generation stale behind its own
  mirrors — is the failure mode this rule exists for.

## Rules for changing these files

1. **Verify the provider API live first** — `/provider-api-check` skill;
   Sentry gotchas are pinned in master `.ai/integration-sources.yaml`
   (`provider_apis.sentry-crons`).
2. **The wire tests ship with the helper** (`tests/`). Any behaviour change
   updates the tests in the same commit — the pre-commit provider-integration
   gate blocks otherwise. Python: `python3 telemetry/tests/test_heartbeat.py`
   (stdlib only). TypeScript: vitest.
3. **Two-signal principle**: surfaces with stateful pipelines pass a `canary`;
   canary intent per surface is declared in master
   `config/drift-sentinel.json` → `telemetry_surfaces` (audited daily).
4. Operator intent vs provider semantics: `checkin_margin_minutes` /
   `checkinMarginMinutes` is ALWAYS the operator's "N minutes silent = page"
   target; the helper converts to Sentry's grace-after-expected-check-in.
   Callers never pass provider-native values.
