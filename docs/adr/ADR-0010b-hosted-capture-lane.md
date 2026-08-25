# ADR-0010b: Hosted capture lane (v0.2.0)

**Status:** Accepted (2026-08-25)
**Decision:** Add an opt-in hosted SPI beside LOCK_HERMETIC_ONLY. Do not replace Mailpit CI, do not provision a broker, and do not rewrite production SMTP.

## Contract

The hosted backend speaks a small HTTPS JSON API: `GET /health`, `GET /messages?to=`, `GET /messages/{id}`, `DELETE /messages/{id}`. List rows must carry a well-formed recipient value (not `null`). `DELETE` runs only when the detail envelope is exclusively owned by the allocation recipient. Requests send `Authorization: Bearer` from `EMAIL_CAPTURE_HOSTED_TOKEN` only, refuse redirects, and never log bodies, recipients, or tokens. Receipts stay metadata-only. Default fidelity is `provider-to-capture`; `deployed-substitution` is explicit.

Hermetic jobs remain the required CI path. Hosted capture may run as a manual/canary job against staging, preview, preprod, or test. Production profiles fail closed. Consumers pin this pack tag; they own vendor credentials and sender routing.

## Still gated

Measured ecosystem concurrency, vendor DPA/deletion/residency, and Knowledge Layer ingest exclusion must pass before a consumer makes hosted capture a required promotion gate or purchases a vendor. `canary` is a health probe, not a real-delivery proof.
