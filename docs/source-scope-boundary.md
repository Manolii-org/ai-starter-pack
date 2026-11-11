# Source-only validation boundaries

An owning data boundary must be established before a validation runner reads,
hashes, diffs, copies or materializes that data. A Git name-only diff, sparse
checkout, skip-worktree flag or a later test skip does not establish this boundary.
Global recipes, old check identities/providers, tool contexts and budgets remain
unchanged until an accepted replacement is qualified.

This first implementation is a deterministic **hold**, not a projection runner.
The reserved local declaration `.ai/source-scope-policy.json`, an independent
`AGENT_SOURCE_SCOPE_SHA256` or `AGENT_SOURCE_SCOPE_REPOSITORY` owner input, or a
committed declaration at HEAD blocks broad validation before configuration,
working bytes, binary diffs, dependency/dotenv/cache reads, setup or execution.
The working policy is not opened, including malformed/symlinked policies. Enclosing declarations remain effective from subdirectories and nested Git
repositories. Before any Git probe, bounded regular/no-follow control metadata
checks reject include/includeIf, config aliases and alternate-object routing.
A bounded descriptor-only directory-metadata pass rejects static ref/object
symlinks, hardlinks and special files before native Git; it never opens those
files for payload reads. Current linked-worktree private refs and packed/shallow
controls are included; symbolic HEAD chains stay inside the checked current ref
namespaces. Graft aliases hold without content reads, and only the fixed metadata
probe disables graft interpretation (it does not change normal owner Git flows).
Ordinary packed metadata uses the same fixed-argv commit/tree/policy-presence
query, with no blob extraction, filters or fetch.
A pack mapping alone proves neither an opaque payload scan nor payload isolation.
Standard linked worktrees require verified control topology and a matching
backpointer; other control indirections hold without reading their targets.
The tree probe uses a fixed path with lazy fetch and transport disabled. No protected blob or old receipt is inspected to explain a
hold. Held exits are distinct from generic missing-tool infrastructure.

`validate_source_scope_policy` validates already supplied compiled policy bytes
against an independent SHA and owner. The policy schema has strict fields and
bounded literal source/protected paths. These inputs must come from reviewed,
pinned owning execution configuration, never from a candidate's self-assertion
or a live cross-organization configuration fetch. Removing or changing a working
declaration cannot withdraw an independently compiled owner pin. Updating these
trust inputs is a normal reviewed source adoption, not a recurring operator task.

The finalise gate denies a declared boundary before root-discovery Git,
query/config/hook reads, installation, logs or starting the runner; the installed
pre-push launcher also discovers its root from directory metadata without native
Git. Inaccessible declaration metadata is held as unknown rather than absent; warn/off and inline bypass cannot grant data-read or hand-off
authority. A late held exit is also handled before stale receipts. The installer
refuses before reading owner prose, layout or recipe files. v1 defaults and receipt
schemas are preserved for repositories with no declaration; an old unguarded
binary or external preflight wrapper is **not** made safe by this source alone.
Each owning caller must install and invoke the guard before its own broad reads.
Unadopted owners remain held operationally; no old receipt is upgraded to proof.

There is intentionally no `projection_verified` input or success override. A
future authorized source-only executor needs a real Git metadata/allowed-byte
projection with opaque objects physically absent, no alternates/lazy fetch/network
or credentials, exact source/tree/policy/path/mode/blob bindings and independently
reviewed command/dependency closure. A test that requests an omitted input must
fail, and the result must have a separate restricted-scope identity with no
full-repository/runtime/merge authority. Merely omitting files while executing
unreviewed whole-repository commands cannot meet that contract. This hold neither
implements that projection nor claims full safe qualification. No existing
required check is removed, skipped or renamed.

Control checks are bounded repeated observations, not an atomic filesystem or
host security boundary. Independently pinned owner scope inputs remain the
required installation binding and refuse before this metadata route. A mutable
or untrusted Git object/control store is not qualified by these observations.
