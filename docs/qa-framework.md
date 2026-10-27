# QA framework

The shared QA lanes every app inherits from this pack. Each check runs at the
cheapest point that can catch its defect class, and the expensive model-driven
lanes run once per release, not per push.

| Lane | Trigger | What runs | Typical cost | Blocks? |
|---|---|---|---|---|
| **L1 Finalise gate** | the agent runs `git push` / `gh pr create` / the PR tool | `scripts/agent-check.py`: the repository's affected recipes, including `ui-smoke` when UI paths changed | ~1s when unchanged (cached); recipe time otherwise | Yes, on a real check failure |
| **L2 PR / preview** | each push; `deployment_status` for previews | CI recipes; `ui-smoke-reusable` against the preview; e2e with `fail_on_inert: true` | 2–3 min smoke, no app build | Caller's required checks |
| **L3 Ready for review** | once per user-facing PR (optional) | 1–2 bug-bash personas chosen by change type | ~1 charter | Never (advisory) |
| **L4 Launch** | launch, feature landing (`Shippable: yes`), onboarding redesign | `ui-smoke --profile launch`; five-persona Launch QA | the expensive lane | Gates the launch, not a merge |
| **L5 Production** | schedule (and after deploy) | `ui-smoke-reusable` against production | 2–3 min per run | Alerts |

Every escaped defect gets a regression test in the lane that should have
caught it (`docs/pre-pr-quality-gate.md` § 3).

## L1 — Finalise gate

`scripts/qa/finalise-gate.py` runs where an agent hands work off, never on
every turn: a Stop hook fires at the end of each turn, including mid-task
questions, so blocking there would interrupt the user and spend a model turn
per stop.

| Host | Wiring (rendered by Copier; mirrored in the plugin) | Block signal |
|---|---|---|
| Claude Code | `.claude/settings.json` PreToolUse, matcher `Bash\|mcp__github__create_pull_request` | exit 2 + reason |
| Codex | `.codex/hooks.json` PreToolUse, matcher `^Bash$` (trust the hook once with `/hooks`) | exit 2 + reason |
| Cursor (incl. cloud agents) | `.cursor/hooks.json` `beforeShellExecution`, matcher on `git push` / `gh pr create` | `{"permission":"deny","agent_message":…}` |
| git (Devin, humans, any host) | `.githooks/pre-push`, `.husky/pre-push`, or `python3 scripts/qa/finalise-gate.py --install-git-hook` | non-zero exit |

Behaviour:

- **No-op** unless `config/agent-checks.json` exists (the repository adopted
  agent validation), and for anything that is not a hand-off (`git push
  --dry-run`, ref deletions, every other command).
- **Pass** → silent; the host's normal permission flow continues. Cursor
  receives `allow` because its permission hooks need an answer; the matcher
  limits that to push/PR commands.
- **Check failure** → the failing check ids and the last 30 log lines go to
  the agent as model-visible context (`additionalContext` / `agent_message`).
  In `mode: warn` (the shipped default) the hand-off proceeds; in
  `mode: block` it is denied. The full log stays in
  `.ai/agent-checks/finalise-gate.log`, so the agent pays tokens for the
  summary only.
- **Incomplete** → allowed with the same context, in either mode. This covers
  a missing integration ref, a missing toolchain, a failed environment setup
  such as `npm ci` on a network error, a per-check timeout, a recipe that
  exits `3` ("could not run here", which is what `ui-smoke` uses when there is
  no browser or server), and a blocked runner. That is an environment fault
  for its owner to fix once, not a source defect to retry around; CI stays
  the authority.
- **Trust boundary**: the gate only runs recipes for a repository inside the
  opened workspace (`CLAUDE_PROJECT_DIR`, Cursor `workspace_roots`). Hook
  commands are anchored to the project, and a missing gate script exits 0.
- **Bypass**: `AGENT_FINALISE_GATE=off` (environment or command prefix) is
  allowed and appended to `.ai/bypass-log.jsonl`, which the pre-PR gate step 0 reads.
- **Cost**: one row per run in `.ai/agent-checks/finalise-gate.jsonl`
  (provider, outcome, duration, cache hit).

Configuration lives in `config/qa.json` (owner-owned, never overwritten by
`copier update`):

```json
"finalise_gate": { "mode": "warn", "timeout_seconds": 1100, "replace_legacy_pre_push": false }
```

`warn` ships as the default, and it is also the mode when `config/qa.json`
is absent, so neither `copier update` nor a plugin upgrade starts blocking
an existing repository's pushes. Set `mode: block` once your recipes pass
on `main`. This is a one-line opt-in, reviewed in your own PR, and is
planned to become the default in v2, like `fail_on_inert`. `replace_legacy_pre_push: true` makes `.husky/pre-push`
skip its full-repo typecheck/lint/test phases once your recipes cover them.

