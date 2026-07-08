# Model Assessment — `tier-review` → DeepSeek V4 Flash (Fireworks)

**Date:** 2026-06-18 | **Alias:** `tier-review` | **Provider/model:**
`fireworks_ai/accounts/fireworks/models/deepseek-v4-flash` | **Status:** Promoted (direct, `internal` ceiling)

Per `docs/model-change-protocol.md` (15 items, Sections A–E).

## A. Discovery
1. **Task class.** Reasoning / structured-output editorial review. Single class, weight 1.0.
2. **Data-sensitivity ceiling.** `internal` (clearance `internal` + `public`). Drives
   Section E rollout = direct promotion + rollback monitor.
3. **Existing-fit check.** No prior alias served a reasoning-model review path. New
   capability, not a swap. Optimising **capability** — V4 Flash is a reasoning model
   chosen for review-output quality.

## B. Technical fit
4. **Capability profile.** Text-only; reasoning model (emits chain-of-thought before
   its answer). Structured output (JSON) is produced *after* the reasoning span, so
   adequate output budget is mandatory. No vision needed.
5. **API parameter compatibility.** Standard OpenAI-compatible chat completions via the
   proxy. **Client requirement: `max_tokens >= ~1500`** (strict-JSON callers `>= 2000`,
   parsed defensively) or the response truncates before content → empty
   `choices[0].message.content`. `drop_params: true` already set proxy-wide; no
   proxy-side param change required.
6. **Benchmark evidence.** DeepSeek V4 Flash is a high-intelligence-per-dollar
   extract/classify/reasoning model. Confirm the Fireworks serverless id
   (`accounts/fireworks/models/deepseek-v4-flash`) is live with a smoke test before
   relying on it in production.
7. **Advisor requirement.** **Not required.** A fail-closed advisor is mandatory only
   at `data_sensitivity_max >= restricted_us_oss_ok`; this alias is capped at `internal`.
8. **Fallback chain.** **None, by design.** The use case depends on this specific
   reasoning model's output shape; a non-reasoning fallback would change output
   semantics. Callers handle errors defensively. Accepted deviation from the
   "≥2 fallbacks" default — justified by feature semantics + low (`internal`/`public`)
   ceiling.

## C. Compliance & cost
9. **Compliance fit.** OSS-only (no Anthropic-direct dependency — safe for client
   deployments). DeepSeek = PRC-origin weights on Fireworks US infra; permitted at
   `internal` under the relaxed-origin OSS policy. Not for client/restricted data.
10. **Cost.** DeepSeek V4 Flash is a low-cost reasoning tier. Reasoning models are
    output-heavy — expect a verbosity multiplier; verify against telemetry once live.
    No model replaced, so no regression gate.
11. **Provider risk.** Single-provider (Fireworks) by design. Acceptable at the
    `internal` ceiling for a non-critical review tier with client-side defensive handling.

## D. Operability
12. **Decision logging.** ADR-0001 (`.ai/decisions/0001-add-tier-review-alias.md`).
    30-day review reminder: **2026-07-18**.
13. **Prompt-caching.** Not configured. Review prompts are largely unique per document;
    cache hit-rate would be low. Revisit if a stable system-prompt prefix emerges.

## E. Rollout
14. **Rollout mode.** Ceiling `internal` → **direct promotion + rollback monitor**
    (signal: error rate >5% OR cost >+20% vs estimate). Validate via the proxy smoke test.
15. **Reversibility.** Two-way door. **Rollback:** remove the `tier-review` `model_name`
    block from `deploy/litellm-proxy/config.yaml` and redeploy; remove any `tier-review`
    entry an instance added to its own `model-routing.json`. No callers depend on it yet.
