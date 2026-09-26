---
name: autonomy-follow-through
version: 1.0.7
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

Max rounds (default 5): read from `config/autonomy-executor-policy.yaml` → `follow_through.max_rearms`.

Before the first round, dispatch one persistent opus-pinned named `infra` agent
with `model=` omitted to own the bounded authenticated read loop. Reuse that
same agent for every wake; do not create an N-call credential-bearing fan-out.
The deterministic broker is preferred when it covers the operation.

Each round:
1. Ask the persistent `infra` agent to fetch PR metadata + CI status + review
   state + issue threads + the complete current label array in one credential-
   bearing batch. The skill receives only its sanitised receipt; it never
   carries `GH_TOKEN`, auth headers, or raw authenticated payloads through the
   Sonnet execution path.
2. Run `check-amber-labels` against that complete label array. A Red result
   first disables any auto-merge request armed by an earlier wake, then vetoes
   all mutation and terminates `blocked_on_human`.
3. Emit heartbeat via `create_trigger` (prevents zombie detection)
4. Triage events: new review comments, CI failure, merge conflict, branch out of sync
5. Decide: continue | terminal
6. If continue: spawn inline fix or request human decision
7. No-delta detection: 2 consecutive wakes with no new changes → force terminal (state=`blocked_on_human`), **except** empty Codex L4 seeds (next section)

## Termination Taxonomy

| State | Trigger | Action |
|-------|---------|--------|
| `done` | Every Merge-When-Safe rule below is true, including qualifying review and veto gates | Merge (auto-merge preferred) |
| `blocked_on_human` | Waiting for review / decision | Notify once, escalate |
| `blocked_on_infra` | Shared infra (PAT / missing config / session proxy / Retry-After / Fly 502) or unmatched transient | Record/attach via `scripts/infra_incident.py` (`infra-incident` label). Dependents stay listed; retries suppressed while the incident is open. One cheap GET probe; agents attach evidence and do not close. `infra_backoff_minutes` applies only when no matching open incident exists. Code/test failures are not this state — they remain visible via required checks. Do not `min(Retry-After, 30)` in hosted jobs. |
| `superseded` | New PR opened for same scope, **or** a Codex L4 seed (`codex/audit-*` / `codex/mesh-*`) still has `changed_files=0` after one wake (or a connector summary with no commit) once an implementing session-prefix PR exists (`cursor/` or `claude/`, never `codex/`) | Close the seed; link the Act PR |
| `gave_up` | `max_rearms` exhausted without a recoverable infra path or a specific human decision to request | Escalate with evidence; Red and other human-decision gates use `blocked_on_human` instead |

**No-delta rule:** 2 consecutive wakes with zero new changes → auto-terminal (state=`blocked_on_human`, reason="no progress detected"). Do **not** apply this to an empty Codex L4 seed: after **one** wake with `changed_files=0`, implement on a session prefix (`cursor/` or `claude/`, never `codex/`) per `docs/runbooks/daily-leftover-act.md` and terminate the seed `superseded`. A native `chatgpt-codex-connector` summary is not an implementation. Kickoff has no empty-seed closer.

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
  "reason": "2 wakes no-delta; waiting for review",
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
- At least one qualifying GitHub review exists on the current head SHA. A qualifying review is an independent human `APPROVED` review, or an allowlisted reviewer-bot `APPROVED` review that is still current-head and is not superseded by a later `CHANGES_REQUESTED` review. Bot comments and non-approving review states do not satisfy this rule. The current allowlist is `chatgpt-codex-connector[bot]` (native Codex review) plus the reviewer bots in `config/reviewer-bots.json`. The merge still fails closed if any current-head review has `CHANGES_REQUESTED`.
- Review threads resolved (no open conversations)
- No current-head review has state `CHANGES_REQUESTED`. A resolved conversation
  does not dismiss or override a formal blocking review.
- The automated gate has a qualifying, non-blocking semantic review tied to the
  current head SHA. A mere unclassified comment is not a qualifying signal.
- Not guarded path (verify `.ai/guards.json`)
- Policy kill switches enabled (verify the `autonomy.follow_through.enabled` policy flag)
- Immediately before enabling auto-merge or merging, refresh the complete label
  array and rerun `check-amber-labels`. A Red result disables any armed
  auto-merge request and terminates `blocked_on_human` without further mutation.
- Auto-merge preferred when branch protection allows. If the bot-review gate
  cannot arm, manual merge requires an independent human `APPROVED` review tied
  to the current head SHA; comments and non-approving review states do not
  qualify. Otherwise terminate `blocked_on_human`.

Never merge Red-tier or guarded paths without human approval.
Never treat leftover-Act trusted-packet squash as a `/watch-pr` shortcut.

## Anti-Loop Budgets

From `config/autonomy-executor-policy.yaml`:
- `follow_through.max_rearms` — max re-arm cycles (default 5)
- `follow_through.no_delta_limit` — consecutive no-delta wakes before terminal (default 2)
- `follow_through.infra_backoff_minutes` — ordered cooldown schedule for
  unmatched transients only; skip while an open `infra-incident` matches.
  Exhausting the list terminates `gave_up`. Open incidents suppress dependent
  retries until the platform owner closes them.
- `follow_through.max_implement_spawns_per_run` — max inline fix spawns (default 3)

Enforce all budgets; escalate on exhaust.

## Explicit Design Notes

This skill is the **SSOT for stop/escalate logic** across autonomy kickoff flows. `/watch-pr` defers to these terminal rules when invoked inside orchestrated autonomy. No looping outside these bounds; no silent retries. Leftover Daily 1/2 Act vs Codex L4 split: `docs/runbooks/daily-leftover-act.md`. Shared G3 incidents: `scripts/infra_incident.py`.

## Status Report

When reporting loop progress, keep to ≤150 words. Include: PR link, terminal state (if any), last event type, round count, next action (continue/escalate/merge).
