# Skill: Self Code Review

## Description

Automated code review before every commit. Reviews all staged changes for critical issues and blocks commit if P0/P1 issues are found.

## Activation

- **Trigger:** Before every `git commit` (invoked by pre-commit hook or manually)
- **Always active:** Yes — required before every commit

## Allowed Tools

Read, Glob, Grep, Bash(git diff --staged)

## Instructions

Before committing, review ALL staged changes (`git diff --staged`) for:

### P0 — Critical (block commit)

- Hardcoded secrets, API keys, tokens, or passwords
- SQL injection vulnerabilities (raw string interpolation in queries)
- Missing authentication on API routes
- Data deletion without confirmation or backup
- Disabled security features (CSRF, CSP, auth middleware)
- `parseInt()`/`parseFloat()` without `isNaN()` guard on security-relevant values
  (NaN comparisons silently bypass size/limit checks)
- Silent error fallbacks that defeat security intent (e.g., `|| fallbackToFullData`
  when the intent was to filter sensitive fields)
- `SECURITY DEFINER` SQL functions without `SET search_path` (schema hijack risk)

### P1 — High (block commit)

- Missing input validation on API boundaries
- Unhandled promise rejections in async code
- Missing error handling on external API calls
- Race conditions in concurrent operations
- XSS vulnerabilities (unescaped user input in rendered output)
- `NOT NULL` constraints missing in SQL when TypeScript types are non-nullable
  (runtime errors on NULL insertion)
- Audit/activity log tables with DELETE policies (should be append-only for
  non-service roles)
- User-controlled input interpolated into AI/LLM prompts without sanitization

### P2 — Medium (warn, don't block)

- Missing TypeScript types (implicit `any`)
- Functions longer than 50 lines
- Duplicated logic that should be extracted
- Missing null checks on optional chains
- Console.log statements left in production code

### P3 — Low (note for later)

- Naming convention violations
- Missing JSDoc on exported functions
- Import ordering inconsistencies
- Minor code style issues

## Output Format

```text
Code Review: {N} files, {M} issues
P0: {count} | P1: {count} | P2: {count} | P3: {count}

[P0] file.ts:42 — Hardcoded API key in header
[P1] api/route.ts:15 — No input validation on request body
[P2] utils.ts:88 — Implicit any on return type

Verdict: {PASS | BLOCK}
```

If BLOCK: list the specific fixes needed. Do not commit until P0/P1 issues are resolved.
