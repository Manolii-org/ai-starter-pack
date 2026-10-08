---
name: vercel-build-guard
description: Canonical Vercel "Ignored Build Step" standard — which refs build previews/production, the vercel.json-vs-dashboard precedence rules, required-deployment-gate caveats, and the rollout recipe. Use when configuring, auditing, or fixing per-branch build behaviour on any Vercel project in the ecosystem.
---

# Vercel build guard — the standard

Controls which git refs spend build minutes on Vercel. Prevents preview-build
churn (agent/bot branches, merge-queue refs, dead projects) while keeping the
preview deployments that CI actually consumes.

## The two mechanisms

**1. `git.deploymentEnabled` in vercel.json — for prod-only projects.**

```json
"git": { "deploymentEnabled": { "*": false, "main": true } }
```

Use when nothing consumes preview deployments (cron/ops projects, services whose
e2e gates on the production alias). Deployments on other refs are created and
immediately cancelled at zero build cost. Prefer this over an ignoreCommand —
it is the platform-native switch.

**2. `scripts/vercel-build-guard.mjs` — for projects needing per-ref filtering.**

Ship a byte-identical copy of the canonical script
(`registry/platform/framework/scripts/vercel-build-guard.mjs`) inside the
project's root directory (the dir Vercel builds from — e.g. `app/` in a
subdir-layout repo, repo root otherwise), then reference it in vercel.json:

```json
"ignoreCommand": "node scripts/vercel-build-guard.mjs"
```

Exit 0 skips the build; non-zero builds. Config via env inline in the string:

| Env | Meaning |
|---|---|
| `VERCEL_BUILD_GUARD_ONLY="main"` | Allowlist — only these refs build (prefer `git.deploymentEnabled` for plain prod-only) |
| `VERCEL_BUILD_GUARD_PREFIXES="a/,b"` | Replace the default skip set entirely |
| `VERCEL_BUILD_GUARD_EXTRA_SKIP="x/"` | Append to the active skip set |
| `VERCEL_BUILD_GUARD_DEBUG=1` | Verbose decision log |

Default skip set (agent/bot branches + transient merge-queue refs):
`claude/ cursor/ codex/ devin/ chore/ ci/ automated/ renovate/ dependabot/
gh-readonly-queue/ gh-merge-queue/`

Entries ending in `/` are prefixes; others match exactly (`staging` does not
match `staging-fix`).

## Precedence — the rules that bite

- `vercel.json ignoreCommand` **overrides** the dashboard "Ignored Build Step"
  setting *for that deployment*. If a repo has the file, the dashboard copy is
  dead config — delete it via `PATCH /v9/projects/{id} {"commandForIgnoringBuildStep": null}`
  so nobody edits the wrong layer.
- Both are per-commit: a branch builds according to the vercel.json on THAT
  commit's tree. Branches created before the file lands still use the dashboard
  setting — keep the dashboard copy while stale branches exist if semantics
  must not change on them.
- The dashboard field accepts a self-contained `node -e "..."` snippet — use
  that for immediate estate-wide coverage (works on branches lacking the file)
  and let the vercel.json copy take over as branches roll forward.

## The capability rule — check before you skip

Preview deployments are **load-bearing** wherever CI consumes them. Do not skip
a ref class that opens PRs needing a preview:

- Playwright/e2e pipelines that wait on the GitHub `Preview` deployment or read
  `PLAYWRIGHT_BASE_URL`/`deployment_status.target_url` need a READY preview for
  every PR head — including agent (`devin/`, `codex/`, `claude/`), bot
  (`renovate/`, `dependabot/`) and sync (`automated/`) branches.
- A branch-protection `requiresDeployments: ['Preview']` rule blocks merges
  until a successful Preview deployment exists for the PR head SHA — skipping
  that branch's build bricks every PR on it. (Check via
  `branchProtectionRules { requiresDeployments }` in GraphQL.)
- Transient merge-queue refs (`gh-readonly-queue/*`, `gh-merge-queue/*`) are
  never PR heads and no required check consumes their deployment — always safe
  to skip.
- A CLI-driven preview flow (`vercel deploy --prebuilt` in a workflow) creates
  preview deployments independent of the git integration — projects using one
  can skip the same refs in git-integration builds without losing the PR
  preview.

## Rollout recipe per project

1. Check the repo's required checks + `requiresDeployments` and grep
   `.github/workflows` for `deployment_status` / preview-URL consumers.
2. Decide the mode: prod-only → `git.deploymentEnabled`; filtered → guard
   script with `PREFIXES`/`EXTRA_SKIP`.
3. PR the repo change (script + vercel.json).
4. Patch the dashboard for immediate coverage or dead-config cleanup:
   `PATCH https://api.vercel.com/v9/projects/{id}?teamId={team}` with
   `{"commandForIgnoringBuildStep": "node -e \"...\""}` or `null`.
5. Delete dead projects (no domains, no git link, zero successful builds) —
   project deletion also removes a stale failing deploy check on the repo.
