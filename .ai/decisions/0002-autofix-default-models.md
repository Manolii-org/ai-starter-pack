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

## Amendment 2026-10-28 — proxy default moves to `tier-2-agentic-pro`

**What was wrong.** The decision assumed `tier-2-agentic` is Kimi K2.6 at
$0.50/$2.80. Neither held: the pack template's K2.6 primary (Together) was
removed from serverless on 2026-08-19 and Fireworks K2.6 costs $0.95/$4.00,
and the Manolii proxy maps `tier-2-agentic` to Kimi K3 at $3/$15, more per
token than Sonnet 5.5. The #255 proof therefore most likely ran K3, and the
"about 90% cheaper" claim did not hold on that proxy.

**New decision.** With no model set, proxy-mode Autofix uses
`tier-2-agentic-pro` (MiniMax M3, Fireworks, $0.30/$1.20). The direct-key
default (`claude-sonnet-5-5`) and explicit overrides are unchanged.
Proxies deployed from a template older than v1.25.0 have no
`tier-2-agentic-pro` alias, so Autofix checks the proxy's `/v1/models` list
first and uses `tier-2-agentic` when the alias is missing, so the upgrade
does not fail Autofix on those proxies. The check ships in v1.25.1; v1.25.0
requests the alias unconditionally.

**Evidence.** Same two-file bug fixture on the Manolii CI proxy, one run per
model unless noted (probe PRs #264–#266, closed unmerged):

| Alias (model) | Result | Turns | Agent time | $/MTok in/out |
|---|---|---|---|---|
| `tier-2-agentic-pro` (MiniMax M3) | fixed and pushed | 19 | 37 s | 0.30 / 1.20 |
| `tier-1-fast` (DeepSeek V4.1 Flash) | fixed and pushed | 14 | 30 s | 0.035 / 0.29 |
| `candidate-luna-heavy` (GPT-6 Luna, high) | 1 of 2 runs pushed; the other stopped after 12 turns | 25 | 81 s | 0.10 / 0.50 |

M3 is an agentic-class model with vendor-reported SWE-bench Verified ~85 and
1M context; V4.1 Flash is the documented budget override. Luna is not
reliable enough to be a default yet. One run per model on a small fixture is
weak evidence: the 30-day review re-checks real Autofix outcomes.

## Amendment 2026-10-29 — no Anthropic API models adopted

**Decision (owner).** The pack adopts no new Anthropic API model, and does
not route Anthropic models through its open-weights aliases. Concretely:

- The direct-key default is not moved to `claude-haiku-5-5`, and no
  direct-key acceptance run will be done for it. `claude-sonnet-5-5` stays
  the default only for repositories that choose direct mode with their own
  Anthropic key; the pack's recommended plane is proxy mode with an
  open-weights model.
- The `haiku` and `tier-1-fast` aliases stay on open-weights models; they
  are not re-routed to Anthropic's `claude-haiku-5-5`, even though its list
  price is lower than the template's current `haiku` backend.

The 30-day review (#258) drops its Haiku 5.5 question and keeps the failure
rate and cost per fix checks.
