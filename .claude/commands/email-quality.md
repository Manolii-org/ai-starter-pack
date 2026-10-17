# Email quality

Use a pinned Starter Pack checkout's `bin/email-quality` and a repository-owned
`config/email-quality.json`. See the checkout's `docs/email-quality.md`.
`status` checks access and allowance only. `start` allocates a paid test, writes a
private `delivery-<kind>.json` under `--state`, and reports `started`; only `wait`
or `run` can report a completed assertion. Use `run --sender <trusted executable>`
for the composed flow. Run `--help` for exact argument order. Keep the private
state for a bounded resume; never upload it or print provider responses.

Shared-account profiles require explicit acknowledgement: API tokens grant
account-wide access and share history/quotas. Use inert synthetic fixtures only,
never business emails or live auth links. The sender consumes the private JSON
path and must embed `body_marker` in every placement email. It gets no provider
credential; give it only explicitly prefixed `EMAIL_QUALITY_SENDER_*` settings.
Do not silently retry uncertain create/send outcomes, buy credits, alter DNS,
change production SMTP, or interpret `ready`/`started` as delivery proof.
Use the existing capture lane for local content/link assertions. Live reputation
results need freshness and belong in opt-in pre-production/scheduled execution.
MCP OAuth is client-local and separate from this REST integration.
