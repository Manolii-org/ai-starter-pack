# Retrospective delivery policy

Portable capture uses the existing local snapshot and JSONL before optional
delivery. It runs no model inference. Local files need retained owner storage to
survive disposal of an ephemeral checkout.

Set `.ai/config/retrospective.json` to `{"local_only": true}` to disable all
collector delivery paths even when ambient MCP credentials exist. The rendered
template sets this policy when `kl_integration=false`. Existing installations
without that file retain their previous optional delivery behavior. Invalid JSON
or a nonboolean `local_only` fails closed. `SESSION_RETRO_LOCAL_ONLY=1` also
disables delivery and cannot be undone by an ambient key or a false config value.

The policy is retained in captures; changing the current config does not export
captures recorded under local-only. Old records lacking a policy retain legacy
behavior. This is a local capture export control; server tenancy still requires
independent authorization and enforcement.

`--local-only` on Stop remains its historical synchronous-leg option used by the
wrapper before background delivery. For a permanently local canary, use the
configuration or environment policy above. No credential creation is needed.

At session entry, record the checked-out commit in `SESSION_RETRO_START_SHA`.
Diff statistics compare that exact commit with HEAD, and cover committed changes
only. Without a valid commit baseline no diff attribution is reported. A hash
does not establish that the caller actually recorded it at session start.

Verification: run `python3 -m pytest scripts/tests/test_session_retrospective.py`
and `bash scripts/tests/session-stop-retrospective.test.sh`. Start with dummy
ambient keys and `local_only=true`; verify a retained local snapshot and zero
network calls. Local persistence, HTTP acknowledgement, verified remote record
identity and final acceptance are separate states. This change does not upgrade
legacy `kl_written` into proof of exact record-ID/hash persistence.

Rollback: restore the prior collector/package pin and config from owner-controlled
version history. Retain snapshots; do not automatically flush local-only captures.
Opting into delivery is a separate owner decision and does not authorize a foreign
entity, new credential, migration or new provider grant.


## Receipt v2 read adapter (not wired to delivery)

Reuse decision: `capture_receipt_v2` extends the canonical existing collector as
an unused pure adapter; snapshots/JSONL, recovery and sink ownership stay with
existing components. It adds no collector, queue, logger or service. Legacy
`kl_written` remains readable but maps to `acknowledged_unverified`, not verified
persistence. Missing context, identities and artifact hashes stay null; old IDs
are preserved. Trigger provenance is explicit and defaults to unknown; tags do
not establish automatic capture. Unproved operators remain unknown.

Call the adapter with a legacy record and explicit nonsecret owner binding.
The closed binding accepts organization/repository/provider/run/operator evidence,
trigger/package/hook revisions, coverage/missing sources, sanitized artifact
ID/hash, store reference, expected record ID and outcome. Narrative, URLs/userinfo, recognizable credential patterns
and unknown fields are rejected. Opaque IDs still require a trusted owner binding;
this helper is not a general secret detector. The artifact must already be sanitized by the
canonical capture path; a caller-supplied hash is not sanitizer proof. No metric
values are manufactured. This adapter does not infer session-start provenance
from a supplied baseline.

States are local_only, pending, failed, no_actionable_signal, unknown,
acknowledged_unverified and verified. Empty acknowledgement, legacy boolean or
hash alone cannot establish verification. A future trusted sink adapter must
provide an exact readback matching organization/entity/store/capture/expected
record ID/artifact hash plus evidence pointers. Even then this pure function
cannot establish the truth or authorization of caller-supplied evidence.
Captured local-only policy wins over supplied readback. Mismatched readback
produces unknown, requiring reconciliation before any retry. No network delivery,
mutation, durable acknowledgement or automatic hook is activated by this helper.
Retain the result through the existing approved owner-local capture destination
before acknowledging a hook or disposing of the checkout.

Rollback removes the unused adapter or restores the prior package pin; legacy
capture formats and delivery behavior are unchanged. The adapter's deterministic
unit tests include wrong scope/store/hash/record IDs, empty acknowledgements,
explicit local-only, legacy records and unknown operator provenance. Real sink,
tenancy and session-teardown canaries remain NOT RUN.
