"""Materialize committed, policy-admitted source without reading opaque blobs.

This is deliberately not a validator or executor. The initial adapter supports
owned, primary loose-object repositories only. It never invokes Git on an input
repository, reads its configuration, follows alternates, opens packs, or reads
working-tree payloads. Packed/shared/worktree repositories require a separately
qualified object adapter; they are held rather than silently copied.

Policy authority is an independent caller obligation: repository name, exact
base/source IDs and the compiled policy SHA-256 must come from trusted owner
preparation, not candidate-provided arguments. The policy is read only from that
base and must be unchanged in the selected source. Neither this contract nor a
changed cwd/environment establishes command closure on the original host.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from types import MappingProxyType
from typing import Callable, Iterator, Mapping
import unicodedata
import zlib

POLICY_PATH = ".ai/source-scope-policy.json"
SCHEMA = "source-projection/v1"
MAX_OBJECT = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_HISTORY = 256
# Match the reviewed source-scope guard/schema; never fetch live policy limits.
MAX_POLICY_BYTES = 16 * 1024
MAX_POLICY_PATHS = 256
MAX_POLICY_PATH_CHARACTERS = 500
_DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class ProjectionError(ValueError):
    """A fixed category; opaque names or payloads never enter the exception."""

    def __init__(self, code: str, *, cleanup_root: Path | None = None):
        self.cleanup_root = cleanup_root
        self.code = code
        super().__init__(code)


def _require(condition: object, code: str) -> None:
    if not condition:
        raise ProjectionError(code)


def _relative(value: str, *, directory_rule: bool = False, tree_entry: bool = False) -> str:
    _require(isinstance(value, str), "invalid_path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ProjectionError("invalid_path") from exc
    body = value[:-1] if directory_rule and value.endswith("/") else value
    bounded = (0 < len(value) <= MAX_POLICY_PATH_CHARACTERS if directory_rule
               else 0 < len(encoded) <= 1024)
    _require(bounded and not body.startswith("/")
             and not any(ord(char) < 32 or ord(char) == 127 for char in value)
             and not any(char in value for char in ("\\" if tree_entry else "\\*?[]"))
             and all(part and part not in {".", ".."} and part.casefold() != ".git"
                     for part in body.split("/")), "invalid_path")
    return value


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    _require(os.name == "posix" and bool(getattr(os, "O_NOFOLLOW", 0))
             and bool(getattr(os, "O_DIRECTORY", 0)), "descriptor_platform_unavailable")
    absolute = Path(os.path.abspath(path))
    with ExitStack() as stack:
        fd = os.open("/", _DIRECTORY)
        stack.callback(os.close, fd)
        for name in absolute.parts[1:]:
            fd = os.open(name, _DIRECTORY, dir_fd=fd)
            stack.callback(os.close, fd)
        _require(os.fstat(fd).st_uid == os.getuid(), "directory_not_owned")
        yield fd


def _details(fd: int) -> tuple:
    value = os.fstat(fd)
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _oid(kind: str, payload: bytes, algorithm: str) -> str:
    return hashlib.new(algorithm, kind.encode() + b" " + str(len(payload)).encode()
                       + b"\0" + payload).hexdigest()


@dataclass(frozen=True)
class Selection:
    source: str
    tree: str
    base: str
    base_tree: str

    def algorithm(self) -> str:
        values = (self.source, self.tree, self.base, self.base_tree)
        _require(all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item)
                     for item in values) and len({len(item) for item in values}) == 1,
                 "exact_object_id_required")
        return "sha1" if len(self.source) == 40 else "sha256"


class _LooseObjects:
    def __init__(self, objects_fd: int, algorithm: str):
        self.fd, self.algorithm = objects_fd, algorithm
        self.captured: dict[str, tuple[str, bytes]] = {}
        self.total = 0

    def read(self, oid: str, kind: str, limit: int = MAX_OBJECT) -> bytes:
        if oid in self.captured:
            previous, payload = self.captured[oid]
            _require(previous == kind, "object_type_changed")
            return payload
        with ExitStack() as stack:
            prefix = os.open(oid[:2], _DIRECTORY, dir_fd=self.fd)
            stack.callback(os.close, prefix)
            fd = os.open(oid[2:], _FILE, dir_fd=prefix)
            stack.callback(os.close, fd)
            before = _details(fd)
            _require(stat.S_ISREG(before[2]) and before[3] == os.getuid() and before[4] == 1
                     and not before[2] & 0o022 and before[5] <= MAX_OBJECT * 2,
                     "unsafe_object_file")
            decoder, header = zlib.decompressobj(), bytearray()
            # Inspect only the type/size header before permitting any payload.
            header_reads = 0
            while not header.endswith(b"\0"):
                chunk = decoder.unconsumed_tail or os.read(fd, 1)
                header_reads += 1
                _require(bool(chunk) and len(header) < 128 and header_reads <= 8192,
                         "invalid_object_header")
                header.extend(decoder.decompress(chunk, 1))
            match = re.fullmatch(rb"(commit|tree|blob) (0|[1-9][0-9]*)\0", bytes(header))
            if match is None or match[1].decode() != kind:
                raise ProjectionError("object_type_changed")
            size = int(match[2])
            _require(size <= limit and self.total + size <= MAX_TOTAL, "object_budget_exceeded")
            payload = bytearray()
            while not decoder.eof:
                chunk = decoder.unconsumed_tail or os.read(fd, 65536)
                _require(bool(chunk), "truncated_object")
                payload.extend(decoder.decompress(chunk, size - len(payload) + 1))
                _require(len(payload) <= size, "object_size_changed")
            _require(not decoder.unused_data and not os.read(fd, 1) and len(payload) == size,
                     "object_size_changed")
            _require(_details(fd) == before, "object_changed_during_capture")
        value = bytes(payload)
        _require(_oid(kind, value, self.algorithm) == oid, "object_digest_changed")
        self.total += len(value)
        self.captured[oid] = (kind, value)
        return value


def _commit(store: _LooseObjects, oid: str) -> tuple[str, list[str]]:
    raw = store.read(oid, "commit", 1_000_000)
    headers, separator, _ = raw.partition(b"\n\n")
    _require(bool(separator), "invalid_commit")
    trees, parents = [], []
    for line in headers.splitlines():
        if line.startswith(b"tree "):
            trees.append(line[5:].decode("ascii"))
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii"))
    length = 40 if store.algorithm == "sha1" else 64
    lines = headers.splitlines()
    _require(len(trees) == 1 and lines[0] == b"tree " + trees[0].encode()
             and sum(line.startswith(b"author ") for line in lines) == 1
             and sum(line.startswith(b"committer ") for line in lines) == 1
             and len(set(parents)) == len(parents)
             and all(re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", item)
                     for item in trees + parents), "invalid_commit")
    return trees[0], parents


def _tree(store: _LooseObjects, root: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    stack = [("", root, 0)]
    width = 20 if store.algorithm == "sha1" else 32
    visited = 0
    while stack:
        prefix, oid, depth = stack.pop()
        _require(depth <= 128 and visited < MAX_ENTRIES, "tree_budget_exceeded")
        visited += 1
        raw, offset, names = store.read(oid, "tree"), 0, set()
        while offset < len(raw):
            space, zero = raw.find(b" ", offset), raw.find(b"\0", offset)
            _require(offset < space < zero and zero + 1 + width <= len(raw), "invalid_tree")
            mode = raw[offset:space].decode("ascii")
            name = raw[space + 1:zero].decode("utf-8")
            _relative(name, tree_entry=True)
            _require("/" not in name and name not in names, "invalid_tree")
            names.add(name)
            value = raw[zero + 1:zero + 1 + width].hex()
            offset = zero + 1 + width
            path = prefix + name
            _relative(path, tree_entry=True)
            _require(mode in {"40000", "100644", "100755", "120000", "160000"}, "invalid_tree_mode")
            if mode == "40000":
                stack.append((path + "/", value, depth + 1))
            else:
                _require(len(entries) < MAX_ENTRIES, "tree_budget_exceeded")
                entries[path] = (mode, value)
    return entries


def _policy(raw: bytes, expected_sha256: str, repository: str) -> dict:
    _require(isinstance(expected_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
             and isinstance(raw, bytes) and len(raw) <= MAX_POLICY_BYTES
             and hashlib.sha256(raw).hexdigest() == expected_sha256,
             "compiled_policy_digest_changed")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "invalid_policy")
            value[key] = item
        return value

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ProjectionError("invalid_policy")))
    _require(isinstance(value, dict) and set(value) == {
        "schema_version", "repository", "scope", "allowed_paths", "protected_paths"}
        and type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["repository"] == repository and isinstance(repository, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        and value["scope"] == "source-only", "invalid_policy")
    for key in ("allowed_paths", "protected_paths"):
        items = value[key]
        _require(isinstance(items, list) and bool(items)
                 and all(isinstance(item, str) for item in items)
                 and len(items) <= MAX_POLICY_PATHS and len(set(items)) == len(items),
                 "invalid_policy")
        for item in items:
            _relative(item, directory_rule=True)
    return value


def _matches(path: str, rules: list[str]) -> bool:
    return any(path.startswith(rule) if rule.endswith("/") else path == rule for rule in rules)


def _classify(entries: dict[str, tuple[str, str]], policy: dict) -> tuple[dict, dict]:
    allowed: dict[str, tuple[str, str]] = {}
    opaque: dict[str, tuple[str, str]] = {}
    folded: dict[str, str] = {}
    for path, value in entries.items():
        if path == POLICY_PATH:
            allowed[path] = value
        elif _matches(path, policy["protected_paths"]):
            opaque[path] = value
        elif _matches(path, policy["allowed_paths"]):
            allowed[path] = value
        else:
            raise ProjectionError("unclassified_input")
        if path in allowed:
            _require(value[0] in {"100644", "100755"}, "unsupported_source_mode")
            parts = path.split("/")
            for length in range(1, len(parts) + 1):
                prefix = "/".join(parts[:length])
                key = unicodedata.normalize("NFC", prefix).casefold()
                _require(key not in folded or folded[key] == prefix, "source_path_collision")
                folded[key] = prefix
    return allowed, opaque


def _write(root_fd: int, name: str, payload: bytes, mode: int = 0o600) -> None:
    _relative(name, tree_entry=True)
    with ExitStack() as stack:
        directory = root_fd
        for part in name.split("/")[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            directory = os.open(part, _DIRECTORY, dir_fd=directory)
            stack.callback(os.close, directory)
            _require(os.fstat(directory).st_uid == os.getuid()
                     and not os.fstat(directory).st_mode & 0o077, "unsafe_destination")
        fd = os.open(name.split("/")[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=directory)
        stack.callback(os.close, fd)
        before = _details(fd)
        view = memoryview(payload)
        while view:
            size = os.write(fd, view)
            _require(size > 0, "incomplete_destination_write")
            view = view[size:]
        os.lseek(fd, 0, os.SEEK_SET)
        captured = bytearray()
        while len(captured) <= len(payload):
            block = os.read(fd, min(65536, len(payload) + 1 - len(captured)))
            if not block:
                break
            captured.extend(block)
        after = _details(fd)
        _require(bytes(captured) == payload and before[:5] == after[:5]
                 and after[4] == 1 and after[2] & 0o777 == mode,
                 "destination_changed_during_write")


def _verify(root_fd: int, name: str, expected: bytes, mode: int) -> None:
    """Repeat confined output observations immediately before publication."""
    with ExitStack() as stack:
        directory = root_fd
        for part in name.split("/")[:-1]:
            directory = os.open(part, _DIRECTORY, dir_fd=directory)
            stack.callback(os.close, directory)
            current = os.fstat(directory)
            _require(current.st_uid == os.getuid() and not current.st_mode & 0o077,
                     "unsafe_destination")
        fd = os.open(name.split("/")[-1], _FILE, dir_fd=directory)
        stack.callback(os.close, fd)
        before = _details(fd)
        _require(stat.S_ISREG(before[2]) and before[3] == os.getuid() and before[4] == 1
                 and before[2] & 0o777 == mode and before[5] == len(expected),
                 "destination_changed_after_capture")
        value = bytearray()
        while len(value) <= len(expected):
            block = os.read(fd, min(65536, len(expected) + 1 - len(value)))
            if not block:
                break
            value.extend(block)
        current = os.stat(name.split("/")[-1], dir_fd=directory, follow_symlinks=False)
        _require(bytes(value) == expected and _details(fd) == before
                 and (current.st_dev, current.st_ino) == before[:2],
                 "destination_changed_after_capture")


@dataclass(frozen=True)
class Projection:
    root: Path
    source: Path
    metadata: Path
    record: Mapping

    def require_execution_closure(self) -> None:
        raise ProjectionError("execution_boundary_unqualified")


def project_committed_source(repository: Path, *, selection: Selection, repository_name: str,
                             compiled_policy_sha256: str, destination_parent: Path,
                             _admit_sources: Callable[[Mapping[str, tuple[str, str]]], None] | None = None) -> Projection:
    """Capture committed source only; do not execute commands in the result.

    Source and destination paths may not contain symlinks. The destination parent
    must already be private/owned and outside the input repository. Failure may
    leave an unpublished private partial directory; no recursive cleanup of a
    concurrently replaceable pathname is attempted. Errors expose that newly
    created root separately as cleanup_root. The internal metadata admission
    hook can only refuse already-classified source before payload capture; it
    does not expand policy or grant execution. No legacy PASS is produced.
    """
    cleanup_root: Path | None = None
    try:
        _require(isinstance(selection, Selection), "exact_object_id_required")
        algorithm = selection.algorithm()
        _require(Path(repository).is_absolute() and ".." not in Path(repository).parts
                 and Path(destination_parent).is_absolute() and ".." not in Path(destination_parent).parts,
                 "absolute_descriptor_path_required")
        source_path = Path(os.path.abspath(repository))
        parent_path = Path(os.path.abspath(destination_parent))
        _require(not parent_path.is_relative_to(source_path), "destination_inside_source")
        with ExitStack() as stack:
            source_fd = stack.enter_context(_directory(source_path))
            parent_fd = stack.enter_context(_directory(parent_path))
            _require(not os.fstat(parent_fd).st_mode & 0o077, "destination_not_private")
            git_fd = os.open(".git", _DIRECTORY, dir_fd=source_fd)
            stack.callback(os.close, git_fd)
            objects = os.open("objects", _DIRECTORY, dir_fd=git_fd)
            stack.callback(os.close, objects)
            for directory, forbidden in ((git_fd, "commondir"),):
                try:
                    os.stat(forbidden, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ProjectionError("shared_object_store_unqualified")
            for name in ("info", "pack"):
                fd = os.open(name, _DIRECTORY, dir_fd=objects)
                stack.callback(os.close, fd)
                names = os.listdir(fd)
                _require(not (name == "pack" and names)
                         and not (name == "info" and {"alternates", "http-alternates"} & set(names)),
                         "packed_or_alternate_store_unqualified")
            store = _LooseObjects(objects, algorithm)
            tree, _ = _commit(store, selection.source)
            base_tree, _ = _commit(store, selection.base)
            _require((tree, base_tree) == (selection.tree, selection.base_tree), "selected_tree_changed")
            pending = [selection.source]
            seen: set[str] = set()
            while pending and selection.base not in seen:
                current = pending.pop()
                if current in seen:
                    continue
                _require(len(seen) < MAX_HISTORY, "history_metadata_budget_exceeded")
                seen.add(current)
                _, ancestors = _commit(store, current)
                pending.extend(ancestors)
            _require(selection.base in seen, "base_not_selected_ancestor")
            baseline = _tree(store, base_tree)
            selected = _tree(store, tree)
            binding = baseline.get(POLICY_PATH)
            _require(binding is not None and binding[0] == "100644", "compiled_policy_unavailable")
            assert binding is not None  # established before any policy payload access
            _require(selected.get(POLICY_PATH) == binding, "candidate_policy_changed")
            for entries in (baseline, selected):
                _require([path for path, value in entries.items() if value[1] == binding[1]] == [POLICY_PATH],
                         "compiled_policy_alias_ambiguous")
            raw_policy = store.read(binding[1], "blob", MAX_POLICY_BYTES)
            policy = _policy(raw_policy, compiled_policy_sha256, repository_name)
            old_sources, old_opaque = _classify(baseline, policy)
            sources, opaque = _classify(selected, policy)
            _require(old_opaque == opaque, "opaque_input_changed")
            opaque_ids = {value[1] for value in opaque.values()}
            _require(not opaque_ids & {value[1] for value in [*old_sources.values(), *sources.values()]},
                     "ambiguous_object_scope")
            # A trusted executor may only narrow admission using tree metadata.
            # This runs before source blob reads or any destination creation.
            if _admit_sources is not None:
                _admit_sources(MappingProxyType(sources))
            for _, oid in [*old_sources.values(), *sources.values()]:
                store.read(oid, "blob")

            name = "source-projection-" + secrets.token_hex(12)
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            cleanup_root = parent_path / name
            root_fd = os.open(name, _DIRECTORY, dir_fd=parent_fd)
            stack.callback(os.close, root_fd)
            root_identity = _details(root_fd)[:2]
            for folder in ("source", "metadata.git"):
                os.mkdir(folder, 0o700, dir_fd=root_fd)
            files_fd = os.open("source", _DIRECTORY, dir_fd=root_fd)
            stack.callback(os.close, files_fd)
            metadata_fd = os.open("metadata.git", _DIRECTORY, dir_fd=root_fd)
            stack.callback(os.close, metadata_fd)
            outputs = []
            for path, (mode, oid) in sources.items():
                payload, permissions = store.captured[oid][1], 0o700 if mode == "100755" else 0o600
                _write(files_fd, path, payload, permissions)
                outputs.append((files_fd, path, payload, permissions))
            for oid, (kind, payload) in store.captured.items():
                # Every captured object is a verified metadata or admitted source
                # object. Original packs, alternates, refs and config are absent.
                raw = kind.encode() + b" " + str(len(payload)).encode() + b"\0" + payload
                path, compressed = "objects/" + oid[:2] + "/" + oid[2:], zlib.compress(raw)
                _write(metadata_fd, path, compressed)
                outputs.append((metadata_fd, path, compressed, 0o600))
            config = "[core]\nrepositoryformatversion = " + ("0" if algorithm == "sha1" else "1")
            config += "\nbare = true\nhooksPath = /dev/null\nfsmonitor = false\n"
            if algorithm == "sha256":
                config += "[extensions]\nobjectformat = sha256\n"
            _write(metadata_fd, "config", config.encode())
            _write(metadata_fd, "HEAD", (selection.source + "\n").encode())
            _write(metadata_fd, "shallow", (selection.source + "\n" + selection.base + "\n").encode())
            outputs.extend([(metadata_fd, "config", config.encode(), 0o600),
                            (metadata_fd, "HEAD", (selection.source + "\n").encode(), 0o600),
                            (metadata_fd, "shallow", (selection.source + "\n" + selection.base + "\n").encode(), 0o600)])
            os.mkdir("refs", 0o700, dir_fd=metadata_fd)
            # This intentionally remains a bare metadata projection, not a Git
            # working-tree checkout that old full-repository readers can admit.
            fd = os.open(".git", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=files_fd)
            try:
                os.write(fd, b"gitdir: ../metadata.git\n")
            finally:
                os.close(fd)
            outputs.append((files_fd, ".git", b"gitdir: ../metadata.git\n", 0o600))
            record = {"schema": SCHEMA, "scope": "committed-source-only",
                      "status": "materialized-not-qualified", "repository": repository_name,
                      "source_sha": selection.source, "source_tree": tree,
                      "base_sha": selection.base, "base_tree": base_tree,
                      "compiled_policy_sha256": compiled_policy_sha256,
                      "source_files": len(sources), "opaque_entries": len(opaque),
                      "opaque_payloads_materialized": 0, "execution_authorized": False,
                      "execution_closure_verified": False, "native_gate_qualified": False,
                      "full_repository_qualified": False, "cache_reusable": False}
            encoded_record = json.dumps(record, sort_keys=True).encode() + b"\n"
            _write(root_fd, "projection.json", encoded_record)
            outputs.append((root_fd, "projection.json", encoded_record, 0o600))
            for directory, path, payload, permissions in outputs:
                _verify(directory, path, payload, permissions)
            for folder, expected in (("source", files_fd), ("metadata.git", metadata_fd)):
                child = os.stat(folder, dir_fd=root_fd, follow_symlinks=False)
                _require((child.st_dev, child.st_ino) == _details(expected)[:2], "destination_root_changed")
            with _directory(parent_path) as current_parent:
                _require(_details(current_parent)[:2] == _details(parent_fd)[:2], "destination_parent_changed")
            root_current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            _require((root_current.st_dev, root_current.st_ino) == root_identity, "destination_root_changed")
            root = parent_path / name
            return Projection(root, root / "source", root / "metadata.git", MappingProxyType(record))
    except ProjectionError as exc:
        if cleanup_root is not None:
            exc.cleanup_root = cleanup_root
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, zlib.error, TypeError, ValueError) as exc:
        raise ProjectionError("projection_input_unavailable", cleanup_root=cleanup_root) from exc
