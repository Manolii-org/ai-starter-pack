---
description: "Save a fact, decision, or preference to memory"
---

# Remember — Quick Save Fact

Save a fact, decision, or preference to memory.

## Usage

```text
/remember {content}
```

## Process

1. Parse the content
2. Determine type: `fact`, `decision`, `preference`, or `convention`
3. Generate relevant tags from the content
4. **Dedup check** — read `.ai/memory/facts.jsonl` and scan for entries with similar content:
   - If a closely matching entry exists with `reviewed: true` — bump its confidence by 0.05 (max 1.0), update `reinforced_at`, and skip creating a new entry. Report: "Reinforced existing entry."
   - If a closely matching entry exists with `reviewed: false` — ask the user whether to reinforce or replace it, since auto-extracted entries may be imprecise
   - If no match — proceed to save
5. Append new entry to `.ai/memory/facts.jsonl`:

```json
{"id": "{UUID}", "created": "{ISO_DATE}", "type": "{TYPE}", "content": "{CONTENT}", "tags": ["tag1", "tag2"], "confidence": 0.8, "source": "manual", "provenance": "manual", "reviewed": true, "reinforced_at": null}
```

## Examples

- `/remember Project uses PostgreSQL 15 with RLS enabled` → type: `fact`, tags: `database`, `postgres`, `security`
- `/remember We decided to use Zod for all API validation` → type: `decision`, tags: `validation`, `api`, `zod`
- `/remember Always run migrations in a transaction` → type: `convention`, tags: `database`, `migrations`
- `/remember I prefer pnpm over npm for all projects` → type: `preference`, tags: `tooling`, `package-manager`

## Optional: Remote Memory Backend

If a remote memory MCP tool is configured (e.g. a knowledge layer, Notion, Linear, or similar), also write there for cross-session persistence and semantic search. Use the same content and tags.

## Confirmation

After saving, display:

```text
Saved: {type} — "{content}" [tags: {tags}]
```
