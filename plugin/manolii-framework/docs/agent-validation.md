## Validate candidates in the coding session

Before the first edit, inspect the integration branch, owning instructions,
guarded paths, generators and affected callers. Keep required CI names,
fork isolation, secret scanning and production approvals intact.

Before pushing a candidate, run:

```bash
python3 scripts/agent-check.py --provision --provider codex
```

Use your actual provider (`claude`, `cursor`, `codex` or `devin`). The
repository owns commands in `config/agent-checks.json`; keep them aligned
with CI and its toolchain. Missing tools or failed installs are incomplete
validation. This explicit command is required until this host's hook
execution has a receipt. File presence alone proves no hook ran.

Iterate with focused tests, batch corrections and independently review the
settled diff once at the required scope/tier. Review contracts, generated
outputs and process seams; add regression tests for recurring defects.
Keep PRs draft while iterating; expensive draft-skipped lanes must also run
on `ready_for_review`. Follow the owning repository's feature/release lanes.

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

Install only on an isolated task branch. The installer preserves the owner's
integration `base` and optional `protected_branches`, including the current
recipe when a replacement is supplied. Policy sync keeps every previously
managed Claude instruction surface current and preserves other owner prose.

This block is generated from `docs/agent-validation.md` by
`python3 scripts/sync-agent-validation.py`; `--check` detects surface drift.
