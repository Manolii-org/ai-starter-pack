#!/usr/bin/env python3
"""Run repository-owned CI commands before publishing an agent candidate.

No network API, credentials, reviewer or merge authority is provided here.
The ignored receipt proves local execution only. Configuration is trusted code,
just like package.json; never run a fork's configuration with privileged env.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import secrets
import stat
import subprocess
import sys
import time

SCHEMA = "agent-check/v1"
STATE = ".ai/agent-checks"
GIT_TIMEOUT = 30
TOOLS = {
    "python": [sys.executable, "--version"],
    "node": ["node", "--version"],
    "npm": ["npm", "--version"],
    "pnpm": ["pnpm", "--version"],
    "uv": ["uv", "--version"],
    "ruff": [sys.executable, "-m", "ruff", "--version"],
    "go": ["go", "version"],
    "swift": ["swiftc", "--version"],
    "shellcheck": ["shellcheck", "--version"],
    "actionlint": ["actionlint", "--version"],
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def git_environment(environ: dict | None = None) -> dict:
    # Hooks can export the enclosing repository's locations. An explicit cwd
    # must select this repository, while transport/proxy configuration stays.
    locations = {
        "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_QUARANTINE_PATH", "GIT_CONFIG",
    }
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if key not in locations}


def git(root: Path, *args: str) -> bytes:
    if args and args[0] == "diff":
        # Evidence must describe bytes, independently of display-only drivers.
        args = (args[0], "--no-ext-diff", "--no-textconv", "--no-color", *args[1:])
    result = subprocess.run(["git", *args], cwd=root, env=git_environment(), capture_output=True, timeout=GIT_TIMEOUT, check=True)
    return result.stdout


def execution_environment(item: dict, inherited: dict | None = None) -> dict:
    """Use the same effective environment for execution, probes and identity."""
    return {**git_environment(inherited), "CI": "true", "PYTHONDONTWRITEBYTECODE": "1", **item.get("env", {})}


def private_cache_key(root: Path) -> bytes:
    """Create one ignored private key without exposing inherited secret values."""
    path = inside(root, f"{STATE}/environment.key")
    if subprocess.run(["git", "check-ignore", "-q", str(path.relative_to(root))],
                      cwd=root, env=git_environment(), timeout=GIT_TIMEOUT).returncode:
        raise ValueError(f"ignore {STATE}/environment.key before executing")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Link a complete private temporary file into place atomically. A competing
    # invocation uses the existing key; an unsafe existing path is never replaced.
    if not path.exists() and not path.is_symlink():
        temporary = path.parent / f".environment-key-{os.getpid()}-{secrets.token_hex(8)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                print("AGENT-CHECK: reusing concurrently created private cache key")
        finally:
            temporary.unlink(missing_ok=True)
    if path.is_symlink():
        raise ValueError("environment cache key must be a private regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        for attempt in range(20):
            details = os.fstat(stream.fileno())
            if (not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600
                    or details.st_uid != os.getuid()):
                raise ValueError("environment cache key must be an owned private regular file with mode 0600")
            if details.st_nlink == 1:
                break
            # A concurrent publisher briefly owns both the temporary and final names.
            # Keep the same descriptor and require one link before reading any material.
            # Persistent/extra links still fail closed; never replace or repair the key.
            if details.st_nlink != 2 or attempt == 19:
                raise ValueError("environment cache key must be an owned private regular file with mode 0600")
            time.sleep(0.05)
        key = stream.read(33)
    if len(key) != 32:
        raise ValueError("environment cache key is invalid")
    return key


def environment_fingerprint(key: bytes, environ: dict) -> str:
    """Only an aggregate keyed digest may appear in local evidence."""
    return hmac.new(key, encoded(environ), hashlib.sha256).hexdigest()


def inside(root: Path, value: str) -> Path:
    path = root / value
    if Path(value).is_absolute() or not path.resolve().is_relative_to(root):
        raise ValueError(f"path must stay inside the repository: {value}")
    return path


def require_unfiltered_tracked_inputs(root: Path) -> set[str]:
    """Reject Git metadata that can conceal raw working-file changes."""
    names = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                           env=git_environment(), capture_output=True, timeout=GIT_TIMEOUT, check=True).stdout
    attributes = subprocess.run(["git", "check-attr", "--all", "-z", "--stdin"],
                                input=names, cwd=root, env=git_environment(),
                                capture_output=True, timeout=GIT_TIMEOUT, check=True).stdout.split(b"\0")
    if attributes[-1:] == [b""]:
        attributes.pop()
    if len(attributes) % 3:
        raise ValueError("cannot determine tracked Git filter attributes")
    if any(attributes[i + 1] == b"filter" for i in range(0, len(attributes), 3)):
        raise ValueError("Git filter attributes are unsupported for local validation; "
                         "filtered working files need a raw-input validation adapter")
    flagged = subprocess.run(["git", "ls-files", "-v", "-z"], cwd=root,
                             env=git_environment(), capture_output=True, timeout=GIT_TIMEOUT, check=True).stdout
    for entry in flagged.split(b"\0"):
        if not entry:
            continue
        if len(entry) < 3 or entry[1:2] != b" ":
            raise ValueError("cannot determine tracked Git index flags")
        tag, path = entry[:1], root / os.fsdecode(entry[2:])
        # Absent skip-worktree entries are normal in a sparse checkout. Existing
        # paths (including dangling symlinks) can carry hidden, unvalidated bytes.
        # Assume-unchanged is unsafe even when a working path has been removed.
        if tag.islower() or (tag == b"S" and os.path.lexists(path)):
            raise ValueError("Git index flags hide tracked working-file changes; "
                             "clear assume-unchanged/skip-worktree on present paths")
    conversions = {b"text", b"crlf", b"eol", b"ident", b"working-tree-encoding"}
    return {os.fsdecode(attributes[i]) for i in range(0, len(attributes), 3)
            if attributes[i + 1] in conversions}


def directory_state(details: os.stat_result) -> tuple:
    # Reading a directory may update atime, including on every strictatime
    # filesystem scan. Identity, mode and mutation times carry the proof.
    return (details.st_dev, details.st_ino, details.st_mode,
            details.st_mtime_ns, details.st_ctime_ns)


def empty_gitlink_identity(root: Path, name: str) -> str | None:
    path = root / name
    # Descriptor enumeration is unavailable on some hosts. The existing
    # unsupported-submodule block remains their conservative fallback.
    if os.scandir not in os.supports_fd:
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        before = directory_state(details)
        if not stat.S_ISDIR(details.st_mode) or directory_state(path.lstat()) != before:
            return None
        entries = git(root, "ls-files", "--stage", "-z", "--", name).split(b"\0")
        if len(entries) != 2 or entries[1]:
            return None
        metadata, tracked_name = entries[0].split(b"\t", 1)
        fields = metadata.split()
        if (len(fields) != 3 or fields[0] != b"160000" or fields[2] != b"0"
                or tracked_name != os.fsencode(name)):
            return None
        with os.scandir(descriptor) as scan:
            if next(scan, None) is not None:
                return None
        if (directory_state(os.fstat(descriptor)) != before
                or directory_state(path.lstat()) != before):
            return None
        return fields[1].decode("ascii")
    finally:
        os.close(descriptor)


def raw_tracked_inputs(root: Path) -> dict:
    """Hash physical working bytes independently of Git's stat/index hints."""
    algorithm = git(root, "rev-parse", "--show-object-format").decode().strip()
    if algorithm not in {"sha1", "sha256"}:
        raise ValueError("unsupported Git object format for raw-input validation")
    result = {}
    for entry in git(root, "ls-files", "-t", "-z").split(b"\0"):
        if not entry:
            continue
        if len(entry) < 3 or entry[1:2] != b" ":
            raise ValueError("cannot determine tracked raw-input paths")
        name = os.fsdecode(entry[2:])
        path = root / name
        if not os.path.lexists(path):
            if entry[:1] != b"S":
                result[name] = ("missing", "")
            continue
        if path.is_symlink():
            mode, content = "120000", os.fsencode(os.readlink(path))
        elif path.is_file():
            mode = "100755" if path.stat().st_mode & 0o100 else "100644"
            content = path.read_bytes()
        else:
            # Git creates empty directories for uninitialized submodules. They
            # contain no working bytes; bind their stage-zero gitlink instead.
            # Initialized/nonempty modules still need a recursive raw adapter.
            if path.is_dir() and not path.is_symlink():
                identity = empty_gitlink_identity(root, name)
                if identity is not None:
                    result[name] = ("160000", identity)
                    continue
            raise ValueError("tracked directories/submodules need a raw-input validation adapter")
        digest = hashlib.new(algorithm)
        digest.update(f"blob {len(content)}\0".encode())
        digest.update(content)
        result[name] = (mode, digest.hexdigest())
    return result


