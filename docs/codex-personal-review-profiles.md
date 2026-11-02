# Personal subscriptions for private CI reviews

Each team member authorises their own ChatGPT account using the official Codex
login. Profiles nominate who funds future code **and** security reviews. This
does not combine subscriptions into a transferable quota pool. There is no
automatic account rotation on exhaustion, API-key fallback, or Cloud scan.

The implementation is opt-in. Shipping these files does not activate a runner,
authenticate a person, change their billing settings, or prove live coverage.

## Supported execution boundary

Use a dedicated, trusted, persistent Linux host. Administrators must enforce
runner-group access to the exact approved default-branch controller workflows.
Runner labels and repository variables alone do not enforce this boundary.
Every admitted controller revision belongs to the trusted computing base:
Docker access can confer host-root access. Never place ordinary PR jobs,
unrelated workflows, or an unrestricted organisation runner on this host.
Protect controller changes with the owning repository's existing review policy.
If selected-workflow restrictions are unavailable, provision a separate narrow
worker service before activation; do not substitute a label-only deployment.

Multiple runner registrations may consume different organisations' queues on
that host only after their access and entity isolation are verified. One runner
registration does not automatically serve four organisations. All consumers of
one account must use this same host and canonical session directory. A machine
ID detects accidental relocation; it is not a distributed lock or an exclusive
deployment proof. Retire the old host before moving an account to a new host.

The worker still runs PR source only inside its credential-free, networkless,
nonroot container. Install the existing AppArmor policy administratively and
qualify both actual tools. There is no self-hosted sudo/unconfined fallback.

OpenAI's [private CI guidance](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
requires one serialized stream per session and persistence of refreshed auth.
It explicitly excludes public/open-source repositories from this pattern.
Personal-profile mode therefore refuses public repositories. Native GitHub
review evidence remains eligible; a supported public-repo deployment requires
separate qualification. Do not copy personal sessions into public CI secrets.

## Owner login and consent

1. Install the pinned official **native ELF** code CLI, version **0.163.0**, at
   `/opt/codex-review/bin/codex`. Keep its lexical and resolved installation
   paths administrator-owned. The npm Node launcher is refused: killing it
   does not reliably terminate the Rust refresh writer before the lock releases.
2. On the trusted host, the account owner personally runs `codex login` or its
   device flow with a new private `CODEX_HOME` and
   `cli_auth_credentials_store="file"`. Use a separate session from their
   desktop, managed executor, canary, and old review-secret consumers. Never
   send an auth file, password or token through chat, a PR, or an artifact.
3. Compute the SHA-256 of `tokens.account_id` locally; do not print token values.
   Store this one authoritative home at
   `/var/lib/codex-review/accounts/<account-sha256>/`. The state directory,
   `accounts`, account home and auth file must be private (0700 directories,
   0600 auth), owned by the dedicated controller user. Never restore the seed
   over a refreshed file. Do not create another profile/session for the same
   account to obtain apparent additional capacity.
4. The owner explicitly grants the exact repository list and both lanes,
   chooses an expiry and generation, and verifies purchased-credit use and
   automatic purchases are disabled in the applicable account controls.
   The administrator installs that consent separately from repository source
   at `/etc/codex-review/consents/<profile>.json`.

Registry and consent files and all parent directories must be root-owned,
regular files/directories, with no symlinks or group/world write permission.
Repository source cannot grant owner consent. The administrator's access
verification record is a deployment receipt, not a substitute for configuring
and testing runner restrictions. Retain its evidence and expire it when the
host's access policy changes.

Example registry (replace illustrative hashes and timestamps after verification):

```json
{
  "schema": "codex-review-profiles/v1",
  "host_sha256": "<sha256 of stripped /etc/machine-id>",
  "runner_access": {
    "mode": "selected_trusted_workflows",
    "verification_sha256": "<sha256 of actual administrative verification>",
    "verified_until": 0,
    "workflows": ["example/project/.github/workflows/codex-review-worker.yml@refs/heads/main"]
  },
  "profiles": {
    "alice": {"owner": "alice", "account_sha256": "<account hash>", "generation": 1},
    "bob": {"owner": "bob", "account_sha256": "<different account hash>", "generation": 1}
  },
  "organisations": {"example": "alice"},
  "repositories": {"example/project": "bob"}
}
```

Example consent for `bob.json`:

