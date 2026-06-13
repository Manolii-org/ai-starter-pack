# Skill: Browser QA

## Description

Real-browser testing methodology for catching bugs that unit tests and CI miss. Uses Playwright for automated browser interaction with visual verification.

## Activation

- **Trigger:** When `/qa` command is invoked, or before shipping a feature PR
- **Always active:** No — invoke explicitly

## Allowed Tools

Read, Glob, Grep, Bash, Edit, Write, Agent

## Instructions

### Philosophy

Unit tests verify code logic. Browser QA verifies **user experience**. Many production bugs only manifest in a real browser: hydration mismatches, CSS layout breaks, JavaScript errors swallowed by error boundaries, stale data from caches, race conditions in async UI updates.

### Diff-Aware Testing

Always start by understanding what changed:

1. Run `git diff --name-only HEAD~1` (or `main...HEAD` for PR scope)
2. Map file changes to affected user flows
3. Prioritise testing the critical path through changed code

### Testing Checklist

For every flow tested, verify:

- [ ] Page loads without console errors
- [ ] No unhandled promise rejections in browser console
- [ ] Key data renders (not empty states when data exists)
- [ ] Forms submit successfully with valid data
- [ ] Forms show appropriate errors with invalid data
- [ ] Navigation works (links, buttons, back/forward)
- [ ] Loading states appear and resolve
- [ ] Error boundaries don't swallow real errors
- [ ] Mobile viewport renders correctly (if responsive)
- [ ] Capture and save Playwright screenshots for key states (initial, post-action, error)

### Bug Classification

| Severity | Definition | Action |
|----------|-----------|--------|
| P0 | Crash, data loss, security hole | Fix immediately, block PR |
| P1 | Broken user flow, feature doesn't work | Fix before merge |
| P2 | Visual glitch, minor UX issue | Fix or create issue |
| P3 | Cosmetic, text, spacing | Note for later |

### Atomic Fix Pattern

Each bug fix must be:
1. A separate commit with descriptive message
2. Include `Root cause:` in the commit body
3. Accompanied by a regression test that would have caught the bug
4. Verified by re-running the affected flow

### Regression Test Requirements

Every bug found MUST produce a test that:
- Fails without the fix applied
- Passes with the fix applied
- Tests the specific user interaction that triggered the bug
- Lives alongside existing tests (co-located or in test directory)

### When NOT to Use

- Pure API/backend changes with no UI impact
- Documentation-only changes
- CI/infrastructure changes
- Database migrations (use integration tests instead)
