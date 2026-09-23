# Skill: Verification Before Completion

## Description

Ensures every task is fully verified before being marked complete. Prevents shipping partially-working changes.

## Activation

- **Trigger:** Before marking any task as complete, before committing, before declaring "done"
- **Always active:** Yes — this is a meta-skill that governs task completion

## Allowed Tools

Read, Glob, Grep, Bash

## Instructions

Before marking ANY task as complete, verify ALL of the following:

### 0. Detect Environment

Before running any commands, detect the project setup:
- Check for `pnpm-lock.yaml` → use `pnpm`; `yarn.lock` → use `yarn`; else `npm`
- If no `package.json` (e.g., orchestration repos), skip steps 1-4 and instead
  verify: SQL syntax, markdown consistency, migration rollback files exist
- Use the project's actual script names from `package.json` (e.g., `pnpm typecheck`
  not `npx tsc --noEmit` if a `typecheck` script exists)

### 1. Code Compiles

```bash
pnpm typecheck  # or: npx tsc --noEmit
```
Zero type errors. Not "just warnings" — zero errors.

### 2. Tests Pass

```bash
pnpm test  # or: npm test
```
All existing tests still pass. No skipped tests that were previously passing.

### 3. Lint Clean

```bash
pnpm lint  # or: npx eslint .
```
No new lint errors introduced by your changes.

### 4. Build Succeeds

```bash
pnpm build  # or: npm run build
```
The project builds successfully. This catches issues that typecheck alone misses (dead code elimination, import resolution, etc.).

### 5. Change Works End-to-End

Don't just verify in isolation. Trace the change through the full flow:
- If you changed an API route → call it with realistic data
- If you changed a component → verify it renders in context
- If you changed a database query → verify with real data shapes
- If you changed a background job → verify it processes correctly

### 6. No Regressions

- Run the full test suite, not just tests for your change
- Check that related features still work
- Verify that error handling paths still function

### 7. Clean Git State

- No unintended file changes (`git status`)
- No debug code left in (console.log, debugger, TODO hacks)
- No commented-out code that should be deleted

### Completion Checklist

Before saying "done", confirm:
- [ ] Types pass
- [ ] Tests pass
- [ ] Lint clean
- [ ] Build succeeds
- [ ] Works end-to-end (not just in isolation)
- [ ] No regressions
- [ ] No debug artifacts

If ANY check fails, the task is NOT complete. Fix the issue first.
