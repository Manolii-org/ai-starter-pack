---
name: autonomy-follow-through
version: 1.1.3
description: "Shared termination + escalation loop for routine-native autonomy and /watch-pr. Stops on done/blocked_on_human/blocked_on_infra/superseded/gave_up; merges when safe."
type: skill
model: sonnet
data_sensitivity: internal
safety_tier: green
requires_mcp: []
required_entities: []
tools:
  - Bash
  - Read
  - Grep
tags:
  - autonomy
  - follow-through
  - pr-watch
---

# Skill: Autonomy Follow-Through

## Standing Grant Entry

`/proceed` and the exact trusted `<standing-grant>proceed</standing-grant>`
additional-context marker mean the operator has authorised execution of an
**already-clear** plan/session intent without another Green/Amber permission
prompt. They do not authorise new scope, Red actions, guarded-path unfreeze,
cross-entity mixing, or any do-not-touch class in `.claude/commands/proceed.md`.
If intent is missing or ambiguous, terminate `blocked_on_human`; do not guess.
Before PR follow-through, reconcile every session commitment as `DONE`,
`NOT DONE`, or `DEFERRED` with evidence/reason.

## Readiness and review recovery

The author or authorized agent may mark a PR ready once its complete assigned
initial scope and commitment ledger are satisfied, deterministic checks and
user-facing verification pass, required fast checks and secret scanning pass on
the current head, and no demonstrated Critical/P0 or HIGH/P1 defect or
input/security refusal remains. Another person's approval is not a prerequisite to marking ready.
Review workflows may run after readiness. Existing independent current-head
MERGE review, CHANGES_REQUESTED vetoes, guarded paths, kill switches, native
protections and Merge-When-Safe rules below remain unchanged.

Carry existing operator authorization through in-scope repairs and new heads;
do not ask again solely because a hash changed. A finding holds the affected
boundary while diagnosis, repair, verification and independent work continue.
Reproduce and resolve actionable findings, batch each repair round and verify
the new head. Observe existing automated fix owners before editing.

An unavailable reviewer or rejected generated response is unavailable, never
PASS. Preserve the actual result and any earlier finding hold. Respect existing
retry and rejection guidance; never retry a denial unchanged, shop for a passing
verdict, clear hold state by hand or bypass a hook. A draft-save receipt, where
the repository actually implements one, authorizes only that bounded draft save.
This policy does not install checkpoint machinery or grant merge authority.

Keep one real monitoring owner or an accepted durable continuation. Do not claim
background execution from a local note. After merge, complete the parent task's
authorized source/behavior verification and cleanup before declaring completion
and retiring its monitor. Keep each repository's entity, provider, data and
deployment boundaries; a shared policy is not authorization to cross them.

## Ownership after a push or status reply

A push, migration rename, new head SHA, or request to "reply with the new head
SHA" is a progress update within an ongoing delivery task. Send the requested
update and continue the existing review, repair, merge and verification loop.
Only an explicit instruction to stop, hand off, or limit the task to
implementation/publication narrows that scope.

Keep separate commitments for the repair, merge and requested post-merge work.
Completing the repair must not complete the merge commitment or clear the
parent checkpoint. A changed head invalidates old review/check evidence; refresh
the current head and continue under the existing authorization and repair budget.

Retain one continuation owner. Before yielding an unfinished task, record its
repository/PR, full head SHA, remaining commitments, next action, precise blocker
if any, and a verified wake or accepted durable handoff receipt. A SHA, local
checkpoint or proposed owner is not an accepted handoff. Without a working
continuation route, report `blocked_on_infra`; keep the task unfinished and
complete other authorized work. Production approval holds only production.

## Examples

- Valid: "Rename the migration and reply with the new head SHA." During an
  existing delivery task, push the repair, report the SHA, then verify the new
  head, handle review and follow through to merge and requested acceptance.
- Invalid: mark the task done after printing the SHA while its PR remains open.
  An explicit "push only; stop before merge" instead ends at that stated boundary.

