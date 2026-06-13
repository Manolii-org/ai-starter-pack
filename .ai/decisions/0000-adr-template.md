<!-- 
ADR Template for Architecture Decision Records.

Usage:
1. Copy this file and rename to NNNN-kebab-title.md (e.g., 0001-adopt-postgres-vector-search.md)
2. Use sequential numbering starting from 0001
3. Fill in all sections below
4. If decision involves AI models (add/remove/re-route), run /assess-model and attach output
5. Commit to git with message: "docs: ADR-NNNN: <title>"
6. Link from .ai/decisions/README.md once accepted
7. For wrap-up memory capture, add to .ai/memory/sessions.jsonl with ADR reference

See .ai/decisions/README.md for context.
-->

# ADR-NNNN: [Decision Title]

**Date:** YYYY-MM-DD | **Status:** Proposed | **Decision by:** [Name or team] | **Reversibility:** one-way door | two-way door

## Context

Describe the situation that led to this decision. Include:
- What problem are we solving?
- What constraints or pressures exist?
- Why is this decision needed now?

## Decision

State the decision clearly and concisely. What are we doing and why?

## Consequences

What will change as a result of this decision?

### Positive
- ...

### Negative
- ...

### Unknown/Risk
- ...

## Alternatives Considered

List other options that were evaluated and why each was rejected or selected:

1. **Option A: [Description]** — Pros: ...; Cons: ...
2. **Option B: [Description]** — Pros: ...; Cons: ...
3. **Selected: Option X** — Trade-offs and rationale

## Reversibility

- **one-way door**: Once implemented, this decision is difficult or impossible to undo (e.g., database schema change, public API contract, deleting data).
- **two-way door**: This decision can be easily reversed if we learn it was wrong (e.g., adding a config flag, choosing a library).

**This decision is: one-way door | two-way door**

**If one-way door, what's the rollback plan?** Describe how we would undo this if needed (or note if it's irreversible).

## Implementation Notes

- Acceptance criteria: ...
- Owners: ...
- Timeline: ...
- Dependencies: ...

## Related Decisions

- Links to prior ADRs that informed this one
- Links to follow-up decisions

---

**Status History:**
- YYYY-MM-DD: Created as Proposed
- YYYY-MM-DD: Moved to Accepted (if applicable)
