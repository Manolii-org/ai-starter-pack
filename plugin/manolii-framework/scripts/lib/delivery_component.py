"""Deterministic release-record component inputs; no execution authority.

Only fixed public component paths are read. Provider credentials, bindings,
queues and owner evidence do not form part of this component.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import secrets
import subprocess
import sys

SCHEMA = "delivery-component-inputs/v1"
ACTION = ".github/actions/validate-release-manifest"
INPUTS = ACTION + "/source-inputs.json"
MIRROR_SOURCE = "registry/.delivery-component-source.json"
VERIFIER = "scripts/lib/delivery_component.py"
FILES = tuple(sorted((
    ACTION + "/action.yml", ACTION + "/release_manifest.py",
    ACTION + "/release_state.py", ACTION + "/github_approval.py",
    "schemas/release-adoption.v1.schema.json",
    "schemas/release-manifest.v1.schema.json",
    "schemas/release-manifest.v2.schema.json",
    "schemas/release-source-policy.v1.schema.json",
)))
MAX_FILE = 1_000_000
MAX_TREE_ENTRIES = 10_000
MAX_TREE_DEPTH = 128
PUBLIC_INPUTS = ("registry/platform", "registry/plugins.json",
                 "scripts/registry-lint.py", "schemas/registry-scope.schema.json",
                 "registry/secrets-allowlist.txt", "scripts/bootstrap-mirror.py",
                 "scripts/lib/delivery_component.py", "scripts/ai-resolve.py")


class ComponentError(ValueError):
    """Fixed failure categories, never owner content or credential values."""


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode()


def digest(value):
    return hashlib.sha256(b"delivery-component-inputs/v1\0" + encoded(value)).hexdigest()


def _details(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _verify_directory_bindings(bindings):
    for parent, name, descriptor, expected in bindings:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (_directory_identity(named) != expected
                or _directory_identity(os.fstat(descriptor)) != expected):
            raise ComponentError("component_directory_binding_changed")


def _open_bound_directory(parent, name, stack, bindings, *, create=False, observed=None):
    try:
        named = observed if observed is not None else os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o755, dir_fd=parent)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(named.st_mode):
        # Keep the original directory-open OSError contract for old callers.
        raise NotADirectoryError("non_directory_component_ancestor")
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=parent)
    stack.callback(os.close, descriptor)
    expected = _directory_identity(named)
    if _directory_identity(os.fstat(descriptor)) != expected:
        raise ComponentError("component_directory_changed_before_open")
    bindings.append((parent, name, descriptor, expected))
    return descriptor


@contextmanager
def _directory_scope(root, *, create=False):
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")):
        raise ComponentError("qualified_directory_reader_unavailable")
    path = Path(root).absolute()
    if ".." in path.parts:
        raise ComponentError("noncanonical_component_root")
    with ExitStack() as stack:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, descriptor)
        anchor, expected_anchor = descriptor, _directory_identity(os.fstat(descriptor))
        bindings = []
        for part in path.parts[1:]:
            descriptor = _open_bound_directory(descriptor, part, stack, bindings, create=create)

        def verify():
            if (_directory_identity(os.fstat(anchor)) != expected_anchor
                    or _directory_identity(os.stat(path.anchor, follow_symlinks=False)) != expected_anchor):
                raise ComponentError("component_root_anchor_changed")
            _verify_directory_bindings(bindings)

        verify()
        yield descriptor, verify
        verify()


@contextmanager
def _retained_directory_scope(root, binding=None):
    if binding is None:
        with _directory_scope(root) as captured:
            yield captured
    else:
        binding[1]()
        yield binding
        binding[1]()


@contextmanager
def _relative_directory_scope(binding, parts):
    directory, verify_root = binding
    with ExitStack() as scope:
        bindings = []
        for name in parts:
            verify_root()
            directory = _open_bound_directory(directory, name, scope, bindings)

        def verify():
            verify_root()
            _verify_directory_bindings(bindings)

        verify()
        yield directory, verify
        verify()


@contextmanager
def _directory(root, *, create=False):
    with _directory_scope(root, create=create) as (descriptor, _):
        yield descriptor


def _public_tree_files(root, *, _root_binding=None):
    """Enumerate only retained no-follow public directory descriptors.

    Root/ancestor aliases are refused before scandir. Parent replacement never
    redirects enumeration; changed bindings/membership hold the observation.
    These repeated observations are not an atomic filesystem snapshot.
    """
    found, entries = set(), 0
    with _retained_directory_scope(root, _root_binding) as (directory, verify_root), ExitStack() as scope:
        bindings = []
        for name in ("registry", "platform"):
            directory = _open_bound_directory(directory, name, scope, bindings)

        def verify():
            verify_root()
            _verify_directory_bindings(bindings)

        def walk(descriptor, prefix, depth):
            nonlocal entries
            if depth > MAX_TREE_DEPTH:
                raise ComponentError("public_tree_depth_exceeds_bound")
            verify()
            before = _details(os.fstat(descriptor))
            leaves = []
            with os.scandir(descriptor) as children:
                for entry in children:
                    entries += 1
                    if entries > MAX_TREE_ENTRIES:
                        raise ComponentError("public_tree_inventory_exceeds_bound")
                    verify()
                    named = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(named.st_mode):
                        with ExitStack() as child_scope:
                            child = _open_bound_directory(descriptor, entry.name, child_scope, bindings, observed=named)
                            try:
                                walk(child, prefix + "/" + entry.name, depth + 1)
                            finally:
                                bindings.pop()
                    elif stat.S_ISREG(named.st_mode):
                        found.add(prefix + "/" + entry.name)
                        leaves.append((entry.name, _details(named)))
                    else:
                        raise ComponentError("linked_or_nonregular_public_tree_input")
            verify()
            if _details(os.fstat(descriptor)) != before:
                raise ComponentError("public_tree_changed_during_enumeration")
            for name, expected in leaves:
                if _details(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) != expected:
                    raise ComponentError("public_tree_leaf_changed_during_enumeration")

        walk(directory, "registry/platform", 0)
    return found


def read_file(root, name, *, _root_binding=None):
    """Bounded no-follow reads of caller-selected public inputs only."""
    if (not isinstance(name, str) or name.startswith("/")
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ComponentError("invalid_component_input_path")
    with _retained_directory_scope(root, _root_binding) as (directory, _), ExitStack() as stack:
        parts = name.split("/")
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
            stack.callback(os.close, directory)
        named = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
        stack.callback(os.close, descriptor)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 0 <= before.st_size <= MAX_FILE or _details(named) != _details(before)):
            raise ComponentError("unsafe_component_input")
        raw = bytearray()
        while len(raw) <= before.st_size:
            part = os.read(descriptor, min(65536, before.st_size + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        if (len(raw) != before.st_size or _details(before) != _details(os.fstat(descriptor))
                or _details(before) != _details(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False))):
            raise ComponentError("component_input_changed")
        mode = "100755" if before.st_mode & 0o111 else "100644"
        return bytes(raw), mode


def _row(name, raw, mode):
    return {"path": name, "mode": mode, "sha256": hashlib.sha256(raw).hexdigest()}


def capture(root, *, _root_binding=None):
    first = [_row(name, *read_file(root, name, _root_binding=_root_binding)) for name in FILES]
    if first != [_row(name, *read_file(root, name, _root_binding=_root_binding)) for name in FILES]:
        raise ComponentError("component_inputs_changed")
    return {"schema": SCHEMA, "component": "release-record", "input_digest": digest(first),
            "inputs": first}


def snapshot(root, name):
    try:
        return read_file(root, name)
    except FileNotFoundError:
        return None


def write_public_file(root, name, data, mode, expected):
    """Write selected component files only; caller holds the existing repo lock.

    Participating writers serialize; repeated snapshots and rename are not an
    inode/content CAS against unrelated writers. Failed writes retain their
    owned temporary inode, never unlink or restore a pathname automatically.
    """
    if name not in (*FILES, INPUTS, VERIFIER, MIRROR_SOURCE) or mode not in ("100644", "100755"):
        raise ComponentError("unowned_component_output")
    if snapshot(root, name) != expected:
        raise ComponentError("component_destination_changed")
    if expected == (data, mode):
        return False
    with _directory(root, create=True) as directory, ExitStack() as stack:
        parts = name.split("/")
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=directory)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
            directory = child
            stack.callback(os.close, directory)
        temporary = ".delivery-component-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        stack.callback(os.close, descriptor)
        try:
            offset = 0
            while offset < len(data):
                count = os.write(descriptor, data[offset:])
                if count <= 0:
                    raise ComponentError("component_write_incomplete")
                offset += count
            os.fchmod(descriptor, 0o755 if mode == "100755" else 0o644)
            os.fsync(descriptor)
            if snapshot(root, name) != expected:
                raise ComponentError("component_destination_changed")
            os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        except BaseException as primary:
            try:
                owned = os.fstat(descriptor)
                named = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                if (owned.st_dev, owned.st_ino) == (named.st_dev, named.st_ino):
                    os.fchmod(descriptor, 0o600)
            except OSError:
                primary.add_note("component_temporary_privacy_or_location_unknown")
            raise
    return True


def write_inputs(root):
    """Normal generator output; excludes this record from its own input digest."""
    return write_public_file(root, INPUTS, encoded(capture(root)), "100644", snapshot(root, INPUTS))


def verify(root, *, expected_digest=None, _root_binding=None):
    raw, mode = read_file(root, INPUTS, _root_binding=_root_binding)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ComponentError("ambiguous_component_record")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ComponentError("invalid_component_record")))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ComponentError("invalid_component_record") from error
    if (mode != "100644" or value != capture(root, _root_binding=_root_binding)
            or (expected_digest is not None and value.get("input_digest") != expected_digest)):
        raise ComponentError("component_record_differs_from_inputs")
    return {"schema": SCHEMA, "component": "release-record", "input_digest": value["input_digest"],
            "input_files": len(FILES), "admission_authorized": False,
            "production_authorized": False}


def _git(pack, *args, git_program="git", _root_binding=None):
    # Qualify the RAW root and every ancestor before Git can read even index
    # metadata. Keep the parent's descriptor alive; no host descriptor is
    # inherited by Git. Its actual cwd is the retained inode, not a pathname
    # that another process can redirect between repeated observations.
    if _root_binding is None:
        with _directory_scope(pack) as binding:
            return _git(pack, *args, git_program=git_program, _root_binding=binding)
    descriptor, verify_root = _root_binding
    verify_root()
    cwd = f"/proc/{os.getpid()}/fd/{descriptor}"
    if _directory_identity(os.stat(cwd)) != _directory_identity(os.fstat(descriptor)):
        raise ComponentError("retained_git_root_unavailable")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
               GIT_GRAFT_FILE=os.devnull, GIT_SHALLOW_FILE=os.devnull)
    try:
        return subprocess.check_output(
            [git_program, "--no-replace-objects", "-c", "core.commitGraph=false",
             "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, *args],
            cwd=cwd, close_fds=True, env=env, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise ComponentError("immutable_pack_identity_unavailable") from error
    finally:
        verify_root()


def _public_path(name):
    return any(name == prefix or name.startswith(prefix + "/") for prefix in PUBLIC_INPUTS)


def pack_source(pack, *, component_only=False, git_program="git"):
    """Pin public bootstrap inputs to HEAD/index/raw bytes, never owner payloads.

    This records immutable content consumed; it grants no provider/private repo
    identity, publication, native protection or credential authority.
    """
    with _directory_scope(pack) as binding:
        return _pack_source_bound(Path(pack), component_only, git_program, binding)


def _pack_source_bound(pack, component_only, git_program, binding):
    call = lambda *args: _git(pack, *args, git_program=git_program, _root_binding=binding)
    chosen = (("scripts/bootstrap-mirror.py", VERIFIER, "scripts/ai-resolve.py") + tuple(
        "registry/platform/framework/" + name for name in (*FILES, INPUTS, VERIFIER))
        if component_only else PUBLIC_INPUTS)
    selected_path = lambda name: any(name == prefix or name.startswith(prefix + "/") for prefix in chosen)
    head = call("rev-parse", "HEAD").decode().strip()
    tree = call("rev-parse", "HEAD^{tree}").decode().strip()
    algorithm = call("rev-parse", "--show-object-format").decode().strip()
    if algorithm not in ("sha1", "sha256") or len(head) != (40 if algorithm == "sha1" else 64):
        raise ComponentError("unsupported_pack_identity")
    index = {}
    for row in call("ls-files", "--stage", "-z").split(b"\0"):
        if row:
            meta, name = row.split(b"\t", 1)
            mode, identity, stage = meta.split()
            name = os.fsdecode(name)
            if selected_path(name):
                if stage != b"0" or name in index:
                    raise ComponentError("ambiguous_pack_index")
                index[name] = (mode.decode(), identity.decode())
    selected = {}
    for row in call("ls-tree", "-rz", "HEAD", "--", *chosen).split(b"\0"):
        if row:
            meta, name = row.split(b"\t", 1)
            mode, kind, identity = meta.split()
            name = os.fsdecode(name)
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ComponentError("nonregular_pack_input")
            selected[name] = (mode.decode(), identity.decode())
    if not selected or selected != index:
        raise ComponentError("pack_index_differs_from_immutable_inputs")
    if component_only:
        if set(selected) != set(chosen):
            raise ComponentError("missing_pinned_component_input")
    else:
        # Only the canonical PUBLIC platform tree is enumerated. No universe or
        # mirror-owned entry/payload is opened here.
        actual = _public_tree_files(pack, _root_binding=binding)
        if actual != {name for name in selected if name.startswith("registry/platform/")}:
            raise ComponentError("untracked_or_missing_public_platform_input")
    inputs = []
    for name, (mode, identity) in sorted(selected.items()):
        raw, actual_mode = read_file(pack, name, _root_binding=binding)
        hashed = hashlib.new(algorithm, usedforsecurity=False)
        hashed.update(("blob " + str(len(raw)) + "\0").encode() + raw)
        if actual_mode != mode or hashed.hexdigest() != identity:
            raise ComponentError("pack_input_differs_from_immutable_source")
        inputs.append({**_row(name, raw, mode), "git_blob": identity})
    with _relative_directory_scope(binding, ("registry", "platform", "framework")) as component_binding:
        component = verify(pack / "registry/platform/framework", _root_binding=component_binding)
    result = {"schema": "registry-platform-source/v1", "pack_sha": head, "pack_tree": tree,
              "git_object_format": algorithm, "selection": "delivery-component" if component_only else "platform",
              "input_digest": digest(inputs), "inputs": inputs,
              "component_input_digest": component["input_digest"],
              "admission_authorized": False, "production_authorized": False}
    if call("rev-parse", "HEAD").decode().strip() != head:
        raise ComponentError("pack_source_head_changed")
    return result


def verify_copied_platform(root, source):
    # This is only the PUBLIC platform tree copied by full refresh. No owner
    # universe, mixed index or private payload is enumerated here.
    if source["selection"] == "platform":
        actual = _public_tree_files(root)
        expected = {row["path"] for row in source["inputs"]
                    if row["path"].startswith("registry/platform/")}
        if actual != expected:
            raise ComponentError("extra_or_missing_copied_platform_input")
    for item in source["inputs"]:
        name = item["path"]
        if name.startswith("registry/platform/"):
            if _row(name, *read_file(root, name)) != {key: item[key] for key in ("path", "mode", "sha256")}:
                raise ComponentError("copied_platform_differs_from_pinned_inputs")
    return verify(Path(root) / "registry/platform/framework",
                  expected_digest=source["component_input_digest"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-input-digest")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.root, expected_digest=args.expected_input_digest), sort_keys=True))
    except (OSError, ComponentError):
        print("delivery component unavailable or changed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
