# Changelog

## 1.25.0 — 2026-10-28

Upgrade order for consumers:

1. **Before bumping, check Autofix in proxy mode.** With no `model` set,
   v1.25.0 sends PR context to `tier-2-agentic-pro` (MiniMax M3 on
   Fireworks, with OpenRouter fallbacks) instead of `tier-2-agentic` (see
   "Changed"). A repository whose data must stay with a particular provider
   sets `model` / `AUTOFIX_MODEL` first. On a non-LiteLLM Anthropic-compatible
   gateway, `model` is required anyway: any LiteLLM alias is rejected there
   (`docs/autofix-cost-alarms.md` § Gateway authentication).
2. Pin every pack `uses:` to the v1.25.0 commit SHA, with a `# v1.25.0`
   comment, in one PR. The Renovate preset's `manolii-ai-starter-pack` group
   does this. Change any `pack_ref` input to the same release.
3. Optional: callers of `secret-scan-reusable` can read
   `npm_audit_activation` / `pip_audit_activation`, or set
   `fail_on_inert: true` (see "Activation contract" below).
4. Operators who deploy their own LiteLLM proxy from the pack template:
   redeploy it to drop the dead Together routes (see "Fixed").
   `TOGETHER_API_KEY` is no longer needed. Proxies already deployed keep
   their old config until redeployed.

No inputs were removed or renamed. Every new input defaults to the previous
behaviour; the Autofix proxy model default (item 1) is the only changed
default. The feature-control SDK is released separately as component tag
`feature-controls-v0.1.0`. Component tags never move `v1`.

### Feature-control SDK 0.1.0 (component release)

