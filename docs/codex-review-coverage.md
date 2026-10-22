# Codex code and security review coverage

Review connectivity, completed execution, findings and permission to merge are
separate outcomes. A quota notice, skipped job or successful advisory workflow
is not proof that the current PR revision was reviewed. Subscription CLI usage
is subject to the owning account's entitlement and limits; no unlimited quota,
billing saving or usage-priced API fallback is assumed.

## Findings quality

The existing PR Assessment Judge applies four filters to reported findings:
Accuracy (verifiable), Actionability (a concrete fix), Novelty (not duplicated
or already caught by CI), and Specificity (a precise affected location and
explanation). These describe the Judge’s findings-quality contract. Review
completion and merge eligibility retain their separate checks: filtering all
findings does not establish that a missing or incomplete review finished.

## Execution and credential boundary

`codex-pr-review.yml` and `codex-security-diff-scan.yml` retain their names,
`review`/`scan` jobs and separate coverage contexts. These pull-request workflows
contain no subscription/Doppler credential or OIDC grant. They request review and
validate native evidence or an authenticated worker artifact. A PR's own job
outputs and comments can never attest to a CLI review.

`codex-review-worker.yml` runs only as a trusted default-branch `workflow_run`
controller, checked out at `github.workflow_sha`. It verifies the official
requesting workflow ID/path/event/repository and its PR association, then checks
the live open/non-draft same-repository PR, author and full base/head SHAs. Forks
require native proof; restricted/excluded authors do not receive credentials.
A bare Git source input is fetched without application/test/dependency execution,
working-tree checkout, hooks, external diff or credential persistence on the host.
Both lanes review the PR merge-base-to-head scope. The Security SDK receives
that resolved merge base explicitly, excluding unrelated later base-branch edits.

The complete code CLI or Security CLI/SDK/plugin runtime runs in an unprivileged
Docker container with no network, host PID namespace or Docker socket. Its root
and input mounts are read-only; scratch, memory, CPU, PIDs and time are bounded.
It receives public placeholder client auth and a constrained Unix model socket.
Real auth, GitHub tokens, OIDC tokens and host configuration never enter it.
The upstream Codex Security seccomp profile permits its nested Bubblewrap
namespaces while retaining deny-by-default filtering, dropped capabilities and
no-new-privileges. An actual sandbox command from both pinned runtime closures
must succeed before auth is fetched. On ephemeral GitHub-hosted AppArmor runners,
only the reviewed named profile may be loaded. No unconfined/privileged fallback
or shared-runner mutation exists.

A trusted host broker holds the subscription access token only outside that
container. It permits POST Responses traffic to one fixed OpenAI endpoint,
inserts auth upstream, drops caller headers and refuses redirects, refresh,
CONNECT and arbitrary URLs. Bounded admission precedes parsing/thread creation;
killable provider children enforce absolute deadlines through DNS, headers and
slow bodies. A defensive reflection filter does not claim to detect arbitrary
encodings. Isolation prevents auth from becoming model input in the first place.

Before attaching subscription auth, the broker admits only stateless inline
review requests from the pinned clients. Hosted tools, response persistence,
conversation/response references, files/images, unknown fields, ambiguous JSON
and external schema references are refused without an upstream call. Only local
function tools and the qualified custom execution grammar are accepted. Caller
item IDs and session metadata are stripped; cache identity belongs to the host.
Encrypted reasoning may be replayed only after that same broker has received it
in a validated successful provider response. Unsupported client changes fail
closed and require fresh protocol qualification.

## OIDC configuration and bootstrap

`scripts/ci/codex-review-worker.json` is instance-owned and disabled by default.
Copier updates and capability sync must preserve it. Configure a dedicated
owning-tenant secret-read identity only after reviewing its actual scope. The
worker exchanges GitHub OIDC for a short-lived Doppler token, reads only the
named subscription auth and has no static service-token or API-key fallback.
Require exact audience/subject, numeric repository/owner IDs, default-branch ref,
worker `workflow_ref` and `workflow_run` event. Missing/mismatched claims deny
access. Normal protected default-branch review remains a substantive boundary.
An inactive provisioning policy must use a deliberately unmatchable claim until
activation is accepted; a false repository setting alone is not a disabled
Doppler identity.

The trusted worker requires no GitHub protected environment or static repository
review secret. Earlier Administration/Secrets write requests belonged to the
superseded design; they are not bootstrap prerequisites. Separately verify and
retire any legacy review credential with owner/consumer continuity. A denied
metadata read is not proof that the old secret is absent.

