# ADR-0001: Add `tier-review` proxy alias (DeepSeek V4 Flash)

**Date:** 2026-06-18 | **Status:** Accepted | **Decision by:** AI Starter Pack maintainers | **Reversibility:** two-way door

## Context

Editorial / structured-output review workloads (document or code review that
benefits from explicit step-by-step reasoning) had no dedicated tier in the OSS
routing config. The existing tiers are tuned for speed (`tier-1-fast`,
`tier-4-extract`), agentic loops (`tier-2-agentic`), or tool density
(`tier-3-tool`) — none is a reasoning model selected for review-output quality.

## Decision

Add a `tier-review` alias to `deploy/litellm-proxy/config.yaml` routing to
`fireworks_ai/accounts/fireworks/models/deepseek-v4-flash` (DeepSeek V4 Flash on
Fireworks), a reasoning model. `internal` + `public` clearance. Single-model
alias with **no fallback** — see Alternatives. No new secret: it uses the
already-required `FIREWORKS_API_KEY`. Assessment (15-item Model Change Protocol):
`.ai/assessments/tier-review-deepseek-v4-flash-2026-06-18.md`.

## Consequences

### Positive
- A purpose-built reasoning tier for review tasks, without disturbing existing tiers.
- Additive and OSS-only — no Anthropic dependency, safe for client deployments.

### Negative
- Reasoning models are output-heavy: callers must budget `max_tokens >= ~1500`
  (strict-JSON callers `>= 2000`) or responses truncate before content is emitted.
- Single provider (Fireworks) for this alias — acceptable at the `internal` ceiling.

### Unknown/Risk
- Verbosity/cost multiplier of the reasoning span should be confirmed against
  telemetry once the tier sees real traffic.

## Alternatives Considered

1. **Reuse `tier-2-agentic` / a general tier** — rejected: not a reasoning model
   tuned for review-output quality; would conflate budgets and routing semantics.
2. **Add a multi-hop fallback chain** — rejected: the use case depends on this
   specific reasoning model's output shape; a non-reasoning fallback would silently
   change output semantics. Callers handle errors defensively instead.
3. **Selected: single-model `tier-review` alias, no fallback** — minimal,
   semantically honest, reversible.

## Reversibility

**This decision is: two-way door.**

**Rollback:** remove the `tier-review` `model_name` block from
`deploy/litellm-proxy/config.yaml` and redeploy; remove any `tier-review` entry
an instance added to its own `model-routing.json`. No callers depend on it until
a consumer opts in.

## Implementation Notes

- Acceptance criteria: alias resolves through the proxy and returns a non-empty
  `choices[0].message.content` for a review prompt with `max_tokens >= 1500`.
- Dependencies: `FIREWORKS_API_KEY` (already required by other tiers).
- Seed `.claude/model-routing.json` is instance-owned (`_skip_if_exists`); record
  `tier-review` per instance. A follow-up adds it to the seed registry under tests.

## Related Decisions

- Pack proxy deploy pattern: ADR-0023 (no-custom-image `[[files]]` injection).

---

**Status History:**
- 2026-06-18: Created as Accepted (additive, two-way door).
