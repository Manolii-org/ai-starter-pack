# Model Change Assessment — Claude 5 family catalog + platform matrix refresh

Date: 2026-10-28 · Change class: **add models (catalog + passthrough), no
re-routing** · Sensitivity ceiling: unchanged per route.

## What changed

- `.claude/model-routing.json` (5.9.0 → 5.10.0):
  - `provider_registry.anthropic.models` catalog (Fable 5.1, Opus 5.5/5, Sonnet 5.5/5, Haiku 5.5, plus the 4.x models already routed) with context windows and list prices. Catalog only: no tier, alias or agent points at a new model.
  - `platforms` refreshed from primary sources, each entry dated:
    - `claude_code`: alias resolution, per-alias prices, sub-agent precedence (frontmatter wins since v2.1.251, `_FORCE` in v2.1.257), and `cloud_sessions` — gateway variables are not applied on Claude Code web, so OSS tiers are unreachable there.
    - `cursor`: Cloud Agents, per-run/sub-agent models, custom keys documented for chat only, commit identity.
    - `codex_web`: still excluded — model choice for cloud tasks is undocumented.
    - `devin` (new): mode/effort tiers, no routable model, GitHub App and review bot.
- `deploy/litellm-proxy/config.yaml`: Anthropic passthrough routes for `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, so proxied clients and Autofix can request them by ID.
- `scripts/pre-tool-use.py`: Claude 5 IDs and `fable` registered as Anthropic models. Before this, a protected agent asking for one was blocked as unregistered.
- `scripts/classify-message.py`: Anthropic price table corrected (Opus 4.6/4.7 were $15/$75, now $5/$25; Haiku 4.5 was $0.30/$1.20, now $1/$5) and Claude 5 IDs added.

## Checklist notes

- (2) Sensitivity: passthrough routes are first-party Anthropic; no OSS route changes.
- (10) Cost: no route changes cost. The corrected table changes cost *estimates* only.
- (14) Rollout: additive; nothing selects the new routes unless a caller names them.
- (15) Reversibility: remove the three routes and catalog entries; no consumer depends on them by default.

## Open recommendation (not applied)

The pack's `haiku` OSS alias (GPT-OSS 120B, $0.80/$3.20) and `tier-1-fast` now cost more than Anthropic's own `claude-haiku-5-5` ($0.10/$0.50). Re-routing them is a routing change that needs its own decision.

**Declined 2026-10-29.** The owner decided not to adopt Anthropic API models (ADR-0002 amendment 2026-10-29). Both aliases stay on open-weights models; the Claude 5 entries remain catalog and passthrough routes only. A cheaper open-weights backend for these aliases would be a separate routing change.
