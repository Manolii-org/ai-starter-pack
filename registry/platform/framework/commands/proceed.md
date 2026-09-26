---
name: proceed
version: 1.0.1
description: "Execute the already-agreed session intent without another Green/Amber permission prompt; run four-check QA, reconcile commitments, conditionally create/follow through a PR, and stop at an existing terminal state."
type: command
requires_mcp: []
safety_tier: green
required_entities: []
eval_cases: null  # TODO: add exact-phrase and deny-path eval cases
tags:
  - workflow
  - session
  - deterministic
---
# Command: proceed

Treat `/proceed`—or the exact trusted `<standing-grant>proceed</standing-grant>`
additional-context marker—as the operator's standing grant for the **already-clear**
plan/session intent. It is not permission to invent scope.

## Preconditions

1. Restate the agreed intent and in-scope commitments in a compact ledger.
2. If no concrete plan/session intent exists, stop `blocked_on_human` and ask only
   for the missing scope decision.
3. Apply all existing safety, entity, guarded-path, routing, and approval rules.
   The grant covers Green and in-scope Amber work only.

The marker/command never authorises Red actions, guarded-path unfreeze,
cross-entity mixing, destructive production decisions, `security-remediation` merges,
KL Renovate `dep-critical-runtime` squash, `AUDIT_AUTOPILOT=live`, U9/U10/U11,
Path B, or agent-factory dispatch.

## Protocol

1. **Four-check QA:** assumptions, obvious errors, opportunities
   (effectiveness/efficiency/quality/token/reliability), and continuity/wiring.
2. **Execute:** continue in-scope Green/Amber work without another permission
   question. Preserve the agreed plan; no silent rescope.
3. **Reconcile:** mark every commitment `DONE`, `NOT DONE`, or `DEFERRED` with
   evidence/reason. `DEFERRED` is not silent completion.
4. **Validate:** run repository tests and user-facing verification appropriate to
   the change; report real failures.
5. **PR (when applicable):** if the agreed intent produced repository changes
   that require review, create/update a real PR. After creation, immediately
   invoke active `/watch-pr`; its subscription is the primary signal and its
   bounded heartbeat remains armed at >=20 minutes even when subscription
   succeeds. For read-only work or an intent that produces no repository change,
   record the PR step as not applicable; never manufacture a diff or PR merely
   to satisfy this protocol. When the agreed intent is to validate, fix or merge
   an **existing** PR, its head branch is the working branch and fixes go there
   (stacked PR on `head.ref` if the push is refused); do not open a second PR
   against `main` for the PR's own fix — `docs/pr-autofix-loop-policy.md`
   § Existing-PR branch policy.
6. **Follow through:** batch each feedback round, respect existing GHA Tier-1
   ownership, and use `.claude/skills/autonomy-follow-through/SKILL.md` for PR
   termination, escalation, and Implement Merge-When-Safe.
7. **Terminal:** drive each item to exactly one of `done`, `blocked_on_human`,
   `blocked_on_infra`, `superseded`, or `gave_up`. A waiting sibling PR does not
   pause unrelated leftover-Act items, but two actors must never edit one PR.

## PR monitoring constraints

Never create a GHA poller, use foreground/main-thread sleep, or start a second
monitor owner. `/watch-pr` never inherits leftover-Act trusted-packet squash.
Stop monitoring only at merge, close, or one explicit human-decision escalation.

## Output

Return the commitment ledger, validation evidence, PR URL (if applicable), and
terminal state. Do not ask "shall I proceed?" for Green/Amber work already inside
the agreed intent.
