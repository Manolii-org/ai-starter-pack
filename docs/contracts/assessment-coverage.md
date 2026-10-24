# Assessment coverage and response compatibility

Review completion requires every role selected by the trusted classifier, not
merely successful workflow jobs or an empty findings list. The Judge receives
both phase selections from job outputs. Runner-written artifacts bind repository,
PR head/base, run and attempt to the launcher event; model-provided metadata is
overwritten. Missing roles, missing control inputs, stale or mixed bindings,
malformed artifacts and explicit skips keep the verdict incomplete. These local
bindings rely on the existing trusted-base restoration, cleared candidate directory
and run-scoped artifact transfer; they are not authenticated provider receipts or
an authorization mechanism for arbitrary callers. Local legacy callers retain
their existing interface. Old hosted artifacts without these bindings cannot
satisfy the new hosted coverage contract.

The systems-consistency API renderer removes only its marked interactive setup.
Checks, paths, findings, scope and supplied input remain intact. Interactive
sessions retain their instructions. Output exhaustion remains incomplete even if
the returned JSON parses. Existing limits stay 2,400 tokens then one 4,448-token
slot, at most four HTTP attempts per slot, and 240 seconds per request.

The advisor's existing inline-fallback adapter now replaces generation metadata
alongside text. It carries observed completion/truncation, model, response ID and
replacement usage; unavailable values stay unknown. Primary hidden generation
metadata is cleared rather than attributed to the replacement. Usage describes
the replacement generation, not aggregate physical-call cost. Unsupported or
ambiguous envelopes fail closed. Typed envelopes revalidate nested content/usage
before replacing the original. This source correction neither enables fallback
nor changes its route, provider, call limits or deployment. Mocked envelopes and
Pydantic serialization are compatibility evidence, not live LiteLLM accounting or
deployment acceptance.

## Adoption and recovery

1. Qualify and adopt canonical source plus generated plugin/registry copies under
   ordinary owning checks. The assessment executes its trusted PR-base runtime;
   changing runner code at a candidate head does not test that runtime in-place.
2. Preserve direct-route admission. A protected-role skip is not a provider
   outage and must not become an ordinary proxy review. Existing estate proxy
   qualification covers only `security-boundary-test`; it does not establish
   `security-deep-dive` coverage or activate either role. A complete approved
   producer/route for each required role remains a separate prerequisite.
3. After source adoption and protected-role qualification, the coordinator may
   reconcile the exact current head and select one normal recovery run. Retain
   old cancellations, skips and failures. Never infer coverage from green exits.

Rollback reverts the owning source commit and its generated outputs together.
It does not change credentials, policy admission, deployment or review history.
No rollout should reinterpret old incomplete artifacts as successful evidence.

## SAST producer completion and adoption

Both direct and reusable assessment workflows execute the same
`scripts/lib/sast_evidence.py` producer before accepting SAST input. The direct
job loads that helper from the immutable PR base; the reusable job loads only
that file from its existing trusted `pack_ref`. Neither executes a candidate's
normalizer. The scanner checkout is the exact PR head, with every tracked
regular file checked against its Git blob before and after scanning. Redirected,
missing, changed or unsupported source objects fail closed. Runtime restoration
for model jobs remains separate and does not alter the bytes being scanned.

The old pinned Semgrep action does not declare the `output` or `json` inputs
previously supplied by these jobs. The producer uses the supported Semgrep JSON
CLI with the existing `auto` configuration, plus the existing recursive Bandit
configuration. It observes scanner versions, return codes, report hashes,
reported targets/errors and immutable configuration-file content identities.
The resolved remote `auto` rule-set identity remains unknown; this evidence does
not claim immutable external rules, whole-repository language coverage or
complete data-flow analysis. Scanner package installation remains CI-local and
uses a job-temporary virtual environment and no model credential; shared runner
Python dependencies are not modified. The existing 15-minute SAST job limit is unchanged.

An empty findings list is clean input only when both applicable scanner reports
are structurally complete, have no execution/report errors and record observed
targets. Exit 1 with actual findings is a findings result, not an execution
failure. Missing, malformed, truncated or partial reports, unknown versions,
source mismatches, execution failure and cancellation remain incomplete. Valid
normalized findings before and after a malformed entry are retained; that entry
still invalidates completion. Repeated legacy finding IDs retain multiplicity
and position/content accounting. Zero retained findings on an incomplete record
does not mean zero scanner findings. No scanner is retried by this producer.

