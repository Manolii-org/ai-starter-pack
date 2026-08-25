# email-capture v0.2.0

Hosted capture lane: `mode=hosted` + `backend=hosted`, HTTPS SPI (loopback HTTP only in environment=test), env-only bearer token, `canary` CLI, and optional fidelity. Hermetic Mailpit/MailDev/Inbucket behavior is unchanged and remains fail-closed in staging/production.

Null `to` on summaries is `CAPTURE_INFRA_UNAVAILABLE`. Shared To/Cc/Bcc messages are not deleted. The profile schema rejects the `production` environment and hosted endpoints that are neither HTTPS nor test-only loopback HTTP.
