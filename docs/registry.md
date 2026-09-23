# Capability Registry — model and usage

One canonical git-backed store for shareable agent capabilities, organised into
privacy scopes. Consumers **declare dependencies** in `ai-manifest.yaml` instead
of receiving file copies; the resolver materialises the resolved set into the
paths agents already read. Distribution changes; consumption paths don't.

## Scopes

| Scope | Visible to | IP owner | Promotion path |
|-------|-----------|----------|----------------|
| `platform` | every repo in every universe | Manolii Pty Ltd | — (top of the funnel) |
| `manolii` | Manolii universe repos | Manolii Pty Ltd | → platform after sanitisation |
| `buro` | Buro Built universe repos | Buro Built | → platform after sanitisation |
| `impaktful` | Impaktful universe repos | Impaktful Pty Ltd | → platform after sanitisation |
| `cpdcheck` | CPDcheck universe repos | Ensombl Pty Ltd | → platform after sanitisation |
| `repo` | the owning repo only | owning repo | → its universe scope |
| `personal` | the owning user only | owning user | → repo scope |

Privacy is structural, not procedural — three enforcement layers:

1. **Resolver fail-closed.** `ai-resolve.py` lets a repo require only
   `platform/*`, `<its-universe>/*`, `repo/*`, `personal/*`. A `cpdcheck` repo
   asking for `buro/core` is a hard error before any bytes move.
