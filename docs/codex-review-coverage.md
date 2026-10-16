# Codex subscription review coverage

The code-review and selective security-diff workflows use ChatGPT subscription
authentication. Subscription allowances still apply; there is no unlimited-use
claim and no provider API-key fallback.

Provision `DOPPLER_TOKEN_CODEX` as a read-only Actions secret that can retrieve only
`CODEX_SUBSCRIPTION_AUTH_JSON` from the dedicated `master/prd_codex` configuration.
Never substitute a general workspace token. Configure the authoritative credential
rotation outside PR jobs; ephemeral review credentials are removed before reporting.

Existing comment markers and advisory model job names remain stable. The additional
`codex-review-receipt:v1` marker records repository, PR, full base/head SHA, lane,
run URL, billing mode, execution state and reason. It contains no model text.

| State | Meaning |
|---|---|
| `completed` | Model verdict or valid security findings output was produced |
| `exempt` | Resolved empty diff or a security diff outside sensitive path classes |
| `blocked_credentials` | Dedicated credential missing or fetch failed |
| `blocked_quota` | Provider quota prevented review |
| `failed_range` | Diff range could not be resolved |
| `failed_install` / `failed_scan` | Reviewer infrastructure did not complete |
| `failed_output` | Verdict/findings missing, malformed or withheld |
| `cancelled` | Scan was cancelled |

Completion is separate from a safety judgment. Findings remain advisory and must
be triaged under the repository's existing reviewer/security policy. A completed
scan with findings does not permit ignoring them. A missing findings file is never
a clean scan.

`Codex review coverage` and `Codex security coverage` fail without a current-head
completed or exempt receipt, or authenticated native completion evidence for the same
head. Code and security evidence are evaluated separately; a quota notice cannot
satisfy either lane. Adopt them as required contexts only after a genuine
native smoke on each intended branch. Merge-queue repositories need merge-group
wiring before requiring these PR-only contexts. Preserve existing check names,
approval counts, reviewer vetoes, human holds, deployment gates and promotion lanes.
Do not require the advisory model job itself.

Ready same-repository PRs by recognized collaborators/contributors or explicitly
listed installed bots can run the credential-bearing jobs. Drafts cancel/skip
model work. Forks, excluded authors and `CODEX_REVIEW_ENABLED=false` do not produce
a code completion receipt; they require an explicit external review/recovery path,
not an assumption that the skipped job reviewed the code.

Security review is selected conservatively from changed sensitive paths: auth,
tenant/RLS/schema, secrets, CI/agent permissions, network/crypto, deployment/audit
boundaries and dependency manifests. Resolved ordinary diffs receive a recorded
exemption; unknown/unresolved ranges fail closed. This is a path-based selection
policy, not proof that other files can never contain a security defect. Existing
static/dependency/secret checks still apply on all their configured changes.

Run the regression contracts with:

```sh
python3 -m pytest tests/test_codex_review_receipt.py tests/test_codex_review_workflows.py
```

Roll back a reviewed implementation by reverting its PR. Roll back completion
enforcement through the owner's reviewed branch policy, preserving the other
required checks. Never buy quota, enable API billing, expose raw artifacts, or
weaken another review gate as an automatic recovery action.

The recovery workflow listens only for native Codex review events. When a late
review arrives, it rechecks the live head and reruns only the failed coverage job.
It never reruns the model job, publishes a forged status, or retries exhausted
quota. Install this workflow on the default branch for event-driven recovery.
Before requiring coverage, verify that the repository permits Actions to rerun
its own failed jobs and exercise a real late-native-review recovery.


The classifier, credential redactor, and receipt writer execute from the immutable
PR base SHA in an isolated Python process outside the checkout. A PR's modified
helper is scan input only. First-time adoption without that base helper fails
before credentials are fetched: bootstrap with native code/security review and
required human workflow review, then merge the reviewed helper before CLI use.
Helper updates take effect for subsequent PRs after their reviewed base includes
them. Same-repository workflow authors retain the existing ability to edit the
workflow itself; this does not substitute for workflow review or branch controls.
Merge, deployment, release, hold, guard and policy paths require security review.
