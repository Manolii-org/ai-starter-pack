---
description: "Create or update an implementation plan for the current task"
---

# Plan — Implementation Planning

Create or update an implementation plan for the current task.

## Process

1. **Read context**: Check existing plans in `.claude/plans/`, review the task description
2. **Structure the plan**:

```markdown
# Plan: {TITLE}

## Goal

{What we're trying to achieve and why}

## Phases

### Phase 1: {NAME}

- [ ] Task 1.1: {description}
- [ ] Task 1.2: {description}
**Verification**: {how to verify this phase is complete}

### Phase 2: {NAME}

- [ ] Task 2.1: {description}
- [ ] Task 2.2: {description}
**Verification**: {how to verify this phase is complete}

## Risks

- {Risk 1}: {mitigation}
- {Risk 2}: {mitigation}

## Dependencies

- {External dependency or blocker}

## Definition of Done

- [ ] All phases verified
- [ ] Tests pass
- [ ] No lint/type errors
- [ ] Changes reviewed
```

3. **Save** to `.claude/plans/{slug}.md`

## Updating

If a plan already exists, update task checkboxes and add notes on what changed.

## Next Step

For features >2 hours of work, database changes, cross-repo changes, or security-sensitive work:
run `/plan-review` to challenge scope and lock architecture before coding.
