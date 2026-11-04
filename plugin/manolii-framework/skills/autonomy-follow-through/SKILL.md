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

## Exact production candidates

Before requesting production approval, prepare the complete subject described in
`docs/agent-release-experience.md`. The portable `scripts/lib/release_subject.py`
helper binds source/artifact, ordered migrations/compatibility, verification,
trusted policy/workflow revisions and exact permitted recovery. A changed subject,
rejected/cancelled decision or expired approval retains the production hold.

Scope comparison is evidence only. A local document, actor string, PR-modified
policy or automatically dispatched workflow cannot grant production authority.
Use the owner's authenticated native approval and trusted production controller.
Retain pre-merge acknowledgements until every relevant writer is demonstrably
confined. If the helper or owning adapter is absent, record adoption as pending;
do not claim this instruction installed a boundary or resumed an executor.

## Standing Grant Entry

`/proceed` and trusted `<standing-grant>proceed</standing-grant>` additional-context authorize an **already-clear** intent without another Green/Amber prompt, never new scope, Red, guarded-path unfreeze, cross-entity mixing or `.claude/commands/proceed.md` do-not-touch classes. Missing/ambiguous intent → `blocked_on_human`; never guess. Reconcile every session commitment as `DONE`, `NOT DONE` or `DEFERRED`, with evidence/reason.

## Readiness and review recovery

The author or authorized agent may mark a PR ready once its complete assigned initial scope and commitment ledger are satisfied, deterministic checks and user-facing verification pass, required fast checks and secret scanning pass on the current head, and no demonstrated HIGH defect or input/security refusal remains. Another person's approval is not required for readiness; review workflows may follow it. Independent current-head MERGE review, `CHANGES_REQUESTED`, guards, kill switches, native protections and Merge-When-Safe remain mandatory.

Carry existing authority through in-scope repairs/new heads without renewed consent. Findings hold affected boundaries, not diagnosis/repair/verification/independent work. Reproduce/resolve findings, batch repairs, verify new heads; respect existing automated repair owners.

Unavailable/rejected review is never PASS: preserve results/prior holds and retry guidance. Never retry denial unchanged, verdict-shop, hand-clear holds or bypass hooks. Only an actually implemented draft-save receipt permits its bounded save; this policy installs neither checkpoint machinery nor merge authority.

Keep one real owner/accepted durable continuation, never claim background work from notes. Complete authorized parent verification/cleanup before declaring done/retiring monitor. Preserve each repository's entity/provider/data/deployment boundaries; shared policy never grants cross-boundary authority.

## Ownership after a push or status reply

Pushes, migration renames, new SHAs and requests to report them are progress: reply, continue review/repair/merge/verification. Only explicit stop/handoff/implementation-publication limits narrow scope. Keep repair/merge/post-merge commitments separate; repair never clears parent checkpoint. Changed heads invalidate old checks/reviews; refresh within existing authority/budgets.

Before yielding, retain repository/PR/full head, unfinished commitments, next action/blocker and verified wake/accepted durable handoff receipt. A SHA, local checkpoint or proposed owner is not handoff. Without continuation, report `blocked_on_infra`, retain unfinished work, complete unaffected tasks. Production approval holds only production.

## Examples

“Rename and report SHA” continues delivery; “push only; stop before merge” ends at that boundary. Never declare done while its PR remains open.

## Inputs

Required PR number; optional handoff item id (maps to producer issue URL) and producer issue URL.

## Pre-Process QA (Ecosystem-Audit Phase 4.0)

Before loop, post a PR comment tagged `<!-- qa-pre-process -->`: assumptions/scope; obvious errors (secrets/imports/syntax); opportunities (coverage/blind spots/follow-ons); continuity (context/cache/environment). Block only P0 errors.

## Outer Loop

Policy: `config/autonomy-executor-policy.yaml`; max repair rounds default 5. Passive CI/review waits consume none.

Bound passive heartbeats separately: record owner's explicit wait deadline/wake lease. Without one, permit only one one-shot for an unchanged pending operation, then existing subscription/accepted handoff. Never reset expired waits/chain one-shots. At bound, persist pending status/evidence/actual owner, suspend active wait chain; timeout means `blocked_on_infra`, never consent/PASS. Retain user-authorized durable schedules. Without real continuation, terminate this invocation blocked_on_infra, report monitoring unavailable with pending-work evidence, and perform incident/escalation actions. Resume active work on substantive events/decisive evidence under retry rules.

Use owning runtime dispatch policy: reuse its named `infra` agent/model-plane binding, deterministic broker or batched main-thread route. Unavailable agent types never justify new supervisors/stopping supported authorized routes. Preserve credential/sensitivity boundaries; aliases do not prove providers.

Each round:
1. Batch authorized PR/CI/review/thread/complete-label reads; keep credentials/raw authenticated payloads out of delegated model context; return permitted sanitized evidence.
2. `check-amber-labels` on complete labels: Red disables previously armed auto-merge, vetoes all mutation and terminates blocked_on_human.
3. Primary signal: existing PR subscription/accepted durable wake owner. Bounded heartbeat: `create_trigger`/`run_once_at`; remote fallback `ScheduleWakeup`, then supported background wait, only if a real policy-compliant wake results. Otherwise skip scheduling, retain actual owner/handoff. Never simulate wake with blocking sleep/competing trigger/poller.
4. Triage reviews/CI/conflicts/drift; continue/terminate, inline repair or concrete human decision.
5. Count failed repairs only, not passive/duplicate waits or reviewer outages. Diagnose after two same-cause failures; retain empty-seed exception.

