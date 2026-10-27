# Virtual inbox contract

The v1 interface is backend-neutral. Allocation is deterministic for entity/repository/environment/run scope and produces a high-entropy address. `await` applies a monotonic `(received_at, opaque_id)` cursor, time window, exact count, and typed timeout/ambiguity/replay failures. `assert` performs local deterministic link, numeric-code, and header extraction and emits counts only. `release` is idempotent and allocation-scoped; adapters never globally delete a shared mailbox. TTL is a receiver/janitor backstop.

Profiles accept major version 1. Modes are `off|hermetic|hosted`. Hermetic backends remain `memory|mailpit|maildev|inbucket` and stay fail-closed in staging/production. Hosted mode requires backend `hosted`, HTTPS (loopback HTTP only when `environment=test`), and `EMAIL_CAPTURE_HOSTED_TOKEN` from the process environment — never from profile JSON. Hosted environments are allowlisted: `test|ci|preview|preprod|staging`. Production/`prd` remains `CONFIG_INVALID`.

Optional `fidelity` is `provider-to-capture` (default) or `deployed-substitution`. The CLI `canary` command probes hosted health and emits a metadata-only receipt. Sender routing, vendor accounts, and provider evidence stay consumer-owned. Do not bake MailSlurp/Mailtrap keys into this pack.

Consumer journey coverage (which product paths actually send mail) is documented in `docs/email-capture-journeys.md`. The protocol does not imply every Auth template is tested.

Live spam/inbox-placement checks use the separate `bin/email-quality` contract;
see `docs/email-quality.md`. Capture modes, receivers and existing pins retain
their current semantics.

## Versions and allocation ownership

Email-capture releases are tagged `email-capture-vX.Y.Z` (README § Tag
namespaces). The legacy bare `v0.1.x`–`v0.2.x` tags remain for lanes that
already check them out. From `v0.2.4` (`email-capture-v0.2.6` is the first
prefixed tag), `await` and `release` resolve the allocation against
the local registry that `allocate` wrote — by default
`$RUNNER_TEMP/email-capture/allocations.json`, overridable with
`EMAIL_CAPTURE_PRIVATE_DIR` or `EMAIL_CAPTURE_ALLOCATION_REGISTRY` — and refuse
anything else with `AUTHORIZATION_DENIED`. A lane that runs `allocate` in one job
and `await`/`release` in another must move those steps into one job
(or carry the registry file between jobs as a private artifact). `assert` does
not consult the allocation registry, so it can run in another job, but like every
subcommand it first loads the capture profile and refuses `mode=off`
(`CAPABILITY_UNSUPPORTED`).