At accepted activation, remove only the inert all-zero workflow_sha predicate
and retain every exact stable claim above; do not pin a live identity to one
main-branch commit. GitHub workflow_sha advances with the trusted workflow
revision and a single-commit pin would stop later reviews. Recheck positive
trusted exchange
after a subsequent protected default-branch update, alongside negative PR,
fork, wrong-workflow/ref/event/audience and owner/repository claim cases. The
preparation CLI does not perform this connection or activation.

First adoption cannot execute a PR's proof reader or scanner with credentials.
Land reviewed inactive source through the repository's existing review/merge
policy, using independent/native bootstrap review as required. Before activation,
qualify the actual container/tool closures, perform a bounded actual subscription
code/security smoke and verify real OIDC positive/negative access from the trusted
workflow. Synthetic endpoint tests prove transport/isolation, not a real security
assessment or provider entitlement. Do not require new coverage contexts until
the real per-repository smoke and merge-group compatibility are accepted.

## Evidence and recovery

Native proof is authenticated and exact-head. Code and security lanes remain
separate. Native inline code findings require the original commit, submitted
parent review at that full head, native actor and code-lane marker; relays,
replies, quota messages and remapped older comments do not qualify. Abbreviated
summary SHAs must resolve uniquely through GitHub. Findings need not be clean to
prove execution; merge reviews and blocking security findings still apply.

Before a model call the worker persists an authenticated revision/lane/policy
attempt reservation. A failed/cancelled attempt suppresses automatic model
retries within artifact retention; it never satisfies coverage. Disabled or
unprovisioned requests cannot reserve an attempt. Exact-head native proof or a
successful trusted worker result avoids duplicate inference. Concurrency
serializes repository/PR/head/lane work; no hosted polling or quota retry exists.

Worker proof authenticates the producing workflow ID/path/event/repository,
default branch, official artifact ownership, full repository/PR/base/head/lane,
policy/config/tool closure and image identity. Signed download redirects never
receive the GitHub token. Bounded inert ZIP/JSON is validated without extraction.
A completed result requires a host-observed completed provider turn plus a
validated, digest-bound full `review.txt`. The code verdict comes from the
host-observed final model message. Security requires a successful scanner and
valid findings array; a genuine empty array differs from missing output.

The complete bounded result and receipt are retained together. Comments retain
the existing lane markers, carry the full head and stay revision-specific. A
shortened comment explicitly links the full artifact. Publication follows
successful retention; old workers do not overwrite newer-head comments.
`codex-review-recovery.yml` handles native events and authenticated worker
completion, after the producer finishes. It rechecks the live revision and
reruns only a failed coverage job, never the model job. No output artifact or
comment can authorize merge by itself.

## Limits and operating checks

The code CLI is pinned to 0.151.0. Security CLI 0.2.0 retains its exact embedded
Codex/SDK 0.162.0-alpha.15 closure. Do not force the standalone code CLI version
into the Security SDK or assume one runtime's probe certifies the other.

Each attempt permits 32 code-model requests or 96 Security-model requests within
the same 20-minute broker deadline. Bounds include two active upstream calls,
four client connections, 2 MiB requests, 32 MiB provider turns, 1 MiB published results and
bounded tmpfs. Host Git staging limits one pack/file to 256 MiB, each Git process
to 768 MiB address space/180 CPU seconds/240 wall seconds, 2,000 history depth,
500,000 objects, 640 MiB staged data and 20,000 paths/2 MiB path output. An
unresolvable/oversized range is incomplete coverage, not an exemption. Split or
rebase an oversized/stale PR, or review a capacity change; never bypass review.

Sensitive selection includes auth, tenant/RLS/schema, credentials, dependencies,
CI permissions, network/crypto, deployment, merge, release, holds and policy
controls, including both sides of renames. Ordinary resolved diffs receive an
explicit security exemption. This path policy does not prove other files safe;
existing static, dependency, secret and human checks still apply.

Run the distributed receipt, workflow, broker and controller tests. Run the
container qualification on a Docker-capable host with only synthetic auth.
Keep source installation, actual execution, account settings and billing
reconciliation separate in fleet reports. A paused application still needs
repository maintenance; archived/signing/evidence/retired repositories retain
their explicit governance classification. Revert the reviewed source PR to roll
back; change enforcement only through reviewed branch policy. Preserve all
existing deployment/promotion/merge controls and entity isolation.

## Subscription protocol compatibility

Both pinned executables explicitly use `gpt-5.6-sol`; the trusted broker rejects
other model names and the tool receipt records the model. The subscription
service may label a completed Responses SSE body `text/plain`. The broker
normalizes that label only after validating a completed Responses event stream;
arbitrary text, HTML and partial/error streams do not qualify. Provider retries
are disabled in the CLI configuration, and the broker stops subsequent upstream
calls after a refusal. A quota or authentication failure remains incomplete.