## Inputs

- **PR number** (required) — GitHub PR for autonomy output
- **Handoff item id** (optional) — orchestrator task id; maps to producer issue URL
- **Producer issue URL** (optional) — original issue that triggered kickoff

## Pre-Process QA (Ecosystem-Audit Phase 4.0)

Record on the PR before entering outer loop:

1. **Assumptions check** — diff matches stated scope; no unintended files touched
2. **Obvious errors** — no hardcoded secrets, missing imports, syntax errors
3. **Opportunities** — coverage gaps, test blind spots, follow-on scope
4. **Continuity** — session context preserved, memory cache valid, no environmental drift

Post findings as PR comment tagged `<!-- qa-pre-process -->`. Block on P0 errors only.

## Outer Loop

Max repair rounds (default 5; passive CI/review waits do not consume them): read from `config/autonomy-executor-policy.yaml` → `follow_through.max_rearms`.

Bound passive heartbeat scheduling separately from repair attempts. Before
arming a heartbeat, record the existing owner's explicit wait deadline or wake
lease. If it has none, allow at most one one-shot heartbeat for the unchanged
pending operation, then return to the existing subscription or accepted durable
handoff. Do not reset an expired wait or chain new one-shots merely because the
same check or review is still pending. At the bound, persist the pending status,
evidence and actual continuation owner, and suspend this active wait chain;
an infrastructure timeout is `blocked_on_infra`, not a new human-consent request
or PASS. Retain a user-authorized durable owner's existing schedule. If no real
continuation exists, terminate this invocation `blocked_on_infra` and follow
that state's incident/escalation actions below; report monitoring as unavailable
with the pending-work evidence. Resume active work on a substantive new event
or decisive evidence under the existing retry rules.

Use the owning repository's runtime dispatch policy for the authenticated read
loop. Where that policy uses a persistent named `infra` agent, reuse it with the
required model/plane binding; do not create one per wake. Where the runtime uses
a deterministic broker or a batched main-thread read, use that existing route.
An unavailable agent type is not a reason to add a supervisor or stop an
otherwise authorised supported route. Preserve secret-handling and sensitivity
boundaries; a model alias is not proof of its actual provider.

Each round:
1. Fetch PR metadata + CI status + review state + issue threads + the complete
   current label array through that existing authorised route in one bounded
   batch. Keep credentials and raw authenticated payloads out of delegated
   model context; return only the permitted sanitised evidence.
2. Run `check-amber-labels` against that complete label array. A Red result
   first disables any auto-merge request armed by an earlier wake, then vetoes
   all mutation and terminates `blocked_on_human`.
3. Keep the existing PR-activity subscription or accepted durable wake owner as
   the primary signal. For that owner's bounded heartbeat, use `create_trigger`
   with `run_once_at` where supported; in remote sessions, fall back to
   `ScheduleWakeup`, then one supported background wait. Only use a fallback
   that delivers a real wake within the existing owner's policy. If none works,
   skip heartbeat scheduling and retain the actual owner or accepted handoff.
   Never simulate a wake with a blocking sleep or add a competing trigger/poller.
4. Triage events: new review comments, CI failure, merge conflict, branch out of sync
5. Decide: continue | terminal
6. If continue: spawn inline fix or request human decision
7. Count failed repairs, not passive wakes. Pending CI, automated review, reviewer infrastructure outages and duplicate informational events do not consume repair/no-delta budgets or create a human decision. After two failed repairs with the same cause, diagnose instead of repeating unchanged work. Preserve the empty Codex L4 seed exception below.

## Termination Taxonomy

