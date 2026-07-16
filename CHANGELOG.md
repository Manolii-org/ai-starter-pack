# Changelog

## [1.4.0] - 2026-07-08

Activated the standalone starter pack as the go-forward distribution source by porting the 1.3.x `tier-review` routing changes, the LiteLLM verifier timeout/token overrides, consumer-instance-proven pricing and fallback-chain fixes, and the monitor workflow parity copy. Added the Renovate preset used by consumers to receive future pack bump PRs.

All notable changes to the AI Starter Pack are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] — 2026-07-11

### Added

- **`manolii-om` marketplace plugin** — second entry in `.claude-plugin/marketplace.json`,
  built by `scripts/build-plugin.py --plugin manolii-om` from
  `plugin-sources/manolii-om/` (eval pack + README) plus the copier-rendered
  Operational Memory skills. Ships four propose-only / read-only OM skills
  (`om-fact-capture`, `om-readiness`, `om-staff-answer`, `om-handover`), the
  14-case OM eval pack, and a README pinning the contract-schema source of
  truth (`<your-om-contracts-repo>/contracts/operational-memory/`) — schemas
  are referenced, not forked. Starts at v0.1.0; bumps independently of pack
  version via `plugin/manolii-om/.claude-plugin/plugin.json`.
- **`kl_integration` copier payload** — rendered instances with
  `kl_integration=true` now include `.claude/skills/om-{fact-capture,readiness,staff-answer,handover}/SKILL.md`
  and a `docs/knowledge-layer-access.md` stub. `pack.manifest.yml`
  `feature_excludes.kl_integration` lists the files removed when the flag is
  off; `pack-components.yml.jinja` already required `MCP_API_KEY` in the
  instance Doppler config under this flag (unchanged). The OM eval pack and
  contract validators do NOT render into consumer instances — install the
  `manolii-om` plugin from the marketplace to get them.

### Changed

- **`scripts/build-plugin.py`** generalised to build multiple plugins
  (`--plugin manolii-framework|manolii-om|all`, default `all`). The
  `manolii-framework` skills bundle now explicitly drops the OM skills so
  they ship only via `manolii-om` (dual-run retirement policy §6 / the anchor client engagement
  execution plan B5). Framework plugin bytes are unchanged aside from the
  removed OM skill directories that were never intended to appear there.
- **`.github/workflows/plugin-eval-gate.yml`** builds and drift-checks both
  plugins on every `plugin/**`, `plugin-sources/**`, `.claude/**`, or
  `.claude-plugin/**` change.
- **`copier.yml`** `_exclude` now covers `plugin-sources/**` so plugin build
  inputs never render into consumer instances.

### Notes

- A `v1.3.0` release tag should follow merge so downstream consumers can pin
  the standalone pack (Renovate `github>Manolii-org/ai-starter-pack` picks up
  both plugins via the marketplace manifest).
- OM skills are deliberately excluded from `shared-ai-skills` sync scope —
  they ship via this plugin only.

## [1.2.2] — 2026-06-12

### Fixed

- **Docs accuracy (`local_path` resolution)** — fly.toml, `deploy/litellm-proxy/README.md`,
  and `scripts/setup-litellm.sh` comments incorrectly stated `[[files]] local_path`
  resolves against flyctl's working directory; per Fly docs it resolves against the
  directory containing `fly.toml`. Deploy guidance unchanged (deploying from the proxy
  dir remains the simplest invocation — flyctl auto-discovers fly.toml).

## [1.2.1] — 2026-06-12

Proxy no-custom-image convergence (plan step 3; ADR-0023 Decision 3). Validated
2026-06-12 on live throwaway Fly apps: both `[[files]] local_path` and
`secret_name` injection variants deployed green (`/health/liveliness` 200), and
a `--detailed_debug` run captured `sonnet_advisor_guardrail.SonnetAdvisorGuardrail`
registered in LiteLLM's callback manager at startup.

### Changed

- **`deploy/litellm-proxy/fly.toml`** — converged to the no-custom-image
  pattern: pinned public `ghcr.io/berriai/litellm-non_root` image + `[[files]]`
  injection of BOTH `config.yaml` and `sonnet_advisor_guardrail.py`
  (`local_path` form) + `[env] PYTHONPATH=/tmp`. Previously only config.yaml
  was injected (via `secret_name`) while the guardrail required the Dockerfile
  bake. `config.yaml` is instance policy and must never be baked into a shared
  image. The `secret_name` (base64) form remains documented as the
  production-parity alternative.
- **`scripts/setup-litellm.sh`** — deploy step no longer passes
  `--dockerfile`/`--build-arg`; it now runs `flyctl deploy` from the proxy
  directory so `[[files]] local_path` resolves correctly.

### Added

- **`deploy/litellm-proxy/README.md`** — deploy pattern, injection-variant
  trade-offs (`local_path` vs `secret_name`), and a migration note for
  instances still on a `[build] dockerfile` custom image.

