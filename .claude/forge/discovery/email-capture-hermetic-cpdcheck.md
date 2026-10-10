# Discovery brief — activate hermetic email-capture for CPDcheck

Repo(s): cpd-marketing-api, cpd-assessment-api-v2 (later: cpd-assessment-frontend-v2, cpd-professional-frontend)
Type: feature (CI/test infrastructure; one additive seam in api-v2)
Work item: none yet

## 1. What is actually being asked for
Adrian: adopt the shared virtual-inbox email testing framework (bcp-core +
Manolii pattern) for CPDcheck — "implement and activate it end to end" after
the proposal was reviewed. Proposal doc: /home/ubuntu/cpdcheck-email-capture-proposal.md.
Consumption mode approved as Option A: pinned tag checkout of
Manolii-org/ai-starter-pack @v0.2.3 in CI only — no runtime/secrets/data
coupling across universes (same pattern bcp-core uses).

## 2. What the code does today (verified this session)
- cpd-marketing-api: `internal/services/smtp.go` SMTPEmailSender selected when
  ACS_CONNECTION_STRING unset (cmd/api/main.go:73-91); sqlite via
  USE_LOCAL_DB=1 + LOCAL_DB_PATH with embedded migrations; cert handler
  POST /assessments/:ref/certificate sends `SendWithBCC` to CERTIFICATE_BCC
  (certificate.go:27-105); POST /contact sends SendWithReplyTo to
  CONTACT_FORM_RECIPIENT (contact.go:24-74). Boots fine without AOAI creds.
- cpd-assessment-api-v2: GenericEmailer/CpdCertificateEmailer send via MS
  Graph (app/services/emailer.py); call sites: auth_service.py:581,
  teams_quiz_email_service.py:54, provider_invoice_email_service.py:29 +
  ~11 CpdCertificateEmailer sites. CpdCertificateEmailer is hard-frozen by
  cert_emailing_frozen() — cert emailing retired, no live sends.
- Routes: POST /api/auth/send-reset {email, portal?} → reset link
  `{portal_base}/{route}?token={raw}` (auth_service.py:700-770, 5-min token);
  POST /api/auth/reset-password {token, new_password} → 204.
- MODE env DEV|UAT|PROD (app/core/secrets.py:12); TESTING=1 dummy DB.
- Pack assert schema: flat {exact_count, negative, extract[]}; extract only
  inspects messages[0]; message JSON has subject/envelope/content/attachments[]
  {filename,content_type,size}.

## 3. Who else depends on this
- No production path changes in marketing-api (CI-only workflow + scripts +
  ADR). api-v2 change is transport-selection inside Emailer only; every
  existing caller keeps its class. CERTIFICATE_BCC override only in CI env.
- Seed script writes only to job-local sqlite; no shared DB, no migrations.
- Framework checkout is CI tooling; universe isolation preserved (public repo
  tag, no credentials).

## 4. Backwards compatibility
- api-v2 seam is opt-in via EMAIL_CAPTURE_MODE=hermetic AND refuses to engage
  when MODE is UAT/PROD (fail-closed). Default path (unset) = MS Graph,
  unchanged. Existing tests mocking Graph unaffected.

## 5. Database and migration impact
- None. Journeys seed into throwaway sqlite files only.

## 6. Security and tenancy
- Capture stays job-local: allocation recipients ec-*@capture.test, Mailpit on
  127.0.0.1, mode-0600 artifacts, metadata-only receipts. CERTIFICATE_BCC
  cleared in CI so no real mailbox is copied. No PII added.

## 8. Plan and how we will know it worked
- Phase 1 (marketing-api): workflow + scripts/email_capture/hermetic_journey.py
  + ADR-0001. Journeys: bounded negative, cert PDF (subject+attachment),
  contact delivery. Verified: workflow runs green on the PR.
- Phase 2 (api-v2): SMTP transport seam in Emailer gated on
  EMAIL_CAPTURE_MODE=hermetic + MODE∉{UAT,PROD}; seed script for an
  allocation-recipient provider-portal AuthUser (+profile/role/subscription
  companions per local_dev_seeder.py:287-320); journeys: forgot-password
  negative+positive (link extraction → /api/auth/reset-password → login),
  OTP request/confirm, invite if cheap. Workflow with
  `Guarded-Path: deploy-workflows` commit trailer.
- Phase 3 (frontends): capture specs driving forgot-password end-to-end via
  local api-v2+Mailpit+dev server; dedicated workflow per repo.
- Phase 5: journeys-doc section in pack + consumer ADRs.
