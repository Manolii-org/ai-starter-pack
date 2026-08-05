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
