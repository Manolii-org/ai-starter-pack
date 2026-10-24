#!/usr/bin/env python3
"""Install/update the bounded validation capability from a reviewed pack checkout.

This reads the local branch to reject protected branches, performs no Git
mutations or network operations, and never updates settings or secrets.
Repository owners supply recipes once; later updates preserve those recipes.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable

SOURCE = Path(__file__).resolve().parents[1]
FILES = (
    "scripts/agent-check.py", "scripts/sync-agent-validation.py",
    "scripts/install-agent-validation.py", "scripts/tests/test_agent_check.py",
    "docs/agent-validation.md", ".claude/skills/agent-validation/SKILL.md",
)
ALIASES = {".claude/skills": ".github/skills", ".cursor/skills": ".github/skills"}
PROVENANCE = ".ai/agent-validation-source.json"
RECOVERY = ".ai/agent-checks/layout-recovery.json"
DIRFD_ATOMIC = (os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd
                and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def strict_json(data: bytes):
    """Do not silently accept duplicate policy or provenance fields."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def relative_name(value: str) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", value)):
        raise ValueError("layout paths must be canonical repository-relative names")
    return value


def load_module(name: str, path: Path, data: bytes | None = None):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load bundled {name}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        exec(compile(path.read_bytes() if data is None else data, str(path), "exec"), module.__dict__)
    finally:
        sys.dont_write_bytecode = previous
    return module


