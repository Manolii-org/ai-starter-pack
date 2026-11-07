---
name: tool-design
version: 1.0.0
description: "Effective tool design for AI agents. Use when building MCP tools, designing agent capabilities, or optimizing tool interfaces. Covers tool architecture and best practices."
type: skill
data_sensitivity: internal
safety_tier: green
requires_mcp: []
required_entities: []
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
tags:
  - agents
  - mcp
  - design
---

# Tool Design Skill

Design tools that AI agents can use effectively with clear interfaces and predictable behavior.
---

## Quick Reference

```typescript
// Good tool design
interface Tool {
  name: string;              // Clear, action-oriented
  description: string;       // When and why to use
  parameters: JSONSchema;    // Strict, validated
  returns: JSONSchema;       // Predictable output
}
```

---

## 1. Design Principles

### 1.1 Clarity

```typescript
// ❌ WRONG - Vague name and description
{
  name: "do_stuff",
  description: "Does various things",
}
// ✅ CORRECT - Clear purpose
{
  name: "search_codebase",
  description: "Search for code patterns using regex. Use when finding function definitions, imports, or specific patterns across files.",
}
```

### 1.2 Single Responsibility

```typescript
// ❌ WRONG - Does too much
{
  name: "manage_user",
  description: "Creates, updates, deletes, and retrieves users",
  // Too many responsibilities!
}
// ✅ CORRECT - Focused tools
{ name: "create_user", description: "Create a new user account" }
{ name: "get_user", description: "Retrieve user by ID or email" }
{ name: "update_user", description: "Update user profile fields" }
{ name: "delete_user", description: "Soft-delete a user account" }
```

### 1.3 Predictability

```typescript
// ❌ WRONG - Unpredictable output
{
  name: "get_data",
  returns: "string | object | array | null", // What will it be?
}
// ✅ CORRECT - Consistent structure
{
  name: "get_data",
  returns: {
    type: "object",
    properties: {
      success: { type: "boolean" },
      data: { type: "array", items: { type: "object" } },
      error: { type: ["string", "null"] },
    },
  },
}
```

---

## 2. Parameter Design

### 2.1 Required vs Optional

```typescript
// ❌ WRONG - Everything optional, confusing
{
  parameters: {
    query: { type: "string" },        // Required but not marked
    limit: { type: "number" },        // Optional but not marked
    format: { type: "string" },       // What's the default?
  },
}
// ✅ CORRECT - Clear requirements
{
  parameters: {
    type: "object",
    required: ["query"],
    properties: {
      query: {
        type: "string",
        description: "Search pattern (regex supported)",
      },
      limit: {
        type: "number",
        default: 10,
        description: "Max results (1-100)",
      },
      format: {
        type: "string",
        enum: ["json", "text"],
        default: "json",
      },
    },
  },
}
```

### 2.2 Validation

```typescript
// ❌ WRONG - No constraints
{
  parameters: {
    type: "object",
    properties: {
      age: { type: "number" },
      email: { type: "string" },
    },
  },
}
// ✅ CORRECT - Validated inputs
{
  parameters: {
    type: "object",
    properties: {
      age: {
        type: "integer",
        minimum: 0,
        maximum: 150,
      },
      email: {
        type: "string",
        format: "email",
        maxLength: 255,
      },
    },
    required: ["email"],
  },
}
```

---

## 3. Output Design

### 3.1 Structured Output

```typescript
// ❌ WRONG - Unstructured text
return "Found 5 files: file1.ts, file2.ts...";
// ✅ CORRECT - Structured data
return {
  count: 5,
  files: [
    { path: "src/file1.ts", matches: 3 },
    { path: "src/file2.ts", matches: 1 },
  ],
  truncated: false,
};
```

### 3.2 Error Handling

```typescript
// ❌ WRONG - Throws exceptions
function searchFiles(query: string) {
  if (!query) throw new Error("Query required");
  // ...
}
// ✅ CORRECT - Returns structured errors
function searchFiles(query: string): ToolResult {
  if (!query) {
    return {
      success: false,
      error: {
        code: "INVALID_QUERY",
        message: "Query parameter is required",
        suggestion: "Provide a search pattern like 'function.*login'",
      },
    };
  }
  // ...
}
```

---

## 4. Tool Categories

### 4.1 Read Tools