| State | Trigger | Action |
|-------|---------|--------|
| `done` | Every Merge-When-Safe rule below is true, including qualifying review and veto gates | Merge (auto-merge preferred) |
| `blocked_on_human` | A concrete required human approval, authority, design or risk decision | Notify once with the specific decision; ordinary pending automated review stays monitored |
| `blocked_on_infra` | Shared infra (PAT / missing config / session proxy / Retry-After / Fly 502) or unmatched transient | Record/attach via the repo's infra-incident recorder (`infra-incident` label; ecosystems that ship `scripts/infra_incident.py` use it). Dependents stay listed; retries suppressed while the incident is open. One cheap GET probe; agents attach evidence and do not close. `infra_backoff_minutes` applies only when no matching open incident exists. Code/test failures are not this state — they remain visible via required checks. Do not `min(Retry-After, 30)` in hosted jobs. |
| `superseded` | New PR opened for same scope, **or** a Codex L4 seed (`codex/audit-*` / `codex/mesh-*`) still has `changed_files=0` after one wake (or a connector summary with no commit) once an implementing session-prefix PR exists (`cursor/` or `claude/`, never `codex/`) | Close the seed; link the Act PR |
| `gave_up` | `max_rearms` exhausted without a recoverable infra path or a specific human decision to request | Escalate with evidence; Red and other human-decision gates use `blocked_on_human` instead |

**No-delta rule:** `follow_through.no_delta_limit` bounds repeated failed repairs, not passive CI/review waits or duplicate informational wakes. An unavailable review service is infrastructure, not missing execution consent. A required independent human merge approval is a real human hold; readiness needs no such preapproval. Do **not** apply the passive-wait exception to an empty Codex L4 seed: after **one** wake with `changed_files=0`, implement on a session prefix (`cursor/` or `claude/`, never `codex/`) per the orchestrator repo's `docs/runbooks/daily-leftover-act.md`. Only once that implementation PR actually exists, terminate the seed `superseded` and link the replacement PR. Until then, keep the seed open with the implementation work outstanding. A native `chatgpt-codex-connector` summary is not an implementation. Kickoff has no empty-seed closer.

