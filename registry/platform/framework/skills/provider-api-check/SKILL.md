---
name: provider-api-check
version: 1.0.0
description: "Verify-before-you-code for third-party provider APIs (Sentry, Fly, Vercel, Doppler, Supabase, Neon, OpenAI/Anthropic). Use BEFORE writing or changing code that calls a provider SDK/API surface — fetch the live doc, confirm the call shape, record the verification."
type: skill
data_sensitivity: internal
safety_tier: green
requires_mcp: []
required_entities: []
allowed-tools:
  - Read
  - Grep
  - WebFetch
  - Bash
tags:
  - quality-gate
  - telemetry
intent_phrases:
  - "verify the provider API"
  - "check the Sentry API"
  - "is this API still current"
  - "write against the Fly API"
disallowed-tools:
  - Edit
  - Write
---

# Skill: Provider API Check

Memory is not verification. The 2026-07 heartbeat helper was written against
`Sentry.metrics.increment` — sunset in Sentry v10 (October 2024). Every deployed
heartbeat would have been a silent no-op; only a CI typecheck caught it. This
skill is the author-time gate that prevents that class.

## Quick Reference

1. **Look up the registry row.** `.ai/integration-sources.yaml` → `provider_apis`.
   Find the `id` matching the provider surface you're about to code against.
   No row? Add one in the same branch (doc_url + gotcha + last_verified).
2. **WebFetch `doc_url`.** Confirm, against the live doc, every field name,
   value semantic, and casing you plan to write. Pay special attention to the
   row's `gotcha` line — it exists because memory got exactly that wrong before.
3. **Semantics, not just shape.** For every numeric/string you forward to a
   provider config field, state at the definition site (docstring/comment):
   (a) what the operator means by it, (b) the provider's exact semantics with
   doc link. If they differ, the helper converts — callers never pass
   provider-native values. Exemplar: `scripts/lib/heartbeat.py` checkin_margin.
4. **Record the verification.** Update the row's `last_verified` in the same
   branch. Add `API-Verified: <id>@<YYYY-MM-DD>` to the PR body.
5. **Test the wire.** Provider-integration code requires a mock-provider test
   in the same commit (the `provider-integration` gate lives in the
   orchestrator repo's `scripts/run-pre-commit-specialists.py` — not shipped
   in this pack).

## When this applies

Any diff that adds or changes calls against: `@sentry/*` / `sentry_sdk`,
`langfuse`, `@opentelemetry/*`, `api.machines.dev`, `api.vercel.com` /
`vercel.json` crons, `api.doppler.com`, `api.supabase.com`, Neon API,
OpenAI/Anthropic SDK request shapes.

## Enforcement

- Advisory pre-commit warning (`provider-api-freshness`, enforced by the
  orchestrator repo's `scripts/run-pre-commit-specialists.py`) from 2026-07-16.
- Flips to blocking 2026-07-30 (operator decision, telemetry-hardening plan).
- Do NOT bypass by copying a stale `last_verified` forward — the date asserts
  "a human/agent read the live doc on this date."
