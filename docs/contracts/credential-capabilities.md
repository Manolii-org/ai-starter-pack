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
   needs. The resolver intersects manifest × table → the exact env-var
   names the bootstrap may fetch. Absence of a capability means absence of
   the credential.

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
  "mode": "capabilities",               // or "legacy" during migration
  "capabilities": [
    "git-read", "mcp-knowledge", "llm-proxy"
  ],
  "retrieval_overrides": {              // optional per-secret override
    "SUPABASE_ACCESS_TOKEN": "eager"
  }
}
```

## Capability table (canonical entries)

| capability | secret names | source | retrieval | privilege |
|---|---|---|---|---|
| `git-read` | — (ambient `GITHUB_TOKEN`/git credentials) | env | — | standard |
| `github-actions-read` | `GH_TOKEN` | doppler | deferred | managed |
| `github-actions-dispatch` | — | broker | — | privileged |
| `doppler-read` | `DOPPLER_TOKEN_PRD` | doppler | eager | managed |
| `mcp-knowledge` | `MCP_API_KEY` | doppler | eager | managed |
| `llm-proxy` | `LLM_API_KEY` (or `LITELLM_PROXY_API_KEY`) | doppler | eager | managed |
| `deploy-vercel` | `VERCEL_TOKEN` | doppler | deferred | managed |
| `deploy-fly` | `FLY_API_TOKEN` | doppler | deferred | managed |
| `db-admin-supabase` | `SUPABASE_ACCESS_TOKEN` | doppler | deferred | managed |
| `agent-telemetry` | `MCP_FINANCIAL_KEY`, `LANGFUSE_*` | doppler | deferred | managed |
| `external-browser` | `BROWSERBASE_API_KEY` | doppler | deferred | managed |
| `billing-read` | `GH_BILLING_TOKEN` | doppler | deferred | managed |

Repo-local capabilities extend the table; a consumer must not silently
narrow a pack capability it did not define.

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