```json
{
  "schema": "codex-review-consent/v1",
  "profile": "bob",
  "owner": "bob",
  "account_sha256": "<same account hash as registry>",
  "generation": 1,
  "revoked": false,
  "expires_at": 0,
  "repositories": ["example/project"],
  "lanes": ["code", "security"],
  "purchased_credits_disabled": true,
  "automatic_purchases_disabled": true
}
```

Zeros deliberately fail closed. The owner controls expiry and scope; a repo or
organisation default never widens the exact consent list. Use an atomic replace
for administrative edits and keep an audit record. The profile is selected
repo first, then organisation. There is no cross-organisation/global fallback.

## Nomination, switching and revocation

To nominate an account for an organisation, set `organisations["example"]` to
its profile. To override a repository, set `repositories["example/project"]`.
To return to the organisation choice, remove that repo override. Invalid pins
fail closed. Changes apply to future reservations. A reservation records the
selected profile, owner, account/host hashes, consent generation and digests;
execution rechecks them under the account lock. Changing a queued assignment
returns incomplete rather than silently charging a different owner. Explicitly
rearm an incomplete revision only after inspecting its attempt; completed
reviews are reused, so switching accounts does not buy a duplicate review.

For revocation, set consent `revoked: true` or advance the registry generation
without issuing a replacement grant. Queued work is refused. Stop any current
job and revoke the provider connection to terminate already-started work;
editing the registry alone does not retroactively cancel an in-flight turn.
To reseed a session, stop its jobs and acquire its existing `review.lock` before
the owner's new login replaces the file. Keep the old session out of every
other machine and secret writer. No changes are made to the managed
executor/canary's separate GitHub secret or its lineage protocol.

The exclusive OS lock uses the actual account hash across every repository
and both lanes. The CLI performs proactive refresh through `account/read`,
followed by `account/rateLimits/read`; refreshed auth stays in its canonical
home. The fixed broker never receives a refresh token. The access token must
cover the bounded scan plus margin. Unavailable auth/quota is incomplete.
Busy accounts wait under a bounded 1,250-second lock-admission deadline and do
not invoke another account. The job has a 60-minute outer limit covering Git,
image preparation, lock admission and the bounded scan. Different runner
registrations do not share a GitHub queue: avoid over-registering the host.
Never create one shared Actions concurrency group whose pending slots replace
unrelated reviews. A lock timeout is visible incomplete coverage. A new explicit
request may rearm an authenticated result proving `provider_invocation_started:
false` and zero successful turns; only that result's own reservation is ignored.
After inference might have started, or cancellation leaves no such result,
attempt suppression remains. Do not delete receipts or blindly retry paid work.

## Funding controls and activation

The account probe must confirm ChatGPT auth, backend ordinary included usage
permission, both short/weekly windows below 80%, future reset times, and no
available/unlimited credit balance. Unknown telemetry blocks work. The 20%
headroom is an admission guard, not a token-to-plan conversion or a guaranteed
spend cap. Zero available credits is deliberately conservative: an account with
a purchased balance remains blocked even if the owner normally uses its plan.
Account settings can change outside the worker, so receipts explicitly report
`owner_credit_controls_required`; `billing_mode: subscription` proves configured
authentication only. Reconcile actual usage/billing after the first real pair.

The newer [Sign in with ChatGPT plan-sharing controls](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites)
are a different integration. Commercial/private clients require supported
availability; this implementation does not impersonate an approved app or reuse
Codex's OAuth client for a custom hosted sign-in flow. App-sharing settings are
not assumed to govern a Codex-managed session.

After owner login, enforce and negatively test runner access, verify the
protected policy and scopes, qualify cancellation/refresh persistence and both
actual containers, then set `CODEX_TRUSTED_REVIEW_RUNNER` to the verified runner
label JSON. Set the instance-owned `codex-review-worker.json` to
`enabled: true`, `credential_mode: personal_profiles`, `identity: null` in its
reviewed default branch. Existing `CODEX_REVIEW_ENABLED` and
`CODEX_SECURITY_ENABLED` kill switches retain their names. Personal profiles
and legacy OIDC credentials are mutually exclusive; no fallback crosses modes.

Prove a completed code/security pair, correct profile attribution, private
scope, quota refusal, revoked consent and late-native recovery for each rollout
configuration. Only then retire each old review-secret consumer and adopt the
normal starter-pack release/pins. Preserve CI names, markers, merge queues,
existing static/secret checks and owner merge controls. Disable the new worker
for rollback; retain receipts and refreshed sessions. Do not restore revoked
seeds or silently revert to credential-bearing PR execution.