def snapshot(root: Path) -> dict:
    require_unfiltered_tracked_inputs(root)
    # Include staged AND unstaged edits, additions and deletions. Generated
    # ignored files are not evidence inputs; declare their source/config.
    untracked = {}
    for raw in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = root / name
        if path.is_symlink():
            untracked[name] = sha(b"symlink\0" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            # Git records only the executable bit; bind it like tracked inputs so
            # a helper that loses +x cannot reuse a PASS it can no longer earn.
            kind = b"executable\0" if path.stat().st_mode & 0o111 else b"regular\0"
            untracked[name] = sha(kind + path.read_bytes())
    # Git records link text, not mutable target contents. Keep normal check
    # execution for linked candidates, but never reuse or save a cached PASS.
    names = set(untracked)
    names.update(os.fsdecode(p) for p in git(root, "ls-files", "-z").split(b"\0") if p)
    symlinks = sorted(name for name in names if (root / name).is_symlink())
    return {
        "head": git(root, "rev-parse", "HEAD").decode().strip(),
        "tree": git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
        "diff": sha(git(root, "diff", "--binary", "HEAD")),
        "tracked_raw": raw_tracked_inputs(root),
        "untracked": untracked,
        "symlinks": symlinks,
    }


def config_for(root: Path) -> dict:
    config = json.loads(inside(root, "config/agent-checks.json").read_text(encoding="utf-8"))
    if config.get("schema_version") != 1 or not config.get("base"):
        raise ValueError("config/agent-checks.json requires schema_version=1 and base")
    identifiers = set()
    for kind in ("checks", "environments"):
        for item in config.get(kind, []):
            identifier = item.get("id", "")
            if not identifier or identifier in identifiers:
                raise ValueError("check and environment ids must be nonempty and unique")
            identifiers.add(identifier)
            inside(root, item.get("cwd", "."))
            if not item.get("commands") or not all(
                isinstance(cmd, list) and cmd and all(isinstance(s, str) and s for s in cmd)
                for cmd in item["commands"]
            ):
                raise ValueError(f"{identifier}: commands must be nonempty argv arrays")
            if not isinstance(item.get("paths", ["*"]), list):
                raise ValueError(f"{identifier}: paths must be an array")
    environments = {item["id"] for item in config.get("environments", [])}
    for check in config.get("checks", []):
        if check.get("environment") and check["environment"] not in environments:
            raise ValueError(f"unknown environment for {check['id']}")
        for source in check.get("ci_sources", []):
            text = inside(root, source["path"]).read_text(encoding="utf-8")
            if source["contains"] not in text:
                raise ValueError(f"{check['id']}: CI command changed in {source['path']}; reconcile the local recipe")
    return config


def matches(paths: list[str], patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in patterns)


def working_tree_matches_base(root: Path, name: str, actual: tuple, baseline: tuple | None) -> bool:
    """Compare to baseline checkout bytes, never clean away a working edit.

    The caller has rejected external filters. Built-in checkout conversions
    (CRLF, ident, encoding) are applied to the immutable base blob, so edits
    hidden by clean conversion still select their owning checks.
    """
    if baseline is None or actual[0] == "missing":
        return False
    mode, identity = baseline
    if mode == "160000":
        return actual == baseline
    if actual[0] != mode:
        regular = {"100644", "100755"}
        file_mode = git(root, "config", "--type=bool", "--default=true", "--get", "core.filemode").strip()
        symlinks = git(root, "config", "--type=bool", "--default=true", "--get", "core.symlinks").strip()
        if not ((actual[0] in regular and mode in regular and file_mode == b"false")
                or (actual[0] in regular and mode == "120000" and symlinks == b"false")):
            return False
    expected = (git(root, "cat-file", "blob", identity) if mode == "120000" else
                git(root, "cat-file", "--filters", "--path=" + name, identity))
    # raw_tracked_inputs has already validated the repository object format.
    digest = hashlib.new("sha256" if len(identity) == 64 else "sha1")
    digest.update(f"blob {len(expected)}\0".encode())
    digest.update(expected)
    return actual[1] == digest.hexdigest()


def plan_for(root: Path, config: dict, base: str | None = None) -> dict:
    conversion_paths = require_unfiltered_tracked_inputs(root)
    # Include conservative potential conversions even when bytes equal the
    # canonical blob (e.g. an expanded ident replaced by literal $Id$).
    autocrlf = git(root, "config", "--default=false", "--get", "core.autocrlf").strip().lower()
    global_conversion = autocrlf not in {b"false", b"no", b"off", b"0", b"input"}
    ref = base or config["base"]
    base_sha = git(root, "merge-base", ref, "HEAD").decode().strip()
    changed: set[str] = set()
    for command in (("diff", "--no-renames", "--name-only", "-z", base_sha),
                    ("ls-files", "--others", "--exclude-standard", "-z")):
        changed.update(os.fsdecode(p) for p in git(root, *command).split(b"\0") if p)
    baseline = {}
    for entry in git(root, "ls-tree", "-r", "--full-tree", "-z", base_sha).split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, _, identity = metadata.decode().split()
            baseline[os.fsdecode(name)] = (mode, identity)
    changed.update(name for name, value in raw_tracked_inputs(root).items()
                   if (value != baseline.get(name) or name in conversion_paths or global_conversion)
                   and not working_tree_matches_base(root, name, value, baseline.get(name)))
    paths = sorted(changed)
    # Owners explicitly declare global inputs in each recipe. Framework-only
    # changes must not implicitly install/build every application in the repo.
    checks = []
    for check in config.get("checks", []):
        patterns = check.get("paths", ["*"])
        if not matches(paths, patterns):
            continue
        check = dict(check)
        if check.get("append_changed_paths"):
            affected = [p for p in paths if matches([p], patterns)]
            selected = affected if all(inside(root, p).is_file() for p in affected) else []
            # Deleted workflows require checking remaining callers; no args
            # lets actionlint validate that remaining workflow graph.
            check["commands"] = [cmd + selected for cmd in check["commands"]]
        checks.append(check)
    needed = {c.get("environment") for c in checks}
    return {"base": base_sha, "paths": paths, "checks": checks,
            "environments": [e for e in config.get("environments", []) if e["id"] in needed],
            "review_paths": [p for p in paths if matches([p], config.get("review_paths", []))],
            "tool_requirements": config.get("tool_requirements", {})}


def tool_contexts(items: list[dict], requirements=()) -> set[tuple[str, str, str]]:
    # Keep the framework interpreter; explicit tool contexts retain owner pins.
    contexts = {("python", ".", "{}")}
    for item in items:
        for name in item.get("tools", []):
            contexts.add((name, str(Path(item.get("cwd", "."))), encoded(item.get("env", {})).decode()))
    # A required tool with no selected context still needs a root probe.
    present = {context[0] for context in contexts}
    contexts.update((name, ".", "{}") for name in requirements if name not in present)
    return contexts


def tool_context_key(name: str, cwd: str, env_json: str) -> str:
    key = name if cwd == "." and env_json == "{}" else f"{name}@{cwd}"
    return key + "#" + sha(env_json.encode())[:12] if env_json != "{}" else key


def versions(root: Path, plan: dict, inherited: dict | None = None) -> dict:
    # Corepack/pyenv and similar shims select versions from their working
    # directory. Probe each distinct execution context, never the caller's cwd.
    contexts = tool_contexts([*plan["checks"], *plan["environments"]], plan.get("tool_requirements", {}))
    result = {"platform": platform.platform()}
    for name, cwd, env_json in sorted(contexts):
        if name not in TOOLS:
            raise ValueError(f"unknown tool fingerprint: {name}")
        declared_env = json.loads(env_json)
        key = tool_context_key(name, cwd, env_json)
        env = execution_environment({"env": declared_env}, inherited)
        try:
            proc = subprocess.run(TOOLS[name], cwd=inside(root, cwd), env=env,
                                  capture_output=True, text=True, timeout=15)
            result[key] = (proc.stdout or proc.stderr).strip() if proc.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            result[key] = "unavailable"
    return result


def execute(root: Path, item: dict, inherited: dict | None = None) -> dict:
    start = time.monotonic()
    status, reason = "passed", "completed"
    env = execution_environment(item, inherited)
    for command in item["commands"]:
        print(f"AGENT-CHECK {item['id']}: {json.dumps(command)}", flush=True)
        try:
            proc = subprocess.run(command, cwd=inside(root, item.get("cwd", ".")), env=env,
                                  timeout=item.get("timeout_seconds", 900), check=False)
            if proc.returncode:
                status, reason = "failed", f"exit={proc.returncode}"
                break
        except FileNotFoundError:
            status, reason = "unavailable", "executable or working directory missing"
            break
        except subprocess.TimeoutExpired:
            status, reason = "failed", "timeout"
            break
    return {"id": item["id"], "status": status, "reason": reason,
            "duration_ms": round((time.monotonic() - start) * 1000)}


def environment_tools(item: dict, tools: dict, plan: dict) -> dict:
    consumers = [check for check in plan["checks"] if check.get("environment") == item["id"]]
    required = plan.get("tool_requirements", {})
    contexts = tool_contexts([item, *consumers], required)
    # A root requirement constrains every selected context of that tool.
    contexts.update(context for context in tool_contexts([*plan["checks"], *plan["environments"]])
                    if context[0] in required)
    keys = {"platform", *(tool_context_key(*context) for context in contexts)}
    return {name: value for name, value in tools.items() if name in keys}


def environment_digest(root: Path, item: dict, tools: dict, key: bytes, inherited: dict,
                       *, plan: dict | None = None) -> str:
    inputs = {}
    for name in item.get("inputs", []):
        path = inside(root, name)
        inputs[name] = sha(path.read_bytes()) if path.is_file() else None
    return sha(encoded({"tool_contract": "agent-environment-tools/v2", "recipe": item, "inputs": inputs,
                        "tools": environment_tools(item, tools, plan) if plan is not None else tools,
                        "execution_environment": environment_fingerprint(key, execution_environment(item, inherited))}))


def ready_path(root: Path, value: str) -> Path:
    """Keep the declared name local; external links are usable but never cached."""
    path = root / value
    if Path(value).is_absolute() or not Path(os.path.abspath(path)).is_relative_to(root):
        raise ValueError(f"path must stay inside the repository: {value}")
    # Preserve filesystem semantics of ".." through a link. Lexical collapse
    # would probe a different readiness target; open_owned rejects external data.
    return path


def missing_ready_paths(root: Path, item: dict) -> list[str]:
    return [p for p in item.get("ready_paths", []) if not ready_path(root, p).exists()]


# Tool caches written while checks run; their churn says nothing about the
# installed dependencies and must not force a reinstall on every invocation.
READY_IDENTITY_SKIP = {".cache", ".vite", ".vite-temp", ".vitest", "__pycache__"}
READY_IDENTITY_MAX_ENTRIES = 250_000
READY_IDENTITY_MAX_BYTES = 2_000_000_000
READY_IDENTITY_CONTRACT = "agent-ready-content/v3"


def ready_identity(root: Path, item: dict, *, key: bytes | None = None) -> str | None:
    """Bind owned dependency bytes, or return None to require fresh execution.

    Same-size writes can retain every timestamp and inode on a coarse-clock
    filesystem. Metadata alone cannot authorize setup or candidate PASS reuse.
    Read only declared ready trees and linked targets inside this owner root.
    External/unsafe links, special files, unsupported descriptor operations and
    bounded-scan exhaustion disable both caches without suppressing checks.
    """
    if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd):
        return None
    domain = READY_IDENTITY_CONTRACT.encode("ascii") + b"\0"
    digest = hmac.new(key, domain, hashlib.sha256) if key is not None else hashlib.sha256(domain)
    remaining_entries = READY_IDENTITY_MAX_ENTRIES
    remaining_bytes = READY_IDENTITY_MAX_BYTES
    directory_states: dict[str, tuple[tuple[int, ...], tuple[int, ...], Path]] = {}
    active_directories: set[int] = set()
    missing_paths: list[Path] = []
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, directory_flags)
    except OSError:
        return None

    def state(details: os.stat_result) -> tuple[int, ...]:
        return (details.st_dev, details.st_ino, details.st_mode, details.st_size,
                details.st_mtime_ns, details.st_ctime_ns)

    def named_target(path: Path, entry: tuple[int, ...], target: tuple[int, ...], resolved: Path) -> bool:
        return (state(path.lstat()) == entry and path.resolve(strict=True) == resolved
                and state(path.stat()) == target)

    def names(descriptor: int, *, count: bool) -> list[str]:
        nonlocal remaining_entries
        entries: list[str] = []
        with os.scandir(descriptor) as stream:
            for entry in stream:
                if count:
                    remaining_entries -= 1
                if remaining_entries < 0 or len(entries) >= READY_IDENTITY_MAX_ENTRIES:
                    raise ValueError("dependency directory exceeds bounded identity scan")
                entries.append(entry.name)
        return sorted(entries)

    def open_owned(path: Path) -> tuple[int, Path]:
        # Resolution only inspects link metadata. No file bytes or directory
        # entries are read until the path is proven inside the held owner root.
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root)
        descriptor = os.dup(root_fd)
        try:
            for part in relative.parts[:-1]:
                following = os.open(part, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            if not relative.parts:
                return os.dup(descriptor), resolved
            return os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW
                           | getattr(os, "O_NONBLOCK", 0), dir_fd=descriptor), resolved
        finally:
            os.close(descriptor)

    def add(relative: str, path: Path, details: os.stat_result) -> bool:
        nonlocal remaining_bytes
        if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)
                or stat.S_ISLNK(details.st_mode)):
            return False
        link = os.readlink(path) if stat.S_ISLNK(details.st_mode) else None
        descriptor, resolved = open_owned(path)
        try:
            target = os.fstat(descriptor)
            if stat.S_ISDIR(target.st_mode):
                # Excluded tool-cache churn must not move the tree's identity.
                row = [relative, "link", link, "dir", stat.S_IMODE(target.st_mode), target.st_ino] if link is not None else [
                    relative, "dir", stat.S_IMODE(target.st_mode), target.st_ino]
                directory_states[relative] = (state(target), state(details), resolved)
            elif stat.S_ISREG(target.st_mode):
                if target.st_size > remaining_bytes:
                    return False
                content = hashlib.sha256()
                while block := os.read(descriptor, min(1_048_576, remaining_bytes + 1)):
                    remaining_bytes -= len(block)
                    if remaining_bytes < 0:
                        return False
                    content.update(block)
                after = os.fstat(descriptor)
                if state(target) != state(after):
                    return False
                row = [relative, "file", link, stat.S_IMODE(target.st_mode), target.st_size,
                       target.st_mtime_ns, target.st_ino, target.st_ctime_ns, content.hexdigest()]
            else:
                return False
            if not named_target(path, state(details), state(target), resolved):
                return False
            digest.update(encoded(row) + b"\n")
            return True
        finally:
            os.close(descriptor)

    visited: set[Path] = set()
    try:
        root_identity = state(os.fstat(root_fd))[:3]
        for name in item.get("ready_paths", []):
            path = ready_path(root, name)
            try:
                details = path.lstat()
            except FileNotFoundError:
                digest.update(encoded([name, None]) + b"\n")
                missing_paths.append(path)
                continue
            remaining_entries -= 1
            if remaining_entries < 0 or not add(name, path, details):
                return None
            # Exit frames keep each ancestor descriptor live until its whole
            # subtree has been inspected. A buffered listing alone is not proof
            # that new or replaced inputs were absent during the content walk.
            stack: list[tuple[Path, str, int | None, tuple[int, ...] | None, list[str] | None]] = (
                [(path, name, None, None, None)] if path.is_dir() else [])
            while stack:
                directory, logical, descriptor, before_state, entries = stack.pop()
                if descriptor is not None:
                    assert before_state is not None and entries is not None
                    target_state, entry_state, real = directory_states[logical]
                    if (state(os.fstat(descriptor)) != before_state
                            or names(descriptor, count=False) != entries
                            or not named_target(directory, entry_state, target_state, real)):
                        return None
                    active_directories.remove(descriptor)
                    os.close(descriptor)
                    continue
                descriptor, real = open_owned(directory)
                active_directories.add(descriptor)
                target_state, entry_state, expected_real = directory_states[logical]
                before_state = state(os.fstat(descriptor))
                if (before_state != target_state or real != expected_real
                        or not named_target(directory, entry_state, target_state, real)):
                    return None
                if real in visited:
                    digest.update(encoded([logical, "seen", str(real)]) + b"\n")
                    active_directories.remove(descriptor)
                    os.close(descriptor)
                    continue
                visited.add(real)
                entries = names(descriptor, count=True)
                if state(os.fstat(descriptor)) != before_state:
                    return None
                stack.append((directory, logical, descriptor, before_state, entries))
                children = []
                for entry in entries:
                    full = directory / entry
                    entry_details = full.lstat()
                    if entry in READY_IDENTITY_SKIP and (stat.S_ISDIR(entry_details.st_mode)
                                                         or stat.S_ISLNK(entry_details.st_mode)):
                        continue
                    relative = f"{logical}/{entry}"
                    if not add(relative, full, entry_details):
                        return None
                    if full.is_dir():
                        children.append((full, relative, None, None, None))
                stack.extend(reversed(children))
        if state(root.lstat())[:3] != root_identity or any(os.path.lexists(path) for path in missing_paths):
            return None
        return digest.hexdigest()
    except (OSError, RuntimeError, ValueError):
        # Unknown identity is not missing readiness and is never a cache key.
        return None
    finally:
        for descriptor in active_directories:
            os.close(descriptor)
        os.close(root_fd)


