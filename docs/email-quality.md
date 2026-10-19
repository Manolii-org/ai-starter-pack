# Email quality: deliverability beside capture

`bin/email-quality` is a Python 3 standard-library CLI for Unspam spam and inbox
placement tests. It preserves `bin/email-capture`, its profiles, environment
variables, adapter semantics and existing pins. Capture proves application
behavior/content; deliverability measures the real sending path and provider
reputation at a point in time. Neither substitutes for the other.

## Configure and run

Copy `config/email-quality.example.json` to the consumer's
`config/email-quality.json`, change the org/repository/environment and thresholds,
and validate against `schemas/email-quality/profile.schema.json`. Set
`UNSPAM_API_TOKEN` from the consumer's secret store. Optional
`UNSPAM_ACCOUNT_EMAIL` verifies the expected provider account without printing it.
`UNSPAM_API_BASE_URL`, if set, must equal `https://api.unspam.email/ext/v2`.

```bash
# Tooling comes from a reviewed immutable pack checkout, not registry discovery.
PACK_ROOT=/path/to/pinned-pack
"$PACK_ROOT/bin/email-quality" --profile config/email-quality.json status
state_dir=$(mktemp -d)
"$PACK_ROOT/bin/email-quality" --profile config/email-quality.json \
  --state "$state_dir" --sender scripts/send-quality-fixture run
# Remove private state when the run and any bounded investigation are complete.
```

The sender executable receives exactly one argument: a mode-0600 JSON file with
`schema_version`, `kind`, `test_id`, `recipients`, `mailbox_ids`, `body_marker`. It must send an
inert synthetic message through the consumer's actual email transport to each
returned recipient, with `body_marker` in every placement message's body. Do not
send a real password reset, customer email or live authentication capability.
Successful sender exit is followed by provider polling, so exit zero alone is
never delivery evidence. The CLI strips ambient credentials from the sender;
only basic proxy/CA/runtime settings and `EMAIL_QUALITY_SENDER_*` survive. Supply
only narrowly scoped sender credentials under that prefix. Sender output is
suppressed. Never put credentials in profile JSON or command arguments.

For a sender controlled by another trusted tool, use `start --kind spam` (or
`inbox`) with the same `--profile`/`--state`. The private state directory contains
`delivery-spam.json` or `delivery-inbox.json`. Send through the trusted tool, then
call `wait --kind spam` against that state. Recipient/id files must remain private.
Do not paste their contents into agent prompts or ordinary logs.

## Outcomes and reliability

- `ready` means account API access and remaining allowances were checked.
- `started` means a test was created. Neither outcome asserts mail delivery.
- `passed` / `assertion_failed` are completed tests evaluated against the profile.
- `blocked` carries a typed code (configuration, authorization, quota, provider,
  timeout, sender, ambiguous mutation or private-state problem), never raw errors.
- Calls and polling are bounded, responses are capped, redirects are refused,
  malformed/mismatched results fail closed, and POSTs are never automatically
  repeated. Completed state resumes without another test/send. Save the same
  private checkpoint for in-process/local resume; losing it loses that guarantee.
- `timeout_seconds` is one overall CLI budget shared by account checks, creation,
  sender execution and polling across both checks. Completed results remain in
  a blocked receipt if a later stage runs out of time. The maximum budget is
  900 seconds, leaving cleanup headroom below the workflow's outer 1200-second
  ceiling. External termination and hard kills cannot guarantee a receipt.
- A persisted `creating` or `sending` intent denotes an uncertain mutation after a
  crash or failure. Investigate it; do not erase the state and blindly rerun.
  `SEND_OUTCOME_UNKNOWN` and `CREATE_OUTCOME_UNKNOWN` deliberately stop automation.
- State is bound to an exact profile digest and locked per directory. Treat the
  directory as a private credential-bearing run capability, not a CI artifact.
  Remove it after use. Provider results remain subject to vendor retention;
  the CLI does not claim remote deletion or purge another test's messages.

## Temporary shared accounts

`account_mode: shared` requires `shared_account_acknowledged: true`. Separate
revocable integration tokens support independent rotation but **do not isolate
history, quota or account access**. This is an explicit temporary trust decision.
Org-owned secret stores/runners and inert fixtures remain mandatory. A dedicated
account later needs only a different credential and `account_mode: dedicated`.

