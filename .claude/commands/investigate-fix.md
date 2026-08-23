---
description: "A structured workflow for investigating bugs, diagnosing root causes, implementing fixes, and closing the PR loop"
---

# /investigate-fix — Investigate, Diagnose & Fix

A structured workflow for investigating bugs, diagnosing root causes, implementing fixes, and closing the PR loop.

---

## Process Steps

### 1. Save Instructions
Maintain `.ai/sessions/active-task.json` with a non-null `active_step_id` naming the step you are actually on. Three duties, and all three are required — the checkpoint is the replacement for a withdrawn tool, so it has to carry the state that tool used to:

- **Write** it when you begin step 1.
- **Update** it at **every** step transition, before starting the new step. A checkpoint written once and never advanced survives compaction still naming step 1, and sends recovery back to the beginning — which is worse than having none, because it is *confidently* wrong rather than absent.
- **Re-read** it before any action that depends on knowing which step you are on — not only at step boundaries.

Use that exact key: where `.claude/persistent-instructions.md` is installed, it preserves the file verbatim on a non-null `active_step_id`, so the checkpoint survives compaction automatically. Step 10 clears it once the work is done.

> **Known limitation, plugin-only installs.** The packaged plugin ships no `persistent-instructions.md`, and `hooks/pre-compact.sh` states its stdout is not injected into compaction context. So there is no automatic post-compaction consumer: if compaction fires *mid-step*, recovery depends on this directive itself surviving into the compacted context. Writing the checkpoint still costs nothing and makes recovery possible; it is not guaranteed. Installing the full template closes this.

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

### 10. Checkpoint Cleanup

Once step 9's exit conditions are **all** true, clear the checkpoint:

```bash
rm -f .ai/sessions/active-task.json
```

Step 1 writes a non-null `active_step_id` and nothing else ever clears it, so without
this the finished run's checkpoint persists indefinitely — and `.claude/persistent-instructions.md`
preserves the file verbatim on *every* later compaction while that key is non-null.
A completed task then keeps being re-injected as if it were live, crowding out the
context the session actually needs.

**Leave the file in place** on any other ending: escalation at 9(i), an abandoned
session, or any step still `partial` / `failed`. There, a surviving checkpoint is the
mechanism working as designed — it is only noise once the work is genuinely done.
Same rule as `.claude/agents/orchestrator.md` Phase 4.

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
