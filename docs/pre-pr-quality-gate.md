# Pre-PR Quality Gate

> Complete all checks below before creating any PR.
> Skipping this gate is the single largest source of avoidable review cycles.

## 0. Check for Coverage Gaps

Before any other step:

```bash
for sha in $(git log --format="%h" -5); do grep "$sha" .ai/bypass-log.jsonl 2>/dev/null; done || true
```

For each bypassed specialist scope in recent commits, apply the manual check:

| Bypassed scope | Manual compensation |
|---|---|
| `shell-security` | Complete Step 1b (Shell Script Semantics) below |
| `docs-fact-check` | Complete Step 2 (Cross-Check Documentation) |
| `migration-safety` | Verify rollback file, BEGIN/COMMIT wrapper, IF NOT EXISTS guards |
| `security-deep-dive` | Manually trace all input boundaries for unsanitised interpolation |

## 1. Self-Review the Diff

**For files created FROM SCRATCH in this session:** dispatch `review-internal` before committing:

```
Agent(subagent_type="review-internal", model="haiku",
  prompt="Review this diff for semantic correctness and edge cases.
  Do not praise correct code — report findings only.
  Focus: (1) shell semantics if .sh files present; (2) cross-file consistency;
  (3) edge cases for new logic paths.
  Diff: <git diff HEAD -- <new-file-path>>
  Limit: 15 tool calls. Report under 120 words.")
```

`diff-reflex` is CRITICAL-only and is insufficient for this purpose. Use `review-internal`.

**For modified existing files**, self-review by running `git diff` and reading every changed line. Verify:
- No hardcoded URLs, credentials, or magic values that should come from config/env
- No broad `except`/`catch` handlers that swallow errors silently
- Type consistency at every boundary
- No `null`/`None` dereferences on values that can be absent
- Error codes match their semantics (don't return 400 with a "403 Forbidden" message)
- Regex patterns tested against edge cases

## 1b. Shell Script Semantics (when any `.sh` file is created or modified)

Common shell bugs that pass `shellcheck` but fail semantically:

- **File test flags:** `-f` tests existence only; `-s` tests existence AND non-empty size. Use `-s` when you need a usable file.
- **`2>/dev/null` on file I/O:** Suppresses real errors (permissions, disk full). Remove it on file I/O. Acceptable only on "does this exist" checks.
- **Git command scope:** `git diff` = unstaged only; `git diff HEAD` = all changes vs last commit; `git diff --cached` = staged only.
- **Cross-file instruction consistency:** Every manual instruction in a companion `.md` file that references a script command must byte-for-byte match the actual script.

## 2. Cross-Check Documentation Against Implementation

For every claim in docs/guides/comments that describes system behavior:
- Grep the actual code to verify the claim is true
- Confirm parameters documented as "optional" have actual defaults in code
- Verify tool/function names match their canonical definitions

## 3. Run Tests for Changed Files

Execute all tests for changed files. If tests fail, fix before submitting. If no tests exist for critical paths, write them:
- Verify test files import the correct module (not a stale path)
- Verify test assertions match test names
- Verify stubs exercise the real code path, not just mock files

## 4. Adversarial Security Tests (for security-claiming PRs)

If the PR claims to enforce a security boundary (silo, allowlist, entity isolation, input sanitisation):
- Write at least one test that **tries to break** the boundary
- Test the error/fallback path, not just the happy path
- Verify that request bodies and error messages don't leak untrusted content

## 5. Diff Against Prior Implementation (for rewrites/replacements)

When replacing existing functionality:
- Read the current implementation FIRST
- After writing the replacement, diff old vs new and verify no existing behavior was dropped
- Check: entity parameters still passed, filter conditions still applied, null checks still present

## 6. Verify Config-Derived Values Match Source of Truth

If the code contains a hardcoded set/list/map (model names, agent names, error codes):
- Check it matches the canonical source file (routing config, migrations)
- If the list can drift, derive it dynamically at runtime

## 7. Simplicity Audit

Is this the minimal diff that achieves the goal? Ask:
- Could 50%+ of the changed lines be removed while preserving the same behavior?
- Does the change introduce a new abstraction with only one call site?
- Would a senior engineer say "this is more than was needed"?

If yes to any — simplify before submitting.
