---
name: autonomy-follow-through
version: 1.0.4
description: "Invoke autonomy follow-through skill — termination + escalation loop for routine autonomy and /watch-pr."
type: command
requires_mcp: []
safety_tier: green
required_entities: []
eval_cases: null  # TODO: add eval cases
tags:
  - autonomy
  - follow-through
  - workflow
---
# Command: autonomy-follow-through

Invoke the shared follow-through loop for autonomous PR work.

## Usage

```bash
/autonomy-follow-through <PR_NUMBER> [--handoff-id <ITEM_ID>]
```

**Arguments:**
- `<PR_NUMBER>` (required) — GitHub PR number
- `--handoff-id` (optional) — orchestrator task id; maps to producer issue URL

## What It Does

Executes `.claude/skills/autonomy-follow-through/SKILL.md`:

1. Runs pre-process QA (assumptions, errors, opportunities, continuity)
2. Uses the owning runtime's supported bounded read route under the skill's
   model/plane and credential rules
3. Detects terminal conditions (done, blocked_on_human, blocked_on_infra, superseded, gave_up)
4. Escalates with evidence JSON in PR comment
5. Merges when safe (named required checks green on the current SHA, qualifying
   current-head review present, threads resolved, guard/authority rules met); otherwise retains
   the actual pending, infrastructure or specific human-decision state.
   Leftover-Act **trusted-packet squash** is
   `docs/runbooks/daily-leftover-act.md` only — `/watch-pr` never inherits it.
6. Enforces repair budgets and infrastructure retry limits; passive CI/review
   waits and duplicate informational events do not consume repair budgets
7. Allows author-controlled readiness after complete initial scope, deterministic
   and user verification, current-head required fast/secret checks and no HIGH
   defect or input/security refusal; independent merge review remains required
8. Preserves authorization and actual review outcomes through repairs, keeps one
   real monitor and completes authorized parent-task verification after merge

Returns terminal state + escalation status or merge confirmation.

Empty Codex L4 seeds (`changed_files=0` after one wake) are `superseded` after an Act session-prefix PR (`cursor/` or `claude/`, never `codex/`) exists — not `blocked_on_human`. See `docs/runbooks/daily-leftover-act.md`.

## See Also

- `.claude/skills/autonomy-follow-through/SKILL.md` — full specification
- Orchestrator-repo surfaces referenced by the skill but not shipped in this pack: `docs/runbooks/daily-leftover-act.md`, `config/autonomy-executor-policy.yaml`, `scripts/infra_incident.py`, `/watch-pr`

Review publication formats and current-head acceptance: [review-evidence.md](../skills/autonomy-follow-through/review-evidence.md).
