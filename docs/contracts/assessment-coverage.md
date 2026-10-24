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

## Staged SAST helper source

The SAST evidence helper and generated copies are dormant source. Existing
assessment workflows remain unchanged. Local synthetic helper tests do not
establish hosted scanner completion or authenticated producer provenance. Later
workflow wiring needs an accepted helper-containing base or trusted `pack_ref`,
plus fresh exact-head reviews and checks. This slice does not resolve existing
assessment failures or authorize release, consumer installation, deployment or
protected-role activation.
