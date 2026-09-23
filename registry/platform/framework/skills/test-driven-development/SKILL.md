# Skill: Test-Driven Development

## Description

TDD workflow and testing best practices for reliable, maintainable test suites.

## Activation

- **Trigger:** When writing tests, implementing features that need tests, or reviewing test coverage
- **Always active:** No — activated by context

## Allowed Tools

Read, Glob, Grep, Edit, Write, Bash

## Instructions

### TDD Cycle

1. **Red:** Write a failing test that describes the desired behavior
2. **Green:** Write the minimum code to make the test pass
3. **Refactor:** Clean up while keeping tests green

Don't skip the Red step. Writing the test first forces you to think about the API before the implementation.

### Test Structure

```typescript
describe("calculateDiscount", () => {
  it("applies 10% discount for orders over $100", () => {
    const result = calculateDiscount({ subtotal: 150, tier: "standard" });
    expect(result.discount).toBe(15);
    expect(result.total).toBe(135);
  });

  it("returns zero discount for orders under $100", () => {
    const result = calculateDiscount({ subtotal: 50, tier: "standard" });
    expect(result.discount).toBe(0);
  });

  it("throws for negative subtotal", () => {
    expect(() => calculateDiscount({ subtotal: -10, tier: "standard" })).toThrow("Subtotal must be positive");
  });
});
```

### What to Test

- **Pure functions:** Input → output. Test edge cases: empty, null, boundary values.
- **API routes:** Request → response. Test auth, validation, success, and error paths.
- **Data transformations:** Property-based testing for invariants.
- **Error paths:** Network failures, invalid data, permission denied.

### What NOT to Test

- Framework internals (Next.js routing, React rendering lifecycle)
- Third-party library behavior (test YOUR integration, not THEIR logic)
- Simple getters/setters with no logic
- Private implementation details — test through public API

### Assertions

```typescript
// GOOD — specific, meaningful assertions
expect(result.status).toBe(201);
expect(result.body.id).toMatch(/^usr_[a-z0-9]+$/);
expect(result.body.createdAt).toBeInstanceOf(Date);

// BAD — vague, meaningless assertions
expect(result).toBeTruthy();
expect(response.status).toBeLessThan(500);
expect(data).toBeDefined();
```

### No Over-Mocking

- Mock external services (APIs, databases in unit tests)
- Don't mock the thing you're testing
- Don't mock internal modules — if you need to mock everything, the design needs refactoring
- Prefer integration tests with real databases (test containers) over heavily mocked unit tests

### Property-Based Testing

For data transformations, use property-based testing:
```typescript
import * as fc from "fast-check";

// "encoding then decoding always returns the original"
fc.assert(
  fc.property(fc.string(), (input) => {
    expect(decode(encode(input))).toBe(input);
  })
);
```

### Test Isolation

- Each test creates its own data — no shared mutable state
- Tests must be order-independent — runnable in any sequence
- Clean up after tests (database rows, temp files, mocks)
- Use `beforeEach` for setup, not `beforeAll` (unless truly shared immutable state)
