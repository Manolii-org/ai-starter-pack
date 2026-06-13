# {Title}

> **Goal:** One sentence describing what this plan accomplishes and why.

## Context

| Item | Reference |
|------|-----------|
| Branch | `codex/<slug>` |
| Related docs | — |
| Prior decisions | — |

## Files Changed

| Path | Change type | Reason |
|------|------------|--------|
| `src/...` | add / edit / delete | — |

## Tasks

1. **{Task name}**
   - What: describe the change
   - Files: `path/to/file.ts`
   - Success: `pnpm typecheck && pnpm test` passes; specific assertion passes
   - Escalate if: ambiguous requirement or touches auth/security boundary

2. **{Task name}**
   - What:
   - Files:
   - Success:
   - Escalate if:

## Test Command

```bash
{pnpm typecheck && pnpm lint && pnpm test}
```

## Out of Scope

- {List items explicitly excluded from this plan}

## Escalation Triggers

- Any change touching auth, permissions, or data isolation
- Ambiguous requirement where two reasonable interpretations exist
- Test suite fails after 2 fix attempts

## PR Description Template

```
## Summary
- {bullet 1}
- {bullet 2}

## Test plan
- [ ] Run `{test command}` locally — all pass
- [ ] {manual check}
```
