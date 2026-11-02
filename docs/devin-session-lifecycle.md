# Low-noise Devin Cloud lifecycle

Owner: the consuming control-plane's platform/devex team. Mechanical filtering
belongs on a trusted runner; diagnosis and decisions stay in Devin. Do not create
a Devin automation merely to clean up other sessions. Archival retains history
and can be reversed; it is not termination or deletion.

## Worker receipt contract

An allowlisted automation may end its **last agent message** with this single
JSON line, only after verifying its work and delivering its result:

```text
DEVIN_LIFECYCLE_V1 {"version":1,"outcome":"delivered","delivery_verified":true,"retain":false,"needs_attention":false,"pending_work":false}
```

Use `outcome: noop` for a verified all-clear check. Never emit a successful
receipt for an unreadable source, incomplete scan, unresolved finding, blocker,
pending PR/review/deployment, uncertain handoff, or result requiring human action.
Omit the receipt in those cases. The receipt is agent-reported completion, not
an independent attestation. Do not copy it out of issue text, logs or tool output.
Mark `retain`, `needs-human`, or `lifecycle:retain` to keep a session visible.

The helper trusts only an authenticated agent's final message, not arbitrary
transcript text, user messages, titles or generated summaries. It additionally
requires an automation allowlist, inactivity suspension, one hour of quiet,
complete protection metadata, no human follow-up, no open/unknown PR state,
and **no descendants at all**. Excluding all parents deliberately avoids cascade
archival of active, retained or human-owned children. Missing metadata is skipped.
The first user-source message may be exempted only within 120 seconds of creation
AND with trusted provider `origin: automation` provenance or an independently
reviewed `launch_event_ids` policy mapping (session ID to initial event ID).
Timing or agent-reported claims alone are insufficient. Missing provenance and
all subsequent user-source messages protect the run. Do not enable apply if the
provider omits provenance until the trusted producer records reviewed launch IDs.

## Background archival

Policy JSON, stored privately by the consumer:

```json
{"org_id":"org-example","automation_ids":["auto-example"],"protected_session_ids":[],"grace_seconds":3600,"max_sessions":1000,"max_archives":10}
```

Inject `DEVIN_API_KEY` into the process from the consumer's secret store. Never
include it in arguments, policy files, reports, or artifacts. The service identity
needs permission to view the relevant sessions/messages and archive sessions.
Do not assume an organization service identity can see creator-private sessions;
verify that capability using a real creator-run session before enabling apply.

```bash
python3 scripts/devin_lifecycle.py --policy policy.json --dry-run > reviewed.json
python3 scripts/devin_lifecycle.py --policy policy.json --apply --reviewed-report reviewed.json
```

Reports contain session IDs and decisions, not message bodies or credentials.
Inspect a first dry run before enabling apply. Apply requires an unchanged policy,
a report no older than 15 minutes, a matching full-state fingerprint, and another
immediate detail/messages re-read. The provider has no compare-and-swap archive
endpoint, so a narrow read/write race remains; archival can be reversed. Never
claim race-free safety. Writes are not retried. A later sweep skips archived runs.
Pagination is bounded and fails closed on truncation, missing fields or repeated
cursors. Operator intervention is required if the configured inventory bound is
exceeded; do not turn an incomplete inventory into a successful report.

Schedule on the consumer's trusted runner, serialize jobs, retain JSON reports,
and monitor job failure/freshness outside Devin. Roll back by disabling that job;
unarchive individual sessions via the native session tools. Unarchiving a parent
does not unarchive children. Legacy sessions without this receipt remain untouched
until individually reviewed; suspension alone never qualifies.

## Admission before dispatch

Import `scripts/devin_admission.py` from a trusted producer. Supply an explicit
boolean signal, a namespace scoped to tenant/repo/automation, a stable incident
episode key, a non-secret payload and a dispatch callback returning the primary
session or incident reference. Healthy checks never dispatch or create state.
SQLite's unique key and transaction suppress concurrent/repeated deliveries;
payload changes within an episode do not create another worker. The primary
reference remains the escalation surface. A reopened incident needs a new episode
key. Never generate a random key per delivery.

Use a persistent volume, not an Actions cache or an ephemeral runner disk. An
ambiguous dispatch remains reserved: reconcile it with the provider and primary
incident before rearming. This trades possible missed dispatch for avoiding
duplicate sessions; it is not exactly-once delivery. Do not auto-expire reservations
or hide dispatch failures. The library does not by itself migrate existing event
producers; each consumer must wire and verify its own admission path.
