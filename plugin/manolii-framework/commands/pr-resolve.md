---
description: "Find the PR for the current branch and resolve all issues in priority order"
---

# PR Resolve — One-Shot Fix Loop

Find the PR for the current branch and resolve all issues in priority order.

## Steps (max 5 iterations)

Track each iteration: "Round N of 5: [what was found] → [what was fixed] → [waiting for re-check]"

### 1. Find PR

Use GitHub MCP `pull_request_read` (preferred) or `scripts/ci/check-pr-comments.sh`
(if available) to find the PR for the current branch.

### 2. Detect issues in this order

- **Merge conflicts** → resolve them
- **Failing CI checks** → use `pull_request_read` with `get_check_runs` to get
  status. Read logs, fix failures, commit + push. Do not declare success based
  on a stale check run from a previous commit — verify the check SHA matches HEAD.
- **Unresolved review comments** → use `pull_request_read` with
  `get_review_comments` to fetch threads.

### 3. Triage each finding before acting

- **Is it a true issue or a false positive?** Cross-repo references (e.g., a
  function defined in another repo) are commonly flagged incorrectly. If false
  positive, reply explaining why and move on.
- **Handle by reviewer type:**
  - Human comments: always take priority — address every one
  - CodeRabbit: address medium+ severity (marked `⚠️ Potential issue` or higher)
  - Gemini Code Assist: address medium+ priority (colored badges). Advisory
    comments can be acknowledged without code changes.

### 4. Fix all detected issues

1. Merge conflicts first
2. Failing checks second
3. Review comments last

For each issue: read the reviewer's comment fully, understand the concern,
implement the fix.

**After applying a reviewer's suggestion, check if the fix itself introduces a
new edge case.** Reviewer suggestions optimize for one vector — verify they don't
open another.

### 5. Commit and push

```bash
git add <specific-files>
git commit -m "fix: address PR review comments (round N)"
git push
```

### 6. Reply to review comments

For each comment addressed, reply with a brief note explaining the fix and
which commit resolves it.

### 7. Update PR Comment Cache

After each round, write the current PR state to `.git/.pr-comments-cache/latest.json`
so the `user-prompt.py` hook can surface unresolved comments on subsequent prompts:

```bash
mkdir -p .git/.pr-comments-cache
echo '{"unresolved_count": {N}, "pr_number": {PR_NUMBER}, "updated_at": "{ISO_DATE}"}' > .git/.pr-comments-cache/latest.json
```

When all issues are resolved, write `"unresolved_count": 0` to clear the blocking reminder.

### 8. Re-check

After fixing, re-check for remaining issues. Repeat up to 5 rounds. If issues
persist after 5 rounds, stop and escalate with:
- What was tried in each round
- What keeps failing or getting flagged
- Whether failures are: (a) caused by this change, (b) pre-existing/flaky,
  (c) environmental

## Rules

- Fix merge conflicts before CI issues (CI can't pass with conflicts)
- Fix CI before review comments (reviewers may have flagged things CI would catch)
- Never force push — always create new commits
- If a review comment is incorrect or not applicable, reply explaining why instead of making a bad change
- Run `/verify` after all fixes before the final push
