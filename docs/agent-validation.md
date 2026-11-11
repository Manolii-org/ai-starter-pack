## Validate candidates in the coding session

Before the first edit, inspect the integration branch, owning instructions,
guarded paths, generators and affected callers. Keep required CI names,
fork isolation, secret scanning and production approvals intact.

Before pushing a candidate, run:

```bash
python3 scripts/agent-check.py --provision --provider codex
```

Use your actual provider (`claude`, `cursor`, `codex` or `devin`).
Keep owner `config/agent-checks.json` aligned with CI/toolchains.
Missing tools or failed installs mean incomplete validation. Run explicitly
until this host has a hook-execution receipt; files alone prove no hook ran.

Batch fixes with focused tests; add regression tests for recurring defects.
Independently review settled diffs,
contracts, generated files and seams once at required scope/tier. Open ready only
with a passing exact-head `agent-check` receipt, no expected pushes and no
multi-repo/provider iteration. Else draft until final-head Fast Tier + secret scan
pass. Run draft-skipped lanes on `ready_for_review`; honor owner feature/release lanes.

Local receipts are reusable only for matching inputs, commands and tools;
they grant no merge or protected-path authority. Run any additional owning
preflight/review gate. Classify recurring infrastructure failures separately
from source defects; use one owning incident and bounded recovery rather
than repeated code pushes. Measure total agent, workspace and CI cost.

Reusable results also bind the effective execution environment without recording
its values. Keep the ignored private cache key and dependency state local; do
not upload them. Candidates containing symlinks still run their checks, but
cannot reuse or save a cached PASS based only on Git's link text. Tracked files
with Git filter attributes block local validation before selection or cache reuse;
filtered working files require a raw-input adapter to validate their actual bytes.

Declared dependency readiness binds regular file bytes rather than relying on
timestamps. Tool-cache exclusions stay excluded. Unsafe/external links, special
files and bounded-scan limits disable setup/PASS reuse; fresh provisioning and
checks still run. The cache walker never reads file bytes or directory entries
outside the owning repository. Dependency identities use the private cache key
and remain in local evidence.

The portable `READY_IDENTITY_CONTRACT` is `agent-ready-content/v3`; its hash
domain separates earlier readiness evidence without changing receipt schemas,
the CLI or the optional private-key call interface.

Directory membership and physical identity are checked again after each subtree.
Directory timestamps detect drift during a walk but never enter the persisted
directory digest. Cached and freshly executed PASS results both recheck source,
declared dependencies, dotenv and tool identities before publication. These are
bounded repeated observations, not an atomic filesystem snapshot: concurrent
writes that occur after an observation or fully reverse between observations
cannot be ruled out. Observable changes fail closed.

Install only on an isolated task branch. The installer preserves the owner's
integration `base` and optional `protected_branches`, including the current
recipe when a replacement is supplied. Policy sync keeps every previously
managed Claude instruction surface current and preserves other owner prose.

This block is generated from `docs/agent-validation.md` by
`python3 scripts/sync-agent-validation.py`; `--check` detects surface drift.
