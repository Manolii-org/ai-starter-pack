# Per-Model Configuration Schema

> **Status:** Specification. Adopted by [`docs/model-change-protocol.md`](model-change-protocol.md) Items 4–6.
> **Applies to:** `.claude/model-routing.json` and any downstream config that declares per-model API parameters.

> **Implementation status:** The bundled `.claude/model-routing.json` does **not** yet implement this per-model schema — it has no top-level `models` block and no per-model `capability_profile`, `api_config`, `benchmark_scores`, `cost`, or `latency` entries. Schema enforcement is "to build" until those fields are added to the routing config.

## What this fixes

`.claude/model-routing.json` declares *which* model an alias routes to but not *how to call it*. Today this is fragmented:

- Reasoning effort and thinking budget are scattered across config files and hardcoded in scripts.
- Vision support is declared per-model in routing.json but not gated at runtime.
- Temperature, top_p, response_format, tool_choice modes, prompt-caching headers — declared nowhere.

This schema centralises those decisions on each model entry and on each tier definition.

## Schema

```jsonc
{
  "models": {
    "<model-id>": {
      "provider": "anthropic | openai | fireworks | together | groq | google | other",
      "weight_origin": "Anthropic | OpenAI | Google | Meta | NVIDIA | DeepSeek | Other",
      "hosting_region": "US | EU | CN | other",
      "data_sensitivity_max": "public | internal | restricted_us_oss_ok | restricted | anthropic_only",

      "advisor_required": true,
      "advisor_model": "<model-id of fail-closed reviewer>",
      "advisor_budget_tokens": 4000,
      "advisor_wired": false,

      "capability_profile": {
        "context_window": 200000,
        "max_output_tokens": 32000,
        "vision": false,
        "tool_use": true,
        "structured_output": "json_mode",
        "function_calling_reliability": 0.87,
        "long_context": true,
        "reasoning_modes": ["high", "medium", "low"],
        "supports_streaming": true,
        "supports_prompt_caching": false
      },

      "api_config": {
        "reasoning_effort_default": "medium",
        "reasoning_effort_supported": ["low", "medium", "high"],
        "thinking_budget_tokens": null,
        "temperature_default": 0.2,
        "top_p_default": 1.0,
        "response_format_supported": ["json_object"],
        "tool_choice_modes": ["auto", "any"],
        "stop_sequences_supported": true,
        "session_affinity_header": "X-Session-Id"
      },

      "benchmark_scores": {
        "aa_intelligence_index": 78,
        "aa_task_class": {
          "agentic": 72,
          "coding": 81,
          "general": 76
        },
        "tool_calling_reliability": 0.87,
        "last_synced": "2026-05-16"
      },

      "cost": {
        "input_per_m_usd": 0.40,
        "output_per_m_usd": 1.60,
        "blended_3to1_usd": 0.70,
        "verbosity_multiplier": 0.85
      },

      "latency": {
        "ttft_p50_ms": 320,
        "ttft_p95_ms": 720,
        "throughput_tps": 110,
        "region": "US"
      }
    }
  },

  "tier_definitions": {
    "<tier-alias>": {
      "task_requirements": {
        "task_class": "agentic",
        "min_context_tokens": 64000,
        "requires": ["tool_use", "long_reasoning"],
        "structured_output_required": false,
        "vision_required": false,
        "latency_slo_ms": null,
        "throughput_floor_tps": 80
      },
      "primary": "<model-id>",
      "fallback_chain": ["<model-id-2>", "<model-id-3>"],
      "data_sensitivity_max": "restricted_us_oss_ok"
    }
  }
}
```

## Field rules

- `data_sensitivity_max` on a model and on a tier MUST be reconcilable — the runtime gate honours the more restrictive of the two.
- If `capability_profile.vision = false` but a tier's `task_requirements.requires` includes `"vision"`, the model is ineligible — validation blocks at commit time.
- `api_config.reasoning_effort_default` is the value the system applies if the caller does not specify one. If the model does not support reasoning at all, set `reasoning_effort_supported: []` and `reasoning_effort_default: null`.
- `advisor_required: true` MUST be paired with `advisor_model` and `advisor_budget_tokens`. Until `advisor_wired: true` is verified, the effective `data_sensitivity_max` is downgraded by one tier.
- `benchmark_scores.last_synced` MUST be within 90 days of any commit that promotes the model. CI fails otherwise.
- `cost.verbosity_multiplier` is the empirically observed ratio of this model's token usage vs a baseline. Defaults to 1.0 when unmeasured.

## Sources of truth for each field

| Field group | Sourced from |
|---|---|
| `provider`, `weight_origin`, `hosting_region` | Provider docs + compliance docs |
| `capability_profile` | Provider model card + in-house testing |
| `api_config` | Provider API reference |
| `benchmark_scores.*` | Public benchmarks (artificialanalysis.ai, BenchLM, etc.) — or in-house evals if available |
| `cost.*` | Provider pricing page; verbosity from telemetry or estimation |
| `latency.*` | In-house probe at the target region, or provider SLAs |

## Migration path

Applying this schema to every existing model in `.claude/model-routing.json` is a separate task. Sequence:

1. **Phase 1 (anchor models):** Primary models in each tier + their direct fallbacks. Covers ~90% of usage.
2. **Phase 2 (secondary fallbacks):** Every model appearing in a `fallback_chain` but not already covered.
3. **Phase 3 (long-tail):** Remaining tier definitions.

Each phase is a separate commit that runs the Model Change Protocol per model.

## Validator (to build)

A JSON schema validator should:

- JSON-schema-validate `.claude/model-routing.json` against this spec.
- Cross-check that every tier alias's `primary` and `fallback_chain` entries exist in `models`.
- Cross-check that `capability_profile` satisfies every `task_requirements.requires` flag of any tier the model serves.
- Cross-check `benchmark_scores.last_synced` is within 90 days for any model with `data_sensitivity_max <= restricted_us_oss_ok`.
- Cross-check `advisor_required: true` implies `advisor_wired: true`.

## Related

- [`docs/model-change-protocol.md`](model-change-protocol.md) — the process that consumes this schema.
- [`docs/us-oss-eligibility-matrix.md`](us-oss-eligibility-matrix.md) — task-class eligibility mapping (if OSS routing is enabled).
- [`.claude/model-routing.json`](../.claude/model-routing.json) — config target.
