# Autofix custody and publication

The standalone and reusable workflows implement the same retained-owner contract.
`scripts/ci/autofix-custody.sh`, `autofix-publish.py` and
`autofix-budget-notice.sh` are embedded in workflow source by
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
repository identity remain authoritative. Existing job locks, bot allowlists,
instruction restoration, commit guards, model/provider selection, human holds,
budgets and protected merge/review controls remain in force. No new scheduler,
poller, provider or editing session is registered.

The editor and acquisition steps use the built-in token with read-only contents
permission. The editor commits locally and never receives the publication
credential. A trusted controller publishes through a fresh bare Git repository
that does not inherit PR hooks, URL rewrites or credentials. It permits only a
fast-forward candidate based on the admitted head, refuses instruction-surface
changes in the repair, sends one push, then requires exact remote-ref readback.
Unknown publication is retained as unknown; it is never blindly resent.

Reusable callers keep the existing optional `GH_PAT` secret. The standalone
caller keeps `MN_GH_AUTOMATION_TOKEN`. These must be owning-entity, CI-triggering
publication credentials; there is no built-in-token publication fallback.
Missing publication access does not prevent independent local implementation.
Unpublished implementation is retained as a seven-day recovery artifact containing
a Git bundle and bounded metadata, without execution logs or authentication files.
The controller captures committed recovery data before checking late publication
admission, so a moved head, retained owner or unavailable read holds the push
without discarding the implementation.
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

Consumers referencing a moving framework ref receive reviewed workflow changes
when that ref advances. Pinned/copy-installed callers require reviewed adoption
of their workflow, generator and policy assets. Keep each entity's own secrets,
provider controls and deployment authority. Policy installation does not install
an owning client or broker; native same-thread continuation must be qualified on
the actual host. An unavailable wake holds monitoring, not independent work.
