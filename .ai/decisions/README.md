# Architecture Decision Records (ADRs)

## What is an ADR?

An Architecture Decision Record (ADR) documents a significant architectural or technical decision made by the team, along with the context, options considered, and consequences. ADRs create a permanent record of *why* decisions were made, not just *what* was decided.

## When to Write an ADR

Write an ADR for decisions that meet one or more of these criteria:

- **One-way doors**: Decisions that are difficult or impossible to undo (database schema changes, public API contracts, major architectural shifts)
- **Irreversible resource commitments**: Selecting infrastructure, choosing primary libraries, or committing to long-term vendor relationships
- **Security or compliance boundaries**: Authentication schemes, permission models, data isolation patterns
- **AI model changes**: Adding, removing, or re-routing AI models — always run the `assess-model` skill and attach its output to the ADR
- **Complex trade-offs**: Decisions with significant pros/cons that future team members need to understand
- **Architectural patterns**: Choices about how core systems interact or are structured

## When NOT to Write an ADR

- Tactical implementation details (which function to refactor)
- Temporary workarounds or debugging steps
- Configuration changes that don't affect design
- Decisions already documented in code comments or issue discussions

## Naming and Numbering

- **Filename format**: `NNNN-kebab-title.md` (e.g., `0001-adopt-postgres-vector-search.md`)
- **Numbering**: Sequential starting from `0001`; do not reuse numbers
- **Title**: Concise, decisive phrasing (action verb if possible: "Adopt X", "Move to Y", "Split Z")

## Status Lifecycle

- **Proposed**: Decision is being considered; gather feedback
- **Accepted**: Team has agreed to implement; move forward
- **Superseded by ADR-XXXX**: This decision has been reversed by a later ADR; record the link
- **Deprecated**: Still in effect but discouraged for future use; newer pattern preferred

## Superseding an ADR

Accepted ADRs are **immutable** — do not edit the decision after acceptance. To change course, write a NEW ADR that (1) states the new decision and why the prior one no longer holds, and (2) references the superseded ADR by number. Then set the prior ADR's status to `Superseded by ADR-XXXX` and **keep its file** — the history is the point. Use `Deprecated` (not `Superseded`) when a decision is discouraged but has no direct replacement.

## Template

Copy `0000-adr-template.md` as your starting point. It includes all required sections and instructions.

## Process

1. Create a new ADR from the template
2. Fill in context, decision, consequences, and alternatives
3. Commit to git with message: `docs: ADR-NNNN: <title>`
4. Link from this README once accepted
5. At end-of-session, use the `memory-keeper` agent to capture ADRs into session memory (`.ai/memory/sessions.jsonl`)

## AI Model Changes

For any ADR involving AI model decisions (adding, removing, or re-routing models):
1. Run the `assess-model` skill
2. Attach or summarize its findings in the ADR under a **"Model Assessment"** section
3. Include the skill's recommendations and cost/capability trade-offs

## Living Document

ADRs are immutable once accepted but can be superseded. To reverse a decision, create a new ADR that references the superseded one.

---

**ADR Index:**
(Add links to accepted ADRs here as they are created)
