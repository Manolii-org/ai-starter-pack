# Model Change Protocol (Required)

> **Authoritative for:** The process to follow whenever you add, remove, or re-route any AI model in your project.
> **Companion docs:** [`docs/model-config-schema.md`](model-config-schema.md), [`.claude/skills/assess-model/SKILL.md`](../.claude/skills/assess-model/SKILL.md).
> **Source of truth for current routing:** [`.claude/model-routing.json`](../.claude/model-routing.json).

## Why this exists

Past model changes have shipped without consistent assessment of:

- per-provider API knobs (reasoning effort, thinking budget, response_format, tool_choice modes, temperature defaults);
- capability fit (vision support, tool-use reliability, structured-output mode, context window);
- compliance fit (weight origin, data residency, policy compatibility);
- operability (cost tracking, fallback reachability);
- reversibility (which command rolls us back, is the prior model still reachable).

This protocol closes those gaps by making them mandatory checks on every model change.

## When to run this

Every time you:

- Add a new model to `.claude/model-routing.json`.
- Change which model a tier alias points to.
- Change which model an `.claude/agents/*.md` or `.claude/skills/*/SKILL.md` declares in its `model:` frontmatter.
- Update the `fallback_chain` for any tier.
- Change a per-model API parameter (reasoning_effort, thinking budget, temperature default, response_format).
- Adjust `data_sensitivity_max` on any model or alias.
- Roll back to a previous model after an incident.

If you are unsure whether this protocol applies, run it anyway. The skill takes minutes; a bad model change can cause hours of debugging.

## How to run this

Invoke `/assess-model <model-id> [--task-class <class>] [--sensitivity <tier>]`. The skill walks the 15-item checklist, produces a `.ai/assessments/<model-id>-<YYYY-MM-DD>.md` artefact, logs a decision, and emits a PR template. It does not auto-commit and does not auto-merge.

If the skill is unavailable, work through the 15 items below by hand and produce the same outputs.

## The 15-item checklist

### A. Discovery

1. **Task class.** Which task class(es) is this model for? Pick from: Agentic, Coding, Reasoning, Extraction, Tool-use, Latency-critical, Long-context, Multimodal. If multiple, list each with a priority weight that sums to 1.0.
2. **Data-sensitivity ceiling.** What is the highest sensitivity tier this model will serve? Typical tiers: `public`, `internal`, `restricted`, `anthropic_only`. This ceiling drives the mandatory rollout mode in Section E.
3. **Existing-fit check.** Is there a model already routed for this task class + sensitivity ceiling? If yes, name it and state the single dimension you are optimising by changing (cost, capability, latency, vendor risk, compliance). If no improvement on any dimension, stop — the change is not justified.

### B. Technical fit

4. **Capability profile.** Declare the model's `capability_profile` per [`docs/model-config-schema.md`](model-config-schema.md): context window, max output tokens, vision support, tool_use support, structured-output mode, function-calling reliability, reasoning modes, streaming support, prompt-cache support. If any required capability is missing for the declared task class, this model is ineligible — stop.
5. **API parameter compatibility.** Declare the model's `api_config` block: `reasoning_effort_default`, `reasoning_effort_supported`, `thinking_budget_tokens`, `temperature_default`, `top_p_default`, `response_format_supported`, `tool_choice_modes`, `stop_sequences_supported`. Reasoning defaults are non-obvious — verify against the provider's docs and benchmark at the chosen reasoning level.
6. **Benchmark evidence.** Capture scores from:
   - Standard intelligence benchmarks (SWE-bench, MMLU, etc. — check artificialanalysis.ai or similar).
   - Per-task-class breakdown if available.
   - In-house eval result if one exists for the declared task class.
   - Tool-calling success rate if the task class is Agentic or Tool-use.
   Set `benchmark_scores.last_synced` to today. CI will fail promotion if the sync date is older than 90 days at promotion time.
