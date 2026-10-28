# Autofix cost alarms (OSS / proxy)

Portable guidance for universes that run pack Autofix with `provider_mode=proxy`
(LiteLLM). Do **not** hardcode secrets; discover names from Doppler (names-only)
and wire alerts to existing telemetry.

## Contract

- Autofix control-plane stays on **GitHub-hosted** runners (`ubuntu-latest`).
- Per-PR successful-fix cap: `max_successful_fixes` (default `1`) on
  `pr-autofix-loop-reusable.yml` — required before enabling Autofix on a product
  repo.
- Dual-ownership XOR: pack Autofix+relay **or** master Autofix+`auto-address-review.yml`,
  never both (`scripts/lint-autofix-xor.py`).

## Recommended alarm path (Buro / LiteLLM)

1. **Proxy spend** — LiteLLM + Langfuse (when enabled on the universe proxy).
   Typical Doppler names (verify live; do not invent):
   - `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
   - LiteLLM master / proxy URL vars already required by `provider_mode=proxy`
2. **Workflow budget exhaustion** — Autofix posts
   `<!-- pack-autofix:budget-exhausted -->` when the per-PR `[autofix]` commit
   cap is hit. Alert on that marker or on repeated skips.
3. **Optional mesh** — OSS-safe dead-agent / spend telemetry is optional and must
   not block enablement (see product-repo ADR gates).

## Proxy mode without LiteLLM

`provider_mode: proxy` works with any endpoint that implements the Anthropic
Messages API — LiteLLM is one option, not a requirement. The reusable exports
`ANTHROPIC_BASE_URL=<litellm_proxy_url>` and sends the `LITELLM_MASTER_KEY`
secret as the API key, so in proxy mode `LITELLM_MASTER_KEY` is simply **the
proxy's key slot**: put the key for whichever endpoint you use there.

```yaml
jobs:
  autofix:
    uses: Manolii-org/ai-starter-pack/.github/workflows/pr-autofix-loop-reusable.yml@<sha> # vX.Y.Z
    with:
      provider_mode: proxy
      litellm_proxy_url: https://openrouter.ai/api   # any Anthropic-compatible base URL
      model: anthropic/claude-sonnet-5.5              # the endpoint's own model id
      max_successful_fixes: '1'
    secrets:
      LITELLM_MASTER_KEY: ${{ secrets.OPENROUTER_API_KEY }}  # the endpoint's key
```

### Gateway authentication

In proxy mode the key reaches the gateway in the `x-api-key` header: the
reusable passes it to `claude-code-action` as `anthropic_api_key`, which
exports it as `ANTHROPIC_API_KEY`. The key never goes to Anthropic, because
`ANTHROPIC_BASE_URL` routes every model request to `litellm_proxy_url`.

- **OpenRouter accepts `x-api-key`.** Verified 2026-10-28 against
  `https://openrouter.ai/api/v1/messages`: `x-api-key`, `Authorization: Bearer`
  and both headers together each returned 200, and a request with neither
  returned 401. No extra input is needed.
- **Base URL has no `/v1`.** Claude Code appends `/v1/messages`, so pass
  `https://openrouter.ai/api`, not `https://openrouter.ai/api/v1`.
- **Set `model`.** With `model` empty, proxy mode sends the LiteLLM alias
  `tier-2-agentic`, which other gateways reject (OpenRouter returns 400
  `not a valid model ID`). Use the gateway's own id. OpenRouter also maps
  bare Anthropic ids such as `claude-sonnet-5-5`, which keeps Claude Code's
  background Haiku-class calls working.
- **Ignore "set `ANTHROPIC_API_KEY` to an empty string" for this workflow.**
  Local Claude Code guides for gateways give that advice. It cannot apply
  here: `claude-code-action` refuses to start unless `anthropic_api_key`,
  `CLAUDE_CODE_OAUTH_TOKEN` or workload identity is set. Its pinned source
  (`base-action/src/validate-env.ts`) shows this, and both the Claude Code
  and OpenRouter GitHub Action guides say the non-empty key is fine in
  non-interactive runs.
- **Bearer-only gateways are not supported yet.** A gateway that reads only
  `Authorization: Bearer` needs `ANTHROPIC_AUTH_TOKEN` as well, which this
  reusable does not set today. Claude Code ranks `ANTHROPIC_AUTH_TOKEN` above
  `ANTHROPIC_API_KEY`. Before enabling Autofix, send one `max_tokens: 1`
  request to `<base URL>/v1/messages` with only `x-api-key` set; a 401 means
  the gateway reads only `Authorization: Bearer`.

### GitHub token

`GH_PAT` on the reusable, and `MN_GH_AUTOMATION_TOKEN` on the standalone
`pr-autofix-loop.yml`, are naming conventions, not maintainer-issued
credentials. Each tenant fills them with **its own** token, scoped to its own
repositories. When unset, Autofix uses the job's `GITHUB_TOKEN`; its pushes
then do not trigger `pull_request` workflows.

Data boundary: in proxy mode, everything the agent puts into a model request
goes to that endpoint. At minimum that includes the PR diff and body, review
comments, CI log excerpts, repository files the agent reads, GitHub metadata,
and the output of commands and tools it runs. Whether the endpoint forwards
requests to upstream providers, and which ones, depends on the endpoint and its
routing settings. Check its retention, region and provider-routing policy
before enabling it.

## Tenants without LiteLLM / Langfuse

The "Recommended alarm path" above assumes a LiteLLM proxy with Langfuse
traces. Tenants that call a provider directly (`provider_mode: anthropic`) or
through a non-LiteLLM proxy have no pack-side spend telemetry. Set the alarm
where the money is spent:

- a **provider-side spend limit or budget alert** on the API key or workspace
  used only by Autofix (one key per purpose makes the alert attributable);
- the workflow-side signals still apply: the
  `<!-- pack-autofix:budget-exhausted -->` marker, `[autofix]` commit counts,
  and the job's runtime cap (`timeout-minutes: 30`).

## Operator checklist

- [ ] Doppler names enumerated (never paste values into chat or YAML).
- [ ] Langfuse project (or equivalent) receives LiteLLM traces for Autofix model
      aliases used by the caller (`model:` input).
- [ ] Alert threshold set (daily/weekly) appropriate to universe size.
- [ ] `max_successful_fixes` left at `1` unless an operator raises it knowingly.
- [ ] `scripts/lint-autofix-xor.py` green on the consumer repo.

## Non-goals

- Do not copy master's ~2k-line Autofix ledger into the pack.
- Do not route Buro Autofix through Manolii KL or master copy-sync.
