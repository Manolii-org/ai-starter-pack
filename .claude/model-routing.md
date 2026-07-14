# Dynamic Model Routing Guide

> **Portable specification** — this guide is tool-agnostic. It works with Claude Code, Cursor, Windsurf, or any AI coding tool that supports model selection.

## Principle

Use the cheapest model capable of completing a task well. The orchestrator (primary model) delegates sub-tasks to the appropriate tier, cutting cost by 5–30× on mechanical work without sacrificing quality where it matters.

Two routing layers:

1. **Claude Code Agent tool** — uses `model="haiku"/"sonnet"/"opus"` from `platforms.claude_code.tier_map` in `model-routing.json`. No external API keys needed (Claude Max subscription).
2. **Application LLM calls** — uses stable tier names (`tier-1-fast`, `tier-2-agentic`, etc.) via a LiteLLM proxy that routes to OSS providers. Optional; requires API keys and a deployed proxy.

---

## Claude Code Tier Reference

Three tiers defined in `model-routing.json → platforms.claude_code.tier_map`:

| Tier | Claude Code model | Cost (subscription) | Primary use |
|------|-------------------|---------------------|-------------|
| `light` | `haiku` | Included | Search, grep, rename, format, boilerplate, list |
| `standard` | `sonnet` | Included | Implement, test, debug, refactor, document |
| `heavy` | `opus` | Included | Architecture, security audit, cross-repo planning, ADRs |

Override any default per-task with the `model` parameter on the Agent tool:

```text
Agent(subagent_type="review", model="opus", prompt="...")
```

---

## Full Tier Reference (8 Tiers)

When the LiteLLM proxy is enabled, application code routes to these stable tier names:

| Tier | Provider | Model | SWE-bench | $/M in | $/M out | Data | Best for |
|------|----------|-------|-----------|--------|---------|------|---------|
| `tier-0-opus` | Anthropic | claude-opus-4-6 | 80.8% | $15 | $75 | any | Architect, security, ADRs |
| `tier-0-sonnet` | Anthropic | claude-sonnet-4-6 | 79.6% | $3 | $15 | any | Implement, debug, review |
| `tier-0-oss-heavy` | Fireworks | Kimi K2.6 | 58.6%† | $0.50 | $2.80 | internal/public | SWE-patch, bulk-extract, math — **RESTRICTED USE** |
| `tier-1-fast` | Fireworks | DeepSeek V3.2 | 73.1% | $0.56 | $1.68 | internal/public | Boilerplate, single-step, format |
| `tier-2-agentic` | Together | Kimi K2.5 | 76.8% | $0.50 | $2.80 | internal/public | Long tool chains, 262K context |
| `tier-3-tool` | Fireworks | GLM-4.7 | 75.0% | $0.60 | $2.20 | internal/public | Tool-heavy agents (90.6% tool-call success) |
| `tier-4-extract` | Groq | Llama 3.1 8B | 60.0% | $0.05 | $0.08 | internal/public | Grep, count, extract, bulk classification |
| `tier-5-latency` | Groq (LPU) | Llama 3.1 8B | — | $0.05 | $0.08 | internal/public | DOM selection, real-time classification (~800 tok/s) |

† SWE-bench Pro score. `tier-0-oss-heavy` has **restricted use cases** — see below.

### `tier-review` — reasoning-model review tier (optional)

An optional alias backed by **DeepSeek V4 Flash** (Fireworks) for editorial /
structured-output review tasks (document or code review that benefits from
explicit reasoning). It is a *reasoning* model — it emits chain-of-thought
**before** its answer — so callers **must** set `max_tokens >= ~1500` (strict-JSON
callers `>= 2000`, parsed defensively) or the response truncates before any
content is returned (`choices[0].message.content` comes back empty).
**Single-model alias, no fallback** by design: the use case depends on this
model's output shape, and a non-reasoning fallback would change output semantics.
`internal` + `public` clearance only (PRC-origin weights on US infra — not for
client/restricted data). It ships in `deploy/litellm-proxy/config.yaml`; add a
matching entry to your instance's `model-routing.json` if you track tiers there.

---

## Data Classification

Every agent/task must have a `data_sensitivity` label. This determines which tiers are permitted:

| Classification | Description | Allowed tiers |
|---------------|-------------|---------------|
| `anthropic_only` | Client code, PII, emails, contact records | `tier-0-opus`, `tier-0-sonnet` only |
| `internal` | Own codebase, deploy scripts, agent prompts | All tiers |
| `public` | Open-source code, boilerplate, linting tasks | All tiers |

> **Important:** `anthropic_only` ≠ governance `RESTRICTED`. `RESTRICTED` means no AI processing at all. `anthropic_only` means AI-processed but exclusively via Anthropic-hosted models (SOC2, no training on API data, US jurisdiction).

### `tier-0-oss-heavy` — Restricted Use

Kimi K2.6 is a restricted tier: high capability on narrow task types, but **explicitly excluded** from others.

**Allowed:** `code-patch`, `swe-patch`, `agentic-loop`, `bulk-extract`, `math-reasoning`, `structured-extraction`, `classification`

**Excluded:** `prose`, `creative-writing`, `client-facing`, `code-review`, `architecture`, `adr`, `orchestration`, `vision`, `long-context-over-64k`

Weaknesses: PRC-origin weights (Moonshot AI) — capped at `internal` data sensitivity; PRC censorship on sensitive topics; unreliable self-evaluation. Vision-capable (text + image). Cap inputs at 64K context pending RULER eval.

---

## Agent Model Defaults

Defined in `model-routing.json → agent_routing`:

