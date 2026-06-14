---
name: plan-devex-review
version: 1.0.0
description: Operator-experience checklist for a plan — pure Python, zero LLM cost
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: ['plan-review', 'devex', 'deterministic']
blast_radius: low
---

# /plan-devex-review — operator-experience checklist

Based on proven patterns from the Manolii ecosystem.

Deterministic checklist for whether a plan is operationally complete. Zero LLM cost — runs `scripts/devex_check.py`.

## What it checks

For any plan that introduces or modifies a skill, command, agent, hook, or MCP server, verify the plan addresses:

1. **CLAUDE.md updated?** New skills/commands referenced? New rules surfaced?
2. **Hook integration?** New behaviour wired into the right hook (PreToolUse / PostToolUse / Stop / SessionStart)?
3. **Skill graph?** `.ai/skill-graph.md` will regenerate at Stop hook — does the new skill have `safety_tier`, `required_mcp_servers`, `blast_radius` fields in its frontmatter?
4. **Multi-IDE coverage?** New behaviour reachable from Cursor (`.cursor/rules/`)? Codex (`AGENTS.md`)? Claude Code Web (hooks fire identically)?
5. **Persistent instructions?** New rule that should land in `.claude/persistent-instructions.md`?
6. **Routing governance?** New agent has explicit `model:` in frontmatter? Restricted-tier agents stay on Anthropic?
7. **Secrets?** New env var declared in a secrets manager (Doppler, Vault, AWS SSM). `.env` files only acceptable in local dev with `.gitignore` protection.
8. **PR assessment skills count?** New specialist skill registered in `.ai/pr-assessment-skills.json`?
9. **Test file present?** New Python script paired with `_test.py`?

## Usage

```
/plan-devex-review <plan-file-or-sprint-id>
```

## Output

```
Check                            Status   Note
CLAUDE.md updated?               ?        Manual check — does plan mention CLAUDE.md?
Hook integration?                AUTO     PreToolUse touched: yes
Skill graph entries?             ?        New file detected at .claude/skills/...
... etc
```

`AUTO` = script can verify deterministically  
`?` = surfaced as a consideration item; operator decides  
`N/A` = not applicable to this plan

The script never blocks — it surfaces consideration items.

## Implementation

1. Determine plan location (read file if path provided; search `.ai/sprints/` if sprint ID)
2. Parse plan frontmatter + body for references to skills, commands, agents, hooks, MCP servers
3. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/devex_check.py" <plan-file>` and capture table output
4. Print table to terminal
5. If sprint context, write findings to `.ai/sprints/<sprint-id>/plan-devex-review.md`

## What this is NOT

- Not architecture review (that's `/plan-eng-review`)
- Not security review (that's the broad-agent + specialist skills)
- Not the pre-PR quality gate (runs against diff after implementation)

## See also

- `/plan-eng-review`, `/plan-design-review`
- `scripts/devex_check.py` — the checklist logic
- `docs/pre-pr-quality-gate.md` — post-implementation quality checks
