# ADR-0010: LOCK_HERMETIC_ONLY virtual inbox v1

**Status:** Accepted (2026-08-21)  
**Decision:** The v1 contract is hermetic-only. Mailpit is the default receiver; MailDev and Supabase Inbucket are compatibility adapters, not separate contracts.

## Evidence and boundaries

The Phase 0 harness compared Mailpit, MailDev, and Inbucket for health, allocation isolation, ordering, MIME/HTML/link/header/attachment normalisation, and scoped cleanup. Mailpit is maintained, MIT-licensed, multi-architecture, exposes SMTP plus a REST API, and avoids MailDev's global-delete legacy. Inbucket remains a Buro exception through **2026-11-21** because Supabase CLI owns local Auth routing and replacing its managed receiver increases platform drift. MailDev remains an Impaktful compatibility exception through **v1.1 / 2026-11-21** while its two legacy seams migrate.

Message content, recipient, subject, links, codes, headers, attachment bytes, and credentials are transient restricted data. Only operation, result, duration, non-secret scope, counts, typed error, and cleanup state may enter receipts. The portable Python 3 standard-library executable parses locally and has no telemetry or AI path.

Owner: Manolii Developer Platform. CI placement: job-local service/process. Capacity: one receiver per job; no central quota or bill. Rollback: set `EMAIL_CAPTURE_MODE=off`, pin the prior tag, and remove the receiver service without changing the business sender.

Hosted capture, vendor procurement, gateway, DNS, real-delivery canaries, and business-mailbox ingestion are separately gated and `NOT_APPLICABLE` to this lock. Reconsider hosted capture only after a deployed synthetic sender and the Phase 0 deletion, residency, key-scope, concurrency, and ingestion-exclusion gates pass.

## Exceptions

| Receiver | Owner | Evidence | Capability difference | Review | Decision |
|---|---|---|---|---|---|
| Inbucket | Buro | Supabase CLI routes local Auth to managed Inbucket | Supabase-owned local Auth fidelity | 2026-11-21 | remove if Mailpit routing is proven simpler; otherwise permanent platform exception |
| MailDev | Impaktful | existing Compose and test abstractions | legacy `/email` API | 2026-11-21 or v1.1 | remove after compatibility window |