def dotenv_identity(root: Path, plan: dict, key: bytes) -> dict:
    """Keyed digests of ignored .env* files where checks and setup run.

    Frameworks read them during builds and tests, but Git ignores them, so the
    candidate snapshot does not. Keyed so cache files never hold a bare hash.
    """
    directories = {"."} | {item.get("cwd", ".") for item in [*plan["checks"], *plan["environments"]]}
    result = {}
    for directory in sorted(directories):
        for path in sorted(inside(root, directory).glob(".env*")):
            name = path.relative_to(root).as_posix()
            if path.is_symlink():
                # Bind the link text and what the link resolves to: the
                # framework reads the target's bytes, not the link.
                try:
                    data = b"symlink\0" + os.fsencode(os.readlink(path)) + b"\0"
                except OSError:
                    data = b"symlink\0" + unreadable_state(path)
                else:
                    data += dotenv_bytes(path) if path.exists() else b"dangling"
            else:
                data = dotenv_bytes(path)
            result[name] = hmac.new(key, data, hashlib.sha256).hexdigest()
    return result


def unreadable_state(path: Path) -> bytes:
    """What can still be seen of an entry whose contents cannot be read.

    A tool that rewrites or replaces a file cannot keep its inode and ctime,
    so an unreadable file still changes identity when it changes.
    """
    try:
        details = path.stat() if path.exists() else path.lstat()
    except OSError:
        return b"unreadable\0gone"
    return b"unreadable\0" + encoded([details.st_mode, details.st_size, details.st_mtime_ns,
                                      details.st_ino, details.st_ctime_ns])


