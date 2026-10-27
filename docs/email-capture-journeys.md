# Email-capture journeys

The CLI is journey-agnostic: `allocate` / `await` / `assert` / `release` (`capabilities` and `doctor` are probes). Link and code extraction run inside `assert` (and in consumer Python helpers via `email_capture.core.extract`); there is no `extract` subcommand. Completeness is a **consumer** property: every application path that actually sends mail must be proven against the contract. Do not invent mailbox tests for `generateLink` / in-app URL flows (`NO_EVIDENCED_SENDER`).

Each consumer records its own journeys in its own repository (for example an
ADR such as `docs/adr/ADR-0001-hermetic-email-capture.md` plus the lane
workflows). This document carries only the lane model, a generic example
journey table to adapt, and the shared playbook patterns. Do not add a
consumer's routes, endpoints, mail paths or repository names here — this
repository is public.

## Lanes

| Lane | What it proves | Required before production go-live |
|---|---|---|
| Hermetic Mailpit/MailDev/Inbucket | Job-local SMTP + capture CLI (profiles: `off` or `hermetic`) | Yes, for every evidenced sender |
| Hosted capture | Pack SPI `mode=hosted` (v0.2.0): HTTPS `/health` + `/messages`, env-only token, fail-closed production. Consumer canary only when a dedicated capture inbox and `EMAIL_CAPTURE_HOSTED_*` secrets exist. Not a substitute for hermetic evidenced-sender jobs. | No |
| Real-delivery canary | DNS/provider receipt to an entity mailbox | Separate from capture; still denied from KL |

## Example journey table (adapt in your repository)

List every sender the application actually has, then one hermetic lane per
sender. Rows below are illustrative shapes, not any real application.

| Journey | Entry point | Capture artifact |
|---|---|---|
| Password reset (API) | `POST /api/auth/password-reset` → mailer | `scripts/email_capture/hermetic_journey.py` |
| Password reset (browser) | `/forgot-password` → captured link → reset form → `/login` | `e2e/email-capture.spec.ts` |
| Invitation + claim | admin invite → queued send → captured claim link → set password → login | API lane script + browser spec |
| Contact form | `POST /contact` → notification to a configurable recipient | API lane script |
| One-time code | request code → captured code → confirm endpoint | API lane script |

Not mailbox-tested until a sender exists: flows that only return a URL
(`generateLink`, in-app invite links) stay `NO_EVIDENCED_SENDER`; disabled
confirmations and provider-hosted templates with no local sender are recorded
as gaps, not tested.

## Shared playbook patterns

- API lanes seed a capture user cloned from a seeded template (for example a
  `seed_capture_user.py --allocation <ref>` helper), so no production
  identity or PII is touched.
- Multi-frontend reset links: when the API composes `{frontend_base}/{route}?token=…`
  per frontend, the lane exports each frontend base URL so links are absolute
  and the browser can follow them.
- CSRF-gated endpoints: the journey's API client mirrors the application's
  CSRF cookie → header exchange.
- Negative (unknown-recipient) assertions run on a distinct `run_id`
  (`${RUN_ID}-neg`) — allocation scope is deterministic, so an identical
  request would share the main inbox.
- Applications with more than one send path (e.g. a direct mailer plus a
  queued provider) route every path through one capture SMTP helper behind
  the same fail-closed seam check, so queued sends (invites, reminders) are
  capturable too.
- A contact-style lane can point the configurable recipient at the
  allocation — the allocation itself is the inbox, no seed user needed.
- Cookie-forwarding API clients must drop deletion cookies (empty-valued
  `Set-Cookie`) or the forwarded jar clobbers the live session.
- `await --count N` counts only messages newer than the allocation cursor —
  after an await advances the cursor, each further expected message is
  awaited with `--count 1` (and `--count 0` bounds the negative window).
- Frontend specs live in a dedicated Playwright project (`email-capture`)
  with an empty storage state; the default project ignores them, keeping any
  post-deploy UAT suite untouched.
- Allocation ownership (email-capture `v0.2.4`+): `await` / `assert` /
  `release` must find the allocation in the same local registry that
  `allocate` wrote (`$RUNNER_TEMP/email-capture/allocations.json`, or
  `EMAIL_CAPTURE_PRIVATE_DIR` / `EMAIL_CAPTURE_ALLOCATION_REGISTRY`). A lane
  that splits these steps across jobs must run them in one job (or carry the
  registry between jobs); otherwise they fail with `AUTHORIZATION_DENIED`.

## Knowledge Layer

Skip ingest only when the authenticated mailbox is an owned capture address. To/Cc/Bcc never authorize skip. That exclusion stays on KL until `needs-human` is cleared.
