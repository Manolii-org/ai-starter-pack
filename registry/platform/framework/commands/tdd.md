---
name: tdd
version: 1.0.0
description: "Test-driven development skill — write failing test first, confirm it fails, implement until green, then refactor."
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags:
  - system
  - workflow
  - claude-code
---

# /tdd — Test-Driven Development

Guides implementation via test-first discipline. Works for TypeScript/JavaScript (Jest, Vitest, Mocha), Python (pytest, unittest), Go (`go test`), and others.

## Usage

```
/tdd [brief description of what to implement]
```

If no description is provided, ask for one before proceeding.

## Protocol

### Step 1: Understand the Requirement

1. Restate the requirement in one sentence
2. Identify the function/module/API endpoint being built or modified
3. Identify the test file location (follow existing convention)
4. If no test file exists, determine where to create it

### Step 2: Detect Test Runner

```bash
# For JS/TS:
cat package.json | python3 -c "import sys,json; s=json.load(sys.stdin).get('scripts',{}); print(s.get('test',''))"
ls vitest.config.* jest.config.* 2>/dev/null || true

# For Python: check pytest.ini, pyproject.toml, or setup.cfg
# For Go: go test is always available
```

### Step 3: Write the Failing Test FIRST

Write the test before any implementation. The test should:
- Test the **behaviour**, not the implementation details
- Have a clear, descriptive name
- Use `expect`/`assert` to verify the expected output
- Be the minimum test needed to drive the next implementation step

**Do NOT write the implementation yet.**

### Step 4: Confirm the Test FAILS

```bash
pnpm test path/to/file.test.ts --run 2>&1 | tail -20
# or: pytest tests/test_module.py::test_function_name -v
```

**Expected output:** test failure (red), NOT a compilation/import error.

If the test passes without implementation: **the test is wrong** — revise it to fail correctly.

### Step 5: Write the Minimum Implementation

Write the simplest code that makes the test pass. Do not generalise or handle edge cases not covered by the current test.

### Step 6: Confirm the Test PASSES

```bash
pnpm test path/to/file.test.ts --run 2>&1 | tail -20
```

Do not move forward until the test passes.

### Step 7: Run the Full Test Suite

```bash
pnpm test 2>&1 | tail -30
```

Fix any regressions before continuing.

### Step 8: Refactor (Optional)

Improve readability, extract helpers, remove duplication. Run tests after each refactor step.

### Step 9: Add Edge Case Tests

Add 1-2 edge case tests (empty/null input, boundary values, error path). Repeat Steps 3–7 for each.

### Step 10: Output Summary

```
## TDD Complete: {feature}

**Test file:** path/to/file.test.ts (N tests)
**Implementation:** path/to/file.ts
**Test run:** {pass count} passed, 0 failed

### Tests Added
- it('should ...'): tests {behaviour}
- it('should ...'): edge case — {scenario}
```

## Anti-Patterns to Avoid

| Anti-Pattern | Why It Fails |
|-------------|-------------|
| Writing implementation before test | Test becomes a formality |
| Tests that always pass | Provides false confidence |
| Testing implementation details | Tests break on refactor |
| Skipping the "confirm it fails" step | Can't trust a test you've never seen fail |
