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
