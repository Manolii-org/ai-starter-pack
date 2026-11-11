# Offline source-only command execution

`source_projection.py` remains an unchanged materializer. The additive
`source_executor.py` library executes one exact reviewed profile through a fixed
Linux bubblewrap adapter. It does not install a caller, change the normal
`agent-check` recipe, mint an `agent-check/v1` PASS, reuse its cache, or authorize
admission, native protection or production. Existing controls still apply.

Root cause: a source projection alone cannot restrict a process that executes
on the original host. Changing its working directory or environment leaves
host files, credentials and networking accessible. The executor therefore
never mounts the input checkout, evidence directories, host root, `/usr`,
`/proc`, user home or a Docker socket.

## Trusted inputs

The owner must prepare and independently review a profile and its SHA-256
outside the selected candidate's authority. Supplying a candidate's own hash
as the expected pin is not review. The same rule applies to repository identity,
source/base commit and tree IDs and the compiled source-policy hash. This
library does not discover authorization or download live central policy.

`execute_committed_source` takes those explicit pins, the source repository,
a private output parent, the exact profile bytes/pin and an owner-prepared
private runtime bundle. The profile has exactly these fields:

| Field | Meaning |
| --- | --- |
| `schema` | `source-execution-profile/v1` |
| `id` | Stable lowercase profile identifier |
| `repository` | The exact reviewed repository identity |
| `source_sha`, `source_tree` | Exact selected commit and tree |
| `base_sha`, `base_tree` | Exact reviewed ancestor commit and tree |
| `compiled_policy_sha256` | Unchanged base policy digest |
| `adapter_sha256` | Reviewed public `/usr/bin/bwrap` binary digest |
| `argv` | Exact argument array, beginning with an executable runtime file |
| `runtime_files` | Exact `{path, sha256, executable}` rows |
| `timeout_seconds` | Positive finite number, at most 300 seconds |
| `max_output_bytes` | Positive integer, at most 1,048,576 bytes |

Profile bytes are at most 64 KiB. Runtime rows are unique, at most 128, and
use relative `bin/`, `lib/` or `lib64/` paths. The runtime bundle contains only
reviewed public tools/libraries, never owner data or credentials. Missing tools,
loaders or dependencies fail; no package installation, host fallback or implicit
runtime discovery occurs. Every runtime file is pinned individually, and their
total captured size is at most 128 MiB. A changed command, profile, source,
base, policy, adapter or runtime byte requires fresh qualification. There is no
reusable PASS cache in this adapter.

## Actual execution boundary

The projection's existing loose-object, ancestor, strict policy and opaque
payload rules run first. The executor reopens only its admitted, hash-verified
Git object copies. It never treats a mutable projected working file as source
authority. Explicit runtime files are captured through no-follow directory/file
descriptors, checked for ownership, hard links, permissions, size, stability and
digest. Source, runtime and adapter bytes become sealed memory files before
execution; there are no pathname binds into the original host.

Bubblewrap creates user, PID, mount, network, IPC, UTS and cgroup namespaces,
drops all capabilities, disables nested user namespaces and clears the inherited
environment. The command receives only `PATH=/bin`, `HOME=/tmp`, null stdin and
bounded stdout/stderr pipes. It sees immutable admitted source at `/source`,
immutable declared runtime paths, an empty private writable `/tmp`, and standard
limited `/dev` devices. Source and runtime directories are remounted read-only,
so their entries cannot be replaced. No host `/proc` or socket is exposed.

The collector counts and discards raw output, stops at the profile deadline or
output bound and settles only its created process group. It observes terminal
status without reaping the group leader until after signalling, so that PID
cannot be recycled into another process group first. Cleanup failure cannot
produce successful evidence. The bubblewrap namespace supervisor confines and
settles its command descendants. No PID search, process-name cleanup or unknown
process termination is used.

## Result and remaining limits

Only a successful actual adapter command sets
`source_execution_closure_verified=true` in a separate `source-execution/v1`
result. Errors or unavailable namespace/runtime support never fall back to host
execution. A nonzero command and adapter failure share a conservative failed
category; output content is not retained to distinguish them. Missing/error
inputs throw fixed error categories without private names or payloads.

Every result keeps `full_repository_qualified`, `native_gate_qualified`,
`admission_authorized`, `production_authorized` and `cache_reusable` false.
The result binds all source/base/policy/profile/adapter pins and aggregate
elapsed/output counts. It is local source qualification, not GitHub/provider
attestation. The projection's own nonauthorizing record remains unchanged.

The initial runtime proof targets Linux x86-64 with actual bubblewrap, shell,
loader and libc bytes. Other architectures/platforms, packed/shared object
stores, product dependency runtimes, installed owner callers, native CI and
production are unqualified until separately tested. The adapter is not a
replacement for owner resource limits or general hostile-code compute hosting.

Run the deterministic tests without credentials:

```sh
python3 -B -m unittest scripts/tests/test_source_projection.py scripts/tests/test_source_executor.py -v
```

Tests require real namespace support and the declared public Linux toolchain;
unavailable isolation is a failure, never a skipped successful proof. Synthetic
host controls demonstrate that the same network probe reaches the host TCP and
Unix listeners outside isolation but cannot reach either inside it. Other tests
cover omitted inputs, host sentinel reads/writes, environment and `/proc`
escapes, read-only directories, sealed bytes, changed pins, symlink/hard-link
refusal, timeout/output failure and unrelated-neighbor preservation.

Distribution is additive: publish the reviewed projection and executor readers
before installing any owner caller. Keep all old controls until that owner's
actual selected runtime and transport pass qualification. Removing this unused
library is reversible and does not alter any v1 control or receipt.
