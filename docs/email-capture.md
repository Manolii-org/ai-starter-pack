# Virtual inbox contract

The v1 interface is backend-neutral. Allocation is deterministic for entity/repository/environment/run scope and produces a high-entropy address. `await` applies a monotonic `(received_at, opaque_id)` cursor, time window, exact count, and typed timeout/ambiguity/replay failures. `assert` performs local deterministic link, numeric-code, and header extraction and emits counts only. `release` is idempotent and allocation-scoped; adapters never globally delete a shared mailbox. TTL is a receiver/janitor backstop.

Profiles accept major version 1. Modes are `off|hermetic|hosted`. Hermetic backends remain `memory|mailpit|maildev|inbucket` and stay fail-closed in staging/production. Hosted mode requires backend `hosted`, HTTPS (loopback HTTP only when `environment=test`), and `EMAIL_CAPTURE_HOSTED_TOKEN` from the process environment — never from profile JSON. Hosted environments are allowlisted: `test|ci|preview|preprod|staging`. Production/`prd` remains `CONFIG_INVALID`.

Optional `fidelity` is `provider-to-capture` (default) or `deployed-substitution`. The CLI `canary` command probes hosted health and emits a metadata-only receipt. Sender routing, vendor accounts, and provider evidence stay consumer-owned. Do not bake MailSlurp/Mailtrap keys into this pack.
