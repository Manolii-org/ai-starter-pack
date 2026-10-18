# Email-quality implementation plan and compatibility map

Approved scope: add portable deliverability testing alongside email capture, with a temporarily shared provider account and independently owned consumer configuration. No application transport, production SMTP or required merge check changes.

## Assumption review

- The existing capture CLI, strict schemas, allocation lifecycle and hermetic/hosted modes are already shipped. Deliverability has different semantics and is a sibling contract.
- Current Unspam OpenAPI: `https://api.unspam.email/ext-api/openapi.json`, verified 2026-10-17. REST base is `/ext/v2`; POST spam returns `id,inbox_address`; POST placement returns `id,mailboxes`. Placement requires its id inside the sent body.
- Tokens have account-wide API scope. Separate revocable tokens do not isolate history, other tests or quota. Shared-account use requires explicit profile acknowledgement and inert synthetic content. The consumer's secret store and runner remain independently owned.
- Account quotas are per test category. Missing/invalid quota is a block; no automatic top-up or quota increase. Cross-repository GitHub concurrency is not an account mutex. Provider quota enforcement is authoritative; a configured reserve is a best-effort headroom check, not a globally atomic budget.
- No documented POST idempotency key exists. Create and send intents must be checkpointed before mutation. Ambiguous outcomes must not be retried blindly.
- Registry discovery does not install executable code. Consumers need an immutable pack checkout. Native MCP uses separate client-local OAuth; CI uses REST.

## Implementation sequence and acceptance

1. Add `email_quality/` and `bin/email-quality`: strict secret-free profiles, documented-origin HTTPS adapter, no redirects, bounded response/request sizes, typed errors and quota preflight. Acceptance: malformed input, origin substitution, nonfinite thresholds and missing credentials fail without disclosure.
2. Add private checkpoints with nonblocking run locks and profile binding. Record create/send intent before side effects, resume completed work and polling without new spend. Ambiguous creation/sending requires intervention. Acceptance: concurrent calls, scope mismatch, private-path violations, duplicate replay and network ambiguity tests.
3. Implement status/start/wait/run and deterministic sender-hook handoff. Sender gets only a private JSON path, a narrow environment and its explicitly prefixed credentials. Suppress hook output and emit allowlisted receipt metadata only. Acceptance: real subprocess -> HTTP double -> sender -> polling integration test; provider token absent from hook environment; body-marker contract and result identity enforced.
4. Add versioned profile/receipt schemas, example profile, opt-in reusable workflow and path-scoped offline conformance. Trust default-branch/manual/pre-production callers only; no fork secrets or PR-controlled shell. Publish only sanitized receipts, never private checkpoints. Acceptance: workflow/schema/CLI contract checks and negative fork/secret-routing tests.
5. Add canonical agent guidance and docs; regenerate plugin/registry outputs from source. Explain capture versus deliverability, ready/started versus passed, quota reserve races, shared visibility, expiry/rotation, sender hook, rollout and rollback. Acceptance: source/generated parity plus installed-command replay with pinned CLI checkout.
6. Bind credentials through each owner's existing secret route. Use separate revocable integration tokens under the shared account where practical; no Master bootstrap token in consumers. Private control-plane documentation records org destinations and temporary account-sharing decision. Acceptance: names-only readback and authenticated status, no secrets in public sources or outputs.
7. Run the changed offline suites plus existing capture regression suite, lint, schemas, workflow checks and secret scan. Conduct independent source review and repair all actionable findings. Perform an inert synthetic live test through the real sender path, report assertion failures separately from infrastructure/allowance failures and record actual remaining quota.
8. Ship reviewed PRs, release at an immutable ref, wire the owning control-plane entrypoint and verify the released command/workflow. Keep adoption opt-in for repositories with actual outbound mail; no meaningless test jobs in libraries or paused products.

## Wiring and terminology inventory

| Surface | Change / continuity contract |
|---|---|
| `email_capture/`, `bin/email-capture`, `EMAIL_CAPTURE_*`, capture schemas and consumer pins | Preserve behavior and versions; cross-link the new lane only. |
| `email_quality/`, `bin/email-quality`, `schemas/email-quality/`, example profile | New independent v1 contract; provider-specific behavior stays in adapter. |
| `UNSPAM_API_TOKEN`, `UNSPAM_API_BASE_URL`, `UNSPAM_ACCOUNT_EMAIL` | Preserve existing names; no API-key aliases. Base URL may only equal documented provider origin. |
| `EMAIL_QUALITY_SENDER_*` | Explicit sender-only secret namespace; provider/admin tokens are stripped before subprocess execution. |
| `.claude/commands` -> plugin -> platform registry | Canonical source then generators; no hand edits to generated trees. Public output contains no private repo names, secret destinations or account identity. |
| Pre-production tiers / Integration Admission | Live external results are time-dependent and secret-bearing; keep out of source-only merge evidence reuse. No new required PR check. |
| Sender integration | Existing product provider remains consumer-owned. Send only synthetic fixtures, preserve provider semantics and embed the returned placement marker. |
| Shared SaaS account | Account-wide history/allowance are explicitly shared. Per-org accounts later require changing the token and profile mode, not caller APIs. |
| MCP | Optional interactive OAuth connection; REST token is never treated as an MCP token. |

## Rollback and evidence

Disable opt-in caller, revoke its integration token and restore previous immutable ref. Existing product transports and capture jobs remain intact. Record source/release SHAs, offline tests, review findings/fixes, actual secret destinations (private, names only), live result and any residual adoption restriction. Status/health alone is not delivery evidence. A local checkpoint is not a cross-run exactly-once guarantee; do not upload it to ordinary CI artifacts.