| Agent | Default model | Rationale |
|-------|--------------|-----------|
| `default`, `generate`, `deployment-verifier` | `haiku` | Mechanical tasks; low reasoning required |
| `review`, `deep-analyse`, `security-reviewer`, `migration-planner` | `sonnet` | May handle sensitive code; Anthropic-pinned |
| `test-architect`, `performance-auditor`, `incident-diagnostician` | `sonnet` | Moderate reasoning required |
| `ecosystem-coordinator` | `opus` | Cross-repo planning; highest reasoning needed |

When `USE_LITELLM_PROXY=true` (optional):

| Agent | OSS tier | Saving |
|-------|----------|--------|
| `generate` | `tier-1-fast` | ~75% vs haiku API |
| `default` | `tier-1-fast` | ~75% vs haiku API |
| `deployment-verifier` | `tier-4-extract` | ~98% vs haiku API |
| `insight-miner` | `tier-2-agentic` | ~94% vs sonnet API |
| `performance-auditor` | `tier-3-tool` | ~80% vs sonnet API |

Savings are vs Anthropic API pay-per-token. Claude Max subscription users pay flat monthly — savings only apply when you have an API key (non-subscription builds, CI, background jobs).

---

## Keyword Heuristics

The `UserPromptSubmit` hook reads `.claude/model-routing.json` and emits a `<model-routing>` block on every prompt suggesting which model tier to use for sub-agents.

**Escalate to heavy** if prompt contains: `architect`, `design`, `plan`, `migration`, `security`, `audit`, `cross-repo`, `orchestrate`, `ambiguous`, `trade-off`

**Escalate to standard** if prompt contains: `implement`, `refactor`, `test`, `review`, `debug`, `integrate`, `summarize`, `analyze`, `document`, `optimize`

**De-escalate to light** if prompt contains: `list`, `count`, `find`, `search`, `rename`, `format`, `lint`, `grep`, `glob`, `status`, `boilerplate`

Precedence: heavy > standard > light (highest match wins).

---

## Parallel Dispatch

Always launch independent sub-agents in parallel. Heavy tasks dominate wall-clock time (~30s vs ~5s for light), so running light/standard tasks alongside heavy adds zero wall-clock cost.

```text
# Launch all three simultaneously — total time = heavy task time only
Agent(model="opus",   prompt="Design the auth architecture...")   # heavy
Agent(model="haiku",  prompt="List all .ts files in src/")        # light
Agent(model="sonnet", prompt="Write tests for auth.ts")           # standard
```

---

## Decomposition Pattern

Before assigning `opus`, ask: can this be split into a `sonnet` analysis step + a `haiku` execution step?

```text
# Instead of: one opus agent for "refactor and update all imports"
# Use:
Agent(model="sonnet", prompt="Plan the refactor — list all files to change and what changes")
# → then:
Agent(model="haiku",  prompt="Apply these renames: [list from above]")
```

---

## Token Efficiency

**Haiku verbosity:** Without output constraints, Haiku uses 2–3× more tokens than Opus for the same task. Always include explicit constraints in light-tier prompts:

- Word/line limit: `"Report in under 200 words"` or `"List max 10 items"`
- Format spec: `"Return as a Markdown table"` or `"List one file per line"`

**File reads:** Grep first → read with offset+limit. Never read a full file to find one function.

**PR context:** Use `git diff main...HEAD` to supply changed-file context. Never read individual files to reconstruct what changed.

---

## LiteLLM Proxy Setup

The proxy translates stable tier names (`tier-1-fast`, `tier-2-agentic`, etc.) into actual provider API calls. It's deployed as a Fly.io app but can run anywhere that accepts HTTP.

### Prerequisites

- Fly.io account + `flyctl` CLI
- API keys: `FIREWORKS_API_KEY`, `TOGETHER_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`
- (Optional) Doppler for secrets management

### Quick start

```bash
# Run the setup script (interactive)
bash scripts/setup-litellm.sh

# Or deploy manually:
cd deploy/litellm-proxy
APP_NAME=myapp-litellm flyctl launch --copy-config --name myapp-litellm
flyctl secrets set \
  LITELLM_MASTER_KEY="$(openssl rand -hex 24)" \
  ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  FIREWORKS_API_KEY="$FIREWORKS_API_KEY" \
  TOGETHER_API_KEY="$TOGETHER_API_KEY" \
  GROQ_API_KEY="$GROQ_API_KEY" \
  --app myapp-litellm
flyctl deploy --app myapp-litellm
```

### Activating in model-routing.json

After deploy, update `.claude/model-routing.json`:

```json
"litellm_proxy": {
  "enabled": true,
  "url": "https://myapp-litellm.fly.dev"
}
```

Then set `USE_LITELLM_PROXY=true` in your application's environment.

### Provider smoke-test

```bash
# Verify all tiers are reachable through the proxy
for TIER in tier-1-fast tier-2-agentic tier-3-tool tier-4-extract tier-5-latency; do
  echo -n "$TIER: "
  curl -s -X POST https://myapp-litellm.fly.dev/v1/chat/completions \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$TIER\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":5}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if d.get('choices') else 'FAIL')"
done
```

> **Note:** Always live-probe Together models before relying on them in production. Together's `/v1/models` catalog includes dedicated-only endpoints not available on serverless plans — `model_not_available` errors at call time are the only reliable signal.

---

## Customising

To change keyword lists or Claude Code model assignments, edit `.claude/model-routing.json → overrides` or `platforms.claude_code.tier_map`. The hook reloads it on every prompt — no restart needed.

To swap an OSS backend (e.g., upgrade `tier-1-fast` to a newer DeepSeek model), edit the `model` field in `tier_definitions.tier-1-fast` only — no agent files change.

To add a new tier, add an entry to `tier_definitions` with a new `litellm_alias`, then add a matching `[[model]]` entry in `deploy/litellm-proxy/config.yaml`.
