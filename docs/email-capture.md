# Virtual inbox contract

The v1 interface is backend-neutral. Allocation is deterministic for entity/repository/environment/run scope and produces a high-entropy address. `await` applies a monotonic `(received_at, opaque_id)` cursor, time window, exact count, and typed timeout/ambiguity/replay failures. `assert` performs local deterministic link, numeric-code, and header extraction and emits counts only. `release` is idempotent and allocation-scoped; adapters never globally delete a shared mailbox. TTL is a receiver/janitor backstop.

Profiles accept major version 1. Modes are `off|hermetic|hosted`. Hermetic backends remain `memory|mailpit|maildev|inbucket` and stay fail-closed in staging/production. Hosted mode requires backend `hosted`, HTTPS (loopback HTTP only when `environment=test`), and `EMAIL_CAPTURE_HOSTED_TOKEN` from the process environment — never from profile JSON. Hosted environments are allowlisted: `test|ci|preview|preprod|staging`. Production/`prd` remains `CONFIG_INVALID`.

Optional `fidelity` is `provider-to-capture` (default) or `deployed-substitution`. The CLI `canary` command probes hosted health and emits a metadata-only receipt. Sender routing, vendor accounts, and provider evidence stay consumer-owned. Do not bake MailSlurp/Mailtrap keys into this pack.

Consumer journey coverage (which product paths actually send mail) is documented in `docs/email-capture-journeys.md`. The protocol does not imply every Auth template is tested.

Live spam/inbox-placement checks use the separate `bin/email-quality` contract;
see `docs/email-quality.md`. Capture modes, receivers and existing pins retain
their current semantics.

## Versions and allocation ownership

Email-capture releases are tagged `email-capture-vX.Y.Z` (README § Tag
namespaces). The legacy bare `v0.1.x`–`v0.2.x` tags remain for lanes that
already check them out. From `v0.2.4` (`email-capture-v0.2.6` is the first
prefixed tag), `await` and `release` resolve the allocation against
the local registry that `allocate` wrote — by default
`$RUNNER_TEMP/email-capture/allocations.json`, overridable with
`EMAIL_CAPTURE_PRIVATE_DIR` or `EMAIL_CAPTURE_ALLOCATION_REGISTRY` — and refuse
anything else with `AUTHORIZATION_DENIED`. A lane that runs `allocate` in one job
and `await`/`release` in another must move those steps into one job
(or carry the registry file between jobs as a private artifact). `assert` does
not consult the allocation registry, so it can run in another job, but like every
subcommand it first loads the capture profile and refuses `mode=off`
(`CAPABILITY_UNSUPPORTED`).

### Legacy bare `v0.x` tags (frozen)

These tags predate the `email-capture-v*` namespace. They are frozen aliases:
never deleted or moved, because existing lanes check them out. Pin
`email-capture-v0.2.6` (`a00b8254d0f02b1ae68bec6de86a8fa81ee639b2`) instead.
The pack's Renovate preset never proposes them; its `allowedVersions` admits
only `vX.Y.Z` with a major version of 1 or more.

| Tag | Kind | Commit | Component |
|---|---|---|---|
| `v0.1.0-rc.1` | annotated | `99aecbf38239acba7b8f1ef3370971e54cb9a524` | email-capture 0.1.0 release candidate |
| `v0.1.0-rc.2` | annotated | `92f3291851cec92fe4c6c3758c6ea56540c45de9` | email-capture 0.1.0 release candidate |
| `v0.1.0` | annotated | `ac0cf13ac61c92597c8d80e64088d764fd21acf9` | email-capture 0.1.0 |
| `v0.1.1` | lightweight | `bd8fa75e35f48acf10bfda87768a4447675d0261` | email-capture 0.1.1 |
| `v0.2.0` | lightweight | `d028696408ab6462a48161e4225ecec2454d4076` | email-capture 0.2.0 (hosted SPI) |
| `v0.2.1` | lightweight | `9a24dc1df3b5b9ab2765e239307f5cf56ebba445` | email-capture 0.2.1 |
| `v0.2.2` | lightweight | `cfb069ea64a770411ca58b0465c9538ac404200f` | email-capture 0.2.2 |
| `v0.2.3` | lightweight | `c963df9d900b017c7df5ca22e054dbd45e270e77` | email-capture 0.2.3, **before** the allocation-ownership fix |
| `v0.2.4` | annotated | `34307e1e9ddb6d8ff83b1caefa73add8b9e4e2d2` | none: tagged on a Codex-review commit; its email-capture files equal `email-capture-v0.2.6` |
| `v0.2.5` | lightweight | `c1da5fe95dddaa4febc5fe1e422441446d8da033` | none: tagged on a Codex-review commit; its email-capture files equal `email-capture-v0.2.6` |
| `v0.2.6` | lightweight | `a00b8254d0f02b1ae68bec6de86a8fa81ee639b2` | same commit as `email-capture-v0.2.6` |

Moving from `v0.2.3` (or earlier) to `email-capture-v0.2.6` picks up the
allocation-ownership change above. Keep `allocate`, `await` and `release` in
one job before you re-pin. Email-capture files means `email_capture/`,
`bin/email-capture` and `schemas/email-capture/`. The module's
`__version__` still reads `0.2.3` from `v0.2.3` through
`email-capture-v0.2.6`; the tag, not that string, identifies the release.


