---
description: "Review accumulated patterns and promote high-confidence ones to skill candidates"
---

# /evolve

Review accumulated patterns and promote high-confidence ones to skill candidates.

1. Read `.ai/memory/patterns.jsonl`
2. Filter for patterns with confidence >= 0.8 AND reinforced >= 3
3. Group by primary tag (first tag). If tags are empty/missing, use `uncategorized` as the group key.
4. For each group with 2+ qualifying patterns:
   a. Draft a skill CLAUDE.md that encodes the patterns as rules
   b. Create `.claude/skills/_candidates/{tag}/` if missing, then save `CLAUDE.md` there (sanitise tag for filesystem safety: replace `/`, `\`, spaces, `:` with hyphens)
   c. Include: description, activation trigger, the rules (from patterns), allowed tools
5. Report: how many patterns reviewed, how many promoted, which skill candidates created
6. Suggest: review candidates in `.claude/skills/_candidates/` and move to `.claude/skills/` when satisfied.
