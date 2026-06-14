---
description: "A structured workflow for investigating bugs, diagnosing root causes, implementing fixes, and closing the PR loop"
---

# /investigate-fix — Investigate, Diagnose & Fix

A structured workflow for investigating bugs, diagnosing root causes, implementing fixes, and closing the PR loop.

---

## Process Steps

### 1. Save Instructions
Use TodoWrite to track which step you are on so context compaction doesn't lose the flow.

### 2. Investigate the Issue
- Read relevant code in the affected area
- Check recent commits for related changes
- Run `/diagnose {error or symptom}` to search memory for similar past issues, known patterns, and session history
- If `/diagnose` returns matches, factor them into your root cause hypothesis

### 3. Diagnose the Issue
- Identify root cause (not just symptoms)
- State: "Root cause: [description]"

### 4. Plan a Fix
- Describe what will change, in which files
- Include a "Root cause:" line explaining why this fix is correct

### 5. Review the Plan (use Audit Flow below)

### 6. Implement Plan
- Make changes in the correct files
- After implementation, verify all changes are in the intended location

### 7. Validate the Implementation

Run each check separately (not chained — chaining masks failures):
```bash
{pnpm typecheck}
{pnpm lint}
{pnpm test}
{pnpm build}
```

Write a regression test that WOULD HAVE CAUGHT the original bug — confirm it fails without the fix and passes with it.

### 8. Create PR

Include in the PR description:
- Root cause explanation
- What changed and why
- How to verify the fix

### 9. PR Resolution Loop

**DO NOT EXIT until all exit conditions are met.**

Track each iteration explicitly: "Round N of 5: [what was found] → [what was fixed] → [waiting for re-check]"

**Loop steps:**

a. After pushing, wait for CI to start. Poll every 60 seconds until all required checks reach a terminal state (`success` / `failure` / `cancelled`), or 20 minutes elapse.

b. Check for review comments and CI status.

c. **Triage each finding before acting** — is it a true issue or a false positive?

d. Address all review comments (human first, then bot reviewers).

e. If unresolved review comments exist → address every actionable comment, push fixes, go back to (a).

f. If CI checks are failing → diagnose, fix, push, go back to (a).

g. After pushing fixes, wait up to 5 minutes for new reviews before declaring done.

h. **EXIT CONDITIONS — all three must be true simultaneously:**
   - All GitHub CI checks pass for the current commit SHA
   - Zero unresolved review comments requiring code changes
   - A clean check AFTER the last push confirms no new comments arrived

i. **Maximum 5 rounds.** If issues persist after 5 rounds — stop and escalate with a summary of what was tried, what keeps failing, and the likely root cause.

---

## Audit Flow

Complete the below audit, then re-apply autonomously until no medium-or-higher issues remain (max 3 iterations).

**Each iteration must:**
1. Run the full audit checklist
2. List all findings with severity (Critical / Major / Minor / Trivial)
3. Fix all Critical and Major issues
4. Re-run the checks relevant to what was just fixed
5. If iteration 3 still has issues, STOP — list what remains and why

### 1. Core Analysis

Audit for:
- Logic errors, edge cases, or missing error handling
- Conflicts with existing patterns or CLAUDE.md conventions
- Security concerns:
  - User-controlled input in prompts/queries without sanitisation
  - PII logged without masking
  - Missing input validation at system boundaries
  - Silent fallbacks that defeat security intent
- Performance or timeout concerns
- Unintended side effects on dependent systems

### 2. Convention Compliance

Cross-reference against CLAUDE.md rules:
- Security markers on all routes
- Input validation at boundaries
- Correct error handling patterns
- Migration rollback files present (if applicable)

### 3. Remediation

For any issues found:
- Describe root cause and impact
- Classify CI failures: (a) caused by this change, (b) pre-existing/flaky, (c) environmental
- Propose a scoped fix that doesn't break unrelated functionality
- Define how to verify the fix worked
