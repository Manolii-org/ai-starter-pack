# ADR-0002: Lean default models for Autofix

**Date:** 2026-10-28 | **Status:** Accepted | **Decision by:** pack owner | **Reversibility:** two-way door

## Context

Autofix (`pr-autofix-loop`, reusable and standalone) started acting in v1.23.0.
When no model was set, the agent used the Claude Code CLI default
(`claude-opus-5[1m]`), the most expensive option. Through a proxy, that model
usually has no route, so the run failed after one turn (PR #253). Consumers use
Claude Code web, Cursor, Codex and Devin; Autofix runs in GitHub Actions for all
of them, so its default should be cheap and work on both planes.

## Decision

When the caller sets no model, Autofix uses:

- `tier-2-agentic` (pack LiteLLM alias, Kimi K2.6) through a proxy, proven end
  to end on PR #255;
- `claude-sonnet-5-5` with a direct Anthropic key.

An explicit `model` input (reusable) or `AUTOFIX_MODEL` variable (standalone)
always wins.

## Consequences

### Positive
- Per-token cost drops by about 60% (direct) and 90% (proxy) against the
  previous default.
- Proxy-mode Autofix works without extra configuration on the pack's LiteLLM.

### Negative
- `tier-2-agentic` is capped at `internal` data sensitivity; repositories above
  that must set a model. Proxy-mode runs with no model previously failed before
  sending anything; they now send PR context to a PRC-origin OSS model on
  Fireworks. The CHANGELOG calls this out so consumers can set a model first.
- Non-LiteLLM proxies do not know the alias and must set a model.
- A behaviour change inside v1 (different model when unset); called out in the
  CHANGELOG.

## Alternatives considered

- **Document only** (no default): leaves every unconfigured consumer on Opus.
- **Cheapest viable** (`tier-1-fast` / `claude-haiku-5-5`): lowest cost, not yet
  proven on multi-file fixes. Haiku 5.5 remains an explicit option.

## Model Assessment

`.ai/assessments/autofix-default-models-2026-10-28.md` (15-item checklist,
evidence and cost table). Review by 2026-11-27.
