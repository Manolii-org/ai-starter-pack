---
name: autonomy-follow-through
version: 1.0.3
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
2. Uses one persistent opus-pinned `infra` agent (`model=` omitted) for the
   bounded credential-bearing read loop; consumes only sanitised receipts
3. Detects terminal conditions (done, blocked_on_human, blocked_on_infra, superseded, gave_up)
4. Escalates with evidence JSON in PR comment
5. Merges when safe (named required checks green on the current SHA, qualifying
   current-head review present, threads resolved, not guarded); otherwise stops
   `blocked_on_human`. Leftover-Act **trusted-packet squash** is
   `docs/runbooks/daily-leftover-act.md` only — `/watch-pr` never inherits it.
6. Enforces anti-loop budgets (rearms, no-delta limit, infra backoff)

Returns terminal state + escalation status or merge confirmation.

Empty Codex L4 seeds (`changed_files=0` after one wake) are `superseded` after an Act session-prefix PR (`cursor/` or `claude/`, never `codex/`) exists — not `blocked_on_human`. See `docs/runbooks/daily-leftover-act.md`.

## See Also

- `.claude/skills/autonomy-follow-through/SKILL.md` — full specification
- `docs/runbooks/daily-leftover-act.md` — leftover Act vs Codex L4; named required checks; G3 `infra-incident` records (`scripts/infra_incident.py`)
- `config/autonomy-executor-policy.yaml` — budget and policy config
- `/watch-pr` — standalone PR monitoring (defers to this skill in orchestrated flows)