7. **Advisor requirement.** Does this model need a fail-closed advisor guardrail? Default: **yes** if the model's origin is not domestic and `data_sensitivity_max >= restricted_us_oss_ok`. Specify `advisor_model` and `advisor_budget_tokens`. Confirm the advisor's wire-in is live by checking your proxy/orchestration config — until verified, treat the advisor as ABSENT and downgrade the sensitivity ceiling accordingly.
8. **Fallback chain.** Declare at least two fallbacks: ideally same task class on a different provider, then a lower-cost model. Confirm each endpoint is reachable today. Confirm each fallback's `data_sensitivity_max` is at least as restrictive as the primary.

### C. Compliance & cost

9. **Compliance fit.** For any sensitivity >= `restricted_us_oss_ok`, confirm the model is permitted under your project's (or client's) AI policy. Document any policy gates or contractual constraints. Check weight origin, data residency, and training data usage if applicable.
10. **Cost budget.** Document blended cost per million tokens (input + output at the task's I/O ratio), apply any empirical verbosity multiplier, and compare to the model being replaced. Flag any increase >10%; require explicit sign-off for >25%.
11. **Provider risk.** Single-provider exposure? Data residency acceptable for the declared sensitivity? Note any known provider incidents in the last 30 days (rate-limit pulls, model deprecations, service degradations).

### D. Operability

12. **Decision logging.** Log the change as an ADR entry in `.ai/decisions/` with options-considered + rationale + reversibility plan. Include a 30-day calendar reminder to review the decision.
13. **Prompt-caching strategy.** If the model supports prompt caching (vendor-specific headers or OpenAI `cache_control`), declare which call patterns get cached. If declined or unsupported, state why — prompt caching is a significant cost lever.

### E. Rollout

14. **Rollout mode (mandatory, by data-sensitivity ceiling).**

   | Sensitivity ceiling | Mode | Eval window | Approval |
   |---|---|---|---|
   | `public` / `internal` | Direct promotion + rollback monitor | 24h auto-rollback at error rate >5% OR cost >+20% | Self-merge; monitor active |
   | `restricted_us_oss_ok` or similar | Shadow eval (if telemetry available) | 3 days; manual review of results | Manual operator review required |
   | `restricted` / `anthropic_only` | 10% canary then 50/50 shadow | 24h canary + 7-day shadow | Operator approval at canary→shadow and shadow→full |

   Shadow modes require working telemetry; if telemetry is unavailable, shadow eval is **not** valid evidence and the change MUST be downgraded to manual review only.

15. **Reversibility plan.** State the exact rollback command and config patch. Confirm the previous model is still reachable (not deprecated by provider). Set a 30-day calendar reminder to revisit the decision; without the reminder, the protocol counts as incomplete.

## Outputs of running this protocol

- A decision entry in `.ai/decisions/ADR-NNNN-model-<alias>-<date>.md` with 15-item answers and reversibility plan.
- A PR that modifies `.claude/model-routing.json` AND the corresponding `models.<id>.api_config` block AND any agent/skill frontmatter pointing at the affected alias.
- A self-contained `.ai/assessments/<model-id>-<YYYY-MM-DD>.md` artefact with the 15-item answers.

## What this protocol does NOT do

- It does not run the model-quality eval itself; it ensures the eval was run and its result captured.
- It does not auto-update prose docs — those are hand-maintained.

## Open gaps the protocol intentionally surfaces

These are checked on every assessment until closed:

- **Benchmark freshness.** Keep benchmark scores within 90 days of any promotion commit. If scores are older, re-sync before promotion.
- **Advisor wire-in.** If an advisor is required, ensure the proxy callback or guardrail is live, not stub.
- **Fallback reachability.** Confirm all fallback endpoints respond before merging. If a fallback provider has deprecated the model, swap it before merging.
- **Cost multipliers.** As soon as telemetry is available after a promotion, verify the empirical verbosity multiplier against the declared estimate. Update it if there is >10% drift.
