# Agent-managed review and merge

An authorized agent owns implementation, verification, review recovery, repair,
merge or queue admission, and the requested postmerge verification. A completed
independent automated review can satisfy review without a human clicking Approve.
Publication format alone must neither block real review nor manufacture one.

## Accepted evidence

Use authenticated GitHub data from the actual PR and its freshly read head:

| Evidence | Qualifier |
| --- | --- |
| Independent human approval | Existing human-review helper; author excluded and current-head vetoes retained |
| Allowlisted review-bot approval | `review_evidence.py --head-sha <full-sha>` on the complete reviews array |
| Completed CodeRabbit clean source review | `review_evidence.py --comments --repo <owner/repo> --pr <number> --head-sha <full-sha>` on comments with complete GraphQL edit provenance |
| Existing clean Judge or native Codex review | Existing canonical helpers, where installed; comment-based Codex evidence goes through `review_evidence.py --codex` and the same provenance check |

The shared helper prints exactly `satisfy`, `block` or `none`. Exit zero only means
the input was classified. Only `satisfy` supplies a review leg; it never authorizes
a merge by itself. Empty/non-approving review objects do not supply that leg.

The CodeRabbit leg requires its real bot identity/type and exact API issue URL,
the latest canonical summary with valid update identity, a completed clean recent
review, and one unambiguous generated coverage record with `kind: reviewed` and
both full commit IDs equal to the requested head. Generated markers must lie
outside the walkthrough, which may quote contributor text. Pending, skipped,
rate-limited, stale, partial, conflicting or copied evidence fails closed.
Fetch evidence from GitHub; never construct a passing input or post an approval
under the implementing account. A reviewer acknowledgement is not source review.

Use `review_evidence.py --fetch-comments --repo <owner/repo> --pr <number> --head-sha <full-sha>` to
read every comment page and complete GraphQL `userContentEdits` history with the
caller's existing GitHub CLI authentication. REST retains the bot author when a
repository writer edits its body, so REST authorship or the latest editor alone
is insufficient. Require matching body/ID/timestamps, creation coverage for edited
comments, every recorded editor being the actual bot, no deleted history, and
complete stable pagination. Authenticate newer bot bodies before dismissing them
as acknowledgements; do not inspect histories older than the selected source.
Capture JSON as classifier input without logging bodies. Missing, partial, racing
or tainted provenance supplies
no passing comment evidence. Never fall back to a REST-only passing result.

## Remaining gates

The caller retains complete review pagination, all current blocking reviews,
actionable finding handling, unresolved-thread rules, authorization, explicit
human-merge holds, secret checks, named required checks on the current commit,
trusted base-branch helper execution and normal server-side protection. Preserve
consumer-specific production, tenant and provider boundaries. Review is one gate.

Use native auto-merge where supported. Otherwise use an explicit merge with the
expected head, or the repository's required native merge queue. Disabling native
auto-merge does not add a second independent approval requirement. An actual
human-only policy or new authority/risk decision remains a human handoff.

## Recovery and completion

Try an existing eligible independent review leg when another is unavailable.
Respect quotas and Retry-After; deduplicate requests, batch repairs and never
change billing or reviewer admission to escape an unavailable result. Keep the
existing durable owner through passive waits; count actual repair attempts
separately. Pending infrastructure is not missing operator consent or a PASS.

After a repair, check and review the new head. After merge, complete the requested
deployment or runtime verification before retiring the owner. A metadata-only
readiness check establishes access and allowance, not delivery quality.

## Distribution and compatibility

The portable helper and this contract are shared across native, rendered plugin
and registry consumers. Existing human, Judge and Codex helper entry points keep
their contracts; current merge gates authenticate mutable bodies before invoking
the canonical Codex recognizer. A narrow legacy docs-summary entry point may remain installed
for older callers; current primary and late-review gates use the shared helper.
Missing or failing helpers return no passing evidence. Ship the helper alongside
gate changes through existing sync manifests; preserve exclusions. Reverting this
review leg needs no credential, data, sender or application-state changes.
