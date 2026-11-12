---
name: watch-pr
version: 2.2.0
description: "Persistent PR resolution loop — subscribes to activity, monitors CI, review comments (Human/CodeRabbit/Gemini/bots), and merge conflicts. Loops until PR is clear or max rounds reached."
type: command
requires_mcp: [github]
requires_scripts: [scripts/pr-watch-observe.py]
safety_tier: green
required_entities: []
tags:
  - workflow
  - automation
  - system
---
# /watch-pr — Persistent PR Resolution Loop

Based on proven patterns from the Manolii ecosystem.

Invoke as `/watch-pr [PR_NUMBER]` or `/watch-pr` (auto-detects from current branch).

## Protocol

### Step 0: Retain the existing task

Read the existing accepted owner checkpoint before subscribing. Preserve its
parent commitments, holds, budgets, instructions and other owners. Execute only
trusted installed sources, never repository tools from an untrusted PR or fork. Missing or
malformed custody is an infrastructure hold for persistence/continuation, not
permission to reconstruct it or stop independent authorized implementation.

### Step 1: Resolve PR Number

```bash
git rev-parse --abbrev-ref HEAD
```

Call `mcp__github__list_pull_requests(owner, repo, state:"open")` and match by branch name, or use the supplied PR_NUMBER.

After resolving the native PR/head and accepted task owner, record a scoped
observation with this code. Set `WATCH_OWNER`, `WATCH_REPOSITORY`, `WATCH_PR`,
`WATCH_HEAD` and `WATCH_PHASE` from the existing trusted owner and native readback;
phase is `watching`, `merged`, `closed` or `verifying`. These are observations,
not execution, review or completion authority. Do not reset a lifetime repair
ledger from a watch invocation or passive wake.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pr-watch-observe.py"
```

Use the same locked observation path after a head/phase change. An unchanged
observation performs no checkpoint replacement. The original task's completion
and budgets remain unchanged, including after this PR merges.

### Step 2: Subscribe (split — do not always subscribe PR activity)

Prefer the exact owning client's qualified native Goal/Queue and relevant events.
Before creating a new hosted heartbeat, require demonstrated same-owner execution,
busy-session queuing/coalescing and original-environment recovery on that host.
A configured schedule, notification or accepted message alone does not qualify it.
Without qualified transport, create no new timer: retain the wake infrastructure
hold and accepted existing repair owner; continue independent authorized work.
Preserve existing user-authorized schedules, owners and provider-required bounded
heartbeats. Never cancel or transfer them merely to satisfy this creation gate.


If GitHub reports auto-merge **actually armed** (`autoMergeRequest` /
`auto_merge`, not intended-later) **and** every changed path is inside
`reports/**`, `docs/**`, `reports/INDEX.md`, `.github/pull_request_template.md`,
`.github/PULL_REQUEST_TEMPLATE.md`: **must not** call
`mcp__github__subscribe_pr_activity` / `subscribe_github_pr`. Optional CI
subscribe or none. Arm a ≥20-minute heartbeat only if CI subscribe is on.

Armed check: REST `GET /repos/{owner}/{repo}/pulls/{n}` — `.auto_merge` object
vs `null` for classic auto-merge. GraphQL `autoMergeRequest { enabledAt }` **or**
non-null `mergeQueueEntry { id }` (merge-queue PRs keep REST `auto_merge: null`).
`mergeable_state: CLEAN` is not arming. `gh pr view <n> --json autoMergeRequest`
covers classic arming only.

Product-red (any other path): subscribe PR **and** CI when the surface has
it, plus a bounded ≥20-minute heartbeat. Cursor Cloud:
`subscribe_github_pr` / `subscribe_github_ci`. Claude Code:
`mcp__github__subscribe_pr_activity` only (no CI-only MCP equivalent —
do **not** invent a GitHub MCP CI-subscribe tool).

```
mcp__github__subscribe_pr_activity(owner, repo, pr_number)
subscribe_github_ci(owner, repo, pr_number)   # Cursor Cloud only
```

Docs/CI with **none** (no CI subscribe): do **not** enter the 60s poll /
5-minute wait. GitHub auto-merge + Judge is the waiter. Continue independent
parent commitments; end the turn only if no runnable work remains.

### Step 3: Continue authorized work (existing repair budget)

Retain the existing lifetime repair ledger. Count failed repair attempts, not
passive CI/review events, duplicate delivery or unchanged waits. The skill's configured repair cap and denial/backoff rules still apply.

Update scoped observations only when facts change. Skip the model wait loop for docs/CI `none`.

**3a. Product-red / CI-subscribe only — event-led, not a 60s sleep loop:**
- `mcp__github__pull_request_read` → check `head.sha` vs current `HEAD`
- Check CI status per SHA
- Check `mergeable` flag: `null` = still computing (retain an eligible event or one bounded recovery), `false` = conflict, `true` = clean
- List all review threads; mark unresolved ones

**3b. Exit condition (ALL must be true):**
- Required CI checks pass on the current full HEAD SHA
- Zero unresolved review threads
- Qualifying independent current-head review and all merge/release gates from
  `autonomy-follow-through` pass; elapsed time is never review evidence
- No merge conflict

**3c. Collect issues (priority order):**
1. Merge conflicts (highest priority)
2. Failing / errored CI checks
3. Unresolved review comments — classified by reviewer type:

| Reviewer | Detection |
|----------|-----------|
| Human | `user.type == "User"` and no bot pattern |
| CodeRabbit | `user.login` matches `coderabbitai` prefix |
| Gemini Code Assist | `user.login` starts with `gemini` OR body starts with `<!-- Gemini Code Assist` |
| Other bots | `user.type == "Bot"` or `user.login` ends with `[bot]` |

**3d. Continue repair in the existing execution owner.** Inspect actual findings,
repair owned code, run relevant tests and publish the new head under existing
budgets and protected review/merge controls. Re-read CI and independent review
for that new full head. Use `/pr-resolve` only within this accepted owner or its
already accepted repair worker; never start an overlapping editor. Route foreign
dependencies through their existing owner and retain the handoff receipt.
Publication/access failure preserves prepared implementation and holds only its
dependent boundary; it does not make all implementation blocked.

**3e. Retain the qualified event/wake owner.** Use only that host's actual cadence
and the skill's bounded recovery rule. Do not re-arm an unchanged one-shot chain
or increment repair rounds for waiting. A wait needs a dependency, unblock
condition, next action and evidence. Finish independent implementation/testing/
review while deployment or access holds only dependent work.

**3f. Before round N+1:** carry the existing subscription forward — `subscribe_pr_activity` is idempotent and the stream does not need resetting. Do NOT unsubscribe+resubscribe: both calls are connector-approval-gated (native claude.ai dialog, unsilenceable by repo config) and the reset buys nothing.

### Step 4: Escalate the affected exhausted repair

Only after the existing failed-repair budget is exhausted:
1. Do NOT unsubscribe — `unsubscribe_pr_activity` is connector-approval-gated (native claude.ai dialog, unsilenceable by repo config) and buys nothing here: simply stop acting on the stream; the subscription lapses with the session, and merged/closed PRs are auto-unsubscribed by the platform. Call it only if the operator explicitly asks to stop watching.
2. Post summary comment classifying each remaining issue as:
   - (a) caused by this PR's changes
   - (b) pre-existing / unrelated
   - (c) environmental / flaky
3. Notify once for a specific required intervention; unchanged evidence stays quiet
4. Record the specific exhausted commitment and next action in its existing
   ledger. Do not mark unrelated parent work escalated or discard its custody.

### On Successful Merge

Record only this PR's scoped `merged` observation and verify the native merge
head. Continue the original task's requested post-merge verification, cleanup,
issue and acceptance commitments. Keep the parent checkpoint unfinished until
its requested outcome is verified. Notify only material progress, a specific
required intervention or verified completion.

## Merge Conflict Protocol

```bash
git log --oneline HEAD..origin/{base_branch}
git show origin/{base_branch}:<conflicted-file>
git show HEAD:<conflicted-file>
```

Understand intent of **both sides** before editing. If ambiguous, surface to user.

## Hard Limits

- Never force-push
- Never merge or rebase onto main/master directly
- Never push to main/master
- Never skip CI or bypass hooks (--no-verify)