def local_git(root: Path, *args: str, input_data=None) -> bytes:
    """Local identity reads cannot inherit another checkout's Git selectors."""
    locations = {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE",
                 "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX",
                 "GIT_QUARANTINE_PATH", "GIT_CONFIG"}
    env = {k: v for k, v in os.environ.items() if k not in locations
           if not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
           and k not in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    # check-ignore accepts literal stdin names, but rejects Git pathspec magic.
    env["GIT_LITERAL_PATHSPECS"] = "0"
    options = (["-c", "core.excludesFile=" + os.devnull]
               if args[0] == "check-ignore" else ["--literal-pathspecs"])
    return subprocess.check_output(["git", *options, *args], cwd=root,
                                   env=env, input=input_data, stderr=subprocess.PIPE, timeout=15)


def regular_git_mode(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"mode identity requires a regular file: {path}")
    return "100755" if info.st_mode & stat.S_IXUSR else "100644"


def tracked(root: Path, rel: str, raw: bytes, modes: tuple[str, ...]) -> dict:
    """Require exact HEAD/index/worktree bytes and mode for one tracked path."""
    index = local_git(root, "ls-files", "--stage", "-z", "--", rel).split(b"\0")
    head = local_git(root, "ls-tree", "-rz", "HEAD", "--", rel).split(b"\0")
    if len(index) != 2 or len(head) != 2:
        raise ValueError(f"layout identity must be tracked once: {rel}")
    imeta, iname = index[0].decode().split("\t", 1)
    hmeta, hname = head[0].decode().split("\t", 1)
    mode, oid, stage = imeta.split()
    hmode, kind, hoid = hmeta.split()
    if mode in ("100644", "100755") and regular_git_mode(root / rel) != mode:
        raise ValueError(f"HEAD/index/worktree mode mismatch: {rel}")
    if (iname != rel or hname != rel or mode not in modes or mode != hmode
            or stage != "0" or kind != "blob" or oid != hoid
            or local_git(root, "hash-object", "--no-filters", "--stdin", input_data=raw).strip() != oid.encode()):
        raise ValueError(f"HEAD/index/worktree layout mismatch: {rel}")
    return {"mode": mode, "blob": oid, "sha256": digest(raw)}


def watch_installed_mode(plan, logical: str, path: Path):
    """Bind existing tracked modes without requiring an immediate target commit."""
    rel = path.relative_to(plan.root).as_posix()

    def identity():
        mode = regular_git_mode(path)
        if Path(logical).parent == Path("scripts") and mode != "100755":
            raise ValueError(f"installed entrypoint lost owner execute: {logical}")
        rows = (local_git(plan.root, "ls-tree", "-z", "HEAD", "--", rel),
                local_git(plan.root, "ls-files", "--stage", "-z", "--", rel))
        for index, raw in enumerate(rows):
            if not raw:
                continue  # Immediate installed checks may precede add/commit.
            entries = raw.split(b"\0")
            if len(entries) != 2 or not entries[0] or entries[1]:
                raise ValueError(f"ambiguous installed Git identity: {logical}")
            meta, name = entries[0].split(b"\t", 1)
            fields = meta.split()
            if (name != rel.encode() or len(fields) != 3 or fields[0] != mode.encode()
                    or (index == 0 and fields[1] != b"blob") or (index == 1 and fields[2] != b"0")):
                raise ValueError(f"installed HEAD/index/worktree mode mismatch: {logical}")
        return mode, rows

    expected = identity()

    def unchanged():
        if identity() != expected:
            raise ValueError(f"installed mode/index identity changed: {logical}")
    plan.validators.append(unchanged)


def state(path: Path):
    """Identity and content, without following a final symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    identity = (info.st_dev, info.st_ino, info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return ("link", identity, os.readlink(path))
    if stat.S_ISDIR(info.st_mode):
        return ("directory", identity)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"non-regular installation path: {path}")
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
        data = stream.read()
        after = os.fstat(stream.fileno())
    if (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns) != (
            *identity, info.st_mtime_ns, info.st_ctime_ns):
        raise ValueError(f"path changed during snapshot: {path}")
    return ("file", identity, info.st_mtime_ns, info.st_ctime_ns, data)


def require_exact_case(path: Path):
    if path != path.parent and path.parent.is_dir():
        names = [entry.name for entry in path.parent.iterdir()
                 if entry.name.casefold() == path.name.casefold()]
        if names and names != [path.name]:
            raise ValueError(f"ambiguous path casing: {path}")


class OutputPlan:
    """One bounded plan for inspection and individually atomic replacements."""
    def __init__(self, root: Path, layout: bool = False):
        self.root = root
        self.layout = layout
        # Snapshot variants contain directory identities, link text or bytes.
        self.watched: dict[Path, Any] = {}
        self.outputs: dict[Path, bytes] = {}
        self.owners: dict[Path, str] = {}
        self.validators: list[Callable[[], None]] = []
        self.completed: list[str] = []
        self.journal: Path | None = None
        mask = os.umask(0)
        os.umask(mask)
        self.new_mode = 0o666 & ~mask

    def watch(self, path: Path):
        current = state(path)
        if path in self.watched and self.watched[path] != current:
            raise ValueError(f"input changed during planning: {path}")
        self.watched[path] = current
        return current

    def regular_path(self, path: Path, *, directory=False):
        """Check every existing parent and reject case-ambiguous spellings."""
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("installation paths must retain the original canonical root")
        chain = [*reversed(path.parents), path]
        for item in chain:
            require_exact_case(item)
            current = self.watch(item)
            wanted = "directory" if item != path or directory else "file"
            if current is not None and current[0] != wanted:
                raise ValueError(f"linked or wrong-type destination: {item}")
        return path

    def add(self, path: Path, data: bytes, owner: str):
        self.regular_path(path)
        if path in self.outputs and (self.outputs[path] != data or self.owners[path] != owner):
            raise ValueError(f"conflicting canonical outputs: {path}")
        self.outputs[path] = data
        self.owners[path] = owner

    def read(self, path: Path) -> bytes:
        self.regular_path(path)
        current = self.watched[path]
        if current is None:
            raise FileNotFoundError(path)
        return current[-1]

    def revalidate(self):
        for validate in self.validators:
            validate()
        for path, expected in self.watched.items():
            require_exact_case(path)
            if state(path) != expected:
                raise ValueError(f"input changed; reconcile before retry: {path}")

    def mkdirs(self, parent: Path):
        for path in [*reversed(parent.parents), parent]:
            current = state(path)
            if current != self.watched[path]:
                raise ValueError(f"parent changed before creation: {path}")
            if current is None:
                path.mkdir()
                self.watched[path] = state(path)

    def replace(self, path: Path, data: bytes, mode: int):
        """Hold the parent inode; do not follow a swapped parent or leaf link."""
        if not DIRFD_ATOMIC:
            return self.portable_replace(path, data, mode)
        self.revalidate()
        self.mkdirs(path.parent)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = None
        try:
            parent = os.fstat(descriptor)
            if (parent.st_dev, parent.st_ino, parent.st_mode) != self.watched[path.parent][1]:
                raise ValueError(f"parent changed before replacement: {path}")
            temporary = ".agent-validation-" + os.urandom(16).hex()
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=descriptor)
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), mode)
                stream.flush()
                os.fsync(stream.fileno())
            self.revalidate()
            os.replace(temporary, path.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            temporary = None
            os.fsync(descriptor)
            current = state(path)
            if current is None or current[0] != "file" or current[-1] != data:
                raise ValueError(f"replacement outcome changed; reconcile: {path}")
            self.watched[path] = current
        finally:
            if temporary is not None:
                os.unlink(temporary, dir_fd=descriptor)
            os.close(descriptor)

    def portable_replace(self, path: Path, data: bytes, mode: int):
        """Retain regular-layout support where directory descriptors are absent."""
        self.revalidate()
        self.mkdirs(path.parent)
        handle, name = tempfile.mkstemp(prefix=".agent-validation-", dir=path.parent)
        temporary: str | None = name
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, mode)
            self.revalidate()
            os.replace(name, path)
            temporary = None
            current = state(path)
            if current is None or current[0] != "file" or current[-1] != data:
                raise ValueError(f"replacement outcome changed; reconcile: {path}")
            self.watched[path] = current
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def apply(self, stale: list[Path]):
        self.revalidate()
        recovery = {"schema": "agent-validation-recovery/v1", "status": "prepared",
                    "completed": [], "before": {str(path.relative_to(self.root)): {
                        "mode": stat.S_IMODE(self.watched[path][1][2]) if self.watched[path] else None,
                        "base64": base64.b64encode(self.watched[path][-1]).decode()
                        if self.watched[path] and self.watched[path][0] == "file" else None,
                        "wanted_sha256": digest(self.outputs[path])} for path in stale}}
        if self.journal:
            self.replace(self.journal, encoded(recovery), 0o600)
        try:
            for path in stale:
                previous = self.watched[path]
                mode = stat.S_IMODE(previous[1][2]) if previous else self.new_mode
                if path.parent.name == "scripts" and path.suffix == ".py":
                    mode = 0o755
                self.replace(path, self.outputs[path], mode)
                self.completed.append(str(path.relative_to(self.root)))
                recovery["completed"] = list(self.completed)
                if self.journal:
                    self.replace(self.journal, encoded(recovery), 0o600)
            recovery["status"] = "complete"
            if self.journal:
                self.replace(self.journal, encoded(recovery), 0o600)
        except (OSError, ValueError) as exc:
            # Keep prepared/per-file evidence; never auto-restore over a writer.
            print("AGENT-VALIDATION-RECOVERY: " + json.dumps({
                "journal": str(self.journal) if self.journal else None,
                "completed": self.completed, "reason": str(exc)}), file=sys.stderr)
            raise


def require_private_recovery(plan: OutputPlan):
    """Require preexisting tracked ignore policy before writing owner before-images."""
    root = plan.root

    def policy():
        if (local_git(root, "ls-files", "-z", "--", RECOVERY)
                or local_git(root, "ls-tree", "-rz", "HEAD", "--", RECOVERY)):
            raise ValueError("private recovery evidence must not be tracked")
        fields = local_git(root, "check-ignore", "-v", "-z", "--stdin",
                           input_data=RECOVERY.encode() + b"\0").split(b"\0")
        if len(fields) != 5 or fields[2].startswith(b"!") or fields[3] != RECOVERY.encode():
            raise ValueError("private recovery evidence requires an existing repository ignore rule")
        rel = relative_name(fields[0].decode())
        directory = Path(RECOVERY).parent
        if (Path(rel).name != ".gitignore" or not directory.is_relative_to(Path(rel).parent)
                or directory == Path(rel).parent):
            raise ValueError("recovery ignore rule must come from a tracked repository .gitignore")
        name = directory.as_posix() + "/"
        ignored_directory = local_git(root, "check-ignore", "-v", "-z", "--stdin",
                                      input_data=name.encode() + b"\0").split(b"\0")
        if (len(ignored_directory) != 5 or ignored_directory[0] != fields[0]
                or ignored_directory[2].startswith(b"!") or ignored_directory[3] != name.encode()):
            raise ValueError("private recovery directory must already be ignored by the same ancestor policy")
        return rel, fields + ignored_directory

    rel, expected = policy()
    path = plan.regular_path(root / rel)
    raw = plan.read(path)
    identity = tracked(root, rel, raw, ("100644", "100755"))
    if path in plan.outputs and plan.outputs[path] != raw:
        raise ValueError("recovery ignore policy must be established before this installation")

    def unchanged():
        if policy() != (rel, expected) or tracked(root, rel, path.read_bytes(), ("100644", "100755")) != identity:
            raise ValueError("private recovery ignore identity changed")
    plan.validators.append(unchanged)


def layout_context(plan: OutputPlan, argument: Path) -> tuple[dict, dict]:
    root = plan.root
    plan.regular_path(root, directory=True)
    if local_git(root, "rev-parse", "--show-toplevel").decode().strip() != str(root):
        raise ValueError("layout root must be the original repository root")
    if ".." in argument.parts:
        raise ValueError("layout path traversal is not allowed")
    path = argument if argument.is_absolute() else root / argument
    if not path.is_relative_to(root):
        raise ValueError("layout must be inside the original root")
    rel = relative_name(path.relative_to(root).as_posix())
    plan.regular_path(path)
    raw = plan.read(path)
    layout = strict_json(raw)
    if (not isinstance(layout, dict) or set(layout) != {"schema", "aliases"}
            or layout["schema"] != "agent-validation-layout/v1" or layout["aliases"] != ALIASES):
        raise ValueError("layout must declare exactly the two supported skills aliases")
    identity = tracked(root, rel, raw, ("100644", "100755"))
    aliases = {}
    for alias, target in ALIASES.items():
        link = root / alias
        plan.regular_path(link.parent, directory=True)
        current = plan.watch(link)
        if current is None or current[0] != "link" or current[2] != "../.github/skills":
            raise ValueError(f"unsupported alias target: {alias}")
        aliases[alias] = {**tracked(root, alias, current[2].encode(), ("120000",)),
                          "target": current[2]}
        canonical = plan.regular_path(root / target, directory=True)
        if not canonical.is_dir() or link.resolve(strict=True) != canonical:
            raise ValueError(f"alias must reach the existing real canonical directory: {alias}")
    context = {"schema": "agent-validation-layout-record/v1",
               "layout_file": {"path": rel, **identity}, "aliases": aliases}
    head = local_git(root, "rev-parse", "HEAD")
    branch = local_git(root, "branch", "--show-current")

    def unchanged():
        if (local_git(root, "rev-parse", "HEAD") != head
                or local_git(root, "branch", "--show-current") != branch
                or tracked(root, rel, path.read_bytes(), ("100644", "100755")) != identity):
            raise ValueError("layout repository identity changed")
        for alias, entry in aliases.items():
            target = os.readlink(root / alias)
            observed = {**tracked(root, alias, target.encode(), ("120000",)), "target": target}
            if observed != entry:
                raise ValueError("tracked alias identity changed")
    plan.validators.append(unchanged)
    return layout, context


def destination(root: Path, logical: str, layout: bool) -> Path:
    relative_name(logical)
    prefix = ".claude/skills/"
    if layout and logical.startswith(prefix):
        return root / ".github/skills" / logical.removeprefix(prefix)
    return root / logical


def validate_provenance(value: dict):
    if (not isinstance(value, dict) or set(value) not in (
            {"schema", "source", "files", "recipes"}, {"schema", "source", "files", "recipes", "layout"})
            or value.get("schema") != "agent-validation-source/v1"
            or value.get("source") != "ai-starter-pack" or value.get("recipes") != "repository-owned"
            or not isinstance(value.get("files"), dict) or set(value["files"]) != set(FILES)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                   for v in value["files"].values())):
        raise ValueError("malformed six-file installation provenance")
    if "layout" not in value:
        return
    record = value["layout"]
    if (not isinstance(record, dict) or set(record) != {"schema", "source", "layout_file", "aliases", "destinations"}
            or record["schema"] != "agent-validation-layout-record/v1"):
        raise ValueError("malformed layout provenance")
    origin = record["source"]
    if (not isinstance(origin, dict) or set(origin) != {"commit", "subdir", "bundle_sha256"}
            or not isinstance(origin["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", origin["commit"])
            or origin["bundle_sha256"] != digest(encoded(value["files"]))):
        raise ValueError("malformed immutable source provenance")
    if origin["subdir"] != ".":
        relative_name(origin["subdir"])
    owner = record["layout_file"]
    if (not isinstance(owner, dict) or set(owner) != {"path", "mode", "blob", "sha256"}
            or owner["mode"] not in ("100644", "100755")
            or not isinstance(owner["blob"], str) or not re.fullmatch(r"[0-9a-f]{40}", owner["blob"])
            or not isinstance(owner["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", owner["sha256"])):
        raise ValueError("malformed layout-file provenance")
    relative_name(owner["path"])
    if not isinstance(record["aliases"], dict) or set(record["aliases"]) != set(ALIASES):
        raise ValueError("malformed alias provenance")
    for alias, entry in record["aliases"].items():
        raw = b"../.github/skills"
        oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if entry != {"mode": "120000", "blob": oid, "target": raw.decode(), "sha256": digest(raw)}:
            raise ValueError(f"malformed alias provenance: {alias}")
    expected = {name: {"canonical": str(destination(Path("/"), name, True)).removeprefix("/"),
                       "sha256": value["files"][name]} for name in FILES}
    if record["destinations"] != expected:
        raise ValueError("malformed logical/canonical provenance")


def source_identity(plan: OutputPlan, paths: dict, hashes: dict, previous) -> dict:
    if SOURCE == plan.root:
        if previous is None or "layout" not in previous or previous["files"] != hashes:
            raise ValueError("installed source requires matching immutable origin provenance")
        return previous["layout"]["source"]
    top = Path(local_git(SOURCE, "rev-parse", "--show-toplevel").decode().strip())
    commit = local_git(SOURCE, "rev-parse", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source must identify an immutable Git commit")
    identities = {}
    for logical, path in paths.items():
        plan.regular_path(path)
        rel = path.relative_to(top).as_posix()
        if local_git(top, "show", f"{commit}:{rel}") != plan.read(path):
            raise ValueError(f"source differs from its immutable commit: {logical}")
        identities[path] = tracked(top, rel, plan.read(path), ("100644", "100755"))
    def unchanged():
        if local_git(SOURCE, "rev-parse", "HEAD").decode().strip() != commit:
            raise ValueError("immutable source commit changed")
        for path, expected in identities.items():
            if tracked(top, path.relative_to(top).as_posix(), plan.read(path), ("100644", "100755")) != expected:
                raise ValueError("immutable source mode/index identity changed")
    plan.validators.append(unchanged)
    return {"commit": commit, "subdir": SOURCE.relative_to(top).as_posix(),
            "bundle_sha256": digest(encoded(hashes))}


def safe(root: Path, rel: str) -> Path:
    path = root / rel
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError(f"linked destination: {rel}")
    return path


def protected_branches(configs: list[dict], remotes: set[str], resolve=None) -> set[str]:
    """Keep both current and proposed owner integration/release names guarded."""
    protected = {"main", "master", "staging", "prod", "develop", "work"}
    for config in configs:
        base = config.get("base")
        extra = config.get("protected_branches", [])
        if (not isinstance(base, str) or not base or not isinstance(extra, list)
                or any(not isinstance(name, str) or not name for name in extra)):
            raise ValueError("owner base and protected_branches must contain branch names")
        for original in [base, *extra]:
            ref = resolve(original) if resolve else original
            if ref.startswith("refs/heads/"):
                protected.add(ref.removeprefix("refs/heads/"))
            else:
                # Preserve slash-containing local branch names. Strip a remote
                # by longest configured prefix: remote names may contain '/'.
                remote_ref = ref.removeprefix("refs/remotes/")
                protected.add(remote_ref)
                prefixes = sorted(remotes | {"origin", "upstream"}, key=len, reverse=True)
                remote = next((name for name in prefixes if remote_ref.startswith(name + "/")), None)
                if remote:
                    protected.add(remote_ref[len(remote) + 1:])
                elif ref.startswith("refs/remotes/"):
                    # A retained remote ref need not still have a configured
                    # remote. Guard every possible branch suffix conservatively.
                    parts = remote_ref.split("/")
                    if len(parts) < 2:
                        raise ValueError("invalid owner remote branch reference")
                    protected.update("/".join(parts[i:]) for i in range(1, len(parts)))
    return protected


def build_plan(args):
    """Preflight every input/output before any write, in all three modes."""
    explicit = args.layout is not None
    if explicit and not DIRFD_ATOMIC:
        raise ValueError("explicit layout requires no-follow directory-descriptor replacement support")
    root = args.repo_root.absolute() if explicit else args.repo_root.resolve()
    plan = OutputPlan(root, explicit)
    context = None
    if explicit:
        _, context = layout_context(plan, args.layout)
    owned = plan.regular_path(safe(root, "config/agent-checks.json"))
    recipe_path = args.recipes.absolute() if args.recipes else owned
    if args.recipes and not explicit:
        # Legacy explicit recipe inputs may traverse links. Watch that mapping
        # as well as the canonical regular input; destinations stay strict.
        for path in [*reversed(recipe_path.parents), recipe_path]:
            plan.watch(path)
        recipe_path = recipe_path.resolve(strict=True)
    plan.regular_path(recipe_path)
    recipe = plan.read(recipe_path)
    config = strict_json(recipe)
    if not isinstance(config, dict) or config.get("schema_version") != 1 or not config.get("base") or "checks" not in config:
        raise ValueError("owner recipes require schema_version=1, base and checks")
    receipt_path = plan.regular_path(safe(root, PROVENANCE))
    previous = None
    if explicit and receipt_path.exists():
        previous = strict_json(plan.read(receipt_path))
        validate_provenance(previous)
        if "layout" in previous and any(previous["layout"][key] != context[key]
                                        for key in ("layout_file", "aliases")):
            raise ValueError("installed layout provenance does not match its tracked owner contract")
        for logical, expected in previous["files"].items():
            installed = plan.regular_path(destination(root, logical, True))
            if not installed.is_file() or digest(plan.read(installed)) != expected:
                raise ValueError(f"installed file differs from provenance: {logical}")
            watch_installed_mode(plan, logical, installed)
    hashes, sources = {}, {}
    for logical in FILES:
        direct = SOURCE / logical
        if explicit and SOURCE == root:
            source = destination(root, logical, True)
        else:
            # Existing Copier versus plugin/registry source selection is retained.
            source = direct if direct.is_file() else SOURCE / logical.removeprefix(".claude/")
        plan.regular_path(source)
        raw = plan.read(source)
        hashes[logical], sources[logical] = digest(raw), source
        target = destination(root, logical, explicit)
        if not explicit:
            target = safe(root, logical)
        plan.add(target, raw, "bundle:" + logical)
    if args.recipes:
        plan.add(owned, recipe, "owner-recipes")
    provenance = {"schema": "agent-validation-source/v1", "source": "ai-starter-pack",
                  "files": hashes, "recipes": "repository-owned"}
    if explicit:
        context["source"] = source_identity(plan, sources, hashes, previous)
        context["destinations"] = {name: {"canonical": destination(root, name, True).relative_to(root).as_posix(),
                                          "sha256": value} for name, value in hashes.items()}
        provenance["layout"] = context
    validate_provenance(provenance)
    plan.add(receipt_path, (json.dumps(provenance, indent=2) + "\n").encode(), "provenance")
    # Capture every generator input before it reads owner prose. The generator
    # retains its own narrow secondary CLAUDE file-alias exception.
    for rel in ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md", ".cursor/rules/agent-validation.mdc", ".gitignore"):
        path = root / rel
        plan.regular_path(path.parent, directory=True)
        plan.watch(path)
    plan.revalidate()
    module = load_module("sync_agent_validation", sources["scripts/sync-agent-validation.py"],
                         plan.watched[sources["scripts/sync-agent-validation.py"]][-1])
    for path, text in module.outputs(root, plan.read(SOURCE / "docs/agent-validation.md").decode("utf-8")).items():
        if not path.is_relative_to(root):
            raise ValueError("instruction output escapes original root")
        plan.add(path, text.encode(), "instructions")
    if explicit:
        plan.journal = plan.regular_path(safe(root, RECOVERY))
        require_private_recovery(plan)
        if plan.journal.exists():
            journal = strict_json(plan.read(plan.journal))
            if (not isinstance(journal, dict) or set(journal) != {"schema", "status", "before", "completed"}
                    or journal["schema"] != "agent-validation-recovery/v1" or journal["status"] != "complete"
                    or not isinstance(journal["before"], dict) or not isinstance(journal["completed"], list)):
                raise ValueError("incomplete/malformed recovery evidence; reconcile without replay")
    plan.revalidate()
    return plan, config, owned


def require_task_branch(root: Path, config: dict, owned: Path, replacing_recipe: bool, runner_data: bytes):
    runner = load_module("agent_check", SOURCE / "scripts/agent-check.py", runner_data)
    branch = runner.git(root, "branch", "--show-current").decode("utf-8").strip()
    owner_configs = [config]
    if replacing_recipe and owned.is_file():
        owner_configs.append(strict_json(owned.read_bytes()))
    remotes = set(runner.git(root, "remote").decode("utf-8").splitlines())

    def owner_branch_ref(ref):
        resolved = subprocess.run([b"git", b"rev-parse", b"--symbolic-full-name", b"--verify", b"--end-of-options", ref.encode("utf-8")],
                                  cwd=root, env=runner.git_environment(), capture_output=True, timeout=15)
        canonical = resolved.stdout.decode("utf-8").strip()
        if resolved.returncode == 0:
            if canonical.startswith(("refs/heads/", "refs/remotes/")) and "\n" not in canonical:
                return canonical
            raise ValueError("owner base/protected references must identify branches, not revisions or tags")
        literal = subprocess.run([b"git", b"check-ref-format", b"--branch", ref.encode("utf-8")], cwd=root,
                                 env=runner.git_environment(), capture_output=True, timeout=15)
        if (ref == "@" or ref.startswith("refs/tags/") or literal.returncode
                or literal.stdout.decode("utf-8").strip() != ref):
            raise ValueError("owner base/protected references must be literal branch names")
        return ref

    if not branch or branch in protected_branches(owner_configs, remotes, owner_branch_ref):
        raise ValueError("install on an isolated task branch")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--recipes", type=Path, help="explicit owner-reviewed recipes for initial adoption or update")
    parser.add_argument("--layout", type=Path, help="explicit tracked agent-validation-layout/v1 owner contract")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan, config, owned = build_plan(args)
        stale = [p for p, data in plan.outputs.items()
                 if plan.watched[p] is None or plan.watched[p][-1] != data]
        if args.check or args.dry_run:
            plan.revalidate()
            for path in stale:
                print(f"{'STALE' if args.check else 'UPDATE'} {path.relative_to(plan.root)}")
            if args.dry_run and stale and plan.journal:
                print(f"RECOVERY {plan.journal.relative_to(plan.root)}")
            return int(args.check and bool(stale))
        plan.revalidate()
        require_task_branch(plan.root, config, owned, args.recipes is not None,
                            plan.outputs[destination(plan.root, "scripts/agent-check.py", args.layout is not None)])
        if stale:
            plan.apply(stale)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"AGENT-VALIDATION-INSTALL-BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
