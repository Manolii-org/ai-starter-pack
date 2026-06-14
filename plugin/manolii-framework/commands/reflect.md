---
name: reflect
version: 2.0.0
description: Reflect on recent eval failures and propose patches — no API key required, works in Claude Code, Cursor, and Codex
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: [eval, reflection, self-improvement, memory]
---

# /reflect — Inline Failure Reflection

Gather failing eval context and analyze it inline — no external API call or key needed.
Works in Claude Code, Cursor, Codex, or any agent with file I/O.

## Step 1: Gather failure context

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/reflect-on-failures.py"
```

The script writes one compact JSON file per failing skill to `.ai/reflections/.staged/`.

- If no HARD failures exist → stop: "No failures to reflect on."
- If `.staged/` is empty after running → stop: "Nothing staged."

## Step 2: Analyze each staged file

Read `.ai/reflections/.staged/` and list all `*.json` files. For each staged file, perform this analysis inline:

For each file you have:
- `skill` / `case_id` — what failed
- `skill_file` — path to the skill definition
- `skill_body` — the skill instructions
- `eval_input` — the prompt given to the agent
- `failing_assertions` — what was expected but not seen
- `trajectory` — tool calls and errors (may be null)

Reason through:
1. **Bucket triage** — for each `failing_assertion`, classify into:
   - **A — Passing/leave alone**: not a real failure. Skip.
   - **B — Skill gap**: skill should cover this but instructions are missing/unclear → patch.
   - **C — Redundant**: agent already does this without the skill → consider *removing* over-prescriptive section.
   - **D — Regression**: skill *caused* the failure → highest priority, patch to remove/reframe.
2. **Root cause** — why did `failing_assertions` fail?
3. **Proposed patch** — smallest unified diff against `skill_file` that fixes the root cause.
4. **Rationale** — 2–3 sentences.
5. **Confidence** — `low` / `medium` / `high`.

Write the reflection to `.ai/reflections/{YYYY-MM-DD}-{skill}-{case_id}.md`.

## Step 3: Review list

```text
Reflections written ({N}):
  .ai/reflections/{filename}  — {skill}/{case_id}  Confidence: {value}
```

## Step 4: Walk through each proposal

```text
[{N}/{total}] {skill}/{case_id}  (Confidence: {value})
Root cause: {first sentence}
Patch: {first non-empty diff line}

Action? [apply / reject / defer / skip-all]
```

**apply**: Verify patch context still matches, apply edit, re-run eval, commit if passing.
**reject**: Move to `.ai/reflections/rejected/` with reason.
**defer**: Leave in `.ai/reflections/` unchanged.
**skip-all**: Defer all remaining.

## Step 5: Summary

```text
## Reflection Session Complete
Applied: {N}  Rejected: {N}  Deferred: {N}  Patch-failed: {N}
```

If applied > 0: "Run /evolve to reinforce patterns."

> **Note:** Requires `scripts/reflect-on-failures.py`. If not present, implement it to scan `.ai/evals/` for HARD failure records and write staged JSON files to `.ai/reflections/.staged/`.
