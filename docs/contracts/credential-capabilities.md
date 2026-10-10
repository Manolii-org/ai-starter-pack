# Credential capabilities contract

Capability-based credential selection for agent sessions. A session declares
what it can do; the resolver maps capabilities to the smallest set of
credential *names* that session may hold — not the largest set it can fetch.
Status: contract v1. Reference resolver: `scripts/lib/agent_capabilities.py`.

## Problem

Agent sessions historically bootstrapped by exporting the union of every
secret the platform could reach (~25 token/key names in the widest loader).
Provider tokens then sat in session env — visible to every tool call, every
sub-agent, every prompt-echo bug — regardless of whether the task used them.
Exposure breadth, not any single credential, was the leak surface.

## Model

Three layers, in descending authority:

1. **Capability** — a named competence the session is granted, e.g.
   `git-read`, `github-actions-dispatch`, `doppler-read`, `mcp-knowledge`,
   `deploy-vercel`, `deploy-fly`, `llm-proxy`. Capabilities are what a
   launcher/platform *declares*, not what a token can technically do.
2. **Capability table** — the pack-level mapping from each capability to:
   `secret_names` (names only), `source` (`doppler` | `env` | `broker` |
   `mcp`), `retrieval` (`eager` | `deferred`), and `privilege`
   (`standard` | `managed` | `privileged`).
3. **Session manifest** — `config/agent-capabilities.json` (or
   `.ai/capabilities.json` in a consumer): the capabilities THIS session
   needs. In `capabilities` mode the resolver intersects manifest × table
   → the exact env-var names the bootstrap may fetch — absence of a
   capability means absence of the credential. (`mode: legacy` instead
   unions the whole table — a temporary migration path that bypasses the
   manifest, deprecated; it is not the normative model.)

## Rules

- **Names, never values.** The contract, manifests and resolver output
  carry credential *names* only. Values move only through the existing
  secret store → env → runtime path.
- **Broker, don't inject, for privileged ops.** Capabilities resolving to
  `source: broker` produce no env var at all — they produce permission to
  dispatch a named broker workflow (e.g. `github:admin` → the Actions
  broker), keeping provider ops off session env entirely.
- **Deferred by default.** `retrieval: deferred` secrets are fetched by the
  first consumer that needs them, not at bootstrap; `eager` is reserved for
  credentials the session cannot start without (model key, memory key).
- **Parallel retrieval.** Independent fetches in the adapter MUST be issued
  in parallel — capability selection must not make cold start slower.
- **Fail closed on ambiguity.** An unknown capability name, a manifest
  parse error, or a capability mapping to a secret not in the table is a
  resolver error — never a silent skip to a broader fallback.
- **Scoped bundles ≠ reduced permissions.** A per-capability secret bundle
  limits *exposure* (which values reach env). It does not reduce the
  permissions of the stored credential — a `doppler-read` token with
  master/prd scope still sees all of master/prd. Permission reduction is a
  store-side decision, made in the store.
- **Degradation is loud.** A missing manifest must not silently mean
  "everything". Consumers choose: strict (error) or documented legacy-mode
  (`"mode": "legacy"` → union set, flagged in diagnostics until migrated).

## Manifest schema

```json
{
  "version": 1,
  "mode": "capabilities",
  "capabilities": [
    "git-read", "mcp-knowledge", "llm-proxy"
  ],
  "retrieval_overrides": {
    "SUPABASE_ACCESS_TOKEN": "eager"
  }
}
```

`mode` is `capabilities` (default) or `legacy` during migration — anything
else is a resolver error. `retrieval_overrides` is optional and accepts
`eager`/`deferred` per secret name only.

## Capability table (canonical entries)

