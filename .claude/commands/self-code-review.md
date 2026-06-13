---
description: "Review staged changes (or recent edits if nothing staged) before committing"
---

# Self-Code Review

Review staged changes (or recent edits if nothing staged) before committing.

## Steps

1. Run `git diff --cached --stat` to see staged files. If nothing staged, run `git diff --stat` for unstaged changes.
2. Run `git diff --cached` (or `git diff`) to get the full diff.
3. Review every hunk against this checklist:

### P0 — Must Fix Before Commit

- [ ] **Hardcoded secrets**: API keys, tokens, passwords, connection strings in code
- [ ] **SQL injection**: String interpolation in queries instead of parameterised bindings
- [ ] **Missing auth**: API routes without security markers (`// PUBLIC:`, `// USER:`, `// ADMIN:`, `// WEBHOOK:`)
- [ ] **XSS / injection**: Unescaped user input rendered in HTML or passed to `eval`/`exec`
- [ ] **Leaked PII**: Email addresses, phone numbers, or names in logs or error messages

### P1 — Should Fix

- [ ] **Missing error handling**: External calls (fetch, DB, file I/O) without try/catch or `.catch()`
- [ ] **Missing timeouts**: HTTP requests without `AbortSignal.timeout()` or equivalent
- [ ] **Broken types**: `any` casts, `@ts-ignore`, or missing return types on public functions
- [ ] **Console.log debris**: Debug logging left in production code paths
- [ ] **Race conditions**: Shared mutable state accessed without locks or atomic operations

### P2 — Consider

- [ ] **Large functions**: >50 lines that could be split
- [ ] **Dead code**: Unreachable branches, unused imports, commented-out blocks
- [ ] **Missing tests**: New public functions without corresponding test coverage

4. Report findings grouped by severity (P0/P1/P2). If P0 issues found, fix them immediately.
5. Update session state to mark review as done:

```bash
python3 -c "
import json, os; from pathlib import Path
f = Path('.git/.session-state.json')
s = json.loads(f.read_text()) if f.exists() else {}
s['self_review_done'] = True
tmp = f.with_suffix('.tmp')
tmp.write_text(json.dumps(s))
os.replace(tmp, f)
"
```

6. Summarise: "Self-review complete. {N} P0, {N} P1, {N} P2 issues found. {Fixed/Ready to commit}."
