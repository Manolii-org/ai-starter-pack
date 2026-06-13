# /plan-review — Architecture & Scope Review

Rigorous pre-implementation review combining CEO-level scope challenge with engineering architecture lock-in.

Based on proven patterns from the Manolii ecosystem.

## Prerequisites

- A plan must exist in `.claude/plans/` (create one with `/plan` first)
- Or: a clear task description to review before implementation

## Process

### Phase 1: Scope Challenge (CEO Review)

Before writing any code, answer these forcing questions:

#### Six Forcing Questions

1. **What problem are we solving?** (1 sentence, no jargon)
2. **Who has this problem?** (specific user/persona, not "everyone")
3. **How do they solve it today?** (current workaround — if none exists, question whether it's a real problem)
4. **What's the simplest version that would be useful?** (cut scope ruthlessly)
5. **What are we explicitly NOT building?** (anti-scope prevents creep)
6. **How will we know it worked?** (measurable success criteria)

#### Scope Decision

After answering, classify the scope:
- **Reduce** — The plan is too ambitious. Cut to the minimal useful version.
- **Hold** — The scope is right. Proceed as planned.
- **Selective Expand** — One specific area needs more depth; the rest stays lean.
- **Expand** — The plan is too conservative for the opportunity. Add capabilities.

### Phase 2: Engineering Review

Lock the architecture before coding:

#### a. Data Flow Diagram

Trace the data path from input to output:

```text
User Action → API Route → Server Action/Function → Database → Response → UI Update
```

Identify every transformation, validation, and side-effect along the path.

#### b. Edge Cases

List edge cases for EVERY interface boundary:
- Empty inputs, null values, missing fields
- Concurrent access (two users editing the same record)
- Network failures mid-operation
- Token/session expiry during a flow
- Rate limits on external APIs
- Serverless timeout (check project's `vercel.json` for `maxDuration`; defaults vary by Vercel plan)

#### c. Security Boundaries

For each new route/endpoint:
- What security marker applies? (PUBLIC/ADMIN/USER/WEBHOOK/AGENT)
- What input validation is needed? (Zod schema)
- What auth check is required?
- What data can this endpoint expose? (PII considerations)

#### d. Test Matrix

Define what must be tested before shipping:

| Component | Test Type | Priority | Approach |
|-----------|----------|----------|----------|
| {route} | Integration | P0 | API test with valid/invalid payloads |
| {component} | Unit | P1 | Render + interaction test |
| {flow} | E2E | P1 | End-to-end flow test (browser if UI, API if backend) |
| {edge case} | Unit | P2 | Boundary value test |

#### e. Dependency Check

- Does this change affect other repos? (Check ecosystem map in CLAUDE.md)
- Does this need database migration? (Rollback plan required)
- Does this need new environment variables? (Doppler update needed)
- Does this change a shared API contract? (Coordinate via master repo)

### Phase 3: Decision & Lock

Output a locked plan:

```markdown
## Architecture Decision Record

**Decision:** {what we're building}
**Scope:** {Reduce|Hold|Selective Expand|Expand}
**Anti-scope:** {what we're NOT building}

### Data Flow
{diagram}

### Edge Cases Covered
{list}

### Security
{markers, validation, auth}

### Test Plan
{matrix}

### Dependencies
{cross-repo impacts, migrations, env vars}

### Success Criteria
{measurable outcomes}

**Status:** LOCKED — do not change architecture without re-running /plan-review
```

Save to `.claude/plans/{slug}-review.md`.

## Output Format

```text
Plan Review — {TITLE}
================================

SCOPE: {Reduce|Hold|Selective Expand|Expand}
Reason: {1-2 sentences}

FORCING QUESTIONS:
  1. Problem: {answer}
  2. Who: {answer}
  3. Current solution: {answer}
  4. Simplest useful: {answer}
  5. Anti-scope: {answer}
  6. Success metric: {answer}

ARCHITECTURE: LOCKED
  Data flow: {summary}
  Edge cases: {count} identified
  Security: {markers assigned}
  Tests: {count} planned

CROSS-REPO IMPACT: {none | list}
MIGRATION NEEDED: {yes/no}
NEW ENV VARS: {yes/no — list if yes}

Verdict: PROCEED | REVISE | BLOCK
================================
```

## When to Use

- Before starting any feature >2 hours of work
- Before any database schema change
- Before any cross-repo change
- Before any security-sensitive change
- When scope feels unclear or too large

## Next Steps

After `/plan-review` produces a PROCEED verdict:
1. Implement according to the locked architecture
2. Run self-code-review before committing (invoke the `self-code-review` skill or pre-commit hook)
3. Run `/qa` if the change has UI impact
4. Run `/canary` after deployment