2. **Registry CI lint** (`registry-lint.yml`). Platform content must be free of
   *every* universe identifier (including the publisher's own), universe content
   must not name other universes, and nothing anywhere may carry a credential
   shape. This merges the two existing gates: `pack-drift-check.py`'s org-leak
   scan and `validate-sync-manifests.py`'s entity-boundary check.
3. **Sensitivity tiers.** `repo`/`personal` scope assets never publish.
   On Devin, org-scope `forbiddenPlugins` globs (deny-wins across scopes)
   provide the same isolation natively.

## Public-repo boundary (hard rule)

**ai-starter-pack is public.** Its `registry/` tree therefore holds
**platform scope only, plus every scope's `scope.yaml` contract** — the
contracts are public-safe metadata (visibility, ip_owner, promotion
policy). Nothing else may land under a non-platform scope here:

- `registry/<universe>/` content lives in a **per-org private mirror**
  (e.g. the Buro universe's own capability-registry repo under its GitHub
  org). The PUBLIC waiver requires **all three** of: an empty
  `registry/.private-mirror` marker file; the sha256 of the mirror's own
  `owner/repo` slug (lowercased) listed in `registry/private-mirrors.txt`;
  and a live `gh api repos/<slug>` result reporting `visibility ==
  "private"` for a GitHub-hosted origin. To stand one up: create the
  repo private, add the marker, vendor `registry/platform/**` +
  `plugins.json` + `scripts/registry-lint.py`, and write
  `registry/private-mirrors.txt` with the sha256 of its own slug.
  Do NOT vendor the canonical `.github/workflows/registry-lint.yml` — its
  other steps invoke `build-registry.py`, `ai-resolve.py`, the manifest
  schema, and `tests/test_registry.py`, none of which the mirror ships.
  Use this reduced workflow instead (`.github/workflows/registry-lint.yml`):

  ```yaml
  name: registry-lint
  on:
    pull_request:
      paths: ['**']
    push:
      branches: [main]
      paths: ['**']
  permissions:
    contents: read
  jobs:
    registry-gate:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with:
            python-version: '3.12'
        - name: Install deps
          run: timeout 120 pip install pyyaml jsonschema
        - name: Lint the registry
          # `.private-mirror` waiver verifies repo visibility via `gh api` —
          # an unauthenticated lookup fails closed and refuses the waiver.
          env:
            GH_TOKEN: ${{ github.token }}
          run: python3 scripts/registry-lint.py
  ```
  Mirror consumers point `ai-resolve.py` at the mirror checkout — the
  resolver needs no flag; scope semantics are identical.
- `registry/repo/` and `registry/personal/` content lives inside each
  consumer repo (or the user's home checkout) and never syncs upstream.
- The PUBLIC check fails the lint on any non-`scope.yaml` file under a
  universe/local scope in this repo — the boundary is enforced, not
  documented.

Separately, `PACK-SURFACE` fails on private-repo slugs (other repos in
this org, and any other org's repos) and infra identifiers (Vercel
project ids, `*.supabase.co`, `*.neon.tech`, `*.internal`) anywhere in
the repo outside `registry/`. Today's references are ratchet-frozen in
`registry/pack-surface-allowlist.txt` — new hits FAIL; regenerate with
`python3 scripts/registry-lint.py --write-pack-allowlist` when scrubbing
one down.

## Authoring a plugin

```
registry/<scope>/<plugin>/
  .claude-plugin/plugin.json   # name must equal the dir name
  .devin-plugin/plugin.json    # same name — makes it git-subdir installable
  skills/<skill>/SKILL.md      # frontmatter: name + description required
  agents/*.md                  # optional
  commands/*.md                # optional
  hooks/, scripts/, data/      # optional — wired via surface renderers
```

Then add it to `registry/plugins.json`. `registry-lint.py` runs all checks
locally; `.github/workflows/registry-lint.yml` runs them on PRs.

`registry/platform/*` is **generated** (`scripts/build-registry.py`, unbranded
render of the canonical `.claude/` template) — never hand-edit; edit the
template and rebuild. Universe scopes are hand-owned by their orgs.

## Consuming (ai-manifest.yaml)

```yaml
version: 1
universe: buro            # your universe id; unregistered repos still get platform/*
requires:
  - plugin: platform/framework
    ref: "^1.14"          # exact semver | ^major | tag:vX.Y.Z | sha:<hex>
feature_flags: {}
surfaces: [claude-code]
```

```bash
python3 scripts/ai-resolve.py --registry <checkout-of-ai-starter-pack>          # dry-run
python3 scripts/ai-resolve.py --registry <checkout> --apply                     # materialise
python3 scripts/ai-resolve.py --registry <checkout> --check                     # CI drift gate
python3 scripts/ai-resolve.py --registry <checkout> --apply --prune             # drop removed deps
```

Resolver guarantees: fail-closed scope check before any write; never clobbers a
hand-edited file — the lockfile records the installed sha256 of every
materialised file, so a file changed since install (by you or by drift) is a
conflict, not an overwrite; writes `.ai/capability-lock.json` (commit it —
paths are repo-relative) so `--check` detects drift.

Materialised components today: `skills/` → `.claude/skills/`, `agents/` →
`.claude/agents/`, `commands/` → `.claude/commands/`. `hooks/`+`scripts/`
materialisation needs the settings-merge surface (P1) — reported as advisory.

## Install paths per surface

- **Devin:** the pack root is a meta-plugin (`.devin-plugin/plugin.json`) whose
  `requiredPlugins` install `registry/platform/*` via `git-subdir`. Enterprise
  manifest installs it org-wide; org manifests pin universe plugins and use
  `forbiddenPlugins` globs (e.g. `buro/*` forbidden outside Buro). Repos pin or
  forbid in `.devin/config.json`. Deny-wins across scopes.
- **Claude Code:** the registry is a marketplace — `.claude-plugin/marketplace.json`
  lists `framework` and `om` sourced at `./registry/platform/*`. Consumers add
  it under `extraKnownMarketplaces` in `.claude/settings.json` and enable via
  `enabledPlugins` (`framework@manolii`). Marketplace source tracks a release
  tag (`ref`); individual plugin sources may pin `sha`.
- **Cursor / Codex / OpenHands:** no plugin system — the resolved set compiles
  to `.cursor/rules/*.mdc` / AGENTS.md sections as generated artifacts (same
  one-source→per-surface pattern as `generate-devin-hooks.py`).

## Migration

| Phase | Content |
|-------|---------|
| P0 (this) | Registry scaffold: scope dirs, contracts, lint gate, resolver, manifest schema, platform seeds generated from `.claude/`, meta-plugin + marketplace wiring. Legacy surfaces (`plugin/`, `.claude/` template, master sync rings) unchanged and still shipping. |
| P1 | Master PR: ADR + `manifest-spec.md` `scope` field + sync-manifest deprecation annotations + `cross-org-map` update. Consumer pilot: one repo (bcp-core) adopts `ai-manifest.yaml` + resolver in CI. Registry gets remote fetch + `repo`/`personal` materialisation + hooks-merge surface. |
| P2 | Render arrows invert: `.claude/` template and `plugin/` become generated FROM registry; rings freeze (`deprecated: true` per file, cleanup PRs via the manifests' own `deprecation_policy`); `templates/ai-starter-pack/` inside master deleted. |
| P3 | Universe seeds: buro/* from Buro sources, impaktful/* from impaktful_3.0 divergence review (35 of 63 skills diverged 88–766 lines — each needs a keep/promote/drop decision), cpdcheck/* from Ensombl. |
| P4 | Jev librarian wiring: `discover_candidates` reads the resolved catalog (entity-filtered upstream), shadow-mode curation signals (dedupe/staleness/promotion) feed `scope.yaml` `promotion_policy`. |

## Legacy name mapping

| Legacy surface | Registry home | Invocation change |
|----------------|---------------|-------------------|
| `plugin/manolii-framework` | `registry/platform/framework` | `/manolii-framework:*` → `/framework:*` |
| `plugin/manolii-om` | `registry/platform/om` | `/manolii-om:*` → `/om:*` |
| `.claude/` template render | `registry/platform/*` (generated from it today; generates it in P2) | none (materialise-in-place) |
| master ring-1/ring-2/routing copies | `registry/platform/*` + universe scopes | copies freeze in P2 |
