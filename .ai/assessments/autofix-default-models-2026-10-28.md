# Model Change Assessment — Autofix default models (`tier-2-agentic`, `claude-sonnet-5-5`)

Date: 2026-10-28 · Change class: **new default for an existing call site**
(`pr-autofix-loop` reusable + standalone agent step) · Sensitivity ceiling:
`internal` · Decision: ADR-0002.

## What changed

The Autofix agent step now always passes `--model`:

| Plane | Before (≤ v1.23.0) | After |
|---|---|---|
| Proxy (`provider_mode: proxy` / `LITELLM_PROXY_URL`), model unset | Claude Code CLI default `claude-opus-5[1m]`; the pack proxy has no route for it, so the agent failed after one turn (PR #253, run 40601003976) | `tier-2-agentic` |
| Direct Anthropic key, model unset | CLI default `claude-opus-5[1m]` | `claude-sonnet-5-5` |
| Model set (`model` input / `AUTOFIX_MODEL`) | that model | unchanged |

In the reusable, `ANTHROPIC_MODEL` carries the same value, because the pinned
action passes it to the CLI and it overrides `--model`.

## 15-item checklist (summary)

**A. Discovery**
1. Task class: Agentic + Coding (1.0) — read CI logs or review comments, edit, run tests, commit.
2. Sensitivity ceiling: `internal` (repository code and CI logs). Repositories above `internal` must set a model explicitly; the docs say so.
3. Existing fit: no default existed; the CLI fallback was the most expensive model. The dimension optimised is **cost**, with capability held at "fixes a failing test end to end".

**B. Technical fit**
4. Capability profile: both models support tool use, streaming and long context, which is what Claude Code needs. `tier-2-agentic` is text-only (no vision), which Autofix does not use.
5. API parameters: none set by the workflow beyond `--model`; effort and thinking stay at Claude Code defaults.
6. Evidence:
   - `tier-2-agentic` (Kimi K2.6): SWE-bench 0.768 per `.claude/model-routing.json` `tier_definitions`. **In-house end-to-end run:** PR #255 — `--model tier-2-agentic` through the pack's CI proxy, 11 turns, 54 s, `is_error: false`, correct `[autofix]` commit `25e5238`.
   - `claude-sonnet-5-5`: current Anthropic Sonnet. Not run here (this repository has no direct Anthropic key). Benchmark score: UNVERIFIED in this repository's sources.
7. Advisor: not required — ceiling is `internal` (the protocol requires an advisor from `restricted_us_oss_ok`).
8. Fallback chain: `tier-2-agentic` keeps the proxy's existing Kimi K2.6 (Fireworks) and Qwen fallbacks. `claude-sonnet-5-5`: no automatic fallback; any failure is visible because a run without an execution file fails the job.

**C. Compliance & cost**
9. Compliance: `tier-2-agentic` runs OSS weights (PRC origin) on US infrastructure, permitted at `internal` under the pack's relaxed-origin OSS policy. Sonnet 5.5 is first-party Anthropic.
10. Cost per million tokens (input / output), Anthropic list prices as of 2026-10-06 and registry prices for OSS:

    | Model | Input | Output | vs previous default |
    |---|---|---|---|
    | `claude-opus-5` (previous CLI default) | $5.00 | $25.00 | — |
    | `claude-sonnet-5-5` (new direct default) | $2.00 | $10.00 | −60% |
    | `tier-2-agentic` (new proxy default) | $0.50 | $2.80 | −89% to −90% |
    | `claude-haiku-5-5` (documented option) | $0.10 | $0.50 | −98% |

    Every change is a decrease; no sign-off threshold is crossed.
11. Provider risk: proxy plane depends on the operator's LiteLLM deployment serving `tier-2-agentic`; non-LiteLLM proxies must set a model (documented).

**D. Operability**
12. Decision log: ADR-0002.
13. Prompt caching: handled inside Claude Code; no workflow-level lever.

**E. Rollout**
14. Mode: `internal` → direct promotion with a rollback monitor. Monitor: the Autofix `activation` output and the fail-loud execution check (a model the endpoint cannot serve fails the job visibly instead of passing).
15. Reversibility: set the `model` input or `AUTOFIX_MODEL` (for example `claude-opus-5-5`) to override per repository, or revert the workflow change. Every previous model remains available. Review by **2026-11-27** (tracked issue).

## Not decided here

Whether `claude-haiku-5-5` should replace `claude-sonnet-5-5` as the direct default. It is 20× cheaper but has no end-to-end Autofix run yet; it needs a direct-key acceptance run first.

## Amendment 2026-10-28 (same day)

The proxy row above is superseded. `tier-2-agentic` is not $0.50/$2.80:
Fireworks Kimi K2.6 is $0.95/$4.00 and the Manolii proxy serves Kimi K3 on
that alias at $3/$15. The proxy default is now `tier-2-agentic-pro` (MiniMax
M3, $0.30/$1.20), chosen from a three-model acceptance probe recorded in
ADR-0002's amendment. Data clearance is unchanged (`internal`, PRC-origin
weights on US hosting).
