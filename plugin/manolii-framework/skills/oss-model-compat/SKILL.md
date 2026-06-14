---
name: oss-model-compat
version: 2.0.0
description: "Checks config.yaml changes for missing capability-matrix entries and code changes for parameters unsupported by the target OSS model (response_format, functions, etc.)."
type: skill
model: haiku
advisor_model: claude-sonnet-4-6
data_sensitivity: internal
max_tokens: 1000
safety_tier: green
requires_mcp: []
required_entities: []
tools:
  - Read
  - Grep
tags:
  - pr-assessment
  - specialist
intent_phrases:
  - "check the OSS model compatibility"
  - "is this model routing correct"
  - "verify the tier classification"
  - "model policy check"
---

# Skill: OSS Model Compatibility

Specialist triggered when `deploy/litellm-proxy/config.yaml`, `.claude/model-routing.json`, or any Python/TypeScript/JavaScript code that calls the LiteLLM proxy is modified.

## Data Sensitivity Note

Receives sanitised diff snippets from the orchestration layer. `data_sensitivity: internal` assumes no client-identifiable content.

## Input

Full diff of changed files. Use `Read` to fetch full file contents where needed (config.yaml, the capabilities matrix).

## Checks

### 1. New model not in capabilities matrix (ERROR)

When a new `model_name` entry appears in `deploy/litellm-proxy/config.yaml` diff:
- Read `docs/model-capabilities-matrix.md`
- Check that the underlying model ID (the `model:` value under `litellm_params`) has a corresponding row in the quick-reference table AND a per-model detail section
- If missing: **ERROR** — "Model `<id>` added to config.yaml but not documented in docs/model-capabilities-matrix.md. Run the add-new-model-checklist (docs/add-new-model-checklist.md) before merging."

### 2. Missing model_info cost fields (ERROR)

When a new `model_list` entry is added to config.yaml without `model_info.input_cost_per_token` and `model_info.output_cost_per_token`:
- **ERROR** — "New model entry `<model_name>` is missing model_info cost fields. Langfuse will log $0.00 for all traces. Add input_cost_per_token and output_cost_per_token."

### 3. `response_format` passed to M2.7-routed tier (ERROR)

When code (`.py`, `.ts`, `.js`) is changed and contains `response_format` passed to a function/call that explicitly references `minimax-m2p7`, `tier-1-fast` (which routes to M2.7 via proxy), or `claude-haiku-4-5-20251001`:
- **ERROR** — "`response_format` is not supported by MiniMax M2.7 and causes HTTP 500. Enforce JSON via system prompt instead. See docs/model-capabilities-matrix.md § MiniMax M2.7."

### 4. `functions` parameter (not `tools`) passed to OSS endpoint (ERROR)

When code passes the legacy `functions` parameter (OpenAI pre-tools format) to any OSS tier or model:
- **ERROR** — "`functions` is unsupported on OSS-hosted models (Fireworks, Together, Groq). Use `tools` instead."

### 5. Empty tool schema `{}` targeting Kimi K2.5 (WARNING)

When code targeting `tier-2-agentic-fallback-kimi`, `tier-3-tool`, or `kimi-k2p5` passes a tool with an empty parameter schema `{}`:
- **WARNING** — "Kimi K2.5 rejects empty `{}` tool parameter schemas with a server-side validation error. Use `{\"type\": \"object\", \"properties\": {}, \"required\": []}` instead."

### 6. New tier-0 model without `reasoning_effort: "high"` (WARNING)

When a new model is added to a `tier-0-oss-heavy` slot in config.yaml without `reasoning_effort: "high"`:
- **WARNING** — "Tier-0 entries should set `reasoning_effort: \"high\"` to maximise quality. Omit only if the model does not support the parameter (document why in a config comment)."

### 7. Model promotion without timeout: 600 when gaps > 30 s (WARNING)

When a model is promoted to a primary tier slot (not a fallback) and the `litellm_params` block is missing `timeout: 600` (or absent), check whether the PR description or linked CI output includes stream-gap measurements from `scripts/check-oss-routing.py --check-stream-timeout --tier <alias>`. If `max_gap_ms > 30000` is reported (or no measurement is present):
- **WARNING** — "A model promoted to primary without `timeout: 600` risks premature stream kills when inter-chunk gaps exceed 30 s. Run `python3 scripts/check-oss-routing.py --check-stream-timeout --tier <alias>`, confirm `max_gap_ms`, and set `timeout ≥ 2 × (max_gap_ms / 1000)`. See Model Promotion Checklist in persistent-instructions.md."

## Important

- Use `Grep` to locate `response_format` and `functions` usage efficiently before reading full files.
- A model appearing under a fallback alias only (not as a new primary) still requires a matrix entry — check 1 applies regardless of tier position.
- Do NOT flag `drop_params: true` in `litellm_settings` — this is intentional global config.

## Output Schema

```json
{
  "source": "oss-model-compat",
  "findings": [
    {
      "file": "scripts/siloed-pr-comment-summariser.py",
      "line": 116,
      "severity": "ERROR",
      "message": "response_format={\"type\": \"json_object\"} passed to tier-1-fast (MiniMax M2.7). M2.7 does not support response_format and returns HTTP 500.",
      "fix": "Remove response_format and enforce JSON-only output via the system prompt."
    }
  ]
}
```

Return `{"source": "oss-model-compat", "findings": []}` if no issues found. Never return prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | Would cause a runtime failure (HTTP 500, validation rejection, $0 cost logging, undocumented model in production) |
| WARNING | Suboptimal configuration that degrades quality or reliability but doesn't hard-fail |

## Phase 1: Executor (Haiku)

Run all checks from the Checks section above.

For each model, config, or code change:
- Apply each check rule exactly as specified
- Draft a finding entry (file, line, severity, message, fix)
- Rate self-confidence: `high` (incompatibility is definitive, will cause runtime failure) | `medium` (pattern suggests incompatibility but context is unclear) | `low` (may be conditional or tolerated)

Emit a draft findings list with confidence ratings.

## Phase 2: Advisor (Sonnet)

Review each draft finding from Phase 1:
1. **Validity check:** Is this actually incompatible with the target model, or is there a workaround?
2. **Severity check:** Does the confidence rating match the actual runtime risk?
3. **Documentation check:** Has the executor verified the model's capabilities matrix?
4. **Escalation check:** Does this finding involve `response_format` passed to M2.7-tier or `functions` passed to OSS endpoint?

Escalation trigger for this skill: `response_format` passed to M2.7-tier, or `functions` (legacy) passed to OSS endpoint

Output: confirmed findings only, each with `advisor_confidence: high|medium|low`.

## Phase 3: Final Output

Merge Phase 1 and Phase 2 results:
- Keep only findings confirmed by advisor
- Sort: escalation findings first, then by severity (ERROR before WARNING), then by advisor_confidence (high first)
- Return the final JSON output schema defined above
