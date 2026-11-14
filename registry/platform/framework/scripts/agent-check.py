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
import re
import secrets
import stat
import subprocess
import sys
import time

SCHEMA = "agent-check/v1"
STATE = ".ai/agent-checks"
GIT_TIMEOUT = 30
# Owner check/environment names cannot shadow framework stability failures.
INTERNAL_STABILITY_IDS = {"input-stability", "context-stability", "dependency-stability"}

SOURCE_SCOPE_POLICY = ".ai/source-scope-policy.json"
SOURCE_SCOPE_PIN = "AGENT_SOURCE_SCOPE_SHA256"
SOURCE_SCOPE_REPOSITORY = "AGENT_SOURCE_SCOPE_REPOSITORY"
SOURCE_SCOPE_EXIT = 4


class SourceScopeError(ValueError):
    """Content is outside this runner's qualified scope; never a cached PASS."""

    metadata_reason: str | None = None


_SOURCE_SCOPE_METADATA_REASONS = frozenset({
    "unqualified Git metadata alias", "missing Git control metadata", "unqualified Git control metadata",
    "no qualified no-follow metadata reader", "Git control metadata changed",
    "no qualified descriptor metadata reader", "Git metadata directory bound exceeded",
    "Git metadata entry bound exceeded", "Git metadata directory changed",
    "Git metadata aliases require a qualified adapter", "unqualified symbolic HEAD route",
    "symbolic HEAD route bound exceeded", "unqualified Git directory pointer",
    "unqualified linked-worktree control namespace", "unqualified linked-worktree metadata",
    "unqualified linked-worktree topology", "linked-worktree ownership differs",
    "Git config includes require a qualified metadata adapter",
    "Git object alternates require a qualified metadata adapter",
    "source-scope ownership ancestry is ambiguous", "source-scope ownership ancestry bound exceeded",
})


def _source_scope_failure(error: BaseException) -> SourceScopeError:
    """Expose only closed static diagnostics, never exception paths or bytes."""
    reason = (error.args[0] if type(error) is ValueError and len(error.args) == 1
              and type(error.args[0]) is str and error.args[0] in _SOURCE_SCOPE_METADATA_REASONS else None)
    detail = ": " + reason if reason is not None else ""
    held = SourceScopeError("source-scope metadata unavailable" + detail + "; content reads held")
    held.metadata_reason = reason
    return held


def _source_scope_exists(path: Path) -> bool:
    """An inaccessible declaration is unknown, never evidence of absence."""
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError("source-scope metadata unavailable") from None


def _source_scope_resolved_path(path: Path) -> Path:
    """Resolve raw components; a symlink cycle before .. is still a cycle."""
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        # Keep absent/dangling observations. The original path is used for
        # payload I/O, so a missing intermediate component cannot open data.
        return path.resolve()


def _source_scope_roots(root: Path) -> tuple[list[Path], bool]:
    """Retain lexical declarations; qualify physical controls outer-owner first."""
    lexical = Path(os.path.abspath(root))
    routes = []
    # Resolving directory names must not erase an enclosing lexical policy.
    # Conversely, an alias must not hide the actual owner's enclosing policy.
    # Resolve the raw request: normpath first changes symlink/.. semantics.
    for path in dict.fromkeys((lexical, _source_scope_resolved_path(root))):
        roots = []
        for parent in (path, *path.parents):
            if _source_scope_exists(parent / SOURCE_SCOPE_POLICY):
                return [], True
            if _source_scope_exists(parent / ".git"):
                roots.append(parent.resolve())
        routes.append(list(dict.fromkeys(reversed(roots))))
    owners = dict.fromkeys(owner for route in routes for owner in route)
    if len(owners) > 256:
        raise ValueError("source-scope ownership ancestry bound exceeded")
    before: dict[Path, set[Path]] = {owner: set() for owner in owners}
    for route in routes:
        for outer, inner in zip(route, route[1:]):
            before[inner].add(outer)
    ordered = []
    while before:
        available = next((owner for owner, required in before.items() if not required), None)
        if available is None:
            raise ValueError("source-scope ownership ancestry is ambiguous")
        ordered.append(available)
        del before[available]
        for required in before.values():
            required.discard(available)
    # Any committed enclosing declaration holds before inner control bytes.
    # The .git marker itself is deliberately not resolved; aliases still hold.
    return ordered, False


def _source_scope_control_metadata(path: Path, *, optional: bool = False):
    """Check a control file without opening it or any aliased payload."""
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError("unqualified Git metadata alias")
    try:
        before = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError("missing Git control metadata") from None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65536:
        raise ValueError("unqualified Git control metadata")
    return before


