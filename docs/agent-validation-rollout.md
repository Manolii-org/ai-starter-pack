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
`--check` first to inspect drift. The installer records content hashes in
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
