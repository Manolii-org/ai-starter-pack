# Changelog

## Unreleased

### Added

- **Delivery OS Session B (portable contract).** `scripts/lint_pull_request_types.py` fails pack CI when explicit `pull_request.types` omit `ready_for_review`, when a PR workflow runs `pnpm dev` without Preview-Ready, or when a readonly job `needs` a lock-holding job. `/watch-pr` jinja + plugin command use the docs/CI vs product-red subscribe split (not always-subscribe / 60s poll). Bot-review-relay skips `cursor[bot]`, empty Judge, Codex About Codex without P0/P1, and `<!-- auto-merge:` before `runs-on`. `scripts/ci/review-thread-lifecycle.py` trusts `cursor[bot]` fix-evidence with exact URL + head SHA.

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

- **CI cost conventions.** `docs/ci-cost-conventions.md` — wait-loop extraction,
  no unfiltered browser/`supabase start` PR workflows, job-level concurrency
  for shared locks, no `on.paths` on internally detect-gated required e2e.
  Autofix is **not** draft-gated. Path-filter examples: `mutation-testing-diff.yml`
  (`on.pull_request.paths`) and `static-review.yml` `changed-files`. Do not cite
  `ci.yml` `detect` (existence check only).
