---
name: systematic-debugging
version: 1.0.0
description: "Systematic debugging methodology for production issues. Use when investigating bugs, debugging errors, fixing test failures, or performing root cause analysis. Provides structured approach: reproduce, isolate, identify, fix, verify."
type: skill
data_sensitivity: internal
safety_tier: green
requires_mcp: []
required_entities: []
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - Edit
tags:
  - debugging
  - quality
---

# Systematic Debugging Skill

A structured methodology for investigating and resolving bugs with emphasis on root cause analysis and verification.
---

## Quick Reference

```text
1. REPRODUCE → 2. ISOLATE → 3. HYPOTHESIZE → 4. TEST → 5. FIX → 6. VERIFY
```

---

## 1. The Debugging Framework

### 1.1 Six-Phase Process

| Phase | Goal | Output |
|-------|------|--------|
| **Reproduce** | Confirm the bug exists | Reliable reproduction steps |
| **Isolate** | Narrow scope | Minimal failing case |
| **Hypothesize** | Form theories | Ranked list of causes |
| **Test** | Validate theories | Confirmed root cause |
| **Fix** | Implement solution | Code changes |
| **Verify** | Confirm fix works | Passing tests, no regression |

---

## 2. Phase 1: Reproduce

### 2.1 Gather Context

```markdown

## Bug Report Template

- **Observed behavior**: What happened?
- **Expected behavior**: What should happen?
- **Environment**: Browser, OS, user type
- **Steps to reproduce**: Exact sequence
- **Frequency**: Always? Sometimes? Once?
- **Error messages**: Exact text, stack traces
- **Logs**: Relevant server/client logs
```

### 2.2 Reproduce Locally

```bash

# Check environment matches

node -v  # Same Node version?
pnpm -v  # Same package manager?

# Sync dependencies

pnpm install

# Match environment variables

pnpm dev  # load the same env vars as the failing environment (your secrets manager or .env)

# Follow exact reproduction steps

# Document: "On step 3, error appears"

```

### 2.3 If Cannot Reproduce

| Possibility | Investigation |
|-------------|---------------|
| Environment diff | Compare local vs production config |
| Data dependency | Check for specific user/data conditions |
| Race condition | Add logging, check timing |
| Caching issue | Clear all caches, retry |
| State accumulation | Fresh browser session, clear storage |

---

## 3. Phase 2: Isolate

### 3.1 Binary Search

```markdown

## Narrowing Scope

1. Does it happen in staging? → Environment issue
2. Does it happen for all users? → User-specific data
3. Does it happen on all routes? → Route-specific logic
4. Does removing X fix it? → X is involved
```

### 3.2 Create Minimal Case

```typescript
// ❌ WRONG - Testing full flow
test("full user journey", async ({ page }) => {
  // 50 steps that sometimes fail
});
// ✅ CORRECT - Isolated test
test("recommendation API returns data", async ({ page }) => {
  const response = await page.request.get("/api/recommendations");
  expect(response.status()).toBe(200);
});
```

### 3.3 Git Bisect

```bash

# Find commit that introduced bug

git bisect start
git bisect bad HEAD
git bisect good v1.2.0  # Last known good version

# Test each commit

pnpm test
git bisect good  # or git bisect bad

# Find culprit commit

git bisect reset
```

---

## 4. Phase 3: Hypothesize

### 4.1 Generate Hypotheses

```markdown

## Hypothesis Template

1. **What**: Description of potential cause
2. **Why**: Evidence supporting this theory
3. **Test**: How to confirm/refute
4. **Likelihood**: High/Medium/Low
```

### 4.2 Common Root Causes

| Category | Examples |
|----------|----------|
| **Data** | Null values, wrong format, missing fields |
| **State** | Race conditions, stale cache, session issues |
| **Timing** | Network timeouts, slow queries, async issues |
| **Config** | Wrong env vars, feature flags, permissions |
| **Types** | Type coercion, undefined vs null, NaN |
| **Logic** | Off-by-one, wrong comparison, missing case |

### 4.3 Rank by Likelihood

```markdown

## Ranked Hypotheses

1. [HIGH] Null user.preferences - Stack trace points to line 45
2. [MEDIUM] Cache returning stale data - Sometimes works on retry
3. [LOW] Race condition in auth - No timing pattern observed
```

---

## 5. Phase 4: Test Hypotheses

### 5.1 Add Diagnostic Logging

```typescript
// Targeted logging (remove after debugging)
console.log("[DEBUG] recommendations input:", {
  userId: user.id,
  preferences: user.preferences,
  timestamp: Date.now(),
});
```

### 5.2 Use Assertions

```typescript
// Add assertions to validate assumptions
function getRecommendations(user: User) {
  // Validate assumption
  if (!user.preferences) {
    console.error("[DEBUG] Missing preferences for user:", user.id);
    throw new Error(`User ${user.id} has no preferences`);
  }
  // Continue with logic
}
```