## Termination Taxonomy

| State | Trigger/action |
|-------|----------------|
| `done` | Every Merge-When-Safe rule below true, including review/vetoes; merge, preferring auto-merge. |
| `blocked_on_human` | Concrete required human approval/authority/design/risk decision; notify once. Ordinary automated review stays monitored. |
| `blocked_on_infra` | Shared PAT/config/proxy/Retry-After/Fly502 or unmatched transient: owning infra-incident recorder (`scripts/infra_incident.py` where shipped), label infra-incident, retain dependents. One cheap GET; attach evidence, never close. Open incidents suppress retries; otherwise infra_backoff_minutes. Code/test failures remain required-check failures. Never shorten hosted Retry-After to 30 seconds. |
| `superseded` | New PR opened for the same scope, or empty L4 exception below; close seed only once its replacement PR exists, link Act PR. |
| `gave_up` | max_rearms exhausted without recoverable infra/specific human decision; escalate evidence. Red/human gates use blocked_on_human. |

**No-delta rule:** bounds failed repairs, never passive/duplicate waits. Unavailable review is infra, not consent; required independent human merge approval is real, readiness needs no such preapproval. Empty `codex/audit-*`/`codex/mesh-*` L4 `changed_files=0` after one wake (including connector summary/no commit): implement on `cursor/` or `claude/`, never `codex/`, per `docs/runbooks/daily-leftover-act.md`. Keep seed open until implementation PR exists, then supersede/link it. Connector summaries aren't implementation; kickoff has no empty-seed closer.

**Named required check:** current-head completed/success required anchor, never combined status or action_required/zero jobs.

Do not call these Gate A/B (Daily1 AUDIT_AUTOPILOT versus kickoff):
- **Trusted-packet squash**: leftover-Act only; verified automated/sync-* (`verify_sync_pr.py --mode merge`) or stale green-intent fix already in product. Current-SHA trio suffices; advisory Assessment/Quality/Dismiss-stale/Fleet aren't its gate. `/watch-pr` never inherits leftover-Act trusted-packet squash.
- **Implement Merge-When-Safe**: default, including empty-seed Act; anchor only floors review/labels/vetoes, never waives them.

## Escalation

Emit one PR HTML comment; notify/mention once per terminal state, never re-arm:
```html
<!-- autonomy-terminal:v1
{"state":"blocked_on_human","reason":"required independent human merge approval is unavailable to automation","evidence":["ci_passed","threads_open"],"owner_hint":"reviewer_github_login","handoff_item_id":"uuid-from-input","budgets_consumed":{"rearms":3,"implement_spawns":1,"infra_backoff_attempts":0}}
-->
```

## Merge-When-Safe Rules

ALL must hold:
- Named required checks pass exact head, never combined status/every advisory job. Ignore advisory Fleet Scale Wake and production migration-ledger drift: neither PR-diff defects nor verification/comment rituals.
- Independent hosted exact-head source review: human/allowlisted bot APPROVED, canonically verified clean Judge/native Codex, or completed clean CodeRabbit summary validated by `scripts/ci/review_evidence.py --comments` against authenticated exact repo/PR/full SHA. See companion `review-evidence.md`. Counts, empty acknowledgements, ordinary/skipped/rate-limited/stale/incomplete publications never qualify. Current-head CHANGES_REQUESTED vetoes merge; resolve threads. Resolved conversations don't dismiss formal vetoes. Automated semantic-leg verification is this same requirement, not another approval.
- Guarded work meets owning authority/enforcement: reuse existing authorization and documented guard/audit mechanism, preserve explicit restrictions/blocking decisions. A filename alone adds no human decision. Never merge Red/guarded work outside explicit authorization.
- Verify controls actually consumed by selected executors/controllers and documented defaults. AUTO_MERGE_ENABLED applies to its consumers; never invent autonomy.follow_through.enabled merely from this skill. Absence enables only by default. Unreadable means unavailable, not disabled/missing consent: hold unless trusted controller explicitly supports verified applicable job-level fallback. Never invent switches, broaden credentials, spoof hosted context or evade denial.
- Immediately refresh complete labels/check-amber-labels before merge/auto-merge. Red disables armed auto-merge and terminates blocked_on_human without further mutation.
- Prefer auto-merge; explicit expected-head/native queue uses same review/authorization/CI gates; unsupported auto-merge adds no approval requirement. Refresh head/labels/checks/reviews/findings immediately. Preserve remote protections, human-merge holds/vetoes; never self-approve/admin-bypass.

Reviewer latency/quota/unavailability stays monitored/infra under existing owner; use eligible independent legs, respect Retry-After/deduplicate. Human handoff requires concrete authority/risk/policy; passive waits never exhaust repair budgets. Never give /watch-pr leftover-Act shortcut.

## Anti-Loop Budgets

Enforce follow_through.max_rearms (default 5), follow_through.no_delta_limit (default 2; consecutive failed repair attempts before diagnosis), follow_through.infra_backoff_minutes (ordered unmatched-transient cooldown; open incident suppresses retries until platform owner closes; exhaustion→gave_up) and follow_through.max_implement_spawns_per_run (default 3). Escalate exhaustion; passive waits consume none.

## Explicit Design Notes

Stop/escalation SSOT for orchestrated /watch-pr/kickoff: no unbounded loops/silent retries. Daily1/2 versus L4 and shared G3 incident recording are owning docs/runbooks/ + scripts/ references where absent here.

## Status Report

≤150 words: PR link, terminal state if any, last event, round count, next action (continue/escalate/merge).
