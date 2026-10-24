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