### 5.3 Trace Data Flow

```markdown

## Data Flow Trace

1. Request received: { userId: 123 }
2. User fetched: { id: 123, preferences: null } ← PROBLEM HERE
3. Recommendations called with null preferences
4. TypeError thrown
```

---

## 6. Phase 5: Fix

### 6.1 Root Cause vs Symptom

```typescript
// ❌ WRONG - Fixes symptom only
function getRecommendations(user: User) {
  const prefs = user.preferences || {};  // Mask the real problem
  return calculateRecs(prefs);
}
// ✅ CORRECT - Fixes root cause
async function ensureUserPreferences(userId: string) {
  const user = await getUser(userId);
  if (!user.preferences) {
    // Create default preferences (actual fix)
    await createDefaultPreferences(userId);
  }
  return getUser(userId);
}
```

### 6.2 Fix Principles

| Principle | Description |
|-----------|-------------|
| **Minimal** | Change only what's necessary |
| **Targeted** | Fix the root cause, not symptoms |
| **Tested** | Add test that would have caught this |
| **Documented** | Explain why in commit message |

---

## 7. Phase 6: Verify

### 7.1 Verification Checklist

```markdown

## Verification Steps

- [ ] Bug no longer reproduces
- [ ] Existing tests still pass
- [ ] New test covers this case
- [ ] No regression in related features
- [ ] Fix works in staging/preview
- [ ] Edge cases considered
```

### 7.2 Write Regression Test

```typescript
test("handles user without preferences", async () => {
  // Setup: Create user without preferences
  const user = await createTestUser({ preferences: null });
  // Act: Request recommendations
  const response = await getRecommendations(user.id);
  // Assert: Returns empty array, no error
  expect(response.status).toBe(200);
  expect(response.data).toEqual([]);
});
```

### 7.3 Monitor After Deploy

```markdown

## Post-Deploy Monitoring

- [ ] Error rates in Sentry normal
- [ ] No new related errors
- [ ] Performance metrics stable
- [ ] User feedback positive
```

---

## 8. Debugging Tools

### 8.1 Logging and Tracing

```bash

# View recent logs

pnpm dev 2>&1 | grep -i error

# Check Vercel logs

vercel logs --follow

# Check Sentry for errors

# Open Sentry dashboard, filter by time range

```

### 8.2 Database Inspection

```sql
-- Check data state
SELECT * FROM users WHERE id = 'user_123';
-- Check for null values
SELECT COUNT(*) FROM users WHERE preferences IS NULL;
-- Check recent changes
SELECT * FROM users ORDER BY updated_at DESC LIMIT 10;
```

### 8.3 Network Debugging

```typescript
// Log all API responses
page.on("response", (response) => {
  console.log(response.url(), response.status());
});
// Capture failed requests
page.on("requestfailed", (request) => {
  console.log("FAILED:", request.url(), request.failure()?.errorText);
});
```

---

## 9. Common Debugging Scenarios

### 9.1 "Works Locally, Fails in Production"

| Check | Command/Action |
|-------|----------------|
| Environment variables | Compare `.env` with Vercel settings |
| Node version | Check `engines` in package.json |
| Dependencies | `pnpm install --frozen-lockfile` |
| Build vs dev mode | `pnpm build && pnpm start` |

### 9.2 "Intermittent Failures"

| Possibility | Investigation |
|-------------|---------------|
| Race condition | Add delays, check timing logs |
| Cache issues | Disable cache, check TTL |
| External service | Check third-party status |
| Resource limits | Check memory, connections |

### 9.3 "Worked Yesterday"

```bash

# Find what changed

git log --oneline --since="yesterday"

# Check recent deployments

vercel ls

# Check dependency updates

git diff HEAD~5 pnpm-lock.yaml
```

---

## 10. Anti-Patterns

| Pattern | Issue | Fix |
|---------|-------|-----|
| Changing multiple things | Can't identify fix | One change at a time |
| Skipping reproduction | May fix wrong thing | Always reproduce first |
| Guessing solutions | Wastes time | Form hypothesis, test |
| No verification | Bug may return | Add regression test |
| Removing logs early | Lose debugging context | Keep until verified |

---

## Debugging Checklist

- [ ] Can reproduce the bug reliably
- [ ] Isolated to minimal failing case
- [ ] Have ranked hypotheses
- [ ] Tested hypotheses systematically
- [ ] Root cause identified (not just symptom)
- [ ] Fix is minimal and targeted
- [ ] Regression test added
- [ ] Verified fix in realistic environment
- [ ] No new issues introduced

## Attribution

Ported from a retired internal skill library; inspired by obra/superpowers (`skills/systematic-debugging`), used under the MIT License:

```text
MIT License

Copyright (c) 2025 Jesse Vincent

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
