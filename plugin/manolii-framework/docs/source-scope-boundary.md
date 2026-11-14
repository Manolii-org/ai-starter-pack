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
Read-only scope, root-discovery and hook-location queries receive only a fixed
platform runtime/locale/temp environment plus explicit no-fetch/no-config/
no-prompt Git controls. Credentials, askpass, tracing and inherited Git selectors
are absent. The parent still applies independently supplied scope pins first;
normal owner Git execution retains its existing environment contract. Repository
hook-manager checks and unmarked hooks remain in place; a local shim file alone
does not prove which hook route the user's native Git actually executes.
Ordinary packed metadata uses the same fixed-argv commit/tree/policy-presence
query, with no blob extraction, filters or fetch.
A pack mapping alone proves neither an opaque payload scan nor payload isolation.
Standard linked worktrees require verified control topology and a matching
backpointer; other control indirections hold without reading their targets.
Relative backpointers are interpreted from their Git control directory, with
the same ownership comparison as absolute pointers. Lexical and physical
enclosing declarations are both checked before inner Git controls. Ordinary
symlinked directory prefixes do not make the physical owner's regular Git
metadata an alias; a `.git` marker that is itself a link remains held.
The tree probe uses a fixed path with lazy fetch and transport disabled. No protected blob or old receipt is inspected to explain a
hold. Held exits are distinct from generic missing-tool infrastructure.
Known unsupported topologies expose only a closed set of static diagnostic
reasons. Arbitrary exception text, filenames and undecodable control bytes are
never included in a hold. A verified unborn branch has the same undeclared
scope result in the runner and finalise gate; damaged or unknown HEAD does not.

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
refuses before reading owner prose, layout or recipe files. External recipe
inputs are checked at both their lexical and resolved owning locations before
any plan snapshot or payload read, including legacy linked recipes. Low-level
file readers likewise check a followed leaf's owner and a directory input's own
boundary; dotenv traversal checks a nested directory before enumerating it.
A declaration anywhere below an owner holds the whole owner: a subfolder's
working or committed policy, or a nested repository whose metadata is declared
or cannot be qualified (including initialized submodules). Detection reads
names and Git metadata only, once per process; validation commands could
otherwise read that subfolder. Reads routed through directory links also check
the target's owners first.
Ordinary unscoped linked and absent inputs keep their existing observations;
cycles and inaccessible ownership metadata hold without exposing private paths.
The Cursor hook classifies commands before repository/scope probes: ordinary
`ls`, `pytest` and `git status` have no hand-off authority to qualify. Actual
push/PR hand-offs still hold before warn/off or inline bypass can apply. v1 defaults and receipt
schemas are preserved for repositories with no declaration; an old unguarded
binary or external preflight wrapper is **not** made safe by this source alone.
Each owning caller must install and invoke the guard before its own broad reads.
Unadopted owners remain held operationally; no old receipt is upgraded to proof.

There is intentionally no `projection_verified` input or success override. A
source-only executor requires a real Git metadata/allowed-byte
projection with opaque objects physically absent, no alternates/lazy fetch/network
or credentials, exact source/tree/policy/path/mode/blob bindings and independently
reviewed command/dependency closure. A test that requests an omitted input must
fail, and the result must have a separate restricted-scope identity with no
full-repository/runtime/merge authority. Merely omitting files while executing
unreviewed whole-repository commands cannot meet that contract. This hold neither
installs the additive projection/executor nor claims full safe qualification.
The separately reviewed Pack libraries have a Pack-only guide at
`docs/source-only-execution.md`; that guide is not installed with this guard.
No installed guard caller routes into those libraries or upgrades an old receipt.
No existing required check is removed, skipped or renamed.

Control checks are bounded repeated observations, not an atomic filesystem or
host security boundary. Independently pinned owner scope inputs remain the
required installation binding and refuse before this metadata route. A mutable
or untrusted Git object/control store is not qualified by these observations.

The Pack owning check separately runs its scope-boundary sentinels and the
`source_projection.py` foundation contract alongside the portable execution
suite. The projection foundation accepts independently pinned committed inputs,
materializes only policy-admitted source and authentic Git metadata, and retains
all execution, closure, cache, native and full-repository authority as false.
`require_execution_closure` always refuses. It is not an executor or admission
adapter; packed/shared/linked-worktree input remains unsupported by that
foundation. The metadata-only guard's narrower packed-query support does not
change those projection limits.