def _source_scope_control_bytes(path: Path, *, optional: bool = False) -> bytes | None:
    """Read bounded verified control metadata, never an alias/include target."""
    read = _source_scope_control_read(path, optional=optional)
    return None if read is None else read[0]


def _source_scope_control_read(path: Path, *, optional: bool = False) -> tuple[bytes, tuple] | None:
    """Return verified control bytes with the descriptor identity they were read from."""
    before = _source_scope_control_metadata(path, optional=optional)
    if before is None:
        return None
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("no qualified no-follow metadata reader")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink) != (
                before.st_dev, before.st_ino, before.st_mode, 1):
            raise ValueError("Git control metadata changed")
        data = os.read(descriptor, 65537)
        after = os.fstat(descriptor)
        if (len(data) > 65536 or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("Git control metadata changed")
        return data, (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    finally:
        os.close(descriptor)


def _source_scope_alias_metadata(path: Path) -> None:
    """Bounded descriptor-only alias check; never read Git object payload bytes."""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("no qualified descriptor metadata reader")
    count = 0
    def visit(descriptor, depth):
        nonlocal count
        if depth > 128:
            raise ValueError("Git metadata directory bound exceeded")
        before = os.fstat(descriptor)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                count += 1
                if count > 100000:
                    raise ValueError("Git metadata entry bound exceeded")
                details = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(details.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                            raise ValueError("Git metadata directory changed")
                        visit(child, depth + 1)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise ValueError("Git metadata aliases require a qualified adapter")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Git metadata directory changed")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(descriptor, 0)
    finally:
        os.close(descriptor)


def _source_scope_head_chain(gitdir: Path, common: Path) -> tuple | None:
    """Bound symbolic HEAD to checked ref namespaces, never other worktrees.

    Returns the exact route observation (path, bytes, descriptor identity per
    hop) only when it ends in a loose object id; otherwise None.
    """
    path = gitdir / "HEAD"
    read = _source_scope_control_read(path)
    route = []
    for _ in range(32):
        if read is None:
            return None  # A missing loose ref may resolve through checked packed refs.
        raw, identity = read
        route.append((str(path), raw, identity))
        text = raw.decode("utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text):
            return tuple(route)
        if not text.startswith("ref: refs/"):
            raise ValueError("unqualified symbolic HEAD route")
        reference = text[5:]
        parts = reference.split("/")
        if (any(part in ("", ".", "..") for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in reference)
                or "\\" in reference):
            raise ValueError("unqualified symbolic HEAD route")
        # Git keeps these namespaces private to the current linked worktree.
        private = reference.startswith(("refs/bisect/", "refs/worktree/", "refs/rewritten/"))
        path = (gitdir if private else common) / reference
        read = _source_scope_control_read(path, optional=True)
    raise ValueError("symbolic HEAD route bound exceeded")


def _source_scope_pointer_path(parent: Path, target: str) -> Path:
    """Reject raw control aliases before normalization can erase their route."""
    requested = parent / target
    for path in (requested, *requested.parents):
        if path.is_symlink():
            raise ValueError("unqualified Git metadata alias")
    return requested.resolve(strict=True)


def _source_scope_git_controls(root: Path) -> tuple | None:
    """Reject config indirections BEFORE native Git can interpret their targets.

    These repeated observations are not an atomic filesystem/host guarantee.
    Independently pinned owning scope inputs refuse before this metadata route.
    Returns this call's control observation when HEAD ends in a loose object id.
    """
    marker = root / ".git"
    if stat.S_ISDIR(marker.lstat().st_mode):
        gitdir = marker
        common = gitdir
    else:
        raw = _source_scope_control_bytes(marker)
        if raw is None or not raw.startswith(b"gitdir: ") or len(raw.splitlines()) != 1:
            raise ValueError("unqualified Git directory pointer")
        target = raw.decode("utf-8").strip()[8:]
        gitdir = _source_scope_pointer_path(root, target)
        # Qualify the standard control namespace before opening anything at the
        # pointer target. Arbitrary directories are not metadata read grants.
        if gitdir.parent.name != "worktrees" or not gitdir.parent.parent.name.endswith(".git"):
            raise ValueError("unqualified linked-worktree control namespace")
        common_raw = _source_scope_control_bytes(gitdir / "commondir")
        if common_raw is None or len(common_raw.splitlines()) != 1:
            raise ValueError("unqualified linked-worktree metadata")
        common = _source_scope_pointer_path(gitdir, common_raw.decode("utf-8").strip())
        # Only the standard linked-worktree topology is qualified here. An
        # arbitrary Git pointer must not expand this metadata read boundary.
        if gitdir.parent.name != "worktrees" or gitdir.parent.parent != common:
            raise ValueError("unqualified linked-worktree topology")
        backpointer = _source_scope_control_bytes(gitdir / "gitdir")
        if backpointer is None or _source_scope_pointer_path(gitdir, backpointer.decode("utf-8").strip()) != marker:
            raise ValueError("linked-worktree ownership differs")
    configs = []
    for path in (common / "config", gitdir / "config.worktree"):
        raw = _source_scope_control_bytes(path, optional=True)
        if raw is not None and re.search(rb"\[\s*include", raw, re.IGNORECASE):
            raise ValueError("Git config includes require a qualified metadata adapter")
        configs.append(raw)
    # Native Git must not follow alternate-object routing from this probe.
    if _source_scope_exists(common / "objects/info/alternates"):
        raise ValueError("Git object alternates require a qualified metadata adapter")
    for directory in dict.fromkeys((common, gitdir)):
        for leaf in ("packed-refs", "shallow"):
            _source_scope_control_metadata(directory / leaf, optional=True)
        # The fixed metadata probe disables graft interpretation independently;
        # static graft aliases also hold without opening any graft contents.
        _source_scope_control_metadata(directory / "info/grafts", optional=True)
    route = _source_scope_head_chain(gitdir, common)
    # Native fixed-argv tree metadata probes may access refs, loose objects or
    # pack/index metadata. Reject static external aliases before any such Git
    # access. No file in these directories is opened for content by this walk.
    for path in (common / "refs", common / "objects"):
        _source_scope_alias_metadata(path)
    private_refs = gitdir / "refs"
    if gitdir != common and _source_scope_exists(private_refs):
        _source_scope_alias_metadata(private_refs)
    return None if route is None else (str(root), str(gitdir), str(common), tuple(configs), route)


# Undeclared fixed-probe answers, keyed by the complete control observation made
# for that call: owner/control paths, config bytes and every HEAD route hop
# (bytes plus descriptor identity) ending in a commit id. A commit id names an
# immutable tree, so the answer is reused only while a fresh observation is
# identical. Every call still re-runs pins, enclosing declarations, control
# checks and the alias walk; any route change (commit, checkout, rewritten ref)
# is a miss. Declared, unborn and packed-only routes are never memoized.
_SOURCE_SCOPE_UNDECLARED_TREES: set = set()


def _source_scope_route_unchanged(observation: tuple) -> bool:
    try:
        return _source_scope_head_chain(Path(observation[1]), Path(observation[2])) == observation[-1]
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def _source_scope_tree_declared(owner: Path, environment: dict, timeout: int,
                                observation: tuple | None = None) -> bool:
    """Use fixed metadata probes; distinguish a verified unborn branch from failure."""
    key = None if observation is None else (observation, tuple(sorted(environment.items())))
    if key is not None and key in _SOURCE_SCOPE_UNDECLARED_TREES:
        return False
    def metadata(*arguments):
        return subprocess.run(["git", "--no-replace-objects", "-c", "protocol.allow=never", *arguments],
                              cwd=owner, env=environment, capture_output=True, timeout=timeout, check=False)
    result = metadata("ls-tree", "-z", "HEAD", "--", SOURCE_SCOPE_POLICY)
    if result.stdout:
        return True
    if result.returncode == 0:
        # Record only if HEAD did not move while the probe ran.
        if key is not None and _source_scope_route_unchanged(observation):
            if len(_SOURCE_SCOPE_UNDECLARED_TREES) >= 256:
                _SOURCE_SCOPE_UNDECLARED_TREES.clear()
            _SOURCE_SCOPE_UNDECLARED_TREES.add(key)
        return False
    head = metadata("rev-parse", "--verify", "--quiet", "HEAD")
    symbolic = metadata("symbolic-ref", "--quiet", "HEAD")
    ref = symbolic.stdout.decode("utf-8").strip()
    unborn = (head.returncode != 0 and symbolic.returncode == 0
              and ref.startswith("refs/heads/") and "\0" not in ref and "\n" not in ref
              and metadata("check-ref-format", ref).returncode == 0
              and metadata("show-ref", "--verify", "--quiet", ref).returncode == 1)
    if not unborn:
        raise ValueError("source-scope metadata unavailable")
    return False


def _source_scope_git_environment() -> dict:
    """Forward only runtime context to non-authorizing local Git metadata probes."""
    names = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
             "TMPDIR", "TMP", "TEMP", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    environment = {key: os.environ[key] for key in names if key in os.environ}
    environment.update({"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_GRAFT_FILE": os.devnull})
    return environment


def source_scope_declared(root: Path) -> bool:
    """Check enclosing policy declarations and safely bounded Git metadata."""
    if SOURCE_SCOPE_PIN in os.environ or SOURCE_SCOPE_REPOSITORY in os.environ:
        return True
    try:
        roots, declared = _source_scope_roots(root)
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise _source_scope_failure(error) from None
    if declared:
        return True
    # Probe-only environment: inherited Git tracing/config/executable routing
    # must not cause reads or side writes before this non-authorizing hold.
    # Normal owner git_environment()/writers retain their existing behavior.
    environment = _source_scope_git_environment()
    for owner in roots:
        try:
            observation = _source_scope_git_controls(owner)
            if _source_scope_tree_declared(owner, environment, GIT_TIMEOUT, observation):
                return True
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            raise _source_scope_failure(error) from None
    return False


def validate_source_scope_policy(raw: bytes, *, expected_sha256: str, repository: str) -> dict:
    """Validate already trusted compiled bytes; this never grants execution scope."""
    if (not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
            or expected_sha256 == "0" * 64 or not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not isinstance(raw, bytes) or len(raw) > 16384 or sha(raw) != expected_sha256):
        raise SourceScopeError("source-scope policy differs from independent owner pins")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise SourceScopeError("duplicate source-scope policy field")
            value[key] = item
        return value
    try:
        policy = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(SourceScopeError("invalid scope number")))
        if (not isinstance(policy, dict)
                or set(policy) != {"schema_version", "repository", "scope", "allowed_paths", "protected_paths"}
                or type(policy["schema_version"]) is not int or policy["schema_version"] != 1
                or policy["repository"] != repository or policy["scope"] != "source-only"):
            raise SourceScopeError("source-scope policy identity or fields differ")
        for field in ("allowed_paths", "protected_paths"):
            values = policy[field]
            if (not isinstance(values, list) or not values or len(values) > 256
                    or any(not isinstance(value, str) or not value or len(value) > 500
                           or value.startswith("/") or "\\" in value or "\0" in value
                           or any(ord(character) < 32 or ord(character) == 127 for character in value)
                           or any(part in ("", ".", "..") for part in value.removesuffix("/").split("/"))
                           or any(character in value for character in "*?[]\r\n") for value in values)
                    or len(set(values)) != len(values)):
                raise SourceScopeError("source-scope paths must be unique bounded literal paths")
            for value in values:
                value.encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SourceScopeError("source-scope compiled policy invalid") from None
    return policy


def require_source_scope(root: Path) -> None:
    """Refuse broad validation without opening even the working policy file.

    The fixed declaration or independent owner pin is a hold, never an allow
    switch. A future projection must verify trusted compiled policy bytes and
    physical absence of opaque objects before any restricted-scope execution.
    """
    if source_scope_declared(root):
        raise SourceScopeError("source-scope requires a reviewed isolated projection; full validation held")


def require_content_path_scope(path: Path) -> None:
    """Check lexical and resolved owners before any linked payload is opened."""
    if SOURCE_SCOPE_PIN in os.environ or SOURCE_SCOPE_REPOSITORY in os.environ:
        raise SourceScopeError("source-scope content reads require a reviewed isolated projection")
    requested = path.absolute()
    require_source_scope(requested.parent)
    try:
        parent = _source_scope_resolved_path(requested.parent)
        selected = _source_scope_resolved_path(requested)
        if selected.parent != parent:
            require_source_scope(selected.parent)
        try:
            details = selected.stat()
        except FileNotFoundError:
            return  # Preserve legacy absent/dangling input state observations.
        if stat.S_ISDIR(details.st_mode):
            require_source_scope(selected)
    except SourceScopeError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise _source_scope_failure(error) from None


TOOLS = {
    "git": ["git", "--version"],
    # Optional Pack Linux source-executor contract; never selected by the
    # portable consumer recipe. Unknown/missing tools remain unavailable.
    "bwrap": ["/usr/bin/bwrap", "--version"],
    "cc": ["/usr/bin/cc", "--version"],
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
    require_source_scope(root)
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
    require_source_scope(root)
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


def require_unfiltered_tracked_inputs(root: Path, *, text_checkout_lf: bool = False) -> set[str]:
    """Reject Git metadata that can conceal raw working-file changes."""
    require_source_scope(root)
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
    conversions = {b"crlf", b"eol", b"ident", b"working-tree-encoding"}
    # A text declaration alone cannot change canonical bytes on LF checkout.
    # Explicit eol/ident/encoding and all global CRLF cases remain conservative.
    if not text_checkout_lf:
        conversions.add(b"text")
    return {os.fsdecode(attributes[i]) for i in range(0, len(attributes), 3)
            if attributes[i + 1] in conversions}


def directory_state(details: os.stat_result) -> tuple:
    # Reading a directory may update atime, including on every strictatime
    # filesystem scan. Identity, mode and mutation times carry the proof.
    return (details.st_dev, details.st_ino, details.st_mode,
            details.st_mtime_ns, details.st_ctime_ns)


def empty_gitlink_identity(root: Path, name: str) -> str | None:
    require_source_scope(root)
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
    require_source_scope(root)
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
    require_source_scope(root)
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
    require_source_scope(root)
    config = json.loads(inside(root, "config/agent-checks.json").read_text(encoding="utf-8"))
    if config.get("schema_version") != 1 or not config.get("base"):
        raise ValueError("config/agent-checks.json requires schema_version=1 and base")
    identifiers = set()
    for kind in ("checks", "environments"):
        for item in config.get(kind, []):
            identifier = item.get("id", "")
            if not identifier or identifier in identifiers:
                raise ValueError("check and environment ids must be nonempty and unique")
            if identifier in INTERNAL_STABILITY_IDS:
                raise ValueError(f"{identifier}: reserved validation result id; rename the owner check or environment")
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
    require_source_scope(root)
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
    require_source_scope(root)
    # Include conservative potential conversions even when bytes equal the
    # canonical blob (e.g. an expanded ident replaced by literal $Id$).
    autocrlf = git(root, "config", "--default=false", "--get", "core.autocrlf").strip().lower()
    global_conversion = autocrlf not in {b"false", b"no", b"off", b"0", b"input"}
    eol = git(root, "config", "--default=native", "--get", "core.eol").strip().lower()
    lf_checkout = not global_conversion and (eol == b"lf" or (eol == b"native" and os.name != "nt"))
    conversion_paths = require_unfiltered_tracked_inputs(root, text_checkout_lf=lf_checkout)
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
    require_source_scope(root)
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
    require_source_scope(root)
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


def changed_tool_contexts(previous: dict, current: dict) -> list[str]:
    # Classify observed availability/output changes; never disclose raw values.
    changed = []
    for name in sorted(previous.keys() | current.keys()):
        if name in previous and name in current and previous[name] == current[name]:
            continue
        if name not in previous:
            transition = "context-added"
        elif name not in current:
            transition = "context-removed"
        elif previous[name] == "unavailable":
            transition = "unavailable-to-available"
        elif current[name] == "unavailable":
            transition = "available-to-unavailable"
        else:
            transition = "available-output-changed"
        context = "platform" if name == "platform" else "tool:" + name
        changed.append(context + "[" + transition + "]")
    return changed


def report_context_change(identifier: str, changed: list[str]) -> None:
    # Optional stderr evidence preserves receipt schemas and old reason strings.
    print("AGENT-CHECK-DIAGNOSTIC " + json.dumps({"id": identifier, "changed": changed}), file=sys.stderr)


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
    require_source_scope(root)
    inputs = {}
    for name in item.get("inputs", []):
        path = inside(root, name)
        inputs[name] = sha(path.read_bytes()) if path.is_file() else None
    return sha(encoded({"tool_contract": "agent-environment-tools/v2", "recipe": item, "inputs": inputs,
                        "tools": environment_tools(item, tools, plan) if plan is not None else tools,
                        "execution_environment": environment_fingerprint(key, execution_environment(item, inherited))}))


def ready_path(root: Path, value: str) -> Path:
    """Keep the declared name local; external links are usable but never cached."""
    require_source_scope(root)
    path = root / value
    if Path(value).is_absolute() or not Path(os.path.abspath(path)).is_relative_to(root):
        raise ValueError(f"path must stay inside the repository: {value}")
    # Preserve filesystem semantics of ".." through a link. Lexical collapse
    # would probe a different readiness target; open_owned rejects external data.
    return path


def missing_ready_paths(root: Path, item: dict) -> list[str]:
    require_source_scope(root)
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
    require_source_scope(root)
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


def readiness_status(previous: str | None, current: str | None) -> str:
    """Distinguish observed byte changes from an unavailable comparison.

    Two unknown observations still allow fresh execution without cache reuse.
    A known identity on only one side cannot prove unchanged or changed bytes.
    """
    if previous == current:
        return "passed"
    return "unavailable" if previous is None or current is None else "failed"


def dotenv_identity(root: Path, plan: dict, key: bytes) -> dict[str, str | None]:
    """Keyed digests of ignored .env* files where checks and setup run.

    Frameworks read them during builds and tests, but Git ignores them, so the
    candidate snapshot does not. Keyed so cache files never hold a bare hash.
    """
    require_source_scope(root)
    directories = {"."} | {item.get("cwd", ".") for item in [*plan["checks"], *plan["environments"]]}
    result: dict[str, str | None] = {}
    budget = [READY_IDENTITY_MAX_ENTRIES, READY_IDENTITY_MAX_BYTES]
    for directory in sorted(directories):
        paths = []
        try:
            with os.scandir(inside(root, directory)) as entries:
                for entry in entries:
                    budget[0] -= 1
                    if budget[0] < 0:
                        result[f"{directory}/.env*"] = None
                        return result
                    if entry.name.startswith(".env"):
                        paths.append(Path(entry.path))
        except OSError:
            result[f"{directory}/.env*"] = None
            continue
        for path in sorted(paths):
            name = path.relative_to(root).as_posix()
            data: bytes | None
            if path.is_symlink():
                budget[0] -= 1
                if budget[0] < 0:
                    result[name] = None
                    continue
                # Bind the link text and what the link resolves to: the
                # framework reads the target's bytes, not the link.
                try:
                    link_data = b"symlink\0" + os.fsencode(os.readlink(path)) + b"\0"
                except OSError:
                    data = b"symlink\0" + unreadable_state(path)
                else:
                    target = dotenv_bytes(path, budget=budget) if path.exists() else b"dangling"
                    data = link_data + target if target is not None else None
            else:
                data = dotenv_bytes(path, budget=budget)
            result[name] = hmac.new(key, data, hashlib.sha256).hexdigest() if data is not None else None
    return result


def unreadable_state(path: Path) -> bytes:
    """What can still be seen of an entry whose contents cannot be read.

    A tool that rewrites or replaces a file cannot keep its inode and ctime,
    so an unreadable file still changes identity when it changes.
    """
    require_content_path_scope(path)
    try:
        details = path.stat() if path.exists() else path.lstat()
    except OSError:
        return b"unreadable\0gone"
    return b"unreadable\0" + encoded([details.st_mode, details.st_size, details.st_mtime_ns,
                                      details.st_ino, details.st_ctime_ns])


def file_digest(path: Path) -> str:
    """SHA-256 of a file read in blocks, or its visible state if unreadable."""
    require_content_path_scope(path)
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return unreadable_state(path).hex()


def dotenv_bytes(path: Path, *, budget: list[int] | None = None) -> bytes | None:
    """Contents of a .env* file, or of every file under a .env* directory.

    Unreadable entries bind their visible state rather than blocking the run.
    A link inside a directory binds its text and, when it resolves to a file,
    that file's contents; links to directories are not followed, so a link
    cycle cannot loop. Shared entry/byte exhaustion returns None, never a
    reusable identity; all payload reads are bounded before growth.
    """
    require_content_path_scope(path)
    if budget is None:
        budget = [READY_IDENTITY_MAX_ENTRIES, READY_IDENTITY_MAX_BYTES]

    def digest_file(file: Path) -> str:
        require_content_path_scope(file)
        details = file.stat()
        if details.st_size > budget[1]:
            raise OverflowError("dotenv identity byte limit")
        digest = hashlib.sha256()
        try:
            with file.open("rb") as stream:
                while block := stream.read(min(1 << 20, budget[1] + 1)):
                    budget[1] -= len(block)
                    if budget[1] < 0:
                        raise OverflowError("dotenv identity byte limit")
                    digest.update(block)
            return digest.hexdigest()
        except OSError:
            return unreadable_state(file).hex()

    try:
        budget[0] -= 1
        if budget[0] < 0:
            return None
        if path.is_file():
            return b"regular-sha256\0" + digest_file(path).encode("ascii")
        if not path.is_dir():
            return b"other\0"
        rows = []
        stack = [(path, "")]
        while stack:
            directory, prefix = stack.pop()
            require_content_path_scope(directory)
            try:
                entries = []
                with os.scandir(directory) as stream:
                    for directory_entry in stream:
                        budget[0] -= 1
                        if budget[0] < 0:
                            return None
                        entries.append(Path(directory_entry.path))
                entries.sort()
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
                        row.append(digest_file(entry))
                    rows.append(row)
                elif entry.is_dir():
                    rows.append([relative, "dir"])
                    stack.append((entry, relative + "/"))
                elif entry.is_file():
                    rows.append([relative, "file", digest_file(entry)])
                else:
                    rows.append([relative, "other"])
        return b"directory\0" + encoded(sorted(rows))
    except OverflowError:
        return None
    except OSError:
        return unreadable_state(path)


def write_json(path: Path, value: dict) -> None:
    require_content_path_scope(path)
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
    parser.add_argument("--select-only", action="store_true", help="with --plan, classify without tool probes or readiness evidence")
    parser.add_argument("--provision", action="store_true", help="install selected development dependencies once per input/tool digest")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)
    if args.select_only and not args.plan:
        parser.error("--select-only requires --plan and cannot qualify execution")
    requested_root = args.repo_root.absolute()
    inherited = git_environment()
    receipt_path = None
    try:
        require_source_scope(requested_root)
        try:
            root = requested_root.resolve()
        except (OSError, ValueError, RuntimeError) as error:
            raise _source_scope_failure(error) from None
        if not args.plan:
            state = inside(root, STATE)
            receipt_path = state / "receipt.json"
            # Revoke old evidence before config, Git or setup can block this
            # execution. Planning remains a read-only operation.
            receipt_path.unlink(missing_ok=True)
        config = config_for(root)
        plan = plan_for(root, config, args.base)
        if args.plan and args.select_only:
            print(json.dumps(plan, indent=2))
            return 0
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
        # Candidate authority starts at the readiness actually validated by
        # setup/reuse. A later scan may compare it, never replace this baseline.
        ready_identities: dict[str, str | None] = {}
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
                ready_identities[environment["id"]] = current_ready
                reused_environments.append((environment, env_receipt, env_digest, current_ready,
                    environment_tools(environment, tools, plan),
                    environment_fingerprint(key, execution_environment(environment, inherited))))
                continue
            if not args.provision:
                results.append({"id": environment["id"], "status": "unavailable",
                                "reason": "run again with --provision to validate frozen dependencies"})
                continue
            # An interrupted/failed reinstall must revoke the old success
            # before it can leave a partially populated dependency directory.
            env_receipt.unlink(missing_ok=True)
            setup_execution_context = environment_fingerprint(key, execution_environment(environment, inherited))
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
                ready_identities[environment["id"]] = validated_ready
                provisioned.append((environment, env_receipt, input_digest, validated_digest, validated_ready, result,
                    environment_tools(environment, tools, plan), setup_execution_context))
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
        for environment, env_receipt, reused_digest, reused_ready, reused_tools, reused_context in reused_environments:
            invalidated = False
            status = "failed"
            changed = []
            if environment_digest(root, environment, tools, key, inherited, plan=plan) != reused_digest:
                invalidated = True
                changed = changed_tool_contexts(reused_tools, environment_tools(environment, tools, plan))
                if not changed:
                    changed = ["execution-environment" if environment_fingerprint(
                        key, execution_environment(environment, inherited)) != reused_context else "setup-context"]
            elif (readiness := readiness_status(reused_ready, ready_identity(root, environment, key=key))) != "passed":
                invalidated, status = True, readiness
                changed = ["dependency-readiness" if status == "failed" else "dependency-readiness-unavailable"]
            if invalidated:
                env_receipt.unlink(missing_ok=True)
                results.append({"id": environment["id"], "status": status,
                                "reason": "dependency readiness comparison unavailable; rerun with --provision"
                                if status == "unavailable" else
                                "cached environment invalidated by later provisioning; rerun with --provision"})
                report_context_change(environment["id"], changed)
        # Diagnostic observations are captured in this invocation; old receipts
        # do not supply missing historical tool or execution-context vectors.
        for (environment, env_receipt, input_digest, validated_digest, validated_ready, result,
             validated_tools, setup_context) in provisioned:
            if result["status"] != "passed":
                continue
            invalidated = False
            status = "failed"
            changed = []
            if environment_digest(root, environment, {}, key, inherited, plan=plan) != input_digest:
                invalidated = True
                changed = ["execution-environment" if environment_fingerprint(
                    key, execution_environment(environment, inherited)) != setup_context else "setup-inputs"]
            elif environment_digest(root, environment, tools, key, inherited, plan=plan) != validated_digest:
                invalidated = True
                changed = changed_tool_contexts(validated_tools, environment_tools(environment, tools, plan)) or ["setup-context"]
            elif (readiness := readiness_status(validated_ready, ready_identity(root, environment, key=key))) != "passed":
                invalidated, status = True, readiness
                changed = ["dependency-readiness" if status == "failed" else "dependency-readiness-unavailable"]
            if invalidated:
                env_receipt.unlink(missing_ok=True)
                result.update(status=status,
                              reason="dependency readiness comparison unavailable; rerun with --provision"
                              if status == "unavailable" else
                              "provisioned environment invalidated by later provisioning or changed inputs; rerun with --provision")
                report_context_change(environment["id"], changed)
            else:
                write_json(env_receipt, {"digest": validated_digest, "result": result, "ready": validated_ready})
        identity: dict = {"schema": SCHEMA, "source": sha(Path(__file__).read_bytes()),
                    "config": config, "plan": plan, "tools": tools, "snapshot": before,
                    "dotenv": dotenv_identity(root, plan, key),
                    # A cached PASS is bound to the dependency trees it ran
                    # against, so reprovisioned or edited dependencies rerun checks.
                    "ready": ready_identities,
                    "execution_environment": environment_fingerprint(key, {
                        "default": execution_environment({}, inherited),
                        "checks": {item["id"]: execution_environment(item, inherited) for item in plan["checks"]},
                        "environments": {item["id"]: execution_environment(item, inherited) for item in plan["environments"]}})}
        cache_reusable = (cache_reusable and len(ready_identities) == len(plan["environments"])
                          and all(value is not None for value in ready_identities.values())
                          and all(value is not None for value in identity["dotenv"].values()))
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
            nonlocal cache_reusable
            # These are repeated observations, not an atomic filesystem snapshot.
            # Recheck after reading a cache entry as well as after fresh checks.
            source_stable = snapshot(root) == before
            current_dotenv = dotenv_identity(root, plan, key)
            if any(value is None for value in current_dotenv.values()):
                cache_reusable = False
            if current_dotenv != identity["dotenv"]:
                context_stable, changed_context = False, ["dotenv"]
            else:
                current_tools = versions(root, plan, inherited)
                context_stable = current_tools == tools
                changed_context = changed_tool_contexts(tools, current_tools)
            current_ready_identities = {
                item["id"]: ready_identity(root, item, key=key) for item in plan["environments"]}
            dependency_statuses = []
            for identifier, previous in ready_identities.items():
                current = current_ready_identities[identifier]
                comparison = readiness_status(previous, current)
                dependency_statuses.append(comparison)
                if comparison != "passed" or current is None:
                    # No setup receipt may outlive a changed or unqualified
                    # observation. Two unknowns may still run fresh, uncached.
                    (state / "environments" / f"environment-{sha(encoded(identifier))}.json").unlink(missing_ok=True)
            # An observed change remains a defect even if another scan is
            # unavailable; uncertainty must not conceal a known changed input.
            dependency_status = ("failed" if "failed" in dependency_statuses else
                                 "unavailable" if "unavailable" in dependency_statuses else "passed")
            if "unavailable" in dependency_statuses:
                cache_reusable = False
            checks = [
                ("input-stability", "passed" if source_stable else "failed",
                 "inputs changed during validation; inspect diff and rerun", ["source-inputs"]),
                ("context-stability", "passed" if context_stable else "failed",
                 "dotenv or tool context changed during validation; inspect setup and rerun", changed_context),
                ("dependency-stability", dependency_status,
                 "dependency readiness comparison unavailable during validation; inspect setup and rerun"
                 if dependency_status == "unavailable" else
                 "dependency inputs changed during validation; inspect setup and rerun",
                 ["dependency-readiness-unavailable" if dependency_status == "unavailable" else "dependency-readiness"]),
            ]
            for identifier, status, reason, changed in checks:
                previous_result = next((r for r in results if r["id"] == identifier), None)
                if status != "passed" and (previous_result is None or
                                           (status == "failed" and previous_result["status"] == "unavailable")):
                    if previous_result is None:
                        results.append({"id": identifier, "status": status, "reason": reason})
                    else:
                        previous_result.update(status=status, reason=reason)
                    report_context_change(identifier, changed)

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
    except SourceScopeError as error:
        # Do not read, delete or rewrite stale state under an unqualified
        # boundary. Callers must honor this held exit before any old receipt.
        reason = getattr(error, "metadata_reason", None)
        detail = " Reason: " + reason if type(reason) is str and reason in _SOURCE_SCOPE_METADATA_REASONS else ""
        print("AGENT-CHECK-SOURCE-SCOPE-HELD: no qualified content projection; no validation authority." + detail,
              file=sys.stderr)
        return SOURCE_SCOPE_EXIT
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
