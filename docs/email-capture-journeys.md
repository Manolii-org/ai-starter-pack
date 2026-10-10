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

## CPDcheck suite (evidenced senders)

Four hermetic lanes, all consuming the pack by tag checkout (`@v0.2.3`) at
CI time — CI tooling only, no runtime/secrets/data coupling across universes.
Consumer ADR in each repo: `docs/adr/ADR-0001-hermetic-email-capture.md`.

| Journey | Entry point | Capture artifact |
|---|---|---|
| Certificate request (Go marketing API) | `POST /assessments/:ref/certificate` → `SMTPEmailSender` | `scripts/email_capture/hermetic_journey.py` |
| Contact form (Go marketing API) | `POST /contact` → `SendWithReplyTo` | same job, same script |
| Provider reset + OTP password change (FastAPI) | `POST /api/auth/send-reset` → reset-password → `provider/login` → `change-pw-request-otp` → `change-pw-confirm-otp` | `scripts/email_capture/hermetic_journey.py` |
| Provider browser forgot-password | `/forgot-password` → link → `/forgotpwreset` → `/login` → `/dashboard` | `e2e/email-capture.spec.ts` (provider frontend) |
| Adviser browser forgot-password | `/forgot-password` → link → `/forgot-password-reset` → `/login` → `/dashboard` | `e2e/email-capture.spec.ts` (professional frontend) |

Patterns CPDcheck adds to the shared playbook:

- API lanes seed a capture user cloned from a seeded template
  (`scripts/email_capture/seed_capture_user.py --allocation <ref>`), so no
  production identity or PII is touched.
- Portal-aware reset links: the API composes `{portal_base}/{route}?token=…`
  per `portal` — the lane exports each frontend base URL env var so links are
  absolute and the browser can follow them.
- The CSRF-gated OTP endpoints take `X-CSRF-TOKEN` from the
  `csrf_access_token` cookie; the journey's API client mirrors that.
- Negative (unknown-email) assertions run on a distinct `run_id`
  (`${RUN_ID}-neg`) — allocation scope is deterministic, so an identical
  request would share the main inbox.
- `await --count N` counts only messages newer than the allocation cursor —
  after an await advances the cursor, each further expected message is
  awaited with `--count 1` (and `--count 0` bounds the negative window).
- Frontend specs live in a dedicated Playwright project (`email-capture`)
  with an empty storage state; the default `chromium` project `testIgnore`s
  them, keeping the post-deploy UAT suite untouched.

## Knowledge Layer

Skip ingest only when the authenticated mailbox is an owned capture address. To/Cc/Bcc never authorize skip. That exclusion stays on KL until `needs-human` is cleared.
