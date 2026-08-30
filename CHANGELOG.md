# Changelog

## Unreleased

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
