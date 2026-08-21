# Email capture

Use `bin/email-capture` only with a hermetic test profile. Allocate into a mode-0600 temporary file, mask the recipient immediately, wait/assert without printing content, and run `release` in an always/finally step. Never paste recipients, subjects, bodies, links, codes, headers, attachments, or credentials into prompts or receipts. Capture-enabled production/staging profiles must fail closed.
