# Email-capture journeys

The CLI is journey-agnostic: `allocate` / `await` / `assert` / `release` (`capabilities` and `doctor` are probes). Link and code extraction run inside `assert` (and in consumer Python helpers via `email_capture.core.extract`); there is no `extract` subcommand. Completeness is a **consumer** property: every application path that actually sends mail must be proven against the contract. Do not invent mailbox tests for `generateLink` / in-app URL flows (`NO_EVIDENCED_SENDER`).

## Lanes

| Lane | What it proves | Required for Buro go-live |
|---|---|---|
| Hermetic Mailpit/MailDev/Inbucket | Job-local SMTP + capture CLI (profiles: `off` or `hermetic`) | Yes, for every evidenced sender |
| Hosted capture | Pack SPI `mode=hosted` (v0.2.0): HTTPS `/health` + `/messages`, env-only token, fail-closed production. Consumer canary only when a dedicated capture inbox and `EMAIL_CAPTURE_HOSTED_*` secrets exist. Not a substitute for hermetic evidenced-sender jobs. | No |
| Real-delivery canary | DNS/provider receipt to an entity mailbox | Separate from capture; still denied from KL |

## Buro `bcp-core` (evidenced senders)

Both product paths use Auth `resetPasswordForEmail` and `supabase/templates/recovery.html` (`type=recovery`).

| Journey | Entry point | Capture job |
|---|---|---|
| Forgot password | `/signin/forgot` | `e2e/buro-email-recovery.mjs` |
| Pending activation | `/signin` → **Email me a sign-in link** (`password_set_at` is null) | `e2e/buro-email-activation.mjs` |

Not mailbox-tested until a sender exists: signup confirmation (`enable_confirmations` off locally; production invite-only), GoTrue invite, and email-change. URL-only `generateLink` and in-app partner/space invite URLs stay `NO_EVIDENCED_SENDER`.

## Knowledge Layer

Skip ingest only when the authenticated mailbox is an owned capture address. To/Cc/Bcc never authorize skip. That exclusion stays on KL until `needs-human` is cleared.
