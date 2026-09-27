---
name: consolidate
version: 1.0.0
description: Apply time-decay to memory confidence and consolidate near-duplicate facts (Memory Evolution Phase 7)
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: [memory, consolidation, decay, maintenance]
eval_cases: null
supersedes: []
deprecation: null
---

# /consolidate — Memory Decay + Consolidation (Phase 7)

Runs the executable implementation of the time-decay formula (previously only
documented in `/prune`) plus deterministic consolidation of near-duplicate
memory entries. Intended as a periodic (≈weekly) maintenance pass over
`.ai/memory/facts.jsonl`. Local-only; no KL writes, no LLM calls.

## What it does

- **Decay** — for each entry, computes `adjusted_confidence = confidence -
  (days_since_last_seen / 365) × rate` (rate 0.1, floored at 0.1), matching
  `.claude/commands/prune.md`. The base `confidence` (peak) is left intact and
  `adjusted_confidence` is recomputed each run, so repeated runs do **not**
  compound. Adds `last_seen` (defaulting to `created`) where missing.
- **Consolidate** — within each `(entity_scope, type)` group, merges entries
  with exact or Jaccard-≥0.6 content overlap into the highest-confidence
  canonical entry: unions `tags`, reinforces `confidence` (+0.05 per duplicate,
  cap 0.95), bumps `reinforced`, keeps the most recent member `last_seen`
  (a merge is bookkeeping, not a sighting), drops the duplicates.

## Steps

### Step 1 — Dry-run (always first)

```bash
python3 scripts/memory-decay.py --file "${CLAUDE_PROJECT_DIR:-$PWD}/.ai/memory/facts.jsonl"  # project memory, report only
```

Review the stderr summary and the listed merges. Nothing is written.

### Step 2 — Apply (after reviewing the dry-run)

```bash
python3 scripts/memory-decay.py --file "${CLAUDE_PROJECT_DIR:-$PWD}/.ai/memory/facts.jsonl" --apply
```

Useful flags: `--decay-only`, `--consolidate-only`, `--threshold 0.6`,
`--rate 0.1`, `--floor 0.1`, `--file .ai/memory/<other>.jsonl`.

### Step 3 — Prune (optional)

Entries whose `adjusted_confidence` is at the floor are decay-exhausted
candidates for `/prune`. Run `/prune` separately to remove them — `/consolidate`
never deletes low-confidence entries on its own.

## Output

Report: `entries N → consolidated C (−D dropped), decayed E, below_floor F`,
plus the merge list (kept id, cluster size, dropped ids).
