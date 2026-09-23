---
description: "Generate unit and integration tests for recently changed or specified files"
---

# /gen-tests — Generate Tests for Changed Code

Generate unit and integration tests for recently changed or specified files.

## Arguments

`$ARGUMENTS` — optional file path(s) or glob pattern. If omitted, tests are generated for all files changed on the current branch vs main.

## Process

### 1. Identify Target Files

If arguments provided:
- Use the specified file path(s) or glob pattern

If no arguments:

```bash
git diff --name-only main...HEAD -- '*.ts' '*.tsx' '*.js' '*.jsx'
```

Filter to only source files (exclude test files, configs, types-only files).

### 2. Analyze Each File

For each target file:
1. Read the file to understand exports, functions, and classes
2. Check if a test file already exists (look for `*.test.ts`, `*.spec.ts`, `__tests__/*`)
3. Identify the testing framework from `package.json` (Vitest, Jest, or Playwright)
4. Identify what needs testing:
   - **Pure functions** → unit tests with edge cases
   - **API routes** → request/response tests with auth and error cases
   - **Server actions** → input validation, happy path, error path
   - **React components** → render tests, interaction tests, prop variations
   - **Database queries** → mock DB layer, test query logic
   - **Utilities** → boundary values, type coercion, null/undefined handling

### 3. Generate Tests

For each file, generate tests following these principles:

**Structure:**
- One `describe` block per exported function/component
- Descriptive test names: `it('should return empty array when input is null')`
- Arrange → Act → Assert pattern
- Group by: happy path, edge cases, error cases

**Coverage targets:**
- All exported functions have at least one test
- Error/throw paths are tested
- Boundary values (empty string, 0, null, undefined, MAX_SAFE_INTEGER)
- Return type variations (when function can return different shapes)

**What NOT to test:**
- Private implementation details
- Framework internals (don't test that React renders)
- Simple type re-exports or constants
- Third-party library behavior

**Mocking:**
- Mock external dependencies (DB, HTTP, file system) at the boundary
- Never mock the unit under test
- Use the project's preferred mocking approach (check existing tests)

### 4. Write Test Files

- Place tests adjacent to source: `foo.ts` → `foo.test.ts` (or match existing project convention)
- If `__tests__/` directory exists, place there instead
- Import from the source file using the same alias pattern as the project (`@/`, relative, etc.)

### 5. Validate

Run the generated tests:

```bash
# Use project's test runner
npm test -- {test-file-path}
# Vitest: npm test -- --run {test-file-path}
# Jest:   npm test -- {test-file-path} --watchAll=false
```

If tests fail:
1. Read the error output
2. Fix the test (not the source code)
3. Re-run until green

### 6. Summary

```text
Test Generation Report
======================
Files analyzed: {N}
Test files created: {N}
Test files updated: {N}
Tests generated: {N}
  - Unit tests: {N}
  - Integration tests: {N}
Tests passing: {N}/{N}

New test files:
  {path/to/foo.test.ts} — {N} tests for {foo.ts}
  {path/to/bar.test.ts} — {N} tests for {bar.ts}
======================
```