`minimum_remaining` reserves headroom before each new test. Missing/invalid
allowances block. The provider enforces account limits. Parallel repositories
can race this read; the reserve is not an atomic account-wide budget or fairness
ledger. Per-repo concurrency is not cross-repo serialization. Start with manual
runs, small fixed schedules and explicit per-org budgets whose sum fits the
shared allowance. No automatic top-ups, preview/heatmap purchases or new account
subscriptions are performed. Rotation updates the secret, never source code.

## CI, discovery and rollback

Use `.github/workflows/email-quality-reusable.yml` pinned by full SHA and pass
that same SHA as `pack_ref`. Inputs: `profile_path`, `sender_path`; secrets:
`UNSPAM_API_TOKEN`, optional `SENDER_TOKEN` exposed to the hook as
`EMAIL_QUALITY_SENDER_TOKEN`. Call from a trusted default-branch manual, schedule
or push workflow. Untrusted/fork PRs receive no token; live checks do not join
Integration Admission or become a required merge check. The reusable runs on
Linux with a 20-minute command ceiling, uploads only a seven-day metadata receipt,
and deletes private checkpoints on exit. A new workflow attempt starts a new run;
there is no automatic cross-run exactly-once guarantee or retry.

The platform framework command is generated from `.claude/commands/email-quality.md`.
Copier consumers get the CLI; registry/plugin discovery requires an explicit
immutable tooling checkout. Native Unspam MCP uses browser OAuth per client;
this CLI does not copy a REST token into MCP configuration.

Disable the caller or restore its previous tooling SHA to roll back. Revoke the
integration token independently. Existing product transports, capture jobs,
production SMTP and branch protection stay as configured. Before promotion,
require a fresh result for the real sender/environment and distinguish quota or
provider failure from a product assertion. Do not reuse yesterday's result just
because the source tree is unchanged.

The live placement API uses overall `status=success` and per-mailbox
`status=success` with `tab=Inbox|Spam` (observed 2026-10-17). Delivery success
is not inbox placement success: the CLI evaluates the tab and requires exactly
the expected unique mailbox identities. IDs may contain colons and are encoded
as individual URL path segments.

Provider reference: <https://api.unspam.email/ext-api/openapi.json>, verified
2026-10-17. See `docs/email-quality-implementation-plan.md` for the impact map.

Placement compatibility (verified against public provider documentation on
2026-10-19): per-mailbox `success` or `delivered` selects `tab`; legacy rows
with the category in `status` remain supported. Mailbox status and category
matching are case-insensitive. Supported categories are `Inbox`, `Spam`,
`Missing`, `Promotions`, `Updates` and `Newsletter`. Unspam's
[delivery-status guide](https://help.unspam.email/article/444-understanding-the-delivery-status)
describes Newsletter as promotional placement; its
[placement report](https://unspam.email/inbox-placement) distinguishes Updates
from the primary inbox. The [API guide](https://unspam.email/inbox-placement-api)
documents `delivered` with a separate `tab`. These sources establish category
meaning; they are not a claim of a new live seed test.

The acceptance policy is unchanged: `inbox_ratio` counts only case-insensitive
`Inbox` over **all expected mailboxes**, including tabs, spam and explicit
`Missing`. Tabs are recognized outcomes, not Inbox successes. Unknown labels,
absent/malformed placements and mismatched test/mailbox identities still block
with `PROVIDER_INVALID`; they are never coerced to Missing or Inbox.
New completed inbox receipts add `placement_counts`, keyed by exact validated
provider labels (including original case), without addresses or sender data.
Different capitalizations remain separate keys. Existing cached receipts may
omit this optional field and resume unchanged without another test or send.
Consumers using a strict receipt validator must adopt the matching schema with
the tooling pin. Offline conformance tests cover this correction; adopting a
new pin and validating the consumer remain separate release steps.

Sender hooks are trusted consumer code in the same OS trust domain as the CLI.
Environment filtering reduces accidental exposure; it is not a sandbox against
malicious same-user code reading process memory or local files. Never run an
untrusted candidate's hook with a provider token.
