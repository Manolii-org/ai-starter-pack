---
name: root-cause-tracing
version: 1.0.0
description: "Root cause analysis for bugs and incidents. Use when investigating why something broke, tracing error origins, or understanding system failures. Covers systematic investigation techniques."
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
tags:
  - debugging
  - incident
---

# Root Cause Tracing Skill

Find the true origin of problems by following evidence chains systematically.
---

## Quick Reference

```text
Symptom → Immediate Cause → Contributing Factors → Root Cause
Example:
500 Error → Null pointer → Missing validation → No schema enforcement
```

---

## 1. The 5 Whys Method

### 1.1 Iterative Questioning

```markdown

## Problem: User login fails

1. Why? → API returns 500 error
2. Why? → Database query throws exception
3. Why? → User record has null email
4. Why? → Registration allowed null email
5. Why? → No schema validation on registration endpoint
ROOT CAUSE: Missing input validation on registration
```

### 1.2 Example: Performance Issue

```markdown

## Problem: Page loads slowly (10+ seconds)

1. Why? → Large API response (5MB)
2. Why? → Returning all user records
3. Why? → No pagination implemented
4. Why? → Query fetches everything
5. Why? → No limit clause in ORM call
ROOT CAUSE: Unbounded database query
```

---

## 2. Fault Tree Analysis

### 2.1 Building the Tree

```text
                    [User Can't Login]
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    [Auth Service      [Database        [Network
     Error]             Error]           Error]
        │                  │                │
    ┌───┴───┐         ┌───┴───┐       ┌───┴───┐
    ▼       ▼         ▼       ▼       ▼       ▼
[Invalid  [Token   [Query   [Conn   [DNS   [SSL
 Creds]   Expired] Failed]  Pool]   Fail]  Cert]
```

### 2.2 Elimination Process

```typescript
// Check each branch systematically
const faultTree = {
  symptom: "User can't login",
  branches: [
    {
      cause: "Auth service error",
      check: () => checkAuthLogs(),
      children: [
        { cause: "Invalid credentials", check: () => verifyCredentials() },
        { cause: "Token expired", check: () => checkTokenExpiry() },
      ],
    },
    {
      cause: "Database error",
      check: () => checkDbLogs(),
      children: [
        { cause: "Query failed", check: () => testQuery() },
        { cause: "Connection pool", check: () => checkConnections() },
      ],
    },
  ],
};
```

---

## 3. Timeline Reconstruction

### 3.1 Gather Evidence

```bash

# When did it start?

grep "ERROR" logs/app.log | head -5

# What changed before the error?

git log --oneline --since="2024-01-15"

# Correlate with deployments

vercel logs --since="2024-01-15"
```

### 3.2 Build Timeline

```markdown

## Incident Timeline

14:00 - Deploy v1.2.3 (commit abc123)
14:05 - First error in logs
14:10 - User reports issue
14:15 - Error rate spikes 500%
14:20 - Rollback to v1.2.2
14:25 - Errors stop
CORRELATION: Issue introduced in v1.2.3
```

---

## 4. Code Tracing

### 4.1 Stack Trace Analysis

```typescript
// Error stack trace
Error: Cannot read property 'email' of undefined
    at getUserEmail (src/auth/user.ts:45)
    at validateSession (src/auth/session.ts:23)
    at handler (src/app/api/profile/route.ts:12)
// Trace back:
// 1. route.ts:12 calls validateSession
// 2. session.ts:23 calls getUserEmail
// 3. user.ts:45 accesses user.email
// 4. user is undefined at this point
// Find where user should be defined
```

### 4.2 Data Flow Tracing

```typescript
// Trace data from source to error
// 1. Input source
const { userId } = await request.json();
// 2. Data fetch
const user = await db.query.users.findFirst({
  where: eq(users.id, userId),
}); // Returns undefined if not found!
// 3. Usage (crash point)
const email = user.email; // undefined.email crashes
// ROOT CAUSE: Missing null check after query
```

---

## 5. Investigation Techniques

### 5.1 Bisection

```bash

# Find the commit that introduced the bug

git bisect start
git bisect bad HEAD
git bisect good v1.2.0

# Git will checkout commits for testing

# Mark each as good/bad until found

git bisect good  # or
git bisect bad
```

### 5.2 Log Correlation

```bash

# Find related errors

grep -B5 -A5 "user-id-123" logs/*.log

# Correlate across services

grep "trace-id-abc" logs/auth.log logs/api.log logs/db.log
```

### 5.3 State Inspection

```typescript
// Add debugging to trace state
async function processOrder(order: Order) {
  console.log("1. Input:", JSON.stringify(order, null, 2));
  const validated = await validate(order);
  console.log("2. After validation:", JSON.stringify(validated, null, 2));
  const processed = await transform(validated);
  console.log("3. After transform:", JSON.stringify(processed, null, 2));
  // Find where state becomes invalid
}
```

---

## 6. Common Root Causes

### 6.1 Categories

| Category | Examples |
|----------|----------|
| Input | Missing validation, wrong types, null values |
| State | Race conditions, stale data, inconsistent state |
| Config | Wrong environment, missing secrets, bad settings |
| External | API changes, network issues, service outages |
| Logic | Wrong conditions, off-by-one, edge cases |
| Resources | Memory leaks, connection exhaustion, disk full |

### 6.2 Patterns

```typescript
// Pattern 1: Missing null check
const user = await getUser(id); // might be null
user.email; // crash!
// Pattern 2: Race condition
const balance = await getBalance();
// ... another request modifies balance ...
await setBalance(balance - amount); // wrong!
// Pattern 3: Type coercion
if (userId == "123") // "123" vs 123 issue
```

---

## 7. Documentation

### 7.1 Root Cause Template

```markdown

## Incident: [Title]

### Summary

[One sentence description]

### Timeline

- [Time] Event 1
- [Time] Event 2

### Symptom

[What users experienced]

### Immediate Cause

[Direct technical cause]

### Root Cause

[Underlying issue]

### Contributing Factors

- Factor 1
- Factor 2

### Resolution

[How it was fixed]

### Prevention

[How to prevent recurrence]
```

---

## 8. Prevention Measures

### 8.1 After Finding Root Cause

```typescript
// 1. Fix the immediate issue
- const email = user.email;
+ const email = user?.email ?? "";
// 2. Add defensive checks
+ if (!user) {
+   logger.warn("User not found", { userId });
+   return { error: "User not found" };
+ }
// 3. Add validation at boundary
+ const validated = schema.parse(input);
// 4. Add tests for the case
test("handles missing user gracefully", () => {
  expect(() => getEmail(null)).not.toThrow();
});
```

---

## Root Cause Checklist

### Investigation

- [ ] Reproduced the issue
- [ ] Gathered logs and traces
- [ ] Built timeline
- [ ] Asked "why" 5+ times
- [ ] Traced code path

### Documentation

- [ ] Root cause identified
- [ ] Contributing factors listed
- [ ] Timeline documented
- [ ] Prevention measures defined

### Prevention

- [ ] Immediate fix applied
- [ ] Tests added
- [ ] Monitoring improved
- [ ] Team informed

## Attribution

Ported from a retired internal skill library; adapted from obra/superpowers (`root-cause-tracing`), used under the MIT License:

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