## Recipes

The gate is only as good as `config/agent-checks.json`. Copy what your CI
runs from `config/agent-checks.app-recipes.example.json`: a frozen `npm ci`
environment, then lint, typecheck and unit recipes on changed paths only,
and `ui-smoke`. Keep recipe commands identical to CI; use `ci_sources` to
fail fast when CI changes.

## ui-smoke

`scripts/qa/ui-smoke.mjs` loads routes in Chromium at 375px and 1280px and
fails on:

- a document status of 400 or above, or a navigation failure;
- an uncaught runtime error or a `console.error`;
- a same-origin 5xx, or a broken script, stylesheet or font;
- a missing `width=device-width` viewport, or horizontal overflow on mobile;
- a `critical` or `serious` axe violation (WCAG 2.1 A/AA), checked once per route;
- with `--profile launch`, a slow-3G + 4× CPU mobile pass over its DOMContentLoaded budget.

It writes a screenshot and an ARIA snapshot (`*.aria.yml`) per route into
`.ai/agent-checks/ui-smoke/`. Reading the ARIA text costs an agent far fewer
tokens than a screenshot when it checks structure.

**Route selection (agent lane).** The recipe passes changed paths. Next.js
`app/` and `pages/` files map to their route automatically. Dynamic routes
use `dynamic_samples`, e.g. `{"/blog/[slug]": "/blog/hello"}`. Other files
use `routes[].sources` globs, then fall back to `always_routes`. The list is
capped at `max_routes`, 6 by default.

**Server.** It uses `base_url` when that URL responds. Otherwise it runs
`start_command`, waits up to `ready_timeout_seconds` and stops the process
group afterwards. If neither works, the result is `UI-SMOKE-UNAVAILABLE`, a
failure while `on_unavailable` is `fail`. That failure exits `3`: CI fails on
it, and the finalise gate reports it as incomplete rather than blocking. The
dev server's process group is stopped on exit, on a signal, and by a
watchdog if the run is killed outright.

**Dependencies.** The app needs `playwright` and `axe-core` as
devDependencies, plus `npx playwright install chromium`. When axe is not
installed, `axe.required: true` fails the check rather than passing without
it.

**Auth.** `storage_state` points at a saved Playwright storage state for a
synthetic account. Never use real accounts.

**Secrets.** Each `headers_from_env` header is sent only to the base URL's
origin, never to a CDN or analytics host. Every redirect hop is re-checked,
so a redirect to another origin, such as an SSO page, never carries the
header. By default this maps
`VERCEL_AUTOMATION_BYPASS_SECRET` to `x-vercel-protection-bypass`.

## L2 / L5 — `ui-smoke-reusable.yml`

This reusable workflow runs the same script, loaded from the pack at
`pack_ref`, against a deployed URL. It installs only `playwright` and
`axe-core` into a temporary prefix, with a cached Chromium. There is no app
install or build. Callers:

- **Preview:** `on: deployment_status`, with
  `if: github.event.deployment_status.state == 'success'`,
  `base_url: ${{ github.event.deployment_status.environment_url }}` and the
  `VERCEL_BYPASS_SECRET` secret.
- **Production synthetic monitor:** `on: schedule`, with the production
  `base_url`. Pick an off-peak, non-zero cron minute. These runs read only,
  so never point mutating lanes at production.

Make the preview smoke a required check only once it has been stable. A
required check that flakes trains people to re-run it.

## L3 / L4 — Personas

Use `.claude/skills/exploratory-bug-bash`. It covers calibration with planted
bugs, the `web-mobile` target, the launch personas (`impatient`,
`trust-checker`, `literal`, `breaker`) and § Launch QA. Findings are
candidates until a deterministic test reproduces them. Persona runs never
gate a merge.

## Adopting in an existing repository

1. Run `copier update` to receive `scripts/qa/`, `config/qa.json` and the
   hook files. A Copier-managed `.claude/settings.json` gains the PreToolUse
   gate. Existing `.cursor/hooks.json` and `.codex/hooks.json` are kept: add
   the gate entry from this pack by hand.
2. Add app recipes to `config/agent-checks.json` from the example file, then
   run `python3 scripts/agent-check.py --provision --provider <you>`.
3. Add `playwright` and `axe-core` as devDependencies. Set `ui_smoke.base_url`
   and `start_command`.
4. Add the preview `ui-smoke-reusable` caller. Set `fail_on_inert: true` on
   any e2e caller whose suite must never be empty.
5. Plugin-only consumers get the Claude gate from the plugin. The same
   `config/agent-checks.json` and `config/qa.json` apply. A `ui-smoke`
   recipe needs the script in the repository: copy `scripts/qa/ui-smoke.mjs`
   or adopt through Copier.
