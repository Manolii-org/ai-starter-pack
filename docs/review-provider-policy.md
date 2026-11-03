# Explicit review provider policy

Review runners use the configured governed HTTPS proxy for ordinary calls.
There is no built-in provider-direct endpoint fallback. A missing or unsafe
proxy never turns a proxy credential into a first-party credential.

An external consumer may use its existing direct-provider capability only when
its immutable capability policy admits the call and it explicitly configures
`ANTHROPIC_DIRECT_BASE_URL` (or an Anthropic `ANTHROPIC_BASE_URL`). The configured
value must be an HTTPS origin for `api.anthropic.com`, with no user information,
query, fragment, non-default port or path. Admission and origin validation occur
before reading a direct credential. Credential resolution, review input custody,
complete-input bounds and missing-coverage markers keep their existing checks.

Estate instances remain denied by the existing capability admission rule.
`CLIENT_AI_POLICY` cannot silently fall back to an ordinary proxy review.
Restricted data remains governance no-AI and is never made dispatchable by a
provider setting. The intent-drift stop hook requires the governed proxy and
refuses direct-only policy contexts.

Tests use synthetic credentials and mocked requests; they do not contact either
provider or load production configuration.
