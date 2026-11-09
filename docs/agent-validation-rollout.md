# Shared agent validation adoption

The authored source is this pack's `scripts/agent-check.py`,
`scripts/sync-agent-validation.py`, `scripts/install-agent-validation.py`,
portable contract tests, policy and `agent-validation` skill. The plugin and
platform registry are generated with `scripts/build-plugin.py` and
`scripts/build-registry.py`. Do not edit generated copies.

On an isolated consumer task branch, owners define `config/agent-checks.json`
from their actual CI commands, frozen installs, toolchain and integration branch.
Then run the installer from a reviewed checkout or framework bundle:

```sh
python3 /path/to/reviewed-pack/scripts/install-agent-validation.py \
  --repo-root "$PWD" --recipes /path/to/owner-recipes.json
python3 scripts/agent-check.py --provision --provider codex
```

For a later bounded update, omit `--recipes` to preserve owner recipes. Use
`--check` first to inspect drift. The installer prints `RECIPE-UNCOVERED <path>`
for any managed file no recipe check selects (a newer pack can manage new files);
add it to the check that validates the agent-validation files, or the portable
contract test fails.

Recipe details that keep receipts honest:

- `tool_requirements` maps a tool to a required version prefix, for example
  `{"node": "v24"}`. A different version reports the toolchain unavailable
  instead of passing on an unsupported runtime. Mirror the CI toolchain.
- `ready_paths` are fingerprinted (names, modes, sizes, mtimes, inodes and ctimes,
  walking through symlinked directories; tool caches such as `.cache`,
  `.vite-temp` and `__pycache__` excluded). A deleted or edited dependency needs
  `--provision` again, and a cached PASS is bound to that fingerprint too. List
  the dependency directory itself (`node_modules`, `.venv`), not only a sentinel
  file inside it: a sentinel proves setup ran but fingerprints one file.
- What a cached PASS is bound to: tracked and untracked files (contents and the
  executable bit; other permission bits are not bound), ignored `.env*` files in
  the repository root and in each selected check's or environment's `cwd`
  (effective contents, keyed, including every file under a `.env*` directory;
  an unreadable one binds its inode and times; a dotenv elsewhere, such as `apps/web/.env` read
  by a command run from the root, is not), the `ready_paths` trees, tool
  versions and the execution environment. Other ignored files, such as build
  caches, are not inputs. Run with `--no-cache` when a check depends on any of
  these unbound inputs; `ready_paths` must exist right after provisioning, so
  they suit what setup creates, not caches a check writes later.
- Patterns use `fnmatch`, where `*` also matches `/`: give a linter that only
  accepts workflow YAML `.github/workflows/*.yml` and `*.yaml`, not
  `.github/workflows/*`, if that directory holds other files. The installer records content hashes in
`.ai/agent-validation-source.json`; review the source revision and resulting
hashes in the adoption PR. It preserves unrelated instructions and settings.
The same policy is rendered at the start of AGENTS, Claude instructions and an
always-applied Cursor rule. Devin and Codex use repository AGENTS guidance; all
four providers explicitly execute the same command and record their actual
provider. Host hook execution must be demonstrated separately.

For a new Copier instance, set `agent_validation_base` to the owning integration
ref, for example `--data agent_validation_base=origin/trunk`, `origin/staging`,
or a local `master` branch in a repository without a remote. Initialize or fetch
that ref before validation. The default is `origin/main`; the runner never
silently substitutes `HEAD` for a missing integration ref. Copier renders this
answer into the initial owner recipe and preserves that recipe on later updates.

Run `sync-agent-validation.py --check` and the portable tests inside an existing
fast CI job. Avoid a standalone runner allocation for these small checks.
Keep local receipts ignored. Run owner security and final-candidate CI
independently; local evidence does not authorize a merge or guarded path.

Coordinate a source update through reviewed adoption PRs in each owning
repository. Private registries carry only the public platform capability and
their own universe. Do not activate unrelated integrations or copy another
universe's rules. Revert the adoption commit to roll back; restore a workflow's
runner independently if a Slim compatibility or runtime regression occurs.
