---
description: "Search .ai/memory/ for patterns, facts, and decisions matching a query"
---

# Recall — Search Memory

Search `.ai/memory/` for patterns, facts, and decisions matching a query.

## Usage

```bash
/recall {query or tag}
```

## Process

1. **Search patterns** in `.ai/memory/patterns.jsonl`:
   - Match against `problem`, `solution`, `rule`, and `tags` fields
   - Sort by confidence (highest first)

2. **Search facts** in `.ai/memory/facts.jsonl`:
   - Match against `content` and `tags` fields

3. **Display results**:

```text
Memory Recall: "{query}"
================================

Patterns (3 matches):
  [0.9] #debugging #api
    Problem: API timeout on large payloads
    Rule: Always set AbortSignal.timeout() on fetch calls
  
  [0.7] #testing
    Problem: Flaky tests due to shared state
    Rule: Each test should create its own fixtures

Facts (1 match):
  [2024-03-15] #convention
    Project uses pnpm, not npm

================================
```

## Empty Results

If no matches found, suggest:
- Broaden search terms
- List available tags: `cat .ai/memory/patterns.jsonl .ai/memory/facts.jsonl | jq -r '.tags[]' | sort -u`
