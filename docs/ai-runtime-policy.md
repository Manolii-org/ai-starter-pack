# AI Runtime Policy (Cross-Tool Source of Truth)

> **Authoritative for:** Cross-tool AI runtime policy contract. **Layer:** L2. **Last verified:** 2026-05-13.
>
> If this conflicts with another surface, the lower-numbered layer wins. See `.ai/knowledge-map.md` for the full hierarchy.


This document is the **shared policy contract** for AI coding tools in this repo
(Claude Code, Codex, Cursor, Gemini). Tool-specific files are adapters.

## Purpose

Keep one central policy for critical constraints, while preserving existing
Claude hook workflows and Codex preflight workflows.

Portable repository orientation is compiled from canonical contracts with
`python3 scripts/build-context-bundle.py --runtime <runtime> --repo <owner/name> --dry-run`.
The compiler is a baseline fallback, not a replacement for surface-specific hooks or
deterministic enforcement. See `docs/agent-context-architecture.md`.

## Shared Constraints (All Tools)

1. Follow security and prompt-injection constraints from
   `.claude/persistent-instructions.md`.
2. Follow model-routing guardrails from `.claude/model-routing.json`.
3. Run PR follow-up checks before ending a session when a branch PR exists:
   - review state
   - failing checks
   - unresolved actionable comments
4. Use timeout-safe `curl` for GitHub/API fallbacks:
   - `--max-time 30 --connect-timeout 10`
5. Apply pre-PR quality gate checks before opening a PR.

## Adapter Responsibilities

### Claude adapter

- Runtime/hook behavior remains driven by:
  - `CLAUDE.md`
  - `.claude/persistent-instructions.md`
  - `.claude/settings.json` hooks

### Codex adapter

- Repo behavior is expressed through:
  - `AGENTS.md`
  - `docs/codex-web-parity.md`
  - `docs/mesh-cross-ide-setup.md`
- Session bootstrap is explicit (no Claude hook auto-run).

### Cursor adapter

- Repo behavior is expressed through:
  - `.cursor/rules/runtime-policy.mdc` (always apply baseline)
  - optional local `.cursor/hooks.json` — gitignored and generated only after trust review; runs `sessionStart` / `preToolUse` / `postToolUse` / `postToolUseFailure` / `afterAgentResponse` / `stop` via `scripts/cursor-hooks/` adapters (see `docs/cursor-hooks-parity.md`). Cursor Cloud does not fire `sessionStart`; clean Cloud checkouts have no project hooks.
  - `.cursor/rules/claude-md-parity.mdc` (CLAUDE.md session + hooks + Web limits)
  - `.cursor/rules/*.mdc` (scoped project rules)
  - `scripts/cursor-session-bootstrap.sh` — sources `scripts/load-ecosystem.sh`, seeds `.ai/session-context.md` from `.ai/cursor-session-stub.md` when missing (local terminal or hook subprocess file side effects)
  - `docs/cursor-background-agent-parity.md` (background agent setup)

### Devin adapter

- Repo behavior is expressed through:
  - `AGENTS.md` § Devin (bootstrap + fallback commands)
  - `.devin/hooks.v1.json` — committed hook registration (generated from `.claude/settings.json` by `scripts/generate-devin-hooks.py`; drift guarded by pre-commit)
  - `scripts/devin-hooks/` wrappers — translate Devin's lowercase tool names (`exec`/`edit`/`write`/`run_subagent`), `tool_response` shape, `DEVIN_PROJECT_DIR` root, and `summary`→`compact_summary`, then run the canonical handlers
  - `scripts/devin-session-bootstrap.sh` — writes `.ai/devin-session-env.sh` (mode 600, gitignored) since Devin has no `CLAUDE_ENV_FILE` equivalent
  - `docs/devin-parity.md` — event/tool coverage map and manual gates
- `scripts/hook-dispatch.sh` defers to the Devin layer when `DEVIN_PROJECT_DIR` is set (Devin CLI/Desktop also reads `.claude/settings.json`; the guard prevents double-fire).
- Devin gains two events other surfaces lack: `PermissionRequest` (unwired by default — see parity doc) and `SessionEnd` (final retrospective flush).

## Non-Breaking Rule

Changes to this policy must be additive-first:

1. Update this file.
2. Update adapters (`AGENTS.md`, Codex docs, Claude docs) to reference parity.
3. Preserve existing Claude hook behavior unless explicitly approved.