**Named required check:** Combined commit `status.state=success` is not CI green. A required check (e.g. a repo's anchor job) must be check-run `completed`/`success` on the **current head SHA**. `action_required` with zero jobs is not green.

Two merge paths (do **not** call these Gate A / Gate B — those names are Daily 1 `AUDIT_AUTOPILOT` vs Codex kickoff):

- **Trusted-packet squash** is leftover-Act only (`docs/runbooks/daily-leftover-act.md`): trusted `automated/sync-*` after `verify_sync_pr.py --mode merge`, or a stale green-intent fix PR whose product diff already exists. Trio green on the current SHA is enough. Advisory jobs (PR Assessment, Code Quality, Dismiss-stale, Fleet Scale Wake) are not that merge gate. `/watch-pr` never inherits leftover-Act trusted-packet squash.
- **Implement Merge-When-Safe** (this skill's default, including empty-seed Act PRs): the anchor is a floor. Qualifying current-head review is still required. Anchor-green does not waive review, labels, or `CHANGES_REQUESTED`.

## Escalation

Emit one HTML comment in PR:
```html
<!-- autonomy-terminal:v1
{
  "state": "blocked_on_human",
  "reason": "required independent human merge approval is unavailable to automation",
  "evidence": ["ci_passed", "threads_open"],
  "owner_hint": "reviewer_github_login",
  "handoff_item_id": "uuid-from-input",
  "budgets_consumed": {
    "rearms": 3,
    "implement_spawns": 1,
    "infra_backoff_attempts": 0
  }
}
-->
```

Notify once per terminal state via comment mention; do not re-arm after escalation.

## Merge-When-Safe Rules

Merge only when ALL true:
- Named required checks green on the current head SHA (not combined commit status; not "every advisory job")
- Ignore `Fleet Scale Wake` and production migration-ledger drift on PRs: both are
  advisory infrastructure signals, never PR-diff defects, and require no verify/comment ritual.
- At least one independent hosted source review covers the current head SHA.
  Accept an independent human or allowlisted bot `APPROVED` review, an existing
  canonically verified clean Judge or native Codex review, or a completed
  CodeRabbit clean summary validated by `scripts/ci/review_evidence.py --comments`
  against authenticated GitHub data for this exact repository, PR and full head.
  See the skill's companion `review-evidence.md`. A raw review count, empty acknowledgement,
  ordinary comment, skipped/rate-limited attempt, stale summary or incomplete
  assessment is never approval. Any current-head `CHANGES_REQUESTED` vetoes merge.
- Review threads resolved (no open conversations)
- No current-head review has state `CHANGES_REQUESTED`. A resolved conversation
  does not dismiss or override a formal blocking review.
- The automated gate verifies one of the qualifying semantic review legs above;
  this is the same review requirement, not an additional reviewer or approval.
- Guarded work meets the owning repository's authority and enforcement rules.
  Reuse existing task authorization and its documented guard/audit mechanism;
  preserve explicit restrictions and blocking guard decisions. A filename match
  alone does not establish a new human decision.
- Verify the controls actually consumed by the selected executor and merge
  controller, using the owning repository's documented configuration and
  defaults. For a controller that consumes `AUTO_MERGE_ENABLED`, verify that
  control; do not add a separate `autonomy.follow_through.enabled` requirement
  merely because this shared skill previously named it. Executor switches
  apply only to their actual consumers. An absent value means enabled only
  where the consuming implementation explicitly defines that default.
  Unreadable control state is unavailable, not disabled or missing user consent.
  Hold the affected mutation unless the existing trusted controller has a
  verified applicable job-level decision and explicitly supports that fallback.
  Never invent a flag, broaden credentials, spoof a hosted context or bypass
  a denied permission to satisfy this check.
- Immediately before enabling auto-merge or merging, refresh the complete label
  array and rerun `check-amber-labels`. A Red result disables any armed
  auto-merge request and terminates `blocked_on_human` without further mutation.
- Auto-merge preferred when supported. A normal explicit expected-head merge
  or required native merge queue uses the same review, authorization and CI gates;
  lack of auto-merge support adds no human-approval requirement. Refresh head,
  labels, checks, reviews and unresolved findings immediately before mutation.
  Preserve remote protections, explicit human-merge holds and every review veto.
  Never approve the implementing work under the author's identity or use admin
  bypass to substitute for independent review.

Reviewer quota, latency or unavailability is a monitored wait or infrastructure
hold under the existing owner, not missing operator consent. Use another existing
independent review leg when eligible; respect Retry-After and deduplicate requests.
Only a concrete authority/risk decision or explicit human-merge policy requires a
human handoff. Do not turn passive review waits into exhausted repair budgets.

Never merge Red-tier or guarded paths outside the operator's explicit authorization.
Never treat leftover-Act trusted-packet squash as a `/watch-pr` shortcut.

## Anti-Loop Budgets

From `config/autonomy-executor-policy.yaml`:
- `follow_through.max_rearms` — max failed repair/re-arm cycles (default 5); passive CI/review waits do not count
- `follow_through.no_delta_limit` — consecutive failed repair attempts before diagnosis (default 2); passive waits do not count
- `follow_through.infra_backoff_minutes` — ordered cooldown schedule for
  unmatched transients only; skip while an open `infra-incident` matches.
  Exhausting the list terminates `gave_up`. Open incidents suppress dependent
  retries until the platform owner closes them.
- `follow_through.max_implement_spawns_per_run` — max inline fix spawns (default 3)

Enforce all budgets; escalate on exhaust.

## Explicit Design Notes

This skill is the **SSOT for stop/escalate logic** across autonomy kickoff flows. `/watch-pr` defers to these terminal rules when invoked inside orchestrated autonomy. No looping outside these bounds; no silent retries. Leftover Daily 1/2 Act vs Codex L4 split and shared G3 incident recording (`infra-incident` label) live in the orchestrator repo's `docs/runbooks/` + `scripts/` — treat them as upstream references when they are not shipped here.

## Status Report

When reporting loop progress, keep to ≤150 words. Include: PR link, terminal state (if any), last event type, round count, next action (continue/escalate/merge).