- **First release of `packages/feature-controls`.** npm
  `@manolii/feature-controls` (root `package.json`) and Python
  `manolii-feature-controls` both declare `0.1.0`. The maintainer
  `feature-controls-release.py prepare`/`verify` dry run passed on `main`
  commit `8c33fa2a16f80167f9274613343ab2aaab1eac9b`. That commit is tagged
  `feature-controls-v0.1.0` (annotated, through `release-tag.yml`). A component tag never moves `v1`. The SDK source is
  unchanged: it matches the PR #203 squash on `main` plus the README and
  the patched setuptools build backend (#219).

Upgrade order for SDK consumers:

1. Replace any pin to a commit that is not on `main` (for example a
   pre-squash pull-request commit) with the full SHA that
   `feature-controls-v0.1.0` points at. Pin the npm and Python dependencies
   to the same SHA in one change, and regenerate the lockfile.
2. pnpm consumers that grant build permission by name and version change
   `@manolii/feature-controls@0.0.0` to `@manolii/feature-controls@0.1.0`
   in the same change (`docs/feature-controls.md`).
3. The pack's Renovate preset keeps SDK upgrades on reviewed,
   dashboard-approved PRs. Prereleases are ignored.

### Changed

- **Autofix proxy default is now `tier-2-agentic-pro` (MiniMax M3).** With no
  model set, proxy-mode Autofix uses MiniMax M3 on Fireworks ($0.30/$1.20 per
  MTok) instead of `tier-2-agentic`. The pack LiteLLM template gains the
  `tier-2-agentic-pro` route (fallbacks: M3 on OpenRouter, zero-data-retention
  endpoints only, then Kimi K2.6 on Fireworks). M3 passed
  the Autofix acceptance probe first time; `tier-1-fast` (DeepSeek V4.x Flash
  on proxies that map it) also passed and is the documented budget override.
  Direct-key default and explicit `model` / `AUTOFIX_MODEL` overrides are
  unchanged. Data routing is unchanged: PRC-origin weights on US hosting,
  `internal` data only. Proxies deployed from an older template do not have
  the alias: Autofix checks the proxy's `/v1/models` list first and uses
  `tier-2-agentic` there (with a notice) rather than failing. Redeploy the
  proxy from this template to get M3.

### Fixed

- **The LiteLLM template no longer routes to dead Together models.** Together
  stopped serving Kimi K2.6 and Qwen 3.5 397B serverless; both now need a
  dedicated deployment, so every Together hop in the template returned HTTP
  400. `tier-2-agentic` now runs Kimi K2.6 on Fireworks (as the registry
  already said). Its Kimi and Qwen fallbacks, and the Kimi fallbacks of
  `tier-0-oss-heavy`, `tier-1-fast` and `tier-3-tool`, move to OpenRouter.
  Three fallback names change from `*-together` to `*-openrouter`.
  `TOGETHER_API_KEY` is no longer needed; `setup-litellm.sh` stops asking for
  it and smoke-tests `tier-2-agentic` with the Fireworks key. Every OpenRouter
  route for Kimi, Qwen or MiniMax weights, including the new
  `tier-2-agentic-pro` fallback, is pinned to US-hosted zero-data-retention
  endpoints. Proxies already deployed from an older template keep their
  config until redeployed.
- **v1.24.0 overstated the Autofix saving.** Its notes called
  `tier-2-agentic` Kimi K2.6 at $0.50/$2.80, "about 90% cheaper". The
  template's K2.6 primary host (Together) no longer serves it, Fireworks K2.6
  is $0.95/$4.00, and some proxies (including Manolii's) map the alias to
  Kimi K3 at $3/$15, more per token than Sonnet 5.5. The registry price is
  corrected and ADR-0002 is amended.
- **Finalise gate targets the repository that is actually pushed.** A `cd`
  inside a subshell no longer leaks into later commands, so
  `(cd sub && make) && git push` validates the current repository rather than
  `sub`. A `cd` inside a brace group (`{ cd sub; …; } && git push`) is now
  followed, because it does persist. Before this fix, a nested repository
  could be validated in place of the pushed one.
- **The finalise gate validates every candidate repository.** These are the
  starting directory, each `cd` target and each `git -C` target, in both
  literal and `~`-expanded spellings. It denies if any of them fails.
  Commands with quoted parentheses, brace groups in a pipeline, or a quoted
  `~` can no longer route validation away from the repository that is
  actually pushed. A command that names only one repository behaves exactly
  as before. Several candidates run concurrently, each capped at 1100 s, so
  a slow one cannot starve another and together they stay inside the host's
  1200 s hook timeout.
- **Finalise gate trust boundary when the host declares no workspace.**
  Codex hooks, and Claude hooks without `CLAUDE_PROJECT_DIR`, previously
  treated every path as trusted. `(cd /tmp/x && make) && git push` or
  `git -C /tmp/x push` could therefore run `/tmp/x`'s recipes. The workspace
  now falls back to the repository holding the hook's working directory.

### Docs: legacy bare `v0.x` tags

- `docs/email-capture.md` § Legacy bare `v0.x` tags lists every pre-namespace
  tag (`v0.1.0-rc.1` to `v0.2.6`) with its kind, commit and component. They
  stay frozen: never deleted or moved. `v0.2.4` and `v0.2.5` sit on
  Codex-review commits, and `v0.2.6` is the same commit as
  `email-capture-v0.2.6`. No tag changed; nothing to upgrade. Lanes still on
  `v0.2.3` should move to `email-capture-v0.2.6` by SHA, after keeping
  `allocate`/`await`/`release` in one job.

### Docs: shared rollout and runner-version practice

- `docs/issue-lifecycle.md` § *Pending state as a retry queue*: use the closed-loop issue
  as the retry queue for deferred/blocked work (edit in place, bounded quiet, loud when
  stuck or untracked).
- `docs/workflow-outcome-convention.md` § *Retry polls for `DEFERRED` work*: how a poll
  may pass quietly without breaking the "deferred is not success" rule.
- `docs/fly-runner-setup.md` § *Runner version policy*: what a fleet that disables runner
  auto-update must provide to stay inside GitHub's 30-day window.

### Docs

- **Autofix through an Anthropic-compatible gateway**
  (`docs/autofix-cost-alarms.md` § Gateway authentication). Proxy mode sends
  the key as `x-api-key`. OpenRouter accepts this (verified 2026-10-28), so
  no new input is needed. The base URL takes no `/v1`, and `model` must be
  set because the proxy default (`tier-2-agentic-pro`, see "Changed") is a
  LiteLLM alias. Gateways that
  read only `Authorization: Bearer` are not supported yet. `GH_PAT` /
  `MN_GH_AUTOMATION_TOKEN` are documented as tenant-owned naming conventions.
  No behaviour change; nothing to upgrade.

### Activation contract: secret-scan dependency audits

- **`secret-scan-reusable` reports audits that ran nothing.** Before this,
  a repository without a root `package-lock.json` got a green
  `Dependency Vulnerability Audit` that audited nothing. A repository without
  `requirements*.txt` / `uv.lock` got the same from `Python Dependency Audit`:
  with only a bare `pyproject.toml`, its fallback `pip-audit` sees the runner's
  own packages, not the project's. That case now reports `inert`; the audit
  itself still runs unchanged.
  - New outputs `npm_audit_activation` and `pip_audit_activation`
    (`active` | `inert`).
  - When neither audit has a dependency graph, the run warns
    `secret-scan INERT`.
  - New opt-in input `fail_on_inert` (default `false`) fails the
    `Dependency Vulnerability Audit` job in that case.
  - Gitleaks is unchanged, and so are the job names, and with them the
    required-check names.
- `docs/workflow-outcome-convention.md` lists the reusables reviewed and
  deliberately left without an activation output, with the reason for each.

Upgrade order for this change:

1. Nothing is required: defaults are unchanged and no caller turns red.
2. To make the audit a real gate, read `needs.<job>.outputs.npm_audit_activation`
   / `pip_audit_activation`, or pass `fail_on_inert: true` once the repository
   commits a root `package-lock.json`, `requirements*.txt` or `uv.lock`. pnpm/yarn-only
   repositories without Python stay `inert`; keep their package manager's
   own audit.

## 1.24.0 — 2026-10-28

Upgrade order for consumers:

1. Before bumping, decide the Autofix model for each repository that runs it
   through a proxy without setting one. With no model set, v1.24.0 sends PR
   context to `tier-2-agentic` (see "Changed"); repositories above
   `internal` sensitivity, or that must stay with Anthropic, set `model` /
   `AUTOFIX_MODEL` first (for example `claude-sonnet-5-5`).
2. Pin every pack `uses:` to the v1.24.0 commit SHA (with a `# v1.24.0`
   comment) in one PR; the Renovate preset's `manolii-ai-starter-pack` group
   does this for you. Change any `pack_ref` input to the same release.
3. Optional: list coding-agent bots in `trusted_agent_bots` /
   `AUTOFIX_TRUSTED_AGENT_BOTS` so Autofix fixes CI failures on their PRs.

No inputs were removed or renamed; every new input defaults to the previous
behaviour except the Autofix model default described below. The new QA
finalise gate ships in `warn` mode, so `copier update` and plugin upgrades do
not start blocking pushes.

### QA framework (`docs/qa-framework.md`)

- **Finalise gate** `scripts/qa/finalise-gate.py`. It runs the repository's
  affected `scripts/agent-check.py` recipes when an agent hands work off:
  `git push`, `gh pr create`, or the GitHub PR-creation tool. It does not run
  on every turn. It is wired for Claude Code (a second PreToolUse group in
  `.claude/settings.json` and the plugin's `hooks.json`), Codex
  (`.codex/hooks.json`, which must be trusted once with `/hooks`), Cursor
  including cloud agents (`.cursor/hooks.json`) and git (`.githooks/pre-push`,
  `.husky/pre-push`, or `--install-git-hook` for Devin and humans).
  - An unchanged candidate is a cache hit of about 1s. Only a real check
    failure blocks.
  - A missing integration ref, missing toolchain or timeout is reported as
    incomplete and allows the hand-off.
  - The blocking message carries the failing check ids plus the last 30 log
    lines, so the agent does not read the full log.
  - `AGENT_FINALISE_GATE=off` bypasses the gate and is recorded in
    `.ai/bypass-log.jsonl`.
  - Each run appends a cost/outcome row to `.ai/agent-checks/finalise-gate.jsonl`.
  - A shell prefilter keeps the overhead to about 5 ms per shell call.
  - PR-creation tools are matched under any MCP server name
    (`mcp__.*__create_pull_request`).
  - It is a no-op without `config/agent-checks.json`.
- **Behaviour for adopters.** A new owner-owned `config/qa.json` (in
  `_skip_if_exists`) ships `finalise_gate.mode: warn`. Failures and
  incomplete validation reach the agent as model-visible context, and the
  push or PR proceeds. `copier update` and plugin upgrades therefore never
  start blocking an existing repository. Set `mode: block` once your recipes
  pass; `block` is planned as the v2 default. `.cursor/hooks.json` and
  `.codex/hooks.json` are also `_skip_if_exists`, and so is
  `.githooks/pre-push`: an existing file is kept, so add the gate entry by hand.
- These are not source failures, so the gate never blocks on them:
  - environment setup failures;
  - per-check timeouts;
  - recipes exiting `3` ("could not run here").

  The gate only runs recipes for repositories inside the opened workspace.
- `.husky/pre-push` runs the gate first. It skips its full-repo
  typecheck/lint/test phases only when
  `finalise_gate.replace_legacy_pre_push: true`. The default keeps the
  current behaviour.
- **`scripts/qa/ui-smoke.mjs`** checks the routes a change affects in real
  Chromium at 375px and 1280px. It fails on:
  - HTTP errors, runtime or console errors, and broken same-origin assets
    (scripts, stylesheets, fonts and images);
  - mobile overflow, or a missing `width=device-width` viewport;
  - serious or critical axe violations.

  It infers routes from Next.js `app/` and `pages/` paths (router roots only
  at the repository root, `src/` or `apps|packages/<name>/`; page objects and
  test/story files never become routes), ignores console errors logged by
  third-party scripts, and starts and stops
  the dev server from `start_command`, and writes screenshots and ARIA
  snapshots to the ignored `.ai/agent-checks/ui-smoke/`. The dev server's
  output goes to `server.log` there. `--profile launch` adds a slow-3G + 4x
  CPU mobile pass with DOMContentLoaded and LCP budgets. A bypass header is sent only to the
  app's origin, re-checked on every redirect hop. The run exits `3` when no browser
  or server is available, and a watchdog stops the dev server even if the
  run is killed.
- **`ui-smoke-reusable.yml`** runs the same script against a preview
  (`deployment_status`) or production (schedule) URL. It installs no app and
  runs no build. Its output is `status`.
- **`config/agent-checks.app-recipes.example.json`** has copy-ready Node
  recipes: a frozen `npm ci` environment, lint and unit on changed paths,
  typecheck, and `ui-smoke`.
- **exploratory-bug-bash 1.1.0** adds:
  - a `web-mobile` (375px) target;
  - the launch personas `impatient`, `trust-checker`, `literal` and `breaker`;
  - `charters.launch.example.txt`;
  - § Launch QA, which ranks by severity × journey stage, proves fixes with
    deterministic tests and allows one sanity re-run.

  Changing `e2e.config.ts` changes the calibration fingerprint, so
  recalibrate before the next scheduled run.
- The `qa` agent is now a bounded procedure with a return contract: smoke
  first, then journey, then classification. It was a stub. `/qa` starts with
  the smoke.
- `docs/pre-pr-quality-gate.md` § 3 adds two rules:
  - a fix for an escaped defect carries a test that fails on base;
  - a user-facing change was seen running.

### Added

- **Autofix for PRs pushed by coding agents (opt-in).** New
  `pr-autofix-loop-reusable` input `trusted_agent_bots` and standalone
  repository variable `AUTOFIX_TRUSTED_AGENT_BOTS` list agent bot logins
  (Devin, Codex cloud tasks, Cursor Cloud Agents) whose pushes may trigger
  Autofix when CI fails. Off by default; CI-failure path and same-repository
  heads only; `*` and malformed entries are dropped. The sanitised list
  reaches the action's `allowed_bots` only on CI-failure runs, and every
  `workflow_run` must come from this repository (checked by ID).

### Changed

- **Autofix uses a lean model by default (ADR-0002).** When no model is set
  (reusable `model` input, standalone `AUTOFIX_MODEL`), the agent now runs
  `tier-2-agentic` through a proxy and `claude-sonnet-5-5` with a direct
  Anthropic key, instead of the Claude Code CLI default (Opus), which a proxy
  usually cannot serve. Per-token cost falls by about 60% (direct) and 90%
  (proxy). Set a model explicitly if your code is above `internal`
  sensitivity or your proxy is not the pack's LiteLLM.
  **Data routing:** in proxy mode with no model set, Autofix previously
  failed before sending anything; it now sends PR context (diff, review
  text, CI log excerpts) to `tier-2-agentic`, which is Kimi K2.6 (open
  weights, PRC-origin, hosted on Fireworks; cleared for `internal` data
  only). If that is not acceptable for a repository, set `model` /
  `AUTOFIX_MODEL` (for example `claude-sonnet-5-5`) before upgrading.
- **Model registry 5.10.0 and platform matrix.** Claude 5 family catalog with
  list prices (catalog only, no re-routing); `platforms` refreshed for
  Claude Code (including web: proxy routing is unavailable there), Cursor
  Cloud Agents, Codex web and a new Devin entry, each with dated sources.
- Proxy template: Anthropic passthrough routes for every full Claude ID the
  guard accepts (`claude-fable-5-1`, `claude-opus-5-5`, `claude-opus-5`,
  `claude-sonnet-5-5`, `claude-sonnet-5`, `claude-haiku-5-5`, and the
  previously unrouted `claude-opus-4-7`).

### Fixed

- The pre-tool-use guard blocked protected sub-agents that asked for a
  current Claude model (`claude-sonnet-5-5`, `fable`, …) as unregistered.
- Cost estimates used wrong Anthropic prices (Opus 4.6/4.7 at $15/$75, Haiku
  4.5 at $0.30/$1.20); corrected, and Claude 5 prices added.
- The session cost logger priced Claude 5 main threads at the Sonnet 4.6 rate,
  booked Claude 5 sub-agents at the Haiku proxy rate as proxy spend, and kept
  Haiku at $0.30/$1.20; the `opus` and `fable` aliases were unpriced and
  `sonnet` was always charged at the proxy's DeepSeek rate. Every model name
  the guard accepts is now priced as the Anthropic model it resolves to (proxy
  rates only when a proxy is active), and a parity test keeps the guard,
  proxy routes and cost logger in step.

## 1.23.0 — 2026-10-27

Upgrade order for consumers:

1. Pin every pack `uses:` to the v1.23.0 commit SHA (with a `# v1.23.0`
   comment) in one PR. The Renovate preset's `manolii-ai-starter-pack` group
   does this for you once your preset pin is bumped.
2. Then enable or re-enable Autofix on one repository first, and watch its
   first `[autofix]` commit before rolling out further.

### Behaviour changes

- **The floating `v1` alias moves from v1.21.0 to v1.23.0.** `v1.22.0` was cut
  outside `release-tag.yml`, so `v1` never moved for it. Every caller that
  relies on `pack_ref: v1` loads v1.23.0 code on release. That covers
  `pr-assessment-reusable`'s runner/classifier checkout, the
  `e2e-playwright-reusable` failure classifier and
  `integration-admission-reusable` (all default `pack_ref: v1`), and also
  anything pinned to `@v1`.
- **Autofix now acts (`pr-autofix-loop`, reusable and standalone).** Since the
  claude-code-action v1 pin, Autofix was a silent no-op. The step passed v0
  inputs (`direct_prompt`, `model`, `allowed_tools`), the action saw no
  prompt, logged `No trigger found`, and the job went green.
  - Both workflows now pass `prompt` and `claude_args` (`--allowedTools`,
    plus `--model` when the reusable's `model` input is set).
  - **After upgrading, Autofix WILL push `[autofix]` commits and spend model
    tokens.**
  - Pushes made with `GITHUB_TOKEN` do not re-trigger PR checks. Reusable
    callers pass `GH_PAT`; standalone instances set the
    `MN_GH_AUTOMATION_TOKEN` secret. Without it, Autofix falls back to
    `GITHUB_TOKEN`.
  - Standalone instances receive this on `copier update`.
- **Autofix fails loudly instead of passing silently.**
  - A dispatched agent step that produces no execution file now fails the
    job.
  - Skip paths (loop-break, fix cap, budget, opt-out label) stay green.
  - Actors without write access, and bot actors other than the
    `github-actions[bot]` review relay, are now skipped before dispatch. The
    action would otherwise fail the job for them. The relay is allowed via
    `allowed_bots: github-actions`.
- **Autofix runs are no longer cancelled by later PR comments.** The
  standalone workflow held a workflow-level lock with `cancel-in-progress:
  true`, so a review-bot comment that Autofix then skipped cancelled an agent
  mid-fix. The lock is now on the agent job and queues instead of cancelling.
  Reusable callers that copied the documented example should change their
  `concurrency` to `cancel-in-progress: false`.
- **Autofix uses the base copy of the instruction surface.**
  - A PR that edits `CLAUDE.md`, `AGENTS.md`, `.claude/`, `.mcp.json` and
    related files no longer changes the agent's instructions; see Security.
  - Agent commits that touch those paths are refused.
- **coverage-ratchet INERT warning.** An absent `baseline_file` now emits
  `::warning::coverage-ratchet INERT: no baseline at <path>; this required
  check cannot fail` and sets `activation=inert`. The run passes by
  default; `require_baseline: true` or `fail_on_inert: true` makes it fail.
- **e2e-playwright INERT warning.** A run where Playwright executed no test
  (all skipped, or none found) emits `::warning::e2e-playwright INERT …` and
  sets `activation=inert`. It passes by default; `fail_on_inert: true`
  makes it fail.
- **Devin meta-plugin:** `registry/platform/om` moved from `requiredPlugins`
  to `optionalPlugins` in the root `.devin-plugin/plugin.json`.
  - Tenants without a Knowledge Layer backend can now install the baseline.
  - KL tenants: `om` is no longer auto-installed. Add its `git-subdir` entry
    to your org manifest's `requiredPlugins` (`docs/registry.md` § Install
    paths per surface).
- **Copier: telemetry keys follow `langfuse_telemetry`.**
  `CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_*` and `otelHeadersHelper` are written
  to `.claude/settings.json` only when `langfuse_telemetry` is on.
  - Earlier releases always wrote them, pointing at Langfuse cloud.
    Instances that ran telemetry without the flag lose those keys on
    `copier update`; to keep them, run
    `copier update --data langfuse_telemetry=true`.
  - Instance answer files were not available to enumerate affected instances.
- **Copier: the Codex review workflows follow the new `codex_review`
  question** (default `true` for `isolation_profile=standard`, so standard
  instances keep them).

### New opt-in inputs and outputs

| Workflow | Name | Kind | Default |
|---|---|---|---|
| `coverage-ratchet-reusable` | `require_baseline` | input | `false` |
| `coverage-ratchet-reusable` | `fail_on_inert` | input | `false` |
| `coverage-ratchet-reusable` | `activation` (`active`/`inert`) | output | — |
| `e2e-playwright-reusable` | `fail_on_inert` | input | `false` |
| `e2e-playwright-reusable` | `activation` (`active`/`inert`/`unknown`) | output | — |
| `pr-autofix-loop-reusable` | `activation` (`active`/`skipped`) | output | — |
| `outcome-check-reusable` | `outcome: INERT` | accepted value | — |
| `release-tag.yml` | `notes` | dispatch input | `''` |
| Copier | `isolation_profile` (`standard`/`strict`) | question | `standard` |
| Copier | `codex_review` | question | `true` (standard) |
| `pr-autofix-loop` (standalone) | `AUTOFIX_MODEL` | repository variable | unset |

- `isolation_profile=strict` renders a hooks-only `.claude/settings.json`
  with no telemetry keys and no `bypassPermissions` /
  `skipDangerousModePermissionPrompt`.
  - It excludes the Codex review workflows, `setup-env-secret`,
    `dependabot-manual-automerge`, `monitor-litellm` and the LiteLLM
    deploy/publish files.
  - It refuses `oss_routing`, `kl_integration`, `langfuse_telemetry`,
    `mesh_telemetry` and an explicit `codex_review=true`.
    `scripts/first-run-setup.py` does not offer them.
  - The rendered `pr-assessment.yml` still defaults its LLM jobs to
    self-hosted `fly` runners (unchanged in v1). Instances without that
    fleet must set the repository variable `CI_RUNNER_OVERRIDE` to
    `["ubuntu-latest"]`, or those jobs queue indefinitely.
- Standalone Autofix through a proxy (`LITELLM_PROXY_URL`) needs
  `AUTOFIX_MODEL` set to a model id the proxy routes. Without it the agent
  requests the CLI default model and the job fails.
- Activation contract: `docs/workflow-outcome-convention.md` § Activation
  contract.
- **Planned for v2:** `require_baseline` and `fail_on_inert` default to
  `true`.

### Security

- **#169 hardening ported to `pr-autofix-loop-reusable`** and extended in both
  workflows.
  - The checkout is pinned to the validated head SHA, and the head ref passes
    `git check-ref-format`.
  - Before the agent runs, the instruction surface is restored from the PR
    base SHA, including nested copies on either side: `.claude/`,
    `.mcp.json`, `.claude.json`, `.gitmodules`, `.ripgreprc`, `.husky/`,
    `CLAUDE.md`, `CLAUDE.local.md` and `AGENTS.md`.
  - A trusted pre-commit hook outside the checkout refuses agent commits
    that touch those paths, including renames and quoted paths. It is wired
    via `GIT_CONFIG_*` env, so a PR's `core.hooksPath` cannot displace it.
  - A final step fails the job if an `[autofix]` commit touched them anyway.
    It fails closed if the commit list cannot be read.
  - CI-log reads are capped and treated as untrusted; force-pushes are
    forbidden.
- **Supply chain:** the remaining tag-pinned third-party actions are
  SHA-pinned to the commits their tags resolved to, so behaviour is
  unchanged:
  - `migration-run-gate-reusable`, `plugin-eval-gate`, `registry-lint` and
    the email-capture conformance workflows;
  - `actions/checkout` v4.4.0 and `actions/setup-python` v5.6.0.
  - New lint `tests/test_action_pins.py`: every remote `uses:` must be a
    40-hex SHA (Docker refs a `sha256` digest); only local `./` paths are
    exempt.

### Per-workflow input diff since v1.17.0

Every reusable's `workflow_call` inputs, outputs and secrets were diffed
against v1.17.0, v1.18.0, v1.19.0, v1.20.0, v1.21.0 and v1.22.0. **No input,
output or secret was removed or renamed, none changed type or default, and
none became required.** Added since v1.22.0:

- `coverage-ratchet-reusable`: inputs `require_baseline`, `fail_on_inert`;
  output `activation`.
- `e2e-playwright-reusable`: input `fail_on_inert`; output `activation`;
  secret `VERCEL_BYPASS_SECRET` (optional; lands as
  `VERCEL_AUTOMATION_BYPASS_SECRET`).
- `email-quality-reusable`: inputs `pack_ref`, `profile_path`,
  `sender_path`; secrets `UNSPAM_API_TOKEN`, `SENDER_TOKEN`.
- `pr-autofix-loop-reusable`: output `activation`.

The input matrix in `REUSABLE-WORKFLOWS.md` is now generated from these
schemas (`scripts/gen-reusable-matrix.py`; CI fails on drift). It gains an
Outputs column and renders every input's default, type and required flag in
full. That removes the phantom `paths_ignore` input on static-review and adds
the missing rows (backup-kernel, e2e-playwright, email-quality,
migration-run-gate, outcome-check).

### Tag namespaces

- Component releases now use prefixed tags: `email-capture-vX.Y.Z` and
  `feature-controls-vX.Y.Z`, plus `-alpha|beta|rc.N`.
  - `release-tag.yml` validates them, with canonical numbers only. It now
    checks the whole input string; a multi-line value used to pass the
    per-line check.
  - It never moves `v1` for them.
  - It takes an optional `notes` input for the annotated message.
- **`email-capture-v0.2.6`** is cut on the same commit as the bare `v0.2.6`
  (`a00b825`). Bare `v0.x` tags are kept.
  - Since the allocation-ownership fix, `await` and `release` must find the
    allocation in the registry `allocate` wrote. `assert` does not consult
    the registry, but every subcommand still needs a valid non-`off`
    capture profile.
  - Lanes that split those steps across jobs must share that registry or
    they get `AUTHORIZATION_DENIED` (`docs/email-capture.md` § Versions and
    allocation ownership).
- `packages/feature-controls` stays unreleased.
- Release tags are cut only by `release-tag.yml`; never hand-create one.

### Renovate preset

- New `packageRules` entry groups every `Manolii-org/ai-starter-pack`
  dependency into one `manolii-ai-starter-pack` PR, never automerged. That
  covers SHA pins with `# vX.Y.Z` comments, the preset pin and
  `.starter-pack-source`.
- `allowedVersions` restricts lookups to plain `vX.Y.Z` release tags, so
  component tags, bare `v0.x` and the `v1` alias are never proposed.
- Consumers pick the rule up when they bump their preset pin.

### Docs

- `docs/email-capture-journeys.md` is now generic, with no consumer routes,
  endpoints, mail paths or repository names. Consumer-specific review notes
  under `.claude/forge/reviews/` are redacted.
- `docs/ai-runtime-policy.md` and `docs/model-plane-boundary.md` list the
  orchestrator-only and runtime-generated paths they reference.
- `docs/autofix-cost-alarms.md` covers two things:
  - proxy mode with a non-LiteLLM Anthropic-compatible endpoint (for
    example an OpenRouter base URL; `LITELLM_MASTER_KEY` is just the proxy
    key slot);
  - provider-side spend alarms for tenants without LiteLLM/Langfuse.
- Coverage ratchet:
  - auto-commit needs a write-capable `GH_PAT`;
  - the required check is `<caller job name, or its id when unnamed> /
    coverage`.

### Fixed

- Rendered instances no longer ship the pack-only
  `tests/test_devin_meta_plugin.py`, which aborted their pytest run at
  collection. A render-collection test guards the class.

### Recorded v1 removals

- `tia-shadow` and `mutation-testing-diff` were removed inside v1 in 1.22.0
  (#157, `857e672`). Under the v1 contract (rename/remove = v2) this should have waited
  for v2. There were no callers at removal, and they are not restored.

## 1.22.0 — 2026-10-07

> Tag `v1.22.0` (`5a4538e`) is lightweight and was created outside
> `release-tag.yml`, so the floating `v1` alias was not moved (it stayed on
> v1.21.0 until v1.23.0). Release tags are now cut only through
> `release-tag.yml`.

### Fixed

- **`secret-scan-reusable` audits `uv.lock` projects** instead of the job's
  own environment. Repositories with only `pyproject.toml` + `uv.lock` used to
  fall through to a bare `pip-audit` that could never see lockfile CVEs. The
  audit now runs with `--locked`, plus `--all-packages` for uv workspaces
  (#158).
- `pr-autofix-loop` prefers an elevated PAT for checkout and the action token,
  so autofix pushes re-trigger PR checks when a PAT is provisioned (#149).

### Removed

- **tia-shadow-reusable** and **mutation-testing-diff-reusable**, plus the
  diff caller `mutation-testing-diff.yml`, were retired estate-wide on
  2026-10-05 (#157). A CI-cost audit found observe-only lanes burning runner
  minutes with zero caught defects. Consumers that still reference either
  workflow must drop those `uses:` blocks. No v1 callers remained at removal
  time. Under the v1 contract this should have waited for v2; see the v1.23.0
  note.

## 1.21.0 — 2026-10-05

### Added

- **`secret-scan-reusable` gains opt-in `npm_audit_omit_dev` input** (default
  `false`, #156). When `true`, the npm audit job scopes to production
  dependencies (`--omit=dev` on npm >= 7, `--only=prod` on npm 6).
  - Rationale: dev-only advisories in build/lint tooling chains frequently
    have no patched release (the affected range covers every published
    version). That makes a required audit check permanently red and
    unmergeable for every PR.
  - The default keeps the v1 whole-tree audit contract; callers opt in
    per-repo.
- Copier `cost_profile=lean` drops measured-waste CI callers
  (`monitor-litellm.yml`) (#147).
- `exploratory-bug-bash` weekly-lane operationalisation (calibration, ledger,
  stop rule) (#146).

### Changed

- `pr-autofix-loop` standalone: slim preflight job; only dispatched runs use
  `ubuntu-latest` (#148).

### Fixed

- `pr-autofix-loop`: `--repo` on budget-comment and label lookups; head ref
  resolved with `--repo` and fails closed when empty (#154, #155).
- `pr-classifier` manifest parsing hardening (#152, #153); `run-judge`
  candidate guards (#150).

## 1.20.0 — 2026-10-02

### Added

- **Jev judge shadow (opt-in, observe-only)**: `pr-assessment-reusable` gains
  the `jev_judge_shadow` (`'off'`/`'on'`) and `jev_entity` inputs plus an
  optional entity-scoped `JEV_TYPESAFE_API_KEY` (#141).
  - When the caller passes `'on'` and the repo variable
    `JEV_ENABLED_JUDGE_FINDING_SHADOW` is `'true'`, the Judge job re-asks its
    four gates of pinned `jev-1.13.0`.
  - It uploads payload-free `judge-jev-shadow` receipts.
  - It never changes the verdict, and it is fail-open.
  - It is refused under `CLIENT_AI_POLICY` and for owners on its denial list.
  - See `docs/jev-judge-shadow.md`.
- **Playwright failure classification (advisory)** (#143).
  - `failure_class.py` gains a separate `test_failure_class` vocabulary:
    `product`, `test-defect`, `environment`, `external-dependency`,
    `unclassified` (the last three shared with the session taxonomy). It also
    gains Playwright JSON-report and stdout parsers.
  - New composite action `classify-playwright-failures`.
  - `e2e-playwright-reusable` now runs it after every suite and exposes
    `test_failure_class` / `autofix_eligible` / `retryable` workflow outputs.
  - It never changes a job conclusion. See `REUSABLE-WORKFLOWS.md`.
- **`exploratory-bug-bash` skill (advisory)** (#144, #145).
  - Charter-driven AI exploratory testing via pinned `tester-army/e2e`
    through LiteLLM, with planted-bug calibration. A candidate counts only
    once a deterministic test reproduces it.
  - `fanout.sh` exits nonzero when any charter hits a harness error (exit
    `>1`; `run.py` exits `2` on missing env). Exit `1` (candidate) stays
    advisory.
  - `BB_ACCOUNTS` must map every charter session.
- **Merge danger on PR assessment** (#131).
  - pr-classifier now emits optional `door` (`one-way`/`two-way`),
    `blast_radius` (`small`/`medium`/`large`) and `danger_reason` fields.
  - The judge renders them as a `**Merge danger:**` line in the posted
    review. `unknown` means unclassified and is never rendered as a safe
    verdict.
  - The PR template gains a matching author-filled `## Merge danger` section,
    so disagreement with the classifier is visible.
- **pr-standards-checker agent** validates a PR diff against the repo-owned
  `.ai/pr-standards.yaml` manifest (seeded as a starter file; customize per
  repo). Its `testing` section flags tautological tests: assertions that
  re-derive the expected value from the code under test.
- **scope-adherence under-delivery check.** The skill is now bidirectional:
  besides scope creep, it flags concrete promises in the PR title/description
  that the diff does not deliver (the spec axis).
- CI cost policy, relevance-gate component and cost sampler (#138, #139,
  #140).

### Changed

- Latest-generation sync: proxy-aware `run-*.py`, tier migration, ported
  surfaces (#134). This includes the email-capture allocation-ownership fix,
  later tagged for the email-capture lane as bare `v0.2.4`+ and now
  `email-capture-v0.2.6`.

### Security

- **Jev judge shadow hardening** (#142).
  - The secret-bearing runner checkout now accepts only a release-tag
    `pack_ref` (`vN`, `vN.N`, `vN.N.N`) and checks out
    `refs/tags/<pack_ref>`, so a caller can't point it at
    contributor-controlled code. Any other ref skips the shadow.
  - The runner refuses HTTP redirects, so the TypeSafe `Authorization` header
    is never forwarded.

### Fixed

- Review follow-ups: restore-drill whitespace, proxy key guard (#135);
  bot-review churn throttling (#136); superseded/quota guards (#137);
  `ai-resolve` parser rounds (#128, #130, #132, #133).

## 1.19.0 — 2026-09-25

### Added

- **coverage-ratchet-reusable**: shared coverage gate (#126).
  - Runs a suite, extracts a float metric, and compares it against an
    in-repo baseline file.
  - Absent baseline = measure-and-report (exits 0; see the 1.23.0 activation
    change). Below baseline = fail (enforce) or warn (report).
  - Optional `setup_command` for non-node/python toolchains,
    `cache_path`/`cache_key`, and `auto_commit_baseline` for default-branch
    pushes.
  - The job is intentionally unnamed, so required-check contracts bind to
    the `coverage` job id.
- **restore-drill-reusable**: scheduled Azure SQL restore drill (#126).
  `az sql db restore` (PITR) restores into a throwaway database, runs
  SELECT-only sanity queries, writes JSONL + report artifacts, then deletes
  the drill DB even on failure.
- **check-branch-protection.py** audits `deployment-contracts.yaml`
  `required_checks` (repo-level and per-lane overrides) against live
  `branches/{b}/protection` required status checks via `gh api`. It is
  warn-only without a PAT. `--emit-fixes` writes ready-to-run `gh api -X PUT`
  payloads; it never applies changes itself.
- **Deployment-contract schema fields**: repo-level `protected_branches` and
  `required_checks`, plus lane-level `required_checks` overrides, now
  validate in `check-deployment-contract.py` and
  `schemas/deployment-contract.schema.json`.
- TIA shadow lane (#129; retired in 1.22.0).
- Private-mirror trust for a seeded universe mirror (#122).

### Fixed

- `ai-resolve` robustness rounds (#123, #125, #127).

## 1.18.0 — 2026-09-24

### Added

- `e2e-playwright-reusable`: PR-time Playwright with screenshot artifacts
  (#124).
- Registry trust for three seeded private mirrors (#120).

### Changed

- Light CI legs moved to `ubuntu-slim` (#119).

### Fixed

- Mirror bootstrap validates effective push targets and bounds `gh api`
  (#118).

## 1.17.0 — 2026-09-24

### Added

- `migration-run-gate-reusable`, the `check-guarded-paths` CI action,
  registry and contract schemas (#115).
- Capability registry scaffold: scopes, resolver, lint gate (P0) (#112);
  private-mirror bootstrap and mirror-mode PACK-SURFACE (#116).

### Fixed

- `pr-autofix-loop` skips stale `workflow_run` events before dispatch (#113).
- `ai-resolve` nested script dependency detection (#117).

### Docs

- `ubuntu-slim` opt-in for reusable-workflow callers (#114).

## 1.16.0 — 2026-09-20

### Added

- Closed-loop issue lifecycle helper (#111).
- G-P4 feature-train language (#109).

### Security

- OpenRouter zero-retention enforced on every OpenRouter hop (#110).

## 1.15.1 — 2026-09-14

### Fixed

- **Detect Project treats synced empty outcome scaffolds as scaffold.** Pack
  `ci-reusable` previously required `config/workflow-outcomes.json` to equal
  exactly `{"version":1,"workflows":{}}`. Consumer sync adds `_sync_metadata`,
  which made Detect Project fail with `Invalid workflow-outcome contract shape`
  on pin drafts. Optional `_sync_metadata` is now ignored for scaffold
  classification. Adopted (non-empty `workflows`) and other extra keys are
  unchanged. Does not weaken adopted-contract lint.

## 1.15.0 — 2026-09-13

> Reconstructed in the v1.23.0 release from the commits tagged
> `v1.15.0`; earlier releases (1.12.x, 1.13.x) still have no
> CHANGELOG section.

### Changed

- **`/watch-pr` subscribe split (v2.1.0).** Docs/CI auto-merge PRs (paths ⊆
  `reports/**`, `docs/**`, `reports/INDEX.md`,
  `.github/pull_request_template.md`, `.github/PULL_REQUEST_TEMPLATE.md`)
  must not call `subscribe_github_pr` / `subscribe_pr_activity`. Optional CI
  subscribe; ≥20-minute heartbeat only if CI subscribe is on. Product-red
  keeps PR+CI subscribe and the ≥20-minute heartbeat. No 60s poll loop; do
  not invent `mcp__github__subscribe_ci`. Template:
  `.claude/commands/watch-pr.md.jinja`. Plugin command rebuilt from it.

## 1.14.0 — 2026-09-12

`release-tag.yml` only creates new refs. `git matching-refs tags/v1` already
has `v1.10.0` through `v1.13.1` (latest `v1.13.1` = `8c07778`, 2026-09-07).
#104/#105 assumed 1.11.0 then 1.12.0 were free. Next unused tag is `v1.14.0`.

### Added

- **Claude instruction-load observability.** Portable `InstructionsLoaded`
  handler and verifier with local, ignored, content-free receipts, sidecar
  locking, and consumer-configurable canary paths. Render the hook only when
  `claude_hooks=true`. Codex and Cursor do not emit this event.

- **Provenance-attested LiteLLM product releases.** Publish deterministic, SHA-256
  checked public product bundles from immutable canonical source revisions. Consumers
  can verify both checksum and GitHub build provenance without a Manolii production or
  private-repository credential, then create an independently owned runtime profile.

- **Integration Admission foundation.** Add a deterministic reusable merge-candidate
  planner that maps changes through declared surface dependencies, distinguishes exact-tree
  from input-closure evidence reuse, groups required execution by runtime lane, reads command
  authority from the trusted base ref, and publishes one stable aggregate. The reusable is
  shadow-first and introduces no deployment trigger or secret requirement.
- **Portable migration-tree validation.** Add a provider-neutral composite
  action for Drizzle journals, Alembic revision graphs, Supabase legacy/native
  identifiers, Prisma migration directories, and Flyway versions. Reviewed
  baselines may preserve exact immutable historical Supabase collision groups
  while any new member still fails before database startup.
- **Deployment receipt control-flow composites.** Add tagged step-level actions
  that fail closed on empty/non-default-branch held promote SHAs and emit a
  schema-valid final deployment receipt to both a 30-day Actions artifact and
  the durable `production-receipt` Release asset. Product-owned provider,
  migration, Auth, and smoke commands stay local to each consumer adapter.

### Fixed

- **Guard commands now enforce what they promise.** Added the portable
  `scripts/guard_check.py` engine to the PreToolUse chain, audit every edit
  allowed through a session unfreeze, and clear `session_unfreezes` at the next
  SessionStart (Stop runs after every response, so clearing there made the
  bypass unusable across turns).
- **Feature flags now control optional files.** Copier `_exclude` conditionals
  omit disabled OSS, Operational Memory, Browserbase, Codex, and mesh surfaces;
  render-contract tests cover default and all-enabled inventories.
- **README inventory now describes rendered consumers.** Counts vary with the
  selected feature flags, the repository stub points to the real template
  source, and the two unwired legacy hook examples were removed.
- **Bare root pytest now works for maintainers.** Importlib collection avoids
  duplicate heartbeat-module basenames without hiding generated plugin tests;
  the maintainer-only `pytest.ini` is excluded from consumer renders.
- **Shellcheck accepts shell-file deletions.** Standalone and reusable static
  review now exclude deleted paths before invoking Shellcheck instead of
  failing because the deleted file is absent from the checkout.
- **Reusable secret scan validates on push.** The optional Gitleaks license is
  promoted to job `env` before step conditions; GitHub forbids direct `secrets`
  context access in step-level `if`, which previously created a jobless failed
  run for every pushed commit.

### Upgrade note

- The full `deploy/litellm-proxy/config.yaml` remains available for existing consumers
  but is no longer a canonical product source. New deployments should consume the
  versioned product bundle and profile scaffold. Existing deployments must qualify
  aliases, callbacks, controls and rollback before migrating; there is no flag-day
  replacement.

- Feature flags now govern file presence, not only recorded configuration.
  Before `copier update`, enable any flag whose optional files a consumer uses;
  disabled Browserbase, Operational Memory, OSS-eligibility, Codex-adversarial,
  and mesh files are removed by the update contract.
- Consumers that manually wired `.claude/hooks/pre-tool.py` or
  `.claude/hooks/stop.sh` must move that custom behavior to a project-owned hook
  before updating. Neither file was ever wired by the shipped settings, and
  both have been removed to keep the pack's hook inventory executable.
- Existing consumers receive guard-bypass cleanup through the updateable
  `.claude/settings.json` and `scripts/guard_check.py`; the instance-owned
  `.claude/hooks/session-start.sh` remains untouched by `copier update`.

- **The README's component counts had drifted, and the guard for it was blind.**
  `check_readme_counts` read `README-STARTER-PACK.md` first and only fell back to
  the `.jinja`. Both exist in the template repo — the `.md` is a 135-byte licence
  stub, the `.jinja` is the real 24KB document — so the check always read the stub,
  found no count row, and returned WARN. Six of the eight counts drifted behind
  that permanent warning (commands 47→48, skills 23→28, agents 28→27, scripts
  25→33, docs 8→15, workflows 5→28). Prefer the `.jinja` when present, extend the
  check from agents-only to agents/commands/skills/hooks, and correct the counts.
  Consumers were never affected: a render produces the full document from the
  `.jinja`; only the maintainer-side check read the stub.
- **README documented three hooks that do not run.** The Hook Lifecycle section
  named `pre-tool.py` as the PreToolUse hook (it is `scripts/pre-tool-use.py`, a
  different file), listed `advisor-metrics-hook.py` as an active PostToolUse hook,
  and described `stop.sh` as part of the Stop chain. All three contradicted the
  README's own Scripts table further down. `.claude/hooks/pre-tool.py` and
  `.claude/hooks/stop.sh` now carry a NOT WIRED header so an editor learns they
  are inert before changing them, and `advisor-metrics-hook.py`'s docstring no
  longer claims a settings.json entry it never had.
- **Quick Start step 1 pointed at a retired path.** It told consumers to
  `cp -r /path/to/master/templates/ai-starter-pack/`, the pre-standalone monorepo
  location. Replaced with the `copier copy` / `copier update` flow that `README.md`
  already documents as canonical.

- **Stop hook logged `exit_code: 0` for every session.** In `.claude/settings.json`
  the self-check's `$?` sat in the same word as `$(date -u …)`; the command
  substitution runs during expansion and clobbers the status, so a failing
  `system-self-check.py` / `build-skill-graph.py` was recorded as a success.
  Captured into `_rc` before the `echo`, and `mkdir -p .ai/memory` added so the
  append cannot fail on a fresh checkout. Both already existed in the generated
  plugin `hooks.json`; only the canonical template lagged, which is why
  `build-plugin.py`'s "mirrors settings.json verbatim" comment had gone stale.
- **Stop hook checklist never ran.** `settings.json` invoked
  `scripts/session-stop-checklist.sh` directly while the file was mode 0644 —
  exit 126, `Permission denied`, on every session end. Invoked via `bash` (the
  form the plugin already used) and the executable bit restored.
  `scripts/otel-langfuse-headers.sh` (the `otelHeadersHelper`) got the same
  treatment.
- **`email-capture` misreported a receiver fault as a config error.** The mailpit
  branch of `HttpBackend.list` assigned `detail["headers"]` before its
  `isinstance(detail, dict)` check, so an empty or non-object detail body raised a
  bare `TypeError`. The CLI maps `TypeError` to `CONFIG_INVALID`, turning a
  retryable infra failure into a permanent one. Shape-checked before the
  mutation; regression test in `tests/email_capture/test_http_adapters.py`.
- **`ci.yml` restored stale `node_modules`.** The cache key was labelled `node20`
  while `setup-node` installed 24, and hashed only `package-lock.json` although
  the `detect` job also accepts pnpm/yarn/bun lockfiles — a non-npm consumer got
  a constant key and never invalidated the cache. Now mirrors `ci-reusable.yml`.
- **Least privilege on the last two unscoped workflows.** `ci.yml` and
  `plugin-eval-gate.yml` were the only workflows without a `permissions:` block;
  both now declare `contents: read`.
- **Telemetry wire tests now actually gate.** `telemetry/README.md` names
  `python3 telemetry/tests/test_heartbeat.py` as required, but no workflow ran
  it — the same "documented for manual execution only" gap closed for the three
  hook guards in #71. Added to `plugin-eval-gate.yml`.
- **Broken in-repo references.** Four reusable workflows cited
  `.github/REUSABLE-WORKFLOWS.md` (the file is under `.github/workflows/`);
  `/compact-review` pointed at a non-existent `.claude/hooks/compact-trigger.py`
  for constants that live in `.claude/hooks/post-tool.py`; `.githooks/pre-commit`
  offered a `scripts/install-hooks.sh` that does not exist; `pack.manifest.yml`
  and `.gitignore` still named the removed `scripts/render-pack.py` instead of
  copier.

- **Finish a complete hosted empty list at the await deadline.** `_read_bounded_http_body` no longer raises `MESSAGE_TIMEOUT` merely because `remaining <= 0` after a finished JSON body. Truncated/dripped reads still time out. Bounded-negative `count=0` against an empty hosted mailbox returns success.
- **Do not treat incomplete mailbox reads as a successful negative.** `await_messages(count=0)` still completes an empty window after the last 0.01s poll. A `MESSAGE_TIMEOUT` from `list()` (hosted body read cut off mid-response) stays a timeout, not a false empty mailbox.
- **Fix bounded-negative await on HTTP backends.** v0.2.0 raised `MESSAGE_TIMEOUT` from `_remaining_request_timeout` when the window elapsed, so Mailpit `count=0` waits failed even when the mailbox stayed empty. The last poll uses a 0.01s timeout; negatives return success. Positive waits still time out.
- **Document consumer email-capture journeys.** `docs/email-capture-journeys.md` records evidenced senders versus `NO_EVIDENCED_SENDER` (no mailbox tests for `generateLink` / in-app URL flows). Hosted SPI exists in v0.2.0; hermetic Mailpit jobs remain the Buro go-live lane.
- **Ship email-capture v0.2.0.** Add hosted capture SPI (`mode=hosted` / `backend=hosted`), HTTPS message API (loopback HTTP only in `environment=test`), env-only `EMAIL_CAPTURE_HOSTED_TOKEN`, `canary` CLI, and optional fidelity. Hermetic Mailpit/MailDev/Inbucket CI is unchanged and remains fail-closed in staging/production. Hosted summaries with null `to` fail closed; shared multi-recipient messages are not deleted; profile schema encodes the hosted env allowlist and HTTPS/loopback endpoint pattern. Canonicalize `--not-before` to the same UTC microsecond form as `received_at`. Reject undotted HTTPS hosts (`https://capture`, `https://localhost`) at runtime to match the profile schema. Fail closed on malformed hosted attachment containers, unparseable Cc/Bcc list elements before exclusive delete, and v0.1.1 second-precision persisted cursors. Clear await deadlines only on backends that install them.
- **PR Assessment skips drafts.** Standalone and reusable skip
  `pull_request.draft` and listen for `ready_for_review` and
  `converted_to_draft`. Downstream jobs require
  `needs.classify.result == 'success'` so Semgrep/Bandit cannot start
  when classify is skipped (empty `depth` is not the string `'none'`).
  Standalone `concurrency` + `cancel-in-progress` cancels in-flight
  LLM/SAST when a ready PR is converted back to draft. Do not put
  `paths` / `paths-ignore` on that workflow: GitHub filters
  `converted_to_draft` the same way, which would leave in-flight LLM/SAST
  running. Reusable `classify` requires `github.event_name == 'pull_request'`
  so `workflow_dispatch` never reaches `git diff` against `github.base_ref`
  or a judge comment on an empty PR number.
- **CI cost conventions.** `docs/ci-cost-conventions.md` — wait-loop extraction,
  no unfiltered browser/`supabase start` PR workflows, job-level concurrency
  for shared locks, no `on.paths` on internally detect-gated required e2e.
  Autofix is **not** draft-gated. Path-filter examples: `mutation-testing-diff.yml`
  (`on.pull_request.paths`) and `static-review.yml` `changed-files`. Do not cite
  `ci.yml` `detect` (existence check only).

## 1.11.0 — 2026-08-31

Annotated tag `v1.11.0` points at `2845356813c85612c7563c1bee65beab87cf209b`
(#87 workflow outcome semantics). Manifest lagged at `1.10.0`. Do not retag.

## 1.10.0

Annotated tag `v1.10.0` points at `ce6cc57bd03aa1c590dab426e4063a4b4f5d1b5f`
(held-promotion receipt composites, #86). Do not retag.

## 1.9.7 — 2026-08-21

- **Ship email-capture v0.1.1.** Include the portable hermetic inbox CLI, eight
  Draft 2020-12 contracts, private allocation/message files, receiver SPI,
  metadata-only receipts, and live Mailpit/MailDev/Inbucket conformance.
- **Close v0.1.0 correctness gaps.** Persist cursors, wait through bounded
  negatives, reject unstable timestamps, retire released allocations, enforce
  content/attachment limits, and use receiver-specific detail/delete APIs.


## 1.9.6 — 2026-08-12

- **Repair the standalone Autofix workflow YAML.** Apply the same budget-comment
  correction shipped for the reusable workflow in v1.9.5; validate every
  shipped Autofix workflow in the regression test.
- **Synchronize release bookkeeping.** Update the manifest and consumer examples
  that remained on v1.9.4 when v1.9.5 was tagged.

## 1.9.5 — 2026-08-11

- **Repair reusable Autofix workflow YAML.** Keep the budget-exhausted comment
  inside its shell block so GitHub can schedule the reusable workflow instead of
  producing a zero-job workflow-file failure.
- **Use the workflow token fallback.** Pass `github.token` to Claude Code Action
  when callers do not provide the optional elevated `GH_PAT` secret.

## 1.9.4 — 2026-08-05

- **Hotfix: Autofix workflow YAML headers.** v1.9.3 accidentally inserted an
  uncommented `Per-PR cap` bullet into the file header of both
  `pr-autofix-loop.yml` and `pr-autofix-loop-reusable.yml`, which is invalid
  YAML. Comment the bullet; no behavioural change.

## 1.9.3 — 2026-08-05

- **SHA-pin `anthropics/claude-code-action`.** Standalone and reusable Autofix
  pin immutable `9db594c7a0e82298c121c18b7f08aa1579ce7341` (`# v1.0.185`) —
  no more floating `@beta`.
- **Per-PR successful-fix budget.** Reusable input `max_successful_fixes`
  (default `1`) skips further Autofix once that many `[autofix]` commits exist
  on the PR; standalone hard-defaults to 1. Posts a budget-exhausted PR comment.
- **Dual-ownership XOR lint.** `scripts/lint-autofix-xor.py` +
  `tests/test_autofix_xor_ownership.py` fail closed if pack Autofix relay-accept
  coexists with `auto-address-review.yml`, or if workflow YAML contains literal
  bracketed redaction placeholders.
- **Cost alarm path.** `docs/autofix-cost-alarms.md` documents Langfuse / proxy
  alarm wiring for proxy Autofix without hardcoding credentials.

## 1.9.2 — 2026-08-05

- **`bot-review-relay.yml` stays hosted.** Like `pr-autofix-loop-reusable.yml`,
  the bot-review relay control-plane must remain on GitHub-hosted runners
  (added note to `docs/fly-runner-setup.md`).
- **Autofix accepts relayed github-actions[bot] comments.** The Autofix loop
  in the pack now processes comments **starting with `**[@`** posted by
  `github-actions[bot]` (relayed by `bot-review-relay.yml` from a separate
  master copy). Pack owns relay transport; do **not** install
  `manolii-org/master` `auto-address-review.yml` on the same repo alongside
  pack Autofix (creates duplicate processing).
- **Optional `workflow_run` CI failure path.** Both standalone
  `pr-autofix-loop.yml` and `pr-autofix-loop-reusable.yml` can now trigger on
  `workflow_run` to retry failed CI (documented in
  `.github/workflows/REUSABLE-WORKFLOWS.md`; example caller includes workflow_run trigger for Static Review, PR Assessment, Secret Scan, and CI).
- **[autofix] HEAD commit loop break.** Fixes the edge case where Autofix
  pushes a commit, then the commit hash appears in a new review and re-triggers
  Autofix on its own commit (infinite loop risk on fast feedback). Loop now
  checks `git log -1 --format=%B` for the commit it is about to push and skips
  identical fixes.
- **Docs and caller examples updated.** All reference pins to `pr-autofix-loop-reusable.yml`
  in `REUSABLE-WORKFLOWS.md` bumped to `@v1.9.2`; new `workflow_run`
  example block added showing CI triggers (Static Review, PR Assessment, Secret Scan).

## 1.9.1 — 2026-08-05

- **Autofix control-plane stays on GitHub-hosted runners.** `docs/fly-runner-setup.md`
  no longer tells consumers to move `pr-autofix-loop.yml` onto Fly. Autofix /
  Claude Code Action jobs must remain on `ubuntu-latest` so they do not compete
  with heavy CI on shared self-hosted pools (Manolii master #3601 / #3602). Fly
  remains documented for **heavy CI only**.
- **Documented `pr-autofix-loop-reusable.yml`.** `REUSABLE-WORKFLOWS.md` now
  covers the reusable Autofix caller, including `provider_mode=proxy` for
  OSS-only universes (Buro / LiteLLM) and the hosted `runs_on` default.
- Standalone `pr-autofix-loop.yml` header comments record the same hosted
  contract and point proxy consumers at the reusable form.

## 1.9.0 — 2026-08-01

- **`pr-assessment-reusable.yml` now hydrates its own runtime.** The four runner
  scripts and the agent/skill prompts they read were only ever shipped by the
  Copier template or master's capability sync, so calling the reusable from a
  repo without a `.claude/` tree failed. Each runner job now probes the
  workspace and pulls only the files the caller is missing, from this pack at
  the new `pack_ref` input (default: the `v1` alias). Caller-owned copies always
  win, and the pack is not checked out at all when nothing is missing — so an
  unreachable `pack_ref` cannot affect a repo that ships its own runtime.
  Adopting the pipeline is now a one-file change in the consumer.
  This removes the `pr-assessment-workflow` retirement blocker recorded in
  `manolii-org/master:scripts/sync-retirement-queue.json` ("Workflow-only on
  pack; agents/skills/scripts remain copy-synced").
- **`release-tag.yml` now moves the `v1` major alias.** `REUSABLE-WORKFLOWS.md`
  documented `v1` as a moving alias that Renovate tracks, but nothing ever moved
  it: `v1` pointed at v1.7.2 while releases ran ahead to v1.8.0, so consumers
  extending the alias silently received stale content.
- **PyYAML is no longer an undeclared dependency.** The runners import `yaml` to
  parse frontmatter and relied on GitHub-hosted images happening to ship it;
  the probe step now installs it when the import fails, so slim and self-hosted
  runners work.
- `tests/test_hydrate_assessment.py` derives the required runtime set from the
  runners themselves (`BROAD_AGENTS`, `_VALID_SKILLS`) and fails if the
  hydration list drifts from what the scripts actually read.

## 1.8.0 — 2026-07-31

- **`release-tag.yml` now cuts ANNOTATED tags.** It only ever created
  `refs/tags/X` pointing straight at the commit, which yields a *lightweight*
  tag — so `v1.7.4`, the first release cut through it, is a bare commit ref
  while `v1.7.0`–`v1.7.3` are proper tag objects. Both `README.md` and
  master's `CLAUDE.md` describe pack releases as annotated. The workflow now
  creates the tag object first and points the ref at it.
- **Version bookkeeping caught up.** `pack.manifest.yml` still read `1.7.2`
  after the `v1.7.3` and `v1.7.4` releases; it and the plugin built from it
  now read `1.8.0`.
- **Stale `@v1` pins in `REUSABLE-WORKFLOWS.md` replaced with `@v1.8.0`.**
  The floating `v1` alias points at `06a1d96` — a commit predating the tier
  reusables — so the tier caller examples resolved to workflows that do not
  exist there. No consumer actually uses `@v1` (all 15 live pins across the
  ecosystem are exact versions), so this was a documentation defect only, but
  it was on the copy-paste path for adopting the tiers.

- **CI tiering building blocks** — three reusable workflows implementing the
  fast-tier / pre-production-tier convention documented in
  [`manolii-org/master:docs/cicd-tiering.md`](https://github.com/manolii-org/master/blob/main/docs/cicd-tiering.md):
  - `fast-tier-reusable.yml` — the only tier permitted to block a merge.
    Takes a `gates` JSON array, runs them as a matrix, and exposes **one**
    aggregate check (`fast-tier`) for branch protection. Naming individual
    gates as required checks is what deadlocks a repo when a gate is renamed
    or removed; one aggregate makes that an ordinary code change.
  - `pre-production-tier-reusable.yml` — blocks *promotion*, not merge.
    Natural triggers are push-to-integration-branch / `schedule` /
    `deployment_status`. Optional GitHub Environment scoping and
    `open_issue_on_failure` (recommended for scheduled runs, where nobody is
    watching the run list).
  - `tier-gate-summary-reusable.yml` — single-gate primitive making a
    **skipped** gate impossible to mistake for a **passed** one.
- **CI tiering — the reusables are now executed and tested.** They shipped
  having never run anywhere; a consumer pinning a tag would have been the
  first to execute them.
  - `tier-reusables-selftest.yml` calls all three by local path ref on any PR
    touching them, and asserts the verdicts and pass/skip/fail breakdown they
    actually produce — so a skip folded into the pass count fails CI here
    rather than in a consumer's merge queue.
  - `tests/test_tier_reusables.py` covers the fail-**closed** direction, which
    the self-test structurally cannot: a job calling a reusable workflow may
    not use `continue-on-error`, so a deliberately-failing tier would turn the
    run red with no way to invert it. The suite extracts the shipped
    `validate` and `aggregate` scripts out of the workflow YAML and runs them
    against fail-open inputs (empty/malformed spec, non-boolean `applies`,
    slug collisions, a gate that never reported, a gate job that was cancelled).
    Extraction rather than restatement keeps the workflow the single source of
    truth.
  - **Documented a consumer-facing trap found by actually running them:**
    a caller of `pre-production-tier-reusable.yml` MUST grant `issues: write`,
    even when `open_issue_on_failure` is false. Reusable workflows may only
    retain or reduce the caller's token permissions, and job permissions
    resolve when the run graph is built — so omitting it kills the run as a
    zero-second `startup_failure` with no jobs, no check run, no annotation
    and a 404 from the logs endpoint. There is nothing to read that says why.
    Now stated in `REUSABLE-WORKFLOWS.md` and enforced by a test that fails if
    any tier reusable starts requesting a permission its caller does not grant.

  All three emit `pass` / `skip` / `fail`, and a `skip` is a green check whose
  summary states in words that nothing was verified. Selectivity must be
  computed *in-job* (`dorny/paths-filter`, `git diff --name-only`), never with
  a workflow-level `paths:` filter — a filtered-out run is never created, so
  its absence is indistinguishable from a pass, and a filtered-out *required*
  check stays pending forever.

  Fail-closed throughout. Both tiers run a `validate` job before any gate, and
  the aggregate refuses to report `pass` unless it succeeded. Rejected: an
  empty `gates` array (0 expected + 0 actual verdicts would pass vacuously); a
  gate missing `applies` or giving a non-boolean (falsy in the matrix
  expression, so it would record a green `skip` instead of running);
  `applies: true` with no command; gate names that collide after slugification
  (verdict artifacts would overwrite each other); and a gate that reported no
  verdict at all (cancelled or crashed jobs are failures, not passes).

  Two timeouts, and callers must set **both**. `budget_minutes` is **one
  deadline shared by setup and the gate command** — stamped before checkout,
  with each step wrapping its command in `timeout` against what remains, so a
  gate with a setup step cannot take twice the advertised ceiling.
  `job_timeout_minutes` (fast 15, pre-production 60) is the outer job ceiling
  and the backstop. They are separate inputs, **not derived** — GitHub Actions
  expressions have no arithmetic operators, so a computed `budget_minutes + N`
  fails while evaluating `timeout-minutes` and breaks every caller. The
  consequence runs the other way: raising a pre-production `budget_minutes`
  above 60 without also raising `job_timeout_minutes` gets the matrix leg
  killed at 60 minutes, making the extra budget unreachable.

  The workflow-level `verdict` output is **empty when a tier fails** — GitHub
  does not propagate `workflow_call` outputs from a failed job. Callers must
  branch on `needs.<job>.result` and treat an empty verdict as a failure.
  Keeping the job green so the string survived would make the tier fail *open*.

  Measured motivation (master, 41 head SHAs, 2026-07-30): PR feedback p90
  12.1 → 5.7 min (−53%) and max 15.2 → 7.5 min (−51%), while the median moves
  only 5.5 → 5.1 min. **Tiering is a tail-latency fix, not a median fix.**

- Backup kernel scaffold (`kernel/backup/`, Manolii Resilience Platform PR-H6 /
  WS-2 start): verbatim kernel scripts with provenance hashes, tenant-manifest
  draft schema + example + validator, and the validate-only
  `backup-kernel-validate-reusable.yml`. No consumers yet; master cutover is
  gated on the Phase-1 exit.

## 1.7.4 — 2026-07-21

- New composite action `.github/actions/doppler-preflight/action.yml` (PR #44).
  Consumers add a `uses: Manolii-org/ai-starter-pack/.github/actions/doppler-preflight@v1.7.4`
  step before any workflow reads Doppler secrets, and get a canonical
  "prove-the-token-can-actually-read-this-project/config" preflight — fails
  loud on non-zero exit AND on exit-0-empty-stdout (the CLI's soft-invalidation
  mode). Extracted from the fix that closed the 2026-07-13→18 mesh-daily-intel
  outage in `manolii-org/master` (master PR #2927). Token-agnostic: caller
  passes whatever token has the scope they need to probe (master-scoped or
  workspace). Inputs: `token`, `project` (default `master`), `config` (default
  `prd`), `probe-secret` (default `MESH_ENABLED`), `runbook-anchor`
  (default `Doppler token rotation`).

## 1.7.2 — 2026-07-18

- `ci-reusable`: empty `pnpm_version` (new default) omits `version` for
  `pnpm/action-setup`, deferring to `package.json#packageManager`. Fixes
  "Multiple versions of pnpm specified" when callers declare `pnpm@11.x`.
- Pass an explicit `pnpm_version` only to override `packageManager`.

## 1.7.1 — 2026-07-18

- Annotated pack tags: converted lightweight `v1.3.0`–`v1.6.0`; cut `v1.7.1` on main; floating `v1` → latest.
- Document go-forward consumer pin `@v1.7.1` + Renovate `github>Manolii-org/ai-starter-pack`.
- Clarifies dual-run with `manolii-org/master@v1` for routing-lint / shared-config.

## [1.7.0] - 2026-07-17

### Added

- **Session retrospective loop (pack parity)** — portable `scripts/session-retrospective.py`
  (KL-optional; local `.ai/memory/retrospectives/` fallback) with master's record schema
  including the `failure_class` taxonomy (`scripts/lib/failure_class.py`:
  `instruction-gap|tooling|environment|planning|memory-context|external-dependency|unclassified`).
- **Stop-hook wiring** — `session-stop-checklist.sh` (scripts/ + plugin copy) invokes the
  retrospective with synchronous-with-timeout (inner 8s / outer Stop timeout 10s); on
  failure writes `.ai/memory/retrospectives/.last-capture-failed` and still exits 0.
  Plugin `hooks/hooks.json` Stop entry timeout raised to 10s (also `build-plugin.py`).
- **Memory path migration** — `scripts/migrate-memory-path.sh` moves `.claude/memory/` →
  `.ai/memory/` with relative symlink + no-clobber conflict archive under
  `archive/migration-conflicts/`.
- **Memory scaffolding** — seeded `retrospectives/` (+ zero-row JSONL) so `/learn` /
  `/remember` never write into a void.

- **SessionStart inject + PreCompact staging** — hooks call `--mode inject` / `--mode precompact`.
- **`--kl-only` + mtime gate** — background KL flush without double local append; unchanged session logs skip re-capture (~0 common path).
- **`session-cost-logger.py`** — ported so the Stop checklist invocation is no longer a no-op.
- **Dual-copy sync test** — `scripts/tests/test_plugin_scripts_sync.sh` guards scripts/ ↔ plugin/ drift.

### Notes

- Create annotated tag `v1.7.0`
  on `main` after merge (`git tag -a v1.7.0 <merge-sha> -m "ai-starter-pack v1.7.0"` then
  `git push origin v1.7.0`). Do not tag from the feature branch.

## [1.6.0] - 2026-07-16

### Added

- **Per-repo CLAUDE.md interface contract** — `config/claude-md-contract.json`
  (heading anchors only — cheap by design; `Stack`, `Environments & Data`,
  `Secrets` as the default set) checked by the new stdlib-only
  `scripts/check-claude-md-contract.py`. A missing contract file is a SKIP
  (pass `--require` to enforce), so the check can roll out fleet-wide ahead
  of per-repo contract scaffolding. Renaming a load-bearing heading now
  requires updating the contract in the same commit.
- **`claude-md-contract-reusable.yml`** — reusable workflow running the
  contract check (`require_contract` input, default false), plus the pack's
  own caller `claude-md-contract.yml` (require_contract: true). Completes the
  multi-repo documentation strategy's deferred "per-repo contract checker
  distribution" item (`manolii-org/master`
  `reports/multi-repo-docs-strategy-2026-07-16.md` §6).
- **CLAUDE.md template: `## Environments & Data` + `## Secrets` scaffold sections** —
  every rendered repo now ships an environments matrix placeholder (env × git ref ×
  platform × database) and a Doppler names-only secrets section, so an agent landing
  in a consumer repo without ecosystem context can answer "which database does
  staging use?" from CLAUDE.md alone. Part of the multi-repo documentation strategy
  (`manolii-org/master` `reports/multi-repo-docs-strategy-2026-07-16.md`).

## [1.5.1] - 2026-07-16

Hotfix for the telemetry heartbeat helper shipped in 1.5.0: the synchronous
boot tick no longer runs the canary. `start()` is typically called from the
app's event-loop thread, and an async-bridging canary
(`run_coroutine_threadsafe(...).result()`) can never complete while that
thread is blocked — 1.5.0 would stall boot ~5s and page a false canary
failure on every deploy for such consumers. The boot check-in is now
canary-less (`_tick(run_canary=False)`); the daemon thread's immediate first
tick runs the canary off-thread, so pipeline breakage still pages at boot.

## [1.5.0] - 2026-07-16

Added shared telemetry helpers (`plugin/manolii-framework/telemetry/`): the
drift-sentinel P3 heartbeat + canary helpers (`heartbeat.py`, `heartbeat.ts`)
with their wire tests, distributed with the pack so consumer copies stop being
hand-`cp`'d (the 2026-07-16 reverse-drift class — master's canonical went a
generation stale behind its own mirrors). v2 semantics: operator-intent
`checkin_margin` conversion, first-check-in-only monitor provisioning,
canary `status=error` (two-signal), shutdown-race guards, fail-soft contract.
Consumer dest conventions and change rules: `telemetry/README.md`.
`self-code-review` P1 gains the config-forwarding rule (operator intent vs
provider semantics must both be documented at the definition site).

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
