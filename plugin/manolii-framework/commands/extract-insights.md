---
description: "Automatically extract decisions, preferences, gotchas, and facts from the current conversation and save them to memory"
---

# /extract-insights

Automatically extract decisions, preferences, gotchas, and facts from the current conversation and save them to memory.

This is the "auto-extraction" counterpart to `/remember` (manual). It scans conversation context for implicit knowledge worth persisting.

> ⚠️ **These are AI-generated inferences, not your statements.** All entries are marked `reviewed: false` and `provenance: auto-extracted` until you confirm them.

---

## Steps

### 1. Review the conversation

Look for extractable knowledge:
- **Decisions made** — "Let's use X instead of Y", "We decided to...", architectural choices
- **Preferences expressed** — "I prefer...", "Always use...", "Don't do..."
- **Gotchas discovered** — "Watch out for...", "This doesn't work because...", bugs found
- **Facts learned** — new information about the codebase, infrastructure, or domain

### 2. Apply pre-extraction filters

Before drafting candidates, scan the full conversation for content that must NOT be extracted:

**Opinion filter** — Skip content that:
- Contains subjective assessments of named individuals
- Makes pejorative or speculative claims about a person or company
- Could constitute a performance or character assessment

**Security filter** — Never store credentials, tokens, API keys, passwords, or PII. Summarise rather than quote sensitive details.

### 3. Draft candidate facts

Aim for 3-8 per significant session. For each candidate:
- Type: `decision` | `preference` | `gotcha` | `fact`
- Content: 1-2 sentences (concise, standalone — must make sense without conversation context)
- Tags: relevant categories
- Confidence: **capped at 0.5** (auto-extracted = lower confidence than manual `/remember`)

**Confidence cap:** Auto-extracted insights must not exceed 0.5. Manual `/remember` defaults to 0.8. This cap prevents AI inferences from outweighing deliberate human entries.

### 4. Deduplicate against existing memory

- Read `.ai/memory/facts.jsonl`
- Skip any candidate that duplicates or is very similar to an existing fact
- If a candidate reinforces an existing fact:
  - If the existing entry has `reviewed: true` — bump confidence by 0.05 (max 1.0) instead of creating a duplicate
  - If the existing entry has `reviewed: false` — do **not** bump beyond 0.5; instead flag it in the candidate list as "needs manual review before promotion" and prompt the user to confirm via `/remember`

### 5. Present candidates for review

```text
⚠️ Auto-extracted insights (AI-generated — not your statements, marked unreviewed until confirmed):

1. [decision] "Switched from Prisma to Drizzle for better type inference"
   tags: [database, orm] | confidence: 0.5
2. [gotcha] "Neon connection pool limit is 10 — always use pooled connection string"
   tags: [database, neon, debugging] | confidence: 0.5
3. [preference] "Use Zod for all input validation at API boundaries"
   tags: [validation, api] | confidence: 0.5

Save all? Or select specific items? [all/1,2/none]
```

Wait for user confirmation before saving.

### 6. Save approved items

Append to `.ai/memory/facts.jsonl`:

```json
{"id": "{UUID}", "type": "{TYPE}", "content": "{CONTENT}", "tags": [{TAGS}], "confidence": 0.5, "created": "{ISO_TIMESTAMP}", "reinforced_at": null, "source": "auto-extracted", "provenance": "auto-extracted", "reviewed": false}
```

---

## When to use

- At the end of a significant work session (before `/session-summary`)
- After a long conversation with many decisions
- Periodically during multi-hour sessions

---

## Relationship to other commands

- `/remember` — manual, single fact, high confidence (0.8), `reviewed: true`
- `/extract-insights` — automatic batch, multiple facts, capped confidence (0.5), `reviewed: false`
- `/learn` — extracts patterns (problem/solution/rule), not raw facts
- `/session-summary` — captures session metadata, not individual facts