Bandit with no immutable Python source is explicitly unexecuted/skipped, not a
successful Python scan. The existing empty-diff SAST job skip remains; consumers
validate that the immutable base...head diff really is empty. They reject other
skipped required SAST jobs. Depth-none classification keeps its existing
separate workflow path.

The SAST job uploads normalized findings and a completion sidecar. All three
consumers clear committed SAST scratch files, download the producer's exact
artifact ID within the current run, and validate its source/run/attempt and
runtime binding against trusted producer job-output digests **before** specialist,
broad or Judge execution. Only then are the unchanged findings bytes exposed at
the compatible `.ai/sast-findings.json` path. Existing upstream failure/cancellation
and selected-role guards remain mandatory. Failed validation cannot fall through
to a clean Judge review or model invocation.

These hashes and local checks are not independent authentication. Authority is
the existing accepted workflow/runtime, runner event controls, producer job
outputs and run-scoped GitHub artifact transfer together. Model fields, candidate
files, caller-provided hashes or a standalone CLI cannot establish that authority.
The sidecar is not a protected-role receipt, scanner effectiveness proof or an
export approval. Live deep-dive dispatch and protected-role admission stay dormant.

Adopt the helper and workflow changes together. A trusted base or reusable
`pack_ref` missing the helper must fail, never execute a candidate substitute.
Source review of the adoption PR is not an in-place execution test of its new
trusted-base runtime. Rendered/plugin/registry helpers are generated from the
canonical file; the reusable hydration list includes its consumer dependency.
Rollback reverts these source and generated changes together; old findings-only
artifacts cannot satisfy this new completion protocol. No release tag, consumer
installation, proxy deployment or role activation follows from source integration.

## Security deep-dive source readiness

The provider-free v1 builder/parser in `scripts/run-broad-agents.py` binds complete
supplied SAST bytes, repeated legacy finding IDs with position/content entry IDs,
full base/head diff and complete immutable old/new blobs for every changed and requested source path. It
reuses the existing Judge privacy screen. It refuses missing or sensitive evidence
and serialized input over the existing transport ceiling instead of truncating.
Caller-supplied SAST/repository bindings and content hashes establish consistency,
not authenticated producer identity or completeness of the requested source scope.
The producer must still establish SAST provenance; reviewers must explicitly
identify missing data-flow/validation/exploitability dependencies. Every entry
requires a promotion, documented dismissal or incomplete disposition. Thresholds
and cited evidence are checked without claiming the reasoning is effective.

These functions are deliberately not wired into live dispatch, the protected
adapter or Judge acceptance. Their provisional output cannot satisfy the existing
candidate/receipt schema. No credential, provider or network call is made by
preparation. Source tests do not qualify a live reviewer, physical-call cost or
any deployed SDK. Existing selected-role coverage, `first_party`, direct/client
admission and the boundary-only estate qualification allowlist remain unchanged.
Both security roles require accepted task/producer/route evidence before recovery;
`shell-security` remains required when selected, and neither it nor `sast-scan`
is qualified by this preparation. Rollback reverts these canonical/generated
source changes together and does not reinterpret old incomplete artifacts.

The dormant `build_security_deep_dive_requests` and
`parse_security_deep_dive_responses` interfaces adapt this same packet/parser to
the existing independent-leg Messages shapes. Both legs receive the entire same
packet and the canonical v1 API instructions. Transport bytes, initial output
slot and timeout retain the existing bounds; supplied capacities require trusted
launcher evidence. Responses are parsed separately, retaining disagreement,
dismissals, missing context and escalation. Missing, invalid, truncated or unknown
observations remain incomplete. Physical attempts and provider execution remain
unknown; request hashes are content bindings, not invocation receipts.
Disagreement compares disposition, likelihood and missing-context state; prose
variation is retained in full under each leg. Reasoning agreement remains unknown.

There is no production dispatcher or retry in this adapter. Offline tests inject
a dispatcher outside the production module. Live selectors, workflows,
boundary-only admission and Judge receipts are unchanged. Future live wiring
still requires trusted launcher/SAST/source provenance, complete source scope,
accepted capability/policy for every selected role, permitted routes and actual
receipt behavior, plus a fresh coordinated recovery identity with prior uncertain
outcomes reconciled. These source interfaces establish none of those gates.