def file_digest(path: Path) -> str:
    """SHA-256 of a file read in blocks, or its visible state if unreadable."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return unreadable_state(path).hex()


def dotenv_bytes(path: Path) -> bytes:
    """Contents of a .env* file, or of every file under a .env* directory.

    Unreadable entries bind their visible state rather than blocking the run.
    A link inside a directory binds its text and, when it resolves to a file,
    that file's contents; links to directories are not followed, so a link
    cycle cannot loop.
    """
    try:
        if path.is_file():
            return b"regular\0" + path.read_bytes()
        if not path.is_dir():
            return b"other\0"
        rows = []
        stack = [(path, "")]
        while stack:
            directory, prefix = stack.pop()
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                rows.append([prefix, "dir", unreadable_state(directory).hex()])
                continue
            for entry in entries:
                relative = f"{prefix}{entry.name}"
                if entry.is_symlink():
                    try:
                        row = [relative, "link", os.readlink(entry)]
                    except OSError:
                        rows.append([relative, "link", unreadable_state(entry).hex()])
                        continue
                    if entry.is_file():
                        row.append(file_digest(entry))
                    rows.append(row)
                elif entry.is_dir():
                    rows.append([relative, "dir"])
                    stack.append((entry, relative + "/"))
                elif entry.is_file():
                    rows.append([relative, "file", file_digest(entry)])
                else:
                    rows.append([relative, "other"])
        return b"directory\0" + encoded(sorted(rows))
    except OSError:
        return unreadable_state(path)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_bytes(encoded(value) + b"\n")
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--provider", choices=["claude", "codex", "cursor", "devin", "unknown"], default="unknown")
    parser.add_argument("--plan", "--dry-run", dest="plan", action="store_true")
    parser.add_argument("--provision", action="store_true", help="install selected development dependencies once per input/tool digest")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    inherited = git_environment()
    receipt_path = None
    try:
        if not args.plan:
            state = inside(root, STATE)
            receipt_path = state / "receipt.json"
            # Revoke old evidence before config, Git or setup can block this
            # execution. Planning remains a read-only operation.
            receipt_path.unlink(missing_ok=True)
        config = config_for(root)
        plan = plan_for(root, config, args.base)
        tools = versions(root, plan, inherited)
        if args.plan:
            print(json.dumps({**plan, "tool_versions": tools}, indent=2))
            return 0
        # Receipts must be ignored even before first use; never commit local
        # evidence or accidentally add it to its own cache fingerprint.
        if subprocess.run(["git", "check-ignore", "-q", f"{STATE}/receipt.json"],
                          cwd=root, env=git_environment(), timeout=GIT_TIMEOUT).returncode:
            raise ValueError(f"ignore {STATE}/ in .gitignore before executing")
        key = private_cache_key(root)
        before = snapshot(root)
        cache_reusable = not before["symlinks"]
        results = []
        provisioned = []
        reused_environments = []
        results.append(execute(root, {"id": "diff-check", "commands": [["git", "diff", "--check", plan["base"]]]}, inherited))
        for environment in plan["environments"]:
            env_digest = environment_digest(root, environment, tools, key, inherited, plan=plan)
            # One slot per environment prevents an earlier tool fingerprint
            # from resurrecting success after a failed reinstall changes tools.
            env_receipt = state / "environments" / f"environment-{sha(encoded(environment['id']))}.json"
            ready = not missing_ready_paths(root, environment)
            previous_environment = {}
            if env_receipt.is_file():
                try:
                    previous_environment = json.loads(env_receipt.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    print(f"AGENT-CHECK {environment['id']}: invalid environment receipt; frozen setup needs validation")
            current_ready = ready_identity(root, environment, key=key)
            if (isinstance(previous_environment, dict) and previous_environment.get("digest") == env_digest
                    and isinstance(previous_environment.get("result"), dict)
                    and previous_environment["result"].get("status") == "passed" and ready
                    and current_ready is not None and previous_environment.get("ready") == current_ready):
                reused_environments.append((environment, env_receipt, env_digest, current_ready))
                continue
            if not args.provision:
                results.append({"id": environment["id"], "status": "unavailable",
                                "reason": "run again with --provision to validate frozen dependencies"})
                continue
            # An interrupted/failed reinstall must revoke the old success
            # before it can leave a partially populated dependency directory.
            env_receipt.unlink(missing_ok=True)
            input_digest = environment_digest(root, environment, {}, key, inherited, plan=plan)
            result = execute(root, environment, inherited)
            missing = missing_ready_paths(root, environment)
            if result["status"] == "passed" and missing:
                result.update(status="failed", reason="declared ready paths missing: " + ",".join(missing))
            results.append(result)
            if result["status"] == "passed":
                # Bind setup to the tools it actually left ready, before a
                # later selected environment can replace any of those tools.
                tools = versions(root, plan, inherited)
                validated_digest = environment_digest(root, environment, tools, key, inherited, plan=plan)
                validated_ready = ready_identity(root, environment, key=key)
                provisioned.append((environment, env_receipt, input_digest, validated_digest, validated_ready, result))
        # Provisioning may install tools. Missing tools must not be mistaken
        # for a source failure, or cached as a successful old fingerprint.
        tools = versions(root, plan, inherited)
        # A later setup may remove an earlier environment's readiness output.
        # Recheck all selected environments before recording setup or candidate PASS.
        for environment in plan["environments"]:
            missing = missing_ready_paths(root, environment)
            if missing:
                matching = next((r for r in results if r["id"] == environment["id"]), None)
                if matching is None:
                    results.append({"id": environment["id"], "status": "failed",
                                    "reason": "declared ready paths missing: " + ",".join(missing)})
                elif matching["status"] == "passed":
                    matching.update(status="failed", reason="declared ready paths missing: " + ",".join(missing))
                (state / "environments" / f"environment-{sha(encoded(environment['id']))}.json").unlink(missing_ok=True)
        # A cache hit was proved against the initial tools. Later setup can
        # change a shared tool, so it must still match before checks can run.
        # Revoke stale setup evidence and require a bounded fresh invocation.
        for environment, env_receipt, reused_digest, reused_ready in reused_environments:
            if (environment_digest(root, environment, tools, key, inherited, plan=plan) != reused_digest
                    or ready_identity(root, environment, key=key) != reused_ready):
                env_receipt.unlink(missing_ok=True)
                results.append({"id": environment["id"], "status": "failed",
                                "reason": "cached environment invalidated by later provisioning; rerun with --provision"})
        # New setups also keep their actual validated context. Never re-key an
        # earlier setup as compatible with a tool installed by a later one.
        for environment, env_receipt, input_digest, validated_digest, validated_ready, result in provisioned:
            if result["status"] != "passed":
                continue
            if (environment_digest(root, environment, {}, key, inherited, plan=plan) != input_digest
                    or environment_digest(root, environment, tools, key, inherited, plan=plan) != validated_digest
                    or ready_identity(root, environment, key=key) != validated_ready):
                env_receipt.unlink(missing_ok=True)
                result.update(status="failed",
                              reason="provisioned environment invalidated by later provisioning or changed inputs; rerun with --provision")
            else:
                write_json(env_receipt, {"digest": validated_digest, "result": result, "ready": validated_ready})
        ready_identities = {item["id"]: ready_identity(root, item, key=key) for item in plan["environments"]}
        identity = {"schema": SCHEMA, "source": sha(Path(__file__).read_bytes()),
                    "config": config, "plan": plan, "tools": tools, "snapshot": before,
                    "dotenv": dotenv_identity(root, plan, key),
                    # A cached PASS is bound to the dependency trees it ran
                    # against, so reprovisioned or edited dependencies rerun checks.
                    "ready": ready_identities,
                    "execution_environment": environment_fingerprint(key, {
                        "default": execution_environment({}, inherited),
                        "checks": {item["id"]: execution_environment(item, inherited) for item in plan["checks"]},
                        "environments": {item["id"]: execution_environment(item, inherited) for item in plan["environments"]}})}
        cache_reusable = cache_reusable and all(value is not None for value in ready_identities.values())
        candidate_digest = sha(encoded(identity))
        cache = state / "cache" / f"{candidate_digest}.json"
        unavailable = [name for name, version in tools.items() if version == "unavailable"]
        if unavailable:
            results.append({"id": "toolchain", "status": "unavailable", "reason": ",".join(unavailable)})
        for name, prefix in config.get("tool_requirements", {}).items():
            for context, version in tools.items():
                if context.partition("@")[0] == name and not version.startswith(prefix):
                    results.append({"id": f"toolchain-{context}", "status": "unavailable",
                                    "reason": f"requires {prefix}; provision the CI toolchain before validation"})
        def require_stable_inputs() -> None:
            # These are repeated observations, not an atomic filesystem snapshot.
            # Recheck after reading a cache entry as well as after fresh checks.
            checks = [
                ("input-stability", snapshot(root) == before,
                 "inputs changed during validation; inspect diff and rerun"),
                ("context-stability", dotenv_identity(root, plan, key) == identity["dotenv"]
                 and versions(root, plan, inherited) == tools,
                 "dotenv or tool context changed during validation; inspect setup and rerun"),
                ("dependency-stability", ready_identities == {
                    item["id"]: ready_identity(root, item, key=key) for item in plan["environments"]},
                 "dependency inputs changed during validation; inspect setup and rerun"),
            ]
            for identifier, stable, reason in checks:
                if not stable and not any(r["id"] == identifier for r in results):
                    results.append({"id": identifier, "status": "failed", "reason": reason})

        require_stable_inputs()
        if cache_reusable and all(r["status"] == "passed" for r in results) and cache.is_file() and not args.no_cache:
            previous = json.loads(cache.read_text(encoding="utf-8"))
            if previous.get("digest") == candidate_digest and previous.get("outcome") == "passed":
                require_stable_inputs()
                if all(r["status"] == "passed" for r in results):
                    previous = {**previous, "provider": args.provider, "cached": True}
                    write_json(state / "receipt.json", previous)
                    print(f"AGENT-CHECK-CACHED {candidate_digest[:12]}")
                    return 0
        if all(r["status"] == "passed" for r in results):
            results.extend(execute(root, check, inherited) for check in plan["checks"])
        require_stable_inputs()
        outcome = "passed" if all(r["status"] == "passed" for r in results) else "failed"
        receipt = {"schema": SCHEMA, "digest": candidate_digest, "provider": args.provider, "cached": False,
                   "cache_reusable": cache_reusable,
                   "created_at": time.time(), "outcome": outcome, "snapshot": before,
                   "base": plan["base"], "tool_versions": tools, "results": results,
                   "review_paths": plan["review_paths"], "merge_authority": False}
        write_json(state / "receipt.json", receipt)
        if outcome == "passed" and cache_reusable:
            write_json(cache, receipt)
        elif outcome == "failed":
            # A fresh failure outranks an earlier PASS for the same identity:
            # something outside the identity changed, so never serve it again.
            cache.unlink(missing_ok=True)
        for result in results:
            if result["status"] != "passed":
                print("AGENT-CHECK-RESULT " + json.dumps(
                    {key: result[key] for key in ("id", "status", "reason")},
                    sort_keys=True,
                ))
        print(f"AGENT-CHECK-{outcome.upper()} {candidate_digest[:12]}")
        return 0 if outcome == "passed" else 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if receipt_path is not None:
            try:
                receipt_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                print(f"AGENT-CHECK-BLOCKED: cannot revoke receipt: {cleanup_error}", file=sys.stderr)
        print(f"AGENT-CHECK-BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