```typescript
// Safe, idempotent, no side effects
const readTools = [
  { name: "read_file", risk: "low" },
  { name: "search_code", risk: "low" },
  { name: "get_user", risk: "low" },
  { name: "list_files", risk: "low" },
];
```

### 4.2 Write Tools

```typescript
// Modify state, require confirmation
const writeTools = [
  { name: "edit_file", risk: "medium", confirmFor: "large changes" },
  { name: "create_file", risk: "medium" },
  { name: "delete_file", risk: "high", requireConfirm: true },
];
```

### 4.3 Execute Tools

```typescript
// Run commands, highest risk
const executeTools = [
  { name: "run_command", risk: "high", sandbox: true },
  { name: "deploy", risk: "critical", requireApproval: true },
];
```

---

## 5. MCP Tool Patterns

### 5.1 Resource Pattern

```typescript
// Expose fixed data as resources and parameterized data as resource templates
{
  resources: [
    {
      uri: "config://app/settings",
      name: "App Settings",
      mimeType: "application/json",
    },
  ],
  resourceTemplates: [
    {
      uriTemplate: "file:///src/{path}",
      name: "Source file",
      mimeType: "text/plain",
      description: "Source code files",
    },
    {
      uriTemplate: "db://users/{id}",
      name: "User record",
      mimeType: "application/json",
      description: "User records",
    },
  ],
}
```

### 5.2 Prompt Pattern

```typescript
// Provide contextual prompts
{
  prompts: [
    {
      name: "code_review",
      description: "Review code for issues",
      arguments: [
        { name: "file", description: "Path of the file to review", required: true },
        // Prompt arguments have only name/description/required; describe allowed values in text.
        { name: "focus", description: "One of: security, performance, style", required: false },
      ],
    },
  ],
}
```

---

## 6. Documentation

### 6.1 Tool Description Template

```typescript
{
  name: "action_noun",
  description: `
    Brief description of what the tool does.
    Use when:
    - Scenario 1
    - Scenario 2
    Returns:
    - On success: { data: [...] }
    - On error: { error: { code, message } }
    Example:
    action_noun({ param: "value" })
    => { data: [{ id: 1, ... }] }
  `,
}
```

### 6.2 Parameter Documentation

```typescript
{
  parameters: {
    query: {
      type: "string",
      description: `
        Search pattern to match.
        - Supports regex: /pattern/flags
        - Case-insensitive by default
        - Examples: "function login", "/async.*await/i"
      `,
    },
  },
}
```

---

## 7. Testing Tools

### 7.1 Test Cases

```typescript
describe("search_files tool", () => {
  test("returns matches for valid query", async () => {
    const result = await searchFiles({ query: "function" });
    expect(result.success).toBe(true);
    expect(result.files.length).toBeGreaterThan(0);
  });
  test("returns empty for no matches", async () => {
    const result = await searchFiles({ query: "xyznotfound" });
    expect(result.success).toBe(true);
    expect(result.files).toEqual([]);
  });
  test("returns error for invalid regex", async () => {
    const result = await searchFiles({ query: "[invalid" });
    expect(result.success).toBe(false);
    expect(result.error.code).toBe("INVALID_REGEX");
  });
});
```

---

## 8. Anti-Patterns

| Pattern | Issue | Fix |
|---------|-------|-----|
| God tool | Does everything | Split into focused tools |
| Vague params | Ambiguous inputs | Add constraints, examples |
| String soup | Unstructured output | Return structured objects |
| Silent failure | No error info | Return error details |
| No limits | Can overwhelm | Add pagination, limits |

---

## Tool Design Checklist

### Interface

- [ ] Clear, action-oriented name
- [ ] Specific description with use cases
- [ ] Well-defined parameters
- [ ] Predictable return type

### Safety

- [ ] Input validation
- [ ] Error handling
- [ ] Rate limiting (if applicable)
- [ ] Confirmation for destructive ops

### Documentation

- [ ] Usage examples
- [ ] Parameter descriptions
- [ ] Error codes documented
- [ ] Edge cases noted

## Attribution

Ported from a retired internal skill library; adapted from muratcankoylan/Agent-Skills-for-Context-Engineering (`tool-design`), used under the MIT License:

```text
MIT License

Copyright (c) 2025 Context Engineering Agent Skills Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
