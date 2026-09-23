---
description: "Run the complete validation pipeline and report results"
---

# Verify — Full Validation Suite

Run the complete validation pipeline and report results.

## Detect Environment

Before running commands, detect the project's package manager and available scripts:

1. Check for `pnpm-lock.yaml` → use `pnpm`
2. Check for `yarn.lock` → use `yarn`
3. Check for `package-lock.json` → use `npm`
4. Check for `pyproject.toml` / `requirements.txt` → use Python tooling (`pytest`, `ruff`, etc.)
5. If no `package.json` exists (e.g., orchestration repos with only SQL/markdown),
   skip TypeScript/lint/test/build and instead validate: SQL syntax, markdown
   formatting, internal consistency of documentation counts and references.

Use the detected package manager for all commands below (substitute `npm` → `pnpm`/`yarn` as needed).

## Steps

### 1. TypeScript Check

```bash
pnpm typecheck  # or: npx tsc --noEmit
```

Report: error count, file locations

### 2. Lint

```bash
pnpm lint  # or: npx eslint .
```

Report: warning count, error count, auto-fixable count

### 3. Tests

```bash
pnpm test  # or: npm test
```

Report: pass/fail/skip counts, failing test names

### 4. Build

```bash
pnpm build  # or: npm run build
```

Report: success/failure, output size if available

> **Note:** The build step is run by `/verify` and CI but intentionally excluded from the local pre-push hook to keep pushes fast. Pre-push runs typecheck + lint + tests only.

## On Failure

For each failing check:
1. Report the specific errors
2. Attempt to fix automatically if the fix is clear and scoped
3. Re-run the specific check to confirm the fix
4. If unable to fix, report clearly what needs manual attention

## Output

```text
Verification Report — {DATE}
==============================
TypeScript:  ✓ 0 errors
Lint:        ✗ 3 errors, 7 warnings (2 auto-fixable)
Tests:       ✓ 42 passed, 0 failed, 2 skipped
Build:       ✓ success
==============================
Status: FAIL (lint errors)
```
