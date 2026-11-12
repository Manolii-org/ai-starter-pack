# Autofix custody and publication

The standalone and reusable workflows implement the same retained-owner contract.
`scripts/ci/autofix-custody.sh`, `autofix-publish.py` and
`autofix-budget-notice.sh` and `autofix-event-gate.sh` are embedded in workflow source by
`scripts/sync-autofix-control.py`. Run it after editing those files; `--check`
detects drift. Workflows do not execute controller scripts from a PR checkout.

An existing session records its verified opaque claim in the initial PR body as
`X-Follow-Through-Owner:` and registers it once with an author-owned
`<!-- follow-through-owner:v1 -->` comment. Private session IDs stay private.
Native PR body ownership or a registration by the PR author/GitHub Actions holds
both editor admission and publication. Other commenters cannot impersonate that
registration. The header is denial-only: it grants no permissions and proves
neither accepted handoff nor execution. GitHub keeps the event facts for the
original owner; the guard is not a resume transport.

Preflight checks ownership before editor work, the dispatch step rechecks it,
and publication rechecks native head/state/fork/holds again. Unreadable or
incomplete bounded readback holds the affected action. Current native head and
repository identity remain authoritative. Existing whole-execution locks, bot allowlists,
instruction restoration, commit guards, model/provider selection, human holds,
budgets and protected merge/review controls remain in force. No new scheduler,
poller, provider or editing session is registered.

The editor and acquisition steps use the built-in token with read-only contents
permission. The editor commits locally and never receives the publication
credential. The public caller invokes `pr-autofix-execution-reusable.yml`, which
runs the editor and publisher on separate runners. The standalone caller holds
its existing per-PR job lock over this entire invocation, after admission;
reusable callers retain their existing outer lock without a nested lock.
Unrelated rejected events cannot displace queued admitted work. Copy-installed
consumers must install the internal workflow together with their caller. GitHub.com `$/` nested
calls resolve workflow paths from the calling workflow's repository and
commit, without loading a consumer PR's controller. Job/check names now include
`Autofix Editor (standalone|reusable)` and `Autofix Publisher (standalone|reusable)`
under the existing `Autofix PR` call; review any consumer check-name bindings
before adopting. The existing 30-minute job allowance is split into 25 minutes
for editing and five for publication. Queue time is separate from runner time.

After an attempted editor action, inert capture also runs if the editor step
fails after committing. Successful artifact upload preserves those commits for
the original owner; it does not authorize publication from a failed editor job.
A runner loss or hard job timeout can still prevent capture/upload, so durability
is claimed only from an actual upload receipt.

The editor uploads only a Git bundle and a bounded JSON receipt, named for this
run and attempt. A fresh publisher downloads that same-run artifact using the
Actions artifact service; no editor job outputs, environment variables, proxy/CA
settings, Git config, checkout or model code enter the publisher. It independently
rechecks native state and binds the packet's repository, PR, branch and base to
trusted admission. Receipt size is limited to 16 KiB and bundle size to 64 MiB
before Git import; oversized repairs require the original owner to reduce or
recover them. Artifact download itself remains subject to Actions service limits.
Only the admitted base is fetched using the read token. Bundle references,
ancestry and every candidate commit are validated as data before credentialed
publication. A trusted controller publishes through a fresh bare Git repository
that does not inherit PR hooks, URL rewrites or credentials. It permits only a
fast-forward candidate based on the admitted head, enforces the existing
`[autofix]` commit-subject convention and refuses instruction-surface changes in
any candidate commit, including changes restored later or hidden by a merge.
Protected publication paths include `.github/workflows/` and `.github/actions/`,
so an unprivileged editor cannot redefine a later privileged workflow/action.
GitHub review runs can resolve workflow source at the PR merge ref; separating
runners alone does not make a future modified controller trustworthy. The
original owner must use its approved trusted-source publication route for those
findings; this adds no automatic human-approval requirement and does not hold
independent authorized fixes. The workflow definition admitted at run start must
itself be trusted under the consumer's existing source policy.
An expected-ref lease makes the sole push conditional on the checked head,
including deletion/reset races; validated ancestry prevents history rewrites.
Exact remote-ref readback is still required.
Unknown publication is retained as unknown; it is never blindly resent.

