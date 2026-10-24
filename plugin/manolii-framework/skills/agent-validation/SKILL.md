---
name: agent-validation
description: Run affected CI checks in the coding session and batch settled candidates before pushing.
version: 1.0.2
scope: platform
author: AI Starter Pack contributors
license: Apache-2.0
type: skill
safety_tier: green
requires_mcp: []
required_entities: []
tags: [ci, testing]
---

# Agent Validation

Read `docs/agent-validation.md` and the repository's `config/agent-checks.json`.
Run `python3 scripts/agent-check.py --plan` early to inspect selected checks,
review paths and tool versions. Run the documented command with the actual
provider before pushing; retain failed/unavailable results and satisfy the
owning repository's independent review and release rules.

Reuse requires matching execution inputs, including a private environment
fingerprint. Raw tracked bytes and execute permissions bind selection and reuse,
including files hidden by Git conversions or index flags. Linked parent
directories block validation; symlinks and Gitlinks cannot reuse a cached PASS.
Keep ignored cache keys, receipts and dependency state local.
Successful setup must create every declared readiness path. Preserve owner
integration/protected branches and reconcile all existing managed surfaces.
Tracked Git filter attributes block validation until a raw-input adapter exists;
keep filter configuration intact and retain this incomplete result.

For a plugin-only install, bundled code and policy live under
`${CLAUDE_PLUGIN_ROOT}/scripts/` and `${CLAUDE_PLUGIN_ROOT}/docs/`.
The repository still owns its check recipes: do not replace its configuration
with a generic template or treat a local receipt as GitHub authorization.