## Native Resend receiver and retained-mail lifecycle (additive)

`backend: "resend"` (hosted mode only) reads Resend managed receiving through
`GET /emails/receiving` and `GET /emails/receiving/{id}`; it is a native adapter,
not the generic hosted HTTPS SPI (`backend: "hosted"`), which stays separate.

- **Endpoint.** `https://api.resend.com` only (loopback HTTP in `test`).
- **Token.** `EMAIL_CAPTURE_HOSTED_TOKEN` must be a receiving-capable key from a
  dedicated QA organisation. There is no fallback to `RESEND_API_KEY`. A holder of
  that key can read the whole QA organisation; allocation checks are framework
  discipline, not vendor isolation.
- **Enumeration.** The vendor list has no recipient filter. Pages of 100 are read
  (bounded at 10 pages), matched on the exact `to` address, and only then are
  bodies fetched (once per message per process). An unfinishable scan is
  `CAPTURE_INFRA_UNAVAILABLE`, never "no mail". Polling is 1 s; HTTP 429 backs off
  by `Retry-After` while the await deadline allows.
- **Retention.** The profile must set `"retention": "provider-retained"`
  (default `strict-purge`). A strict-purge profile rejects this receiver. Resend
  has no received-mail delete, so `release` returns `CAPABILITY_UNSUPPORTED`; use
  `email-capture retire --allocation @file`, which closes the allocation (no further
  reads) and reports `retention_state: provider_retained`. Retirement is not a
  purge and does not remove sender logs, backups or revoke Auth sessions. It
  remains reachable for expired and pending-purge allocations.
- **Quiet window.** `await --quiet-window N` keeps observing for N seconds after
  the expected count and fails `MESSAGE_AMBIGUOUS` on a delayed duplicate.
- **Inspection.** `inspect --allocation @a --messages @m --allow-host H
  [--allow-path-prefix P] --output FILE` writes plain text (tags, scripts and remote assets stripped, never rendered), https links on
  approved hosts/paths and numeric codes to a mode-0600 file; stdout only carries
  a count receipt. Expired allocations and messages outside the allocation are
  denied.

Tooling identities stay distinct: component tag `email-capture-vX.Y.Z`, protocol
schema `1.0`, and the Python `__version__` are separate values.

## Configuration map and precedence

| Variable | Meaning | Notes |
|---|---|---|
| `EMAIL_CAPTURE_MODE` | `off` / `hermetic` / `hosted` | Canonical; mixing with legacy `MAILDEV_URL`, `INBUCKET_URL`, `EMAIL_CAPTURE_API_URL`, `SMTP_HOST`, `SMTP_PORT` is rejected |
| `EMAIL_CAPTURE_BACKEND` | `memory`, `mailpit`, `maildev`, `inbucket`, `hosted`, `resend` | `resend` only with mode `hosted` |
| `EMAIL_CAPTURE_ENDPOINT` | Receiver base URL | `resend`: `https://api.resend.com` only |
| `EMAIL_CAPTURE_ENVIRONMENT` | Capture environment label | Required for hosted; production labels rejected. Never overrides a consumer's own resource-identity check |
| `EMAIL_CAPTURE_FIDELITY` | `provider-to-capture` / `deployed-substitution` | |
| `EMAIL_CAPTURE_RETENTION` | `strict-purge` (default) / `provider-retained` | Must be `provider-retained` for `resend` |
| `EMAIL_CAPTURE_HOSTED_TOKEN` | Receiver credential (process environment only) | Never in profile JSON; never defaulted from an application sending key |
| `EMAIL_CAPTURE_PRIVATE_DIR` | Private (0700) workspace for registry, allocations, messages | Allocate, await and cleanup must share it |
| `EMAIL_CAPTURE_ALLOCATION_REGISTRY` | Registry path override | Authoritative for allocation identity and cursor |
| `EMAIL_CAPTURE_STARTER_ROOT` | Consumer-side checkout of this pack | Consumer convention, not read by the core |

A profile JSON file wins over environment variables (`--profile`). Consumers translate their own environment
(for example BCP's `NEXT_PUBLIC_ENV` / `VERCEL_ENV` / `NODE_ENV`) into `EMAIL_CAPTURE_ENVIRONMENT` in their launcher,
and still verify the actual target (frontend URL and database identity) before any write. Supabase's historical
`[inbucket]` / `supabase_inbucket` names remain although the service is Mailpit; live conformance keeps
`EMAIL_CAPTURE_SMTP_HOST` / `EMAIL_CAPTURE_SMTP_PORT`.

## Onboarding a new consumer (CPDcheck, Manolii applications)

1. Pin this pack by full commit SHA (component tag `email-capture-vX.Y.Z` in a comment); never a branch or a caller-controlled ref when secrets are present.
2. Keep application journeys, fixtures, tenant isolation and secret-store routes in the consumer; supply the receiver token only through the process environment of trusted test processes.
3. Start hermetic (Mailpit). Add a hosted receiver only after qualifying it (ordering, pagination, quotas, key scope, retention) and recording its owner and retained-mail policy.
4. Assert on normalized `content.text` / `content.html`; fail when content is absent. Make cleanup failure fail the run.
5. Activation is a separate owning-repository step; this document does not authorise mass repinning or secret relocation.