| capability | secret names | source | retrieval | privilege |
|---|---|---|---|---|
| `git-read` | — (ambient `GITHUB_TOKEN`/git credentials) | env | — | standard |
| `github-actions-read` | `GH_TOKEN` | doppler | deferred | managed |
| `github-actions-dispatch` | — | broker | — | privileged |
| `doppler-read` | `DOPPLER_TOKEN_PRD` | doppler | eager | managed |
| `mcp-knowledge` | `MCP_API_KEY` | doppler | eager | managed |
| `llm-proxy` | `LLM_API_KEY` (or `LITELLM_PROXY_API_KEY`) | doppler | eager | managed |
| `llm-anthropic` | `ANTHROPIC_API_KEY` | doppler | eager | managed |
| `deploy-vercel` | `VERCEL_TOKEN` | doppler | deferred | managed |
| `deploy-fly` | `FLY_API_TOKEN` | doppler | deferred | managed |
| `db-admin-supabase` | `SUPABASE_ACCESS_TOKEN` | doppler | deferred | managed |
| `agent-telemetry` | `MCP_FINANCIAL_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | doppler | deferred | managed |
| `external-browser` | `BROWSERBASE_API_KEY` | doppler | deferred | managed |
| `billing-read` | `GH_BILLING_TOKEN` | doppler | deferred | managed |

`llm-proxy` and `llm-anthropic` are mutually exclusive lanes, not tiers —
`resolve()` rejects a manifest that selects both with a contract error:
`llm-proxy` routes calls through a LiteLLM proxy credential; `llm-anthropic`
is the direct provider key. A consumer selects one — never both — by which
credential is actually published to it. Public consumers take the direct
lane (`llm-anthropic`): a proxy master key must never be published to a
repo that accepts contributions from outside the org boundary, and this
contract does not provide a scoped proxy credential for that surface.

**Amendment 2026-10-10 — estate spend policy and the scoped CI plane.** Two
changes since this section was written:

1. **Policy: no direct Anthropic API credits unless there is absolutely no
   alternative** (operator directive). `llm-anthropic` remains a valid
   *external-consumer* lane — a third party running this pack on their own
   Anthropic account is their spend — but nothing in the Manolii estate may
   provision `ANTHROPIC_API_KEY`/`ANTHROPIC_DIRECT_API_KEY` for its own
   workloads. This repo's `ANTHROPIC_API_KEY` secret was deleted the same day;
   the workflows that consumed it resolve proxy-mode envs first and now take
   the proxy lane unchanged.
2. The missing scoped credential now exists: **a dedicated CI-scoped LiteLLM
   plane** whose per-app `LITELLM_MASTER_KEY` is the scoped key the contract
   said could not be minted (the proxies run DB-less). This repo holds it as
   `LITELLM_MASTER_KEY` + `LITELLM_PROXY_URL`; its blast radius if leaked from
   this public repo is OSS calls on that app only. A manifest selecting
   `llm-proxy` inside the estate resolves against this plane —
   `llm-anthropic` is no longer selected for any org-owned consumer.
   (Deployment identifiers live in the private control-plane repo's instance
   doc — not published here per this repo's surface boundary.)

Repo-local capabilities extend the table; a consumer must not silently
narrow a pack capability it did not define. Extensions are validated
fail-closed: redefining a canonical name, a `secret_names` value that is
not an array of nonempty strings, an unknown `source`/`retrieval`/
`privilege`, or `secret_names` on a `broker` source are all resolver
errors. When two selected capabilities emit the same env name, their
mappings must agree — a conflict is an error, never last-write-wins.

## Reference resolver

`scripts/lib/agent_capabilities.py` resolves `manifest × table → [env names]`:

```bash
python3 scripts/lib/agent_capabilities.py \
  --manifest config/agent-capabilities.example.json \
  --names-only            # prints sorted env names, exit 2 on contract error
python3 scripts/lib/agent_capabilities.py \
  --manifest config/agent-capabilities.example.json \
  --report                # names + source/retrieval/privilege (still no values)
```

The thin adapter per surface (session-start / bootstrap / .mcp.json env) then:
1. Reads the manifest (absent → `legacy` union or error per consumer policy).
2. Resolves → env name list.
3. Fetches `eager` names in parallel at bootstrap; registers `deferred`
   fetchers for first use.
4. Logs capability set + resolved name count — never values.

## Migration path (incremental, per consumer)

1. Land contract + resolver (this change) — no behaviour change.
2. Per consumer bootstrap: add `mode: legacy` manifest → identical env,
   resolver wired but union-wide.
3. Narrow the manifest to real capabilities; keep `deferred` retrieval for
   rarely used creds.
4. Remove the credential from the consumer's injection path ONLY after its
   manifest proves it unneeded — never before (see docs/token-lifecycle.md
   consumer inventory in the control-plane repo).
