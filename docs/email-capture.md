# Virtual inbox contract

The v1 interface is backend-neutral. Allocation is deterministic for entity/repository/environment/run scope and produces a high-entropy address. `await` applies a monotonic `(received_at, opaque_id)` cursor, time window, exact count, and typed timeout/ambiguity/replay failures. `assert` performs local deterministic link, numeric-code, and header extraction and emits counts only. `release` is idempotent and allocation-scoped; adapters never globally delete a shared mailbox. TTL is a receiver/janitor backstop.

Profiles accept only major version 1, modes `off|hermetic`, and backends `memory|mailpit|maildev|inbucket`. Unknown values, mixed legacy/canonical variables, and capture in staging/production are `CONFIG_INVALID`. Sender routing and provider evidence stay consumer-owned.

Consumer journey coverage (which product paths actually send mail) is documented in `docs/email-capture-journeys.md`. The protocol does not imply every Auth template is tested.

