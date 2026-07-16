---
name: watch-pr
version: 2.0.0
description: "Persistent PR resolution loop — subscribes to activity, monitors CI, review comments (Human/CodeRabbit/Gemini/bots), and merge conflicts. Loops until PR is clear or max rounds reached."
type: command
requires_mcp: [github]
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

### Step 0: Write Active-Task Checkpoint

```bash
python3 - <<'EOF'
import json, os, datetime
os.makedirs('.ai/sessions', exist_ok=True)
try:
    data = json.load(open('.ai/sessions/active-task.json'))
except Exception:
    data = {}
data.update({
    'type': 'watch-pr',
    'status': 'watching',
    'round': 0,
    'started_at': datetime.datetime.utcnow().isoformat() + 'Z',
})
json.dump(data, open('.ai/sessions/active-task.json', 'w'), indent=2)
EOF
```

After Step 1 resolves the PR number and branch, patch those fields in. Update `round` at the top of each loop iteration.

### Step 1: Resolve PR Number

```bash
git rev-parse --abbrev-ref HEAD
```

Call `mcp__github__list_pull_requests(owner, repo, state:"open")` and match by branch name, or use the supplied PR_NUMBER.

### Step 2: Subscribe to PR Activity

```
mcp__github__subscribe_pr_activity(owner, repo, pr_number)
```

### Step 3: Outer Loop (max 5 rounds)

Track state as: **"Round N/5: [found issues] -> [dispatched fix] -> [waiting]"**

Update checkpoint at top of every round.

**3a. Poll every 60s:**
- `mcp__github__pull_request_read` → check `head.sha` vs current `HEAD`
- Check CI status per SHA
- Check `mergeable` flag: `null` = still computing (re-poll), `false` = conflict, `true` = clean
- List all review threads; mark unresolved ones

**3b. Exit condition (ALL must be true):**
- All CI checks pass on current HEAD SHA
- Zero unresolved review threads
- At least 5 minutes elapsed since last push
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

**3d. Dispatch `/pr-resolve` with the collected issue list.**

**3e. Wait 5 minutes**, then re-poll. If exit condition met → done. Otherwise increment round.

**3f. Before round N+1:** carry the existing subscription forward — `subscribe_pr_activity` is idempotent and the stream does not need resetting. Do NOT unsubscribe+resubscribe: both calls are connector-approval-gated (native claude.ai dialog, unsilenceable by repo config) and the reset buys nothing.

### Step 4: Escalate at Round 5

If still unresolved after round 5:
1. Unsubscribe from PR activity (single attempt, best-effort — the call is connector-approval-gated; if it prompts or fails, skip it and proceed, the subscription lapses with the session)
2. Post summary comment classifying each remaining issue as:
   - (a) caused by this PR's changes
   - (b) pre-existing / unrelated
   - (c) environmental / flaky
3. Notify the user
4. Set checkpoint `status: "escalated"`

### On Successful Merge

Set checkpoint `status: "merged"`, notify user with merge URL.

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