### Removed

- **`deploy/litellm-proxy/Dockerfile`** — the per-instance image build is gone;
  the public upstream image is used as-is.

## [1.2.0] — 2026-06-12

Copier migration, Phase A (ADR-0023 Decision 1; report §10 step 2).

### Changed

- **The pack is now a Copier template.** `copier.yml` maps the 5 feature flags to
  questions (defaults off) and `install_mode` to a choice question whose brand
  answers mirror `.brand/{branded,unbranded}.yml`. `_templates_suffix: .jinja`
  protects `${{ }}` in workflow files — only the 23 files genuinely carrying
  the attribution-line placeholder were renamed to `.jinja`. Flag-gated files use
  Copier conditional filenames (`{% if flag %}name{% endif %}`), replacing
  `pack.manifest.yml` `feature_excludes` as the exclusion mechanism.
- `scripts/render-pack.py` is now a thin compatibility wrapper around
  `copier copy` (identical CLI; byte-identical output verified across both
  modes and each flag toggled singly). New consumers should invoke Copier
  directly.
- Instance-owned files (`CLAUDE.md`, `.claude/model-routing.json`,
  `.claude/mcp.json`, `.claude/hooks/session-start.sh`) are declared
  `_skip_if_exists` — `copier update` will never overwrite them.

### Added

- `.copier-answers.yml` is emitted into rendered output (machine-written
  provenance; will supersede the hand-maintained `.starter-pack-source` pin).
- `tests/test_copier_render.py` — template render contract tests (canonical
  repo only; excluded from rendered output).

## [1.1.5] — 2026-06-12

Standalone-capability fixes (validation session 2026-06-11, report §11).

### Added

- `codex_adversarial` feature flag (default off) — the `codex-adversarial` agent
  requires `OPENAI_API_KEY`; it is now excluded from renders unless `--codex` is
  passed, so a default render has zero non-Anthropic provider dependencies.

### Fixed

- **Render consistency (langfuse)** — `scripts/otel-langfuse-headers.sh` now always
  ships (dormant without `LANGFUSE_*` keys) instead of being excluded when
  `langfuse_telemetry` is off, because `settings.json` references it via
  `otelHeadersHelper` regardless of the flag. Closes the dangling-reference issue
  documented in downstream `.starter-pack-source` notes.

## [1.1.4] — 2026-06-04

