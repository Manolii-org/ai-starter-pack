---
name: curator
version: 1.0.0
description: "Runs the skill lifecycle curator: detects stale skills (30d/90d thresholds), archives inactive ones, identifies overlapping skills, and produces a curation report."
type: skill
model: haiku
advisor_model: claude-sonnet-4-6
data_sensitivity: internal
max_tokens: 400
safety_tier: green
requires_mcp: []
required_entities: []
tools:
  - Bash
tags:
  - skill-management
  - maintenance
intent_phrases:
  - "curate skills"
  - "clean up skills"
  - "run the skill curator"
  - "find stale skills"
  - "detect duplicate skills"
---

# Skill: Curator

Runs `scripts/skill-curator.py` to detect and archive stale skills and identify overlaps.

## Usage

Invoke with `/curator` — no arguments needed for a standard run.

Options (pass via user message):
- "dry run" → adds `--dry-run` (no mutations)
- "report only" → adds `--report-only` (no archive, no overlap detection)

## Execution

```bash
python3 scripts/skill-curator.py [--dry-run] [--report-only] 2>&1
```

Report is written to `.ai/skill-curation/REPORT-{date}.md`.

## Output

Return the final 30 lines of curator output plus the report path. Under 200 words. No preamble.
