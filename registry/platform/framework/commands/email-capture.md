# Email capture

Use `bin/email-capture` with a hermetic test profile or an opt-in hosted staging/preview profile. Allocate into a mode-0600 temporary file, mask the recipient immediately, wait/assert without printing content, and run `release` in an always/finally step. Never paste recipients, subjects, bodies, links, codes, headers, attachments, or credentials into prompts or receipts. Put hosted tokens only in `EMAIL_CAPTURE_HOSTED_TOKEN`. Production profiles and hermetic staging profiles must fail closed.
