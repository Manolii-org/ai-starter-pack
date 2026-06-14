---
name: assess-model
version: 1.0.0
description: "Run the Model Change Protocol — 15-item assessment whenever you add, remove, or re-route any AI model in your project. Produces a decision log and assessment artefact. Mandatory before any commit that touches .claude/model-routing.json or agent/skill model declarations."
type: skill
model: haiku
data_sensitivity: internal
max_tokens: 4000
safety_tier: amber
requires_mcp: []
required_entities: []
tools:
  - Bash
  - Read
  - Edit
  - Write
tags:
  - model-routing
  - governance
  - assessment
intent_phrases:
  - "assess model"
  - "run model change protocol"
  - "add a new model"
  - "switch model routing"
  - "assess model for tier"
---

# /assess-model

Runs the Model Change Protocol defined in [`docs/model-change-protocol.md`](../../../docs/model-change-protocol.md). 15 items, five sections (Discovery / Technical fit / Compliance & cost / Operability / Rollout). Tiered rollout mode is mandatory and gated by the declared data-sensitivity ceiling.

## When to invoke

**Auto-triggers (via pre-commit advisory):**
- Any edit to `.claude/model-routing.json` that changes a `primary`, `fallback_chain`, `data_sensitivity_max`, `api_config.*`, or `task_requirements.*`.
- Any edit to `.claude/agents/*.md` or `.claude/skills/*/SKILL.md` that changes the `model:` or `data_sensitivity:` frontmatter.

**Manual:**
- `/assess-model <model-id> [--task-class <class>] [--sensitivity <tier>] [--replacing <model-id>]`
- `/assess-model --retro <model-id>` for retroactive assessment of an already-routed model.

## Inputs the skill needs

- The model identifier (e.g. `deepseek-v4-pro-fireworks`).
- The task class(es) the model will serve.
- The data-sensitivity ceiling.
- Optional but recommended: link to benchmark entry; choice of advisor model.

## Steps

1. **Load the protocol checklist** from `docs/model-change-protocol.md` and the schema from `docs/model-config-schema.md`.
2. **Pre-fill what is knowable** from existing config: current alias mapping, current advisor, current fallback chain, cost from the last sync.
3. **Walk the 15 items** in order (Sections A through E). For each item, suggest a default from the schema. Surface explicitly when a default is missing.
4. **Validate against schema rules:**
   - `capability_profile` satisfies every `task_requirements.requires` flag.
   - `data_sensitivity_max` is consistent across model + tier (use the more restrictive).
   - Advisor present AND `advisor_wired: true` if `weight_origin` is not domestic and sensitivity >= `restricted_us_oss_ok`.
   - Fallback chain has >=2 entries across distinct providers, each meeting the same `data_sensitivity_max`.
   - `benchmark_scores.last_synced` within 90 days.
5. **Probe** the model endpoint with a minimal request to confirm reachability and that the declared `api_config` is honoured. If direct API access is unavailable, perform a manual verification and paste the result into the artefact.
6. **Write the assessment artefact** to `.ai/assessments/<model-id>-<YYYY-MM-DD>.md` with the 15-item answers.
7. **Log the decision** as an ADR entry in `.ai/decisions/ADR-NNNN-model-<alias>-<date>.md`. Include options-considered, rationale, and the exact rollback command.
8. **Stage the routing change** as a draft commit on a `claude/assess-<model-id>` branch:
   - Patch to `.claude/model-routing.json` adding/updating the model entry and any affected tier.
   - Patch to affected `.claude/agents/*.md` and `.claude/skills/*/SKILL.md` frontmatter.
   - Do not push, do not auto-merge.
9. **Apply the rollout mode** for the declared sensitivity ceiling:
   - `public` / `internal` -> direct promotion + 24h rollback monitor at 5% error rate OR cost >+20%.
   - `restricted_us_oss_ok` -> shadow eval if telemetry is available; 3-day window. Manual operator review required.
   - `restricted` / `anthropic_only` -> 10% canary for 24h, then 50/50 shadow for 7 days, then operator-approved promotion.
10. **Emit the PR template** with the 15-item answers, the schema patch, the rollout plan, the rollback command, and a calendar reminder for the 30-day review.

## Outputs

- `.ai/assessments/<model-id>-<YYYY-MM-DD>.md` — the answered checklist.
- ADR entry — `.ai/decisions/ADR-NNNN-model-<alias>-<date>.md` with decision context and reversibility.
- Draft commit on `claude/assess-<model-id>` branch.
- PR body template ready for submission.

## What this skill does NOT do

- It does not run the model-quality eval. Capture whatever eval was performed and link to it.
- It does not auto-merge. All routing changes require operator review.
- It does not bypass schema validation. Violations block staging.

## Failure modes

- If `data_sensitivity_max` on the model would violate your project's compliance policy, skill aborts before staging.
- If no benchmark scores are available (missing or older than 90 days), skill flags `[BLOCKED:stale-bench]`.
- If `advisor_required: true` but no advisor is declared, skill blocks with `[BLOCKED:advisor-required]`.
- If a required capability is missing for the declared task class, skill aborts with capability mismatch.

## See also

- [`docs/model-change-protocol.md`](../../../docs/model-change-protocol.md) — full protocol prose.
- [`docs/model-config-schema.md`](../../../docs/model-config-schema.md) — per-model schema.
- [`.claude/model-routing.json`](../../../.claude/model-routing.json) — current routing config.