Pack-only scope/projection/executor tests and both libraries do not render into
consumers or their recipes. The standalone executor guide is Pack-only as well.
The first Pack check additionally requires the explicit Linux `bwrap`/`cc`
contract and runs its exact 17 cases; those tools and that command are excluded
from the consumer recipe. The existing Linux Plugin Eval Gate installs exact
versions resolved from its signed public package candidates, checks actual
namespace capability, and runs the same executor tests. Missing tools or denied
isolation is incomplete qualification (a failing native preflight exits 2), never
a skipped passing proof. Local Pack source qualification requires that Linux
adapter; portable Mac consumers retain their existing Python/Git recipe. Existing
seven check identities, three dependency environments and budgets remain intact.
Actual new-head native execution is still required after source publication.
The existing portable validation suite remains independent of an optional QA helper. Pack consumer tests separately exercise an
actual rendered guard hold and an ordinary failing check; absent maintenance
tests are never counted as successful boundary qualification.

The owning and rendered portable validation check declares the existing `git`
tool fingerprint as well as Python: these real-Git checks and metadata semantics
must bind the actual Git version in the receipt. The helper already supports that
probe. This adds no tool version override, schema exception or unrelated owner
profile change; a changed Git probe invalidates reusable evidence normally.

Native hook installation additionally needs an admitted routing result. A local
`core.hooksPath` retains its manager and is never replaced. Ambient injected
configuration holds installation. With no local manager, only metadata-proven
absent or explicitly disabled nonlocal config permits writing the default shim.
Present or inaccessible HOME/XDG/explicit config and an unknown compiled system
config route produce exit 3 and a static unqualified-routing diagnostic, without
reading external config payloads or changing any hooks. A local empty hooksPath
is still a configured route, not proof that the default shim will run. This
intentional installation-only limitation replaces the unsafe inference that
sanitized config absence proves native routing absence. It introduces no native
Git environment changes, approval or feature-enable action; normal validation
and ordinary commands are unaffected. A future qualified config-input adapter
may preserve richer native routing, but no installation or owner-adoption proof
is inferred while this specific route is unknown.

Lexical ancestry and physical ownership use separate path observations. Physical
resolution starts from the original requested path, including `..` following a
symlink; normalizing that path first would select a different owner. Relative
requests retain the same physical interpretation as native filesystem access.
Both working and committed enclosing declarations hold before any payload read
through that route. Ordinary unscoped dot/parent traversals retain their normal
file observations; unknown or cyclic resolution remains held.

Hook-route metadata uses the requested native Git working directory when a
GLOBAL/SYSTEM/HOME/XDG config path is relative. The Python process working
directory cannot prove that input absent. Before any existing hook payload is
read, both target ownership routes and the physical directory ancestry are
qualified. Directory descriptors and no-follow leaf opens reject aliases,
hardlinks, special files and changed controls; existing unmarked hooks remain
untouched. An exact marked shim with mode 0755 is retained without a write.
A different marked body or mode holds with exit 3: this helper has no portable
expected-old atomic replacement primitive and does not replace or truncate an
installed control. This specific source cutover needs a qualified replacement
adapter, not an operator enable or approval ritual.

Fresh creation uses exclusive no-follow creation at the qualified directory
descriptor. Its mode stays 0600 while complete bytes are written, fsynced and
read back. Scope, inode, link count, entry and ancestry checks precede and follow
activation to 0755. Unknown routing/target, I/O errors and observed races do not
claim installation. Failed creation can leave an inactive partial file, or a
complete file whose publication could not be confirmed; recovery does not unlink
a public name that another writer may have replaced. These are repeated bounded
observations, not filesystem expected-old CAS or a hostile-host security proof.
Existing hooks are conserved through every failure path.

The postactivation observation retains the prepared inode/link/size/mtime and
expected full body. It separately binds chmod's new mode/ctime, reads back the
body and rechecks the held descriptor and directory entry; two newly current
identities alone cannot requalify changed executable bytes.

The installer keeps source qualification separate from writer portability. Its
legacy writer fallback is tested with an installer-only OS facade while the
metadata reader retains the actual host primitives. A host missing the reader's
no-follow or directory-descriptor APIs remains held before payload reads or
owner enumeration; writer fallback does not qualify that host or withdraw scope.
The full installer suite retains its baseline coverage and adds actual
process-global missing-reader controls. No public receipt reader or CLI changes.
