# Explicit skill layout for the validation installer

This opt-in contract maps one installer-owned skill into an existing shared
skills directory. It does not discover aliases, replace links, change the
repository root or authorize another consumer's installation. Without
`--layout`, the installer retains its strict destination behavior and existing
six logical provenance keys.

## Owner contract

On an isolated task branch, commit a regular owner-controlled layout file:

```json
{
  "schema": "agent-validation-layout/v1",
  "aliases": {
    ".claude/skills": ".github/skills",
    ".cursor/skills": ".github/skills"
  }
}
```

Only these two aliases and exact canonical target are supported. Each link must
be tracked in HEAD and index with mode `120000` and the exact relative target
`../.github/skills`; its worktree bytes must match. The layout file must also
match HEAD/index/worktree bytes and executable mode. Unknown/duplicate JSON
fields, undeclared aliases, traversal, external, dangling, cyclic or nested
links and ambiguous path casing are rejected. The original repository root,
canonical directory and all canonical parents must be real, unlinked paths.
The canonical `.github/skills` directory must already exist.
The explicit layout mode requires POSIX no-follow directory-descriptor support;
unsupported hosts fail closed before planning. The regular default layout keeps
its portable path through individual atomic replacements.

The `.claude/skills/agent-validation/SKILL.md` logical output maps to exactly one
`.github/skills/agent-validation/SKILL.md` write. `.cursor/skills` is a read-only
witness, never a second write. Both tracked links stay unchanged. This is not
an exception for other output directories or for the separate instruction-file
alias rules in `sync-agent-validation.py`.

From a reviewed, immutable Git checkout of the Pack source, plugin subtree or
registry subtree, preview before apply:

```sh
python3 scripts/install-agent-validation.py --repo-root /absolute/synthetic-repository \
  --layout config/agent-validation-layout.json --dry-run
```

Omit `--dry-run` for an independently authorized installation; use `--check`
to compare the same complete plan without writing. This source implementation
was exercised only in fresh temporary fixtures, not a real consumer. Initial
explicit-layout adoption requires the six source files to match the source
checkout's immutable Git commit. A Git-less initial producer without qualified
origin evidence is not accepted by falling back to a floating ref. After
installation, the installed entrypoint can check or preview its own matching
bundle using the validated origin receipt, with the original checkout absent.

## Planning, provenance and recovery

All three modes share one complete output plan, including recipe, provenance,
managed instruction surfaces, ignore policy and the reserved recovery path.
Protected branches and both current/proposed owner integration refs remain
guarded on apply. Owner recipes and other-owner prose are preserved. Conflicting
canonical outputs, malformed managed blocks or provenance, and modified inputs
are holds rather than partial-success claims.

`agent-validation-source/v1` keeps exactly the existing six logical `files`
keys and their SHA-256 meanings. Optional `layout` uses
`agent-validation-layout-record/v1` and binds:

- Source Git commit/subdirectory and the digest of the complete six-file hash
  map; hashes include the installer itself.
- Tracked layout path, mode, blob and exact file digest.
- Both tracked alias modes, blobs, targets and content digests.
- Every logical/canonical destination pair and its installed-byte digest.

Existing installed bundle bytes must match their provenance. An existing layout
record must match the current tracked layout identities; malformed or mismatched
evidence is refused rather than silently reminted. These are content and origin
bindings, not an independent signature, review approval or host-loading proof.

Inputs, directory/file identities and content are snapshotted. They are checked
again before each replacement. Replacements use a temporary regular file in the
held parent directory and an atomic rename through its directory descriptor.
Existing non-script modes are preserved; replaced direct `scripts/*.py` outputs
retain the existing executable-mode convention. Caller paths must stay under
exclusive ownership during the batch.

The batch is **not** an atomic filesystem transaction or a compare-and-swap
against a noncooperating writer. There are bounded gaps between checks and
operations. No automatic rollback overwrites a newer writer's files.

Explicit-layout apply records private, mode-0600 evidence in
`.ai/agent-checks/layout-recovery.json`, under the existing ignored receipt
directory. Before planning can succeed, the path must be absent from HEAD and
index. The journal and its whole directory must already be ignored by the same
tracked, unchanged ancestor `.gitignore` outside that directory.
The installation cannot simultaneously change that ignore-policy file; an
owner may establish a dedicated tracked `.ai/.gitignore` with `agent-checks/`
before installation. Global ignores and local excludes are insufficient.
This precondition is revalidated before replacements, so interrupted first
adoption cannot leave the private journal available to `git add .`.
It includes original bytes/modes, intended hashes and completed
paths. A successful batch marks it complete. A blocked or interrupted batch
leaves prepared evidence; stderr reports known completed replacements. If an
input change prevents a journal refresh, the prepared snapshot and reported
progress must be reconciled against actual file hashes. An uncertain last
replacement is not permission to replay it. Incomplete evidence blocks another
apply/check/preview. Keep this private evidence local; it can contain owner text.

## Packaging and reader handoff

The inline planner adds no seventh runtime module. The unchanged builders copy
the installer into the plugin and registry, and installed-entrypoint fixtures
exercise all three layouts without the original producer checkout. The new
guide is a Pack reference document; runtime code does not read it. Default
source-file selection and the installed six-file bundle remain unchanged.

Before publication, the `copier.yml` owner must add precisely this Pack-only test
exclusion to `_exclude`:

```yaml
  - scripts/tests/test_agent_validation_layout.py
```

This candidate does not include the separate proposal directory, so it needs no
proposal-directory exclusion. Do not change the builders or another owner's
branch merely to bundle that edit. Regeneration may expose unrelated managed
drift; retain only byte-identical matching installer copies after inspecting
the complete delta and verifying import closure. No other generated change is
qualified by this source slice.

The separate Master `check-agent-validation-bundle.py` remains a strict reader:
it requires six logical keys and rejects linked installed paths. Passing this
installer's checks does not satisfy that reader. Its owner must qualify a
compatible explicit-layout reader/integration before an alias-aware consumer
can rely on that gate; do not broaden its symlink policy here. Consumer recipe
budgets, trigger classifications and Python-profile admission remain separate.

Release only through actual packaging/reader compatibility, normal review and
side-effect qualification. Each consumer adopts an immutable pin through its
own authorized lane. Neither a local commit nor successful fixture installation
authorizes publication, real consumer adoption or runtime activation. Retain
the original source, layout, recipes, link identities and recovery checkpoint;
recover or roll forward only owned outputs through the normal qualified path.
