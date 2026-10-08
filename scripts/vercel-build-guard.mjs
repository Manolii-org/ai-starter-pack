#!/usr/bin/env node
/**
 * vercel-build-guard.mjs — canonical "Ignored Build Step" for the ecosystem.
 *
 * Canonical source: ai-starter-pack registry/platform/framework.
 * Do not edit in consumer repos — change it at the source and let the sync flow.
 *
 * Wire into a project's vercel.json (per-commit, overrides the dashboard
 * "Ignored Build Step" setting):
 *
 *   "ignoreCommand": "node scripts/vercel-build-guard.mjs"
 *
 * Exit codes (Vercel contract): 0 = SKIP build (deployment -> CANCELED),
 * 1 or greater = run the build. Runs when a deployment enters BUILDING, from
 * the project root directory, with the Vercel system envs available.
 *
 * Modes (env, inline in the ignoreCommand string):
 *   VERCEL_BUILD_GUARD_ONLY="main,release/"
 *     Allowlist — ONLY these refs build. Entries ending in "/" are prefixes,
 *     others are exact ref matches. For plain prod-only behaviour prefer
 *     vercel.json `git.deploymentEnabled` ({"*": false, "main": true}) instead —
 *     it is the simpler built-in mechanism; use ONLY= here when the guard must
 *     combine with EXTRA_SKIP or per-repo nuance.
 *   VERCEL_BUILD_GUARD_PREFIXES="claude/,codex/"
 *     Full replacement of the default skip set (below).
 *   VERCEL_BUILD_GUARD_EXTRA_SKIP="staging,custom/"
 *     Appended to the active skip set (works with defaults or PREFIXES).
 *   VERCEL_BUILD_GUARD_DEBUG=1
 *     Verbose decision logging.
 *
 * Default skip set: agent/bot branches, dependency-bot branches and
 * transient merge-queue refs — these never need their own preview build in
 * this ecosystem:
 *   claude/ cursor/ codex/ devin/ chore/ ci/ automated/
 *   renovate/ dependabot/ gh-readonly-queue/ gh-merge-queue/
 *
 * IMPORTANT — projects whose PR pipelines consume preview deployments
 * (e2e PLAYWRIGHT_BASE_URL, required "Preview" deployment gate) must NOT skip
 * branches that open PRs needing that preview. Configure PREFIXES explicitly
 * there (e.g. only "gh-readonly-queue/,gh-merge-queue/,staging").
 *
 * No commit ref in env (local/manual build): fails OPEN and builds.
 */

const ref =
  process.env.VERCEL_GIT_COMMIT_REF || process.env.GITHUB_REF_NAME || "";
const debug = process.env.VERCEL_BUILD_GUARD_DEBUG === "1";

const DEFAULT_SKIP = [
  "claude/",
  "cursor/",
  "codex/",
  "devin/",
  "chore/",
  "ci/",
  "automated/",
  "renovate/",
  "dependabot/",
  "gh-readonly-queue/",
  "gh-merge-queue/",
];

const parse = (v) => (v || "").split(",").map((s) => s.trim()).filter(Boolean);
// Trailing-slash entries are ref prefixes; anything else is an exact match.
const matches = (entry, r) => (entry.endsWith("/") ? r.startsWith(entry) : r === entry);

const decide = () => {
  if (!ref) return [1, "no commit ref — building (fail-open)"];

  const only = parse(process.env.VERCEL_BUILD_GUARD_ONLY);
  if (only.length) {
    return only.some((e) => matches(e, ref))
      ? [1, `ref "${ref}" in ONLY allowlist`]
      : [0, `ref "${ref}" not in ONLY allowlist`];
  }

  const skipSet =
    process.env.VERCEL_BUILD_GUARD_PREFIXES !== undefined
      ? parse(process.env.VERCEL_BUILD_GUARD_PREFIXES)
      : [...DEFAULT_SKIP];
  skipSet.push(...parse(process.env.VERCEL_BUILD_GUARD_EXTRA_SKIP));

  const hit = skipSet.find((e) => matches(e, ref));
  return hit
    ? [0, `ref "${ref}" matched skip entry "${hit}"`]
    : [1, `ref "${ref}" builds`];
};

const [code, why] = decide();
console.log(`[vercel-build-guard] ${why}${debug ? ` (env=${JSON.stringify({ ONLY: process.env.VERCEL_BUILD_GUARD_ONLY, PREFIXES: process.env.VERCEL_BUILD_GUARD_PREFIXES, EXTRA_SKIP: process.env.VERCEL_BUILD_GUARD_EXTRA_SKIP })})` : ""}`);
process.exit(code);
