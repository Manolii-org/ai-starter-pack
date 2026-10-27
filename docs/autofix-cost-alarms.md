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
      model: anthropic/claude-sonnet-4.5              # the endpoint's own model id
      max_successful_fixes: '1'
    secrets:
      LITELLM_MASTER_KEY: ${{ secrets.OPENROUTER_API_KEY }}  # the endpoint's key
```

Data boundary: in proxy mode the PR diff, review comments and CI log excerpts
the agent reads are sent to that endpoint (and its upstream providers). Check
the endpoint's retention and region policy before enabling it.

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