Reusable callers keep the existing optional `GH_PAT` secret. The standalone
caller keeps `MN_GH_AUTOMATION_TOKEN`. These must be owning-entity, CI-triggering
publication credentials; there is no built-in-token publication fallback.
Missing publication access does not prevent independent local implementation.
Unpublished implementation is retained as a seven-day recovery artifact containing
a Git bundle and bounded metadata, without execution logs or authentication files.
The unprivileged editor captures committed candidate data before publication.
The publisher captures validated committed recovery data before checking late
publication admission, so a moved head, retained owner or unavailable read holds
the push without discarding the implementation. A foreign-ancestry or protected
history hold does not claim a publisher recovery bundle; its receipt explicitly
points to the original editor run instead.
Recovery is data, not execution authority: the original owner must verify artifact
provenance, base/head/custody, object identity, review and checks before restoring
or publishing it. Artifact upload and comment delivery require their own receipts;
a local recovery path alone does not establish durable storage. No automatic
credential-rotation or artifact-recovery transport is claimed here.

The reusable output `activation` retains its existing meaning: the editor action produced an execution file
or an admission gate skipped it. It is not evidence of an action or publication.
Additive outputs `publication` and `published_sha` expose the controller result.
Only `published` with a verified SHA establishes remote publication; required CI
on that new SHA, independent review, protected merge and parent-task verification
remain separate commitments. `no_change` is not progress. Failed/held publication
fails its step while retaining local implementation and the unfinished task.
Exhausted-budget notices are deduplicated; counters are never reset to continue.
A native Actions-held/uncertain publication receipt suppresses another editor
for the same head. The original owner must recover retained implementation and
resolve that specific dependency before advancing the head. New review/CI facts
remain pending in GitHub; the guard does not acknowledge or discard them.
Automatic artifact recovery and same-owner event delivery still require the
qualified owning client; a legacy workflow cannot claim those capabilities.

The generator validates custody step presence/order and embedded payloads.
Editor admission requires the full approved conjunction, including explicit
`skip=false`; missing outputs and an OR bypass fail validation.

Consumers referencing a moving framework ref receive reviewed workflow changes
when that ref advances. Pinned/copy-installed callers require reviewed adoption
of their workflow, generator and policy assets. Keep each entity's own secrets,
provider controls and deployment authority. Policy installation does not install
an owning client or broker; native same-thread continuation must be qualified on
the actual host. An unavailable wake holds monitoring, not independent work.

The same-commit `$/` call syntax is documented for GitHub.com, including hosted
Enterprise Cloud. GitHub Enterprise Server does not support it; those consumers
need separately reviewed call syntax/adoption. Existing pins are unaffected. The
pinned actionlint and the latest checked release (1.7.12) do not recognize this
syntax. `check-pinned-uses.py --actionlint` provides a validation-only view: it
changes the first byte of valid job-level self-workflow references to the
documented equivalent `./` syntax on stdin, preserving source paths, columns and
all lint diagnostics. It neither changes execution source nor suppresses an
audit. Pin checks recognize only the bounded self-workflow spelling; mutable or
malformed references still fail. Positive relay admission uses the immutable
GitHub Actions actor ID rather than a renameable login.

## Native review event routing

`bot-review-relay.yml` uses the built-in token to display review text. GitHub
recursion protection means that copied comment does not trigger `issue_comment`
workflows; it is not delivery to an editor. The existing native review/comment
triggers now admit approved reviewers directly, including edited findings. The
default immutable bot user IDs are Codex 199175422 and CodeRabbit 136622811, verified
from native GitHub API identities (not their different App IDs). Owning operators
can replace that list with the JSON string array repo/org variable
`AUTOFIX_TRUSTED_REVIEWER_IDS`; `[]` disables the direct bot route. This does not
change `trusted_agent_bots`, which remains a separate opt-in CI-push policy.

The canonical event gate is embedded in standalone preflight, reusable editing
and fresh publisher admission. It verifies native sender type/ID, current native
head and any explicit review commit, passes only the verified reviewer login to
the action's bot allowlist, and retains native permission checks for other users.
Known informational summaries and unchanged reply loops stay quiet. Mixed
P2/P3 findings remain eligible, and unknown new approved review text is inspected
rather than discarded. Bot review text cannot issue the human `skip-autofix`
control. Human edited triggers retain their prior behavior. Reusable callers
need to include `edited` in their review/comment event types to receive updates;
this requires reviewed caller adoption. No new dispatcher, credential scope or
owner is created. Retained-owner custody still declines editing: direct event
admission is not an accepted handoff to a hosted chat.

Reviewer ID admission controls the legacy automatic worker, not which findings
a retained owner may inspect. Original task authority still governs its owner's
triage. The pinned action admits explicitly allowed bot accounts, but requires
`admin`/`write` from the collaborator API for human accounts. The event gate
preserves those exact requirements; it does not use the action's non-write-user
bypass or switch credentials after a denied permission read.