Security hardening + review fixes (PR #1990 review: Gemini + Codex + CodeRabbit).

### Fixed

- **Security (quarantine escape)** — `scripts/injection_scan.py`: `scan()` only skips
  re-wrapping when content is a COMPLETE wrapper (new `_is_fully_quarantined()`), not when
  it merely *starts with* the public marker (spoofable). `quarantine()` now escapes nested
  `</external-content-quarantined>` tags so attacker content cannot terminate the wrapper
  early. Closes a path where raw attacker content could be returned in the `quarantined` field.
- `scripts/model-routing-suggester.py` — governance regex: `guarded? paths?` →
  `guard(?:ed)? paths?` (now matches "guard paths"); `\b…\b` → non-word lookarounds so
  dot-prefixed `.ai/guards` matches after a space.
- `scripts/render-pack.py` — detector exclusion scoped to exact path `scripts/pack-drift-check.py`.
- Tests: live + pack `test_injection_scan.py` reconciled to the always-detect contract and
  given two quarantine-escape regression tests (marker-prefix spoof, nested closing tag).

## [1.1.3] — 2026-06-04

Render/.gitignore hygiene (found during post-seed audit of a downstream instance).

### Fixed

- `.gitignore` — now ignores `.ai/session-context.md` (runtime SessionStart output,
  must never be committed), `.ai/compact-state.json`, and Python caches
  (`__pycache__/`, `*.pyc`). Prevents rendered consumers from committing runtime junk.

## [1.1.2] — 2026-06-04

Render hygiene — ensures unbranded output is clean for seeding into downstream
repos (first consumer: `a downstream instance`, dormant-routing config).

### Fixed

- `scripts/render-pack.py` — `render()` now always skips build artifacts and
  caches (`releases/`, `dist/`, `__pycache__`, `*.pyc`, `*.zip`) regardless of
  brand/feature flags; a prebuilt `releases/*.zip` was previously copied into output.
- `scripts/render-pack.py` — `verify_clean()` no longer flags the brand-leak
  detector (`pack-drift-check.py`), whose pattern list legitimately contains
  marker strings. Unbranded render now passes brand verification cleanly.

## [1.1.1] — 2026-06-04

Drift reconciliation against the upstream harness. Bidirectional triage of all
shared modules (agents, skills, commands, scripts); only generic, vendor-neutral
changes were applied — infra-coupled rewrites were deliberately not back-ported.

### Added

- `scripts/model-routing-suggester.py` — new `governance_judgment` heavy-tier
  signal: prompts about guards/blast-radius, auto-merge & branch protections,
  CI gates, and data-sensitivity/safety-tier reasoning now escalate to the
  strongest tier (the failure mode on these is rationalising existing config
  instead of reasoning blast-radius from first principles). Infra-specific tokens
  (e.g. `lib/safety`) were stripped before porting.

### Note

- `scripts/injection_scan.py` already contained the marker-prefix anti-bypass
  fix; the upstream harness did **not**, and was patched in the same pass. No
  change to the pack copy was required.

## [1.1.0] — 2026-05-30

Security & governance refresh. All additions are generic and vendor-neutral.

### Added

**Security — response hygiene & token-leak defence:**
- `.claude/hooks/post-tool.py` — now scans MCP / web / browser tool *results* for credential-shaped content (`[SECRET-IN-RESPONSE]`) and external content for prompt-injection patterns (`[INJECTION-WATCH]`). Advisory and non-blocking.
- `scripts/pre-tool-use.py` — adds a **scope-budget** guard (write-capable agent dispatches must declare a `SCOPE_BUDGET:` / `allowed_paths` block), a **broad-dispatch** guard (blocks whole-repo `Explore` sweeps), and a **token-leak Bash blocker** (`echo`/`printf`/`printenv`/`declare`/`env|grep` of secret-named variables). (Existing model-routing + PR-targeting guards retained.)
- `.claude/agents/secrets-handler.md` — restricted-tier sub-agent routed through the approved `sonnet` restricted_us_oss_ok path for credential-bearing tool/MCP responses; returns only a sanitised 3-branch summary (write / read-summary / error).
- `scripts/injection_scan.py` (importable `scan()`), `scripts/canary_tokens.py`, `scripts/safe_env.sh` (`is_set`/`safe_summary`/`safe_prefix`/`safe_length`).
- `.ai/security/token-shapes.json` — canonical provider token-shape regex set used by the post-tool scan.
- `docs/mcp-response-hygiene.md`, `docs/token-leak-hygiene.md` — threat model + patterns, aligned to the hooks the pack actually ships.

**Governance:**
- Skills `assess-model` (Model Change Protocol — 15-item checklist; records the decision as a local ADR), `completeness-check`, `phase-gate`.
- `docs/model-change-protocol.md`, `docs/model-config-schema.md` (per-model API config: reasoning_effort, thinking_budget, response_format, tool_choice, capability flags), and `docs/us-oss-eligibility-matrix.md` (gated behind the `oss_routing` feature).

**Memory & decisions:**
- `.claude/agents/memory-keeper.md` — maintains the committed local `.ai/memory/` JSONL knowledge base after significant sessions.
- `.ai/decisions/0000-adr-template.md` + `.ai/decisions/README.md` — ADR template and practice guide (populates the previously-empty decisions directory).

**Anti-drift:**
- `scripts/pack-drift-check.py` — self-consistency validator: org-leak scan (FAIL on hardcoded org names; WARN on the opt-in `kl_` prefix), provider-name WARN, README agent-count check, `feature_excludes` path existence, and a sub-agent model cost guard (FAIL on `opus`). Run `python3 scripts/pack-drift-check.py` (exit 0/1).

### Changed

- `pack.manifest.yml` now carries a `version` field; `docs/us-oss-eligibility-matrix.md` is excluded unless the `oss_routing` feature is enabled.
- `README-STARTER-PACK.md` and the `CLAUDE.md` template refreshed: corrected component counts, a new **Scope & What's Not Included** section, and the new security/governance skills + agents listed.
- Genericized pre-existing organisation coupling in `scripts/check-oss-routing.py` — the optional Langfuse credential source is now read from an env var (`LANGFUSE_DOPPLER_PROJECT`) instead of a hardcoded private secrets-manager project; the session-affinity probe value is now generic.

### Notes

- All additions are genericized — no organisation names, internal repositories, or product-specific tool coupling. Optional integrations (OSS routing, remote memory, browser automation, telemetry) remain off by default and gated in `pack.manifest.yml`.
- Run `python3 scripts/pack-drift-check.py` before shipping a pack variant to catch org-leak, stale README counts, broken `feature_excludes` paths, and cost-guard violations.

## [1.0.0] — 2026-05-26

Initial stable release.

**Included:**
- 9-tier multi-model routing framework (Anthropic Claude + Fireworks/Together/Groq/OpenRouter OSS)
- 26 agents (PR assessment pipeline, critics, explorers, generators)
- 19 skills (security, database, TDD, compliance, analytics, curation)
- 46 slash commands (development, memory, PR monitoring, testing, governance)
- 7 lifecycle hooks (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PreCompact, PostCompact, Stop)
- Memory system (facts, patterns, sessions, decisions, retry queues)
- 21 validation + utility scripts
- Husky git hooks (pre-commit, commit-msg, pre-push)
- CI/CD workflows (lint, test, PR assessment, secret scanning, mutation testing)
- Comprehensive documentation (quality gates, token discipline, OSS delegation, browser automation)

