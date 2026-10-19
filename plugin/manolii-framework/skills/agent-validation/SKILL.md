---
name: agent-validation
description: Run affected CI checks in the coding session and batch settled candidates before pushing.
version: 1.0.0
scope: platform
type: skill
safety_tier: green
requires_mcp: []
required_entities: []
tags: [ci, testing]
---

Read `docs/agent-validation.md` and the repository's `config/agent-checks.json`.
Run `python3 scripts/agent-check.py --plan` early to inspect selected checks,
review paths and tool versions. Run the documented command with the actual
provider before pushing; retain failed/unavailable results and satisfy the
owning repository's independent review and release rules.

For a plugin-only install, bundled code and policy live under
`${CLAUDE_PLUGIN_ROOT}/scripts/` and `${CLAUDE_PLUGIN_ROOT}/docs/`.
The repository still owns its check recipes: do not replace its configuration
with a generic template or treat a local receipt as GitHub authorization.
