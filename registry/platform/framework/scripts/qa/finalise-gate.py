#!/usr/bin/env python3
"""Finalise gate: run the repository's agent checks when work is handed off.

One provider-neutral gate at the seam where an agent finalises work — a
``git push`` or a pull-request creation — instead of on every turn. It runs
``scripts/agent-check.py`` (affected checks only, cached by input digest, so an
unchanged candidate costs about a second) and blocks the hand-off only on a
real check failure. Infrastructure gaps (missing integration ref, missing
toolchain, timeout) are reported as incomplete validation and never block:
CI remains the authority and a recurring environment fault must be fixed at
its owner, not routed around with repeated pushes.

Adapters (``--hook``):
  claude        Claude Code PreToolUse (matcher Bash|mcp__.*__create_pull_request)
  codex         Codex PreToolUse (matcher Bash)
  cursor        Cursor beforeShellExecution (prints the permission JSON)
  git-pre-push  git pre-push hook (husky, .githooks or the installed shim)

Other modes:
  --install-git-hook   write a marked .git/hooks/pre-push shim (Devin, humans)
  --query replace-legacy-pre-push   exit 0 when config/qa.json opts in

Configuration is the optional ``config/qa.json`` ``finalise_gate`` section
(docs/qa-framework.md). A repository without ``config/agent-checks.json`` has
not adopted agent validation, so the gate is a no-op there.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GATE_STATE = ".ai/agent-checks"
LOG_NAME = "finalise-gate.log"
LEDGER_NAME = "finalise-gate.jsonl"
SHIM_MARKER = "# managed-by: ai-starter-pack finalise-gate"
MODES = ("block", "warn", "off")
# Absent config means the repository never opted in explicitly. A plugin-only
# or silently upgraded consumer must not start blocking pushes without review.
DEFAULT_MODE_WITHOUT_CONFIG = "warn"
# Recipe exit code for "this check could not run here" (no browser, no dev
# server): incomplete validation, never a source failure (docs/qa-framework.md).
UNAVAILABLE_EXIT = 3
DEFAULT_TIMEOUT_SECONDS = 1100  # below the 1200s host hook timeout
TAIL_LINES = 30
TAIL_LINE_CHARS = 240
# Any MCP server name: mcp__github__…, mcp__plugin_github_github__…, etc.
PR_TOOL = re.compile(r"^mcp__.+__create_pull_request$")
BYPASS_ENV = "AGENT_FINALISE_GATE"
SOURCE_SCOPE_POLICY = ".ai/source-scope-policy.json"
# Capture the host primitive capability, independently of call observation.
_HOOK_DIRFD_AVAILABLE = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
                         and all(function in os.supports_dir_fd for function in (os.open, os.stat, os.mkdir)))


class SourceScopeHeld(ValueError):
    """A content boundary is held, including non-execution entrypoints."""


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
        return data
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


def _source_scope_head_chain(gitdir: Path, common: Path) -> None:
    """Bound symbolic HEAD to checked ref namespaces, never other worktrees."""
    raw = _source_scope_control_bytes(gitdir / "HEAD")
    for _ in range(32):
        if raw is None:
            return  # A missing loose ref may resolve through checked packed refs.
        text = raw.decode("utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text):
            return
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
        target = (gitdir if private else common) / reference
        raw = _source_scope_control_bytes(target, optional=True)
    raise ValueError("symbolic HEAD route bound exceeded")


def _source_scope_pointer_path(parent: Path, target: str) -> Path:
    """Reject raw control aliases before normalization can erase their route."""
    requested = parent / target
    for path in (requested, *requested.parents):
        if path.is_symlink():
            raise ValueError("unqualified Git metadata alias")
    return requested.resolve(strict=True)


def _source_scope_git_controls(root: Path) -> None:
    """Reject config indirections BEFORE native Git can interpret their targets.

    These repeated observations are not an atomic filesystem/host guarantee.
    Independently pinned owning scope inputs refuse before this metadata route.
    """
    marker = root / ".git"
    if stat.S_ISDIR(marker.lstat().st_mode):
        gitdir = marker
        common = gitdir
        # Git honours a commondir file here and would read refs, objects and
        # config elsewhere; that routing is not modeled, so it holds.
        if _source_scope_exists(gitdir / "commondir"):
            raise ValueError("Git common-directory routing requires a qualified adapter")
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
    for path in (common / "config", gitdir / "config.worktree"):
        raw = _source_scope_control_bytes(path, optional=True)
        if raw is not None and re.search(rb"\[\s*include", raw, re.IGNORECASE):
            raise ValueError("Git config includes require a qualified metadata adapter")
    # Native Git must not follow alternate-object routing from this probe.
    if _source_scope_exists(common / "objects/info/alternates"):
        raise ValueError("Git object alternates require a qualified metadata adapter")
    for directory in dict.fromkeys((common, gitdir)):
        for leaf in ("packed-refs", "shallow"):
            _source_scope_control_metadata(directory / leaf, optional=True)
        # The fixed metadata probe disables graft interpretation independently;
        # static graft aliases also hold without opening any graft contents.
        _source_scope_control_metadata(directory / "info/grafts", optional=True)
    _source_scope_head_chain(gitdir, common)
    # Native fixed-argv tree metadata probes may access refs, loose objects or
    # pack/index metadata. Reject static external aliases before any such Git
    # access. No file in these directories is opened for content by this walk.
    for path in (common / "refs", common / "objects"):
        _source_scope_alias_metadata(path)
    private_refs = gitdir / "refs"
    if gitdir != common and _source_scope_exists(private_refs):
        _source_scope_alias_metadata(private_refs)


def _source_scope_tree_declared(owner: Path, environment: dict, timeout: int) -> bool:
    """Use fixed metadata probes; distinguish a verified unborn branch from failure."""
    def metadata(*arguments):
        return subprocess.run(["git", "--no-replace-objects", "-c", "protocol.allow=never", *arguments],
                              cwd=owner, env=environment, capture_output=True, timeout=timeout, check=False)
    result = metadata("ls-tree", "-z", "HEAD", "--", SOURCE_SCOPE_POLICY)
    if result.stdout:
        return True
    if result.returncode == 0:
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
    environment = {key: value for key, value in os.environ.items() if key in names}
    environment.update({"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_GRAFT_FILE": os.devnull})
    return environment


def source_scope_declared(root: Path) -> bool:
    """A declared or unproven boundary holds before Git/config/log/hook reads."""
    if "AGENT_SOURCE_SCOPE_SHA256" in os.environ or "AGENT_SOURCE_SCOPE_REPOSITORY" in os.environ:
        return True
    try:
        roots, declared = _source_scope_roots(root)
    except (OSError, ValueError, TypeError, RuntimeError):
        return True
    if declared:
        return True
    environment = _source_scope_git_environment()
    for owner in roots:
        try:
            _source_scope_git_controls(owner)
            if _source_scope_tree_declared(owner, environment, 30):
                return True
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
            return True
    return False


def require_source_scope(root: Path) -> None:
    if source_scope_declared(root):
        raise SourceScopeHeld("FINALISE-GATE SOURCE-SCOPE-HELD: no qualified content projection; no hand-off authority.")


# ── command classification ──────────────────────────────────────────────────

_SEPARATORS = "();<>|&\n"
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Heredoc bodies are data (commit messages, fixtures), never commands.
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
# Last resort for unbalanced quoting: only a git/gh word at a command start.
_FALLBACK = re.compile(r"(?:^|[;&|(\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:\S*/)?"
                       r"(?:git\s(?:[^;&|\n'\"]*\s)?push\b|gh\s+pr\s+create\b)")
_WRAPPERS = {"env", "sudo", "doas", "command", "exec", "time", "nice", "nohup", "timeout", "xargs"}
_WRAPPER_VALUE_OPTS = {"sudo": {"-u", "-g", "-C", "-h", "-p"}, "env": {"-u", "-C", "-S"},
                       "timeout": {"-s", "-k", "--signal", "--kill-after"}, "nice": {"-n"}}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                   "--super-prefix", "--config-env"}


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def _simple_commands(tokens: list[str]):
    """Yield ("cmd", words) per simple command and ("open"|"close", None) at
    each subshell boundary, so a `cd` inside `( … )` stays inside it."""
    current: list[str] = []
    for token in tokens:
        if token and all(c in _SEPARATORS for c in token):
            if current:
                yield "cmd", current
            current = []
            for char in token:
                if char == "(":
                    yield "open", None
                elif char == ")":
                    yield "close", None
        else:
            current.append(token)
    if current:
        yield "cmd", current


def _strip_wrappers(words: list[str]) -> tuple[list[str], bool]:
    """Drop VAR=value prefixes and transparent wrappers (env, sudo, timeout, …).

    Returns the remaining words and whether this simple command itself
    carries AGENT_FINALISE_GATE=off (a prefix applies to that command only).
    """
    bypass = False
    while words:
        if _ASSIGNMENT.match(words[0]):
            name, _, value = words.pop(0).partition("=")
            if name == BYPASS_ENV:
                bypass = value.lower() == "off"
            continue
        head = os.path.basename(words[0])
        if head not in _WRAPPERS:
            return words, bypass
        words.pop(0)
        value_opts = _WRAPPER_VALUE_OPTS.get(head, set())
        while words and words[0].startswith("-"):
            option = words.pop(0)
            if option in value_opts and words:
                words.pop(0)
        if head == "timeout" and words:
            words.pop(0)  # the duration
    return words, bypass


def _path_variants(base: str | None, target: str) -> list[str]:
    """Join a cd/-C target, keeping literal and ~-expanded forms.

    Quoting is lost in tokenizing and a quoted `~` is not expanded by the
    shell, so both spellings are candidates; checking one extra repository
    costs a cached run, missing the pushed one costs the gate its purpose.
    """
    forms = [target, os.path.expanduser(target)]
    return list(dict.fromkeys(os.path.join(base or "", form) for form in forms))


def _add_candidates(result: dict, *dirs: str | None) -> None:
    for directory in dirs:
        if directory not in result["_candidates"]:
            result["_candidates"].append(directory)


def _classify(command: str, cwd: str | None, result: dict, depth: int) -> None:
    """Walk the simple commands of one shell string, recording hand-offs.

    `cwd` is the best-guess directory (subshells scoped, brace groups kept).
    Shell scoping has more corners than a tokenizer can see (quoted
    parentheses, pipelines, `$( )`), so every directory the command names is
    also recorded and the gate validates all of them; the guess only orders
    them.
    """
    try:
        tokens = _tokens(_HEREDOC.sub("", command))
    except ValueError:
        if _FALLBACK.search(command):
            result["trigger"] = result["trigger"] or "push"
            result["repo_dir"] = result["repo_dir"] or cwd
            _add_candidates(result, cwd, None, *result["_seen"])
        return
    stack: list[str | None] = []
    for kind, words in _simple_commands(tokens):
        if kind == "open":
            stack.append(cwd)  # a subshell's cd never escapes it
            continue
        if kind == "close":
            cwd = stack.pop() if stack else cwd
            continue
        # Brace groups run in the current shell, so their cd persists.
        while words and words[0] in ("{", "}"):
            words = words[1:]
        while words and words[-1] in ("{", "}"):
            words = words[:-1]
        words, local_bypass = _strip_wrappers(list(words))
        if not words:
            continue
        head = os.path.basename(words[0])
        # `export AGENT_FINALISE_GATE=off` really does reach later commands in
        # the same shell; `unset` withdraws it.
        if head == "export":
            for word in words[1:]:
                name, _, value = word.partition("=")
                if name == BYPASS_ENV:
                    result["_exported"] = value.lower() == "off"
            continue
        if head == "unset" and BYPASS_ENV in words[1:]:
            result["_exported"] = False
            continue
        bypassed = local_bypass or result.get("_exported", False)
        if head in ("cd", "pushd"):
            target = next((w for w in words[1:] if not w.startswith("-")), None)
            if target and target != "-":
                variants = _path_variants(cwd, target)
                # Unscoped chain too: if the scope guess is wrong, the
                # directory the shell really ended in is still a candidate.
                unscoped = result["_unscoped"]
                result["_unscoped"] = _path_variants(unscoped, target)[-1]
                result["_seen"].extend(variants + _path_variants(unscoped, target))
                cwd = variants[-1]
            continue
        if head in _SHELLS and "-c" in words[1:] and depth < 3:
            index = words.index("-c", 1)
            if index + 1 < len(words):
                inner = dict(result, _exported=bypassed)
                _classify(words[index + 1], cwd, inner, depth + 1)
                result.update({k: v for k, v in inner.items() if k != "_exported"})
            continue
        if head == "gh" and words[1:3] == ["pr", "create"]:
            result["trigger"] = result["trigger"] or "pr"
            result["repo_dir"] = result["repo_dir"] or cwd
            result["_handoffs"].append(bypassed)
            _add_candidates(result, cwd, None, *result["_seen"])
            continue
        if head != "git":
            continue
        index, repo_dir, extra = 1, cwd, []
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            if option in _GIT_VALUE_OPTS and index + 1 < len(words):
                if option == "-C":
                    variants = _path_variants(repo_dir, words[index + 1])
                    extra.extend(variants)
                    repo_dir = variants[-1]
                index += 2
                continue
            index += 1
        if index >= len(words) or words[index] != "push":
            continue
        args = words[index + 1:]
        if any(a in ("--dry-run", "-n", "--delete", "-d") for a in args):
            continue
        refspecs = [a for a in args if not a.startswith("-")][1:]
        if refspecs and all(r.startswith(":") for r in refspecs):
            continue  # deleting remote refs is not a candidate hand-off
        result["trigger"] = result["trigger"] or "push"
        result["repo_dir"] = result["repo_dir"] or repo_dir
        result["_handoffs"].append(bypassed)
        _add_candidates(result, repo_dir, *extra, None, *result["_seen"])


def classify_command(command: str) -> dict:
    """Return {"trigger", "repo_dir", "repo_dirs", "bypass"} for one command.

    `repo_dir` is the best guess at where the hand-off runs (None = the
    starting directory); `repo_dirs` lists every candidate, best guess first,
    and the gate validates each adopted repository among them.

    Only a real hand-off triggers the gate: dry runs and ref deletions do not.
    Quotes, heredoc bodies and command substitutions inside quotes are data,
    so a commit message or grep pattern that mentions `git push` never
    triggers. `cd`, `git -C`, wrappers (env, sudo, timeout …) and `sh -c`
    strings are followed. The inline bypass counts only when it reaches the
    hand-off itself (a prefix on that command, or an earlier `export`). Unbalanced quoting falls back to a regex anchored
    at a command start.
    """
    result = {"trigger": None, "repo_dir": None, "_handoffs": [],
              "_candidates": [], "_seen": [], "_unscoped": None}
    _classify(command or "", None, result, 0)
    handoffs = result.pop("_handoffs")
    result["repo_dirs"] = result.pop("_candidates") if result["trigger"] else []
    for key in ("_exported", "_seen", "_unscoped"):
        result.pop(key, None)
    # Bypassed only when every hand-off in the command carries the bypass;
    # an assignment on some other command never reaches the push.
    result["bypass"] = bool(handoffs) and all(handoffs)
    return result


# ── repository + configuration ──────────────────────────────────────────────

def git_toplevel(start: Path) -> Path | None:
    require_source_scope(start)
    try:
        proc = subprocess.run(["git", "--no-replace-objects", "-c", "protocol.allow=never",
                               "rev-parse", "--show-toplevel"], cwd=start,
                              env=_source_scope_git_environment(), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None


def load_qa_config(root: Path) -> tuple[dict, bool]:
    """Return (finalise_gate section, config_present)."""
    require_source_scope(root)
    path = root / "config" / "qa.json"
    if not path.is_file():
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"config/qa.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("config/qa.json requires schema_version=1")
    section = data.get("finalise_gate", {})
    if not isinstance(section, dict):
        raise ValueError("config/qa.json finalise_gate must be an object")
    return section, True


def resolve_mode(section: dict, present: bool, bypass_inline: bool) -> tuple[str, str]:
    """Return (mode, source). Environment and inline overrides win."""
    if bypass_inline:
        return "off", "inline"
    override = os.environ.get(BYPASS_ENV, "").strip().lower()
    if override in MODES:
        return override, "env"
    mode = section.get("mode", DEFAULT_MODE_WITHOUT_CONFIG)
    if mode not in MODES:
        raise ValueError(f"config/qa.json finalise_gate.mode must be one of {MODES}")
    return mode, "config" if present else "default"


def agent_check_path(root: Path) -> Path | None:
    for candidate in (root / "scripts" / "agent-check.py",
                      Path(__file__).resolve().parent.parent / "agent-check.py"):
        if candidate.is_file():
            return candidate
    return None


# ── execution ───────────────────────────────────────────────────────────────

def run_checks(root: Path, provider: str, timeout: int) -> dict:
    """Run agent-check and classify the outcome as pass|fail|incomplete."""
    if source_scope_declared(root):
        return {"outcome": "source-scope-held", "summary": "no qualified content projection; full validation held"}
    runner = agent_check_path(root)
    if runner is None:
        return {"outcome": "incomplete", "summary": "scripts/agent-check.py is not installed"}
    state = root / GATE_STATE
    state.mkdir(parents=True, exist_ok=True)
    log_path = state / LOG_NAME
    start = time.monotonic()
    cmd = [sys.executable, str(runner), "--provision", "--provider", provider,
           "--repo-root", str(root)]
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        # Own process group: a timeout must also stop the recipes' children
        # (dev servers, browsers), not just agent-check itself.
        proc = subprocess.Popen(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
    duration_ms = round((time.monotonic() - start) * 1000)
    if code == 4:
        # The runner may discover a boundary after this caller's first probe.
        # Do not inspect a stale receipt/config or convert this hold to a PASS.
        return {"duration_ms": duration_ms, "outcome": "source-scope-held",
                "summary": "no qualified content projection; full validation held"}
    receipt = {}
    receipt_path = state / "receipt.json"
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = {}
    results = receipt.get("results", []) if isinstance(receipt, dict) else []
    environments = environment_ids(root)
    failed, unavailable = [], []
    for item in results:
        if not isinstance(item, dict) or item.get("status") not in ("failed", "unavailable"):
            continue
        # Setup failures (npm ci on a flaky network), per-check timeouts and
        # self-declared unavailability are environment faults, not defects.
        environmental = (item.get("status") == "unavailable" or item.get("id") in environments
                         or item.get("reason") in ("timeout", f"exit={UNAVAILABLE_EXIT}"))
        (unavailable if environmental else failed).append(item)
    base = {"duration_ms": duration_ms, "digest": str(receipt.get("digest", ""))[:12],
            "cached": bool(receipt.get("cached")), "log": str(log_path.relative_to(root))}
    if code == 0:
        return {**base, "outcome": "pass", "summary": "all affected checks passed"}
    if code is None:
        return {**base, "outcome": "incomplete", "summary": f"agent-check exceeded {timeout}s"}
    if code == 2:
        return {**base, "outcome": "incomplete",
                "summary": "agent-check blocked before running checks: " + blocked_reason(log_path)}
    if failed:
        names = ", ".join(f"{r.get('id')} ({r.get('reason')})" for r in failed)
        incomplete = "; could not run: " + ", ".join(
            f"{r.get('id')} ({r.get('reason')})" for r in unavailable) if unavailable else ""
        return {**base, "outcome": "fail", "summary": f"failed: {names}{incomplete}"}
    if unavailable:
        names = ", ".join(f"{r.get('id')} ({r.get('reason')})" for r in unavailable)
        return {**base, "outcome": "incomplete", "summary": f"unavailable: {names}"}
    return {**base, "outcome": "fail", "summary": f"agent-check exited {code}"}


def environment_ids(root: Path) -> set[str]:
    try:
        config = json.loads((root / "config" / "agent-checks.json").read_text(encoding="utf-8"))
        return {str(e.get("id")) for e in config.get("environments", []) if isinstance(e, dict)}
    except (OSError, ValueError, AttributeError):
        return set()


def blocked_reason(log_path: Path) -> str:
    for line in reversed(tail(log_path, 50)):
        if "AGENT-CHECK-BLOCKED" in line:
            return line.split("AGENT-CHECK-BLOCKED:", 1)[-1].strip()[:TAIL_LINE_CHARS]
    return "see log"


def tail(path: Path, count: int = TAIL_LINES) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line[:TAIL_LINE_CHARS] for line in lines[-count:]]


def record(root: Path, entry: dict) -> None:
    """Append one cost/outcome row; never let telemetry break the hand-off."""
    try:
        path = root / GATE_STATE / LEDGER_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def record_bypass(root: Path, provider: str, source: str) -> None:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=5).stdout.strip()
        path = root / ".ai" / "bypass-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "scope": "finalise-gate", "head": head, "user": provider,
                "reason": f"{BYPASS_ENV}=off ({source})",
            }) + "\n")
    except (OSError, subprocess.SubprocessError):
        pass


def message_for(result: dict, provider: str, mode: str) -> str:
    rerun = f"python3 scripts/agent-check.py --provision --provider {provider}"
    lines = [f"FINALISE-GATE {result['outcome'].upper()}: {result['summary']}"]
    if result["outcome"] == "fail":
        lines.append(f"Fix the failing checks, then re-run: {rerun}")
        if mode == "warn":
            lines.append("(mode=warn: hand-off allowed; set finalise_gate.mode=block to enforce)")
    else:
        lines.append("Validation is incomplete, not passed — CI stays authoritative. "
                     f"Re-run when the environment is fixed: {rerun}")
    if result.get("log"):
        count = TAIL_LINES if result["outcome"] == "fail" else 10
        log_tail = tail(Path(result["root"]) / result["log"], count)
        if log_tail:
            lines.append(f"Last {len(log_tail)} lines of {result['log']}:")
            lines.extend(log_tail)
    return "\n".join(lines)


# ── adapters ────────────────────────────────────────────────────────────────

def read_payload() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def workspaces_for(adapter: str, payload: dict) -> list[Path]:
    """The trust boundary the user opened; the gate never runs code outside it."""
    if adapter == "cursor":
        roots = [r for r in payload.get("workspace_roots") or [] if isinstance(r, str) and r]
    elif adapter == "claude":
        roots = [os.environ.get("CLAUDE_PROJECT_DIR", "")]
    else:
        roots = []
    workspaces = [Path(r).resolve() for r in roots if r and Path(r).is_dir()]
    if workspaces:
        return workspaces
    # No host-declared root (Codex, or an unset CLAUDE_PROJECT_DIR): the hook's
    # own directory bounds trust, so a named `cd`/`git -C` path elsewhere
    # never runs that path's recipes.
    here = Path(str(payload.get("cwd") or "") or os.getcwd())
    here = here if here.is_dir() else Path.cwd()
    return [(git_toplevel(here) or here).resolve()]


def within(root: Path, workspaces: list[Path]) -> bool:
    if not workspaces:
        return True
    resolved = root.resolve()
    return any(resolved == ws or ws in resolved.parents for ws in workspaces)


def hook_target(adapter: str, payload: dict) -> tuple[str | None, list[str | None], bool, str]:
    """Return (trigger, candidate start dirs, bypass, command) for a hook payload."""
    if adapter == "cursor":
        command = str(payload.get("command") or "")
        roots = [r for r in payload.get("workspace_roots") or [] if isinstance(r, str) and r]
        start = payload.get("cwd") or None
    else:
        tool = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        start = payload.get("cwd")
        if PR_TOOL.match(tool):
            return "pr", [start], False, tool
        if tool != "Bash":
            return None, [start], False, ""
        command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    info = classify_command(command)
    # Ordinary shell commands have no hand-off authority to qualify. Classify
    # them before root discovery so a scope hold cannot deny ls/pytest/status.
    if adapter == "cursor" and info["trigger"] is not None:
        # Cursor may report cwd as "" or "/"; preserve the workspace fallback
        # for actual pushes/PR creation, including inline bypass attempts.
        if roots and (not start or git_toplevel(Path(start)) is None):
            start = next((r for r in roots if git_toplevel(Path(r))), roots[0])
    base = start or os.getcwd()
    starts = [os.path.join(base, d) if d else base for d in info["repo_dirs"]] or [base]
    return info["trigger"], starts, info["bypass"], command


def emit(adapter: str, decision: str, text: str) -> int:
    """Translate allow|warn|deny into the host's hook protocol."""
    if adapter == "cursor":
        response = {"permission": "deny" if decision == "deny" else "allow"}
        if text:
            response["agent_message"] = text
            response["user_message"] = text.splitlines()[0]
        print(json.dumps(response))
        return 0
    if decision == "deny":
        print(text, file=sys.stderr)
        return 2 if adapter in ("claude", "codex") else 1
    if text:
        if adapter in ("claude", "codex"):
            # Model-visible without blocking: the agent learns what failed
            # or could not run, and the hand-off proceeds.
            response = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "additionalContext": text}}
            if adapter == "claude":
                response["systemMessage"] = text.splitlines()[0]
            print(json.dumps(response))
        else:
            print(text, file=sys.stderr)
    return 0


def candidate_roots(starts: list[str | None], workspaces: list[Path]) -> list[Path]:
    """Adopted repositories inside the workspace, deduplicated, in order."""
    roots: list[Path] = []
    for start in starts:
        root = git_toplevel(Path(start or os.getcwd())) if start is None or Path(start).is_dir() else None
        if (root is None or root in roots or not (root / "config" / "agent-checks.json").is_file()
                or not within(root, workspaces)):
            continue  # unadopted, or outside the opened workspace: never run
        roots.append(root)
    return roots


def gate_one(root: Path, provider: str, bypass_inline: bool, trigger: str,
             cap: int | None = None) -> tuple[str, str]:
    """Validate one repository; return (allow|warn|deny, message).

    `cap` bounds the repository's own timeout so concurrent candidates
    finish inside the host's hook timeout.
    """
    if source_scope_declared(root):
        return "deny", "FINALISE-GATE SOURCE-SCOPE-HELD: no qualified content projection; no hand-off authority."
    try:
        section, present = load_qa_config(root)
        mode, source = resolve_mode(section, present, bypass_inline)
    except SourceScopeHeld as exc:
        return "deny", str(exc)
    except ValueError as exc:
        return "warn", f"FINALISE-GATE INCOMPLETE: {exc}"
    if mode == "off":
        if source in ("env", "inline"):
            record_bypass(root, provider, source)
        record(root, {"ts": time.time(), "provider": provider, "trigger": trigger,
                      "mode": mode, "outcome": "off", "source": source})
        return "allow", ""
    timeout = section.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS
    if cap is not None:
        timeout = min(timeout, cap)
    result = run_checks(root, provider, timeout)
    if result["outcome"] == "source-scope-held":
        return "deny", "FINALISE-GATE SOURCE-SCOPE-HELD: no qualified content projection; no hand-off authority."
    result["root"] = str(root)
    record(root, {"ts": time.time(), "provider": provider, "trigger": trigger, "mode": mode,
                  "outcome": result["outcome"], "duration_ms": result.get("duration_ms"),
                  "cached": result.get("cached"), "digest": result.get("digest")})
    if result["outcome"] == "pass":
        return "allow", ""
    text = message_for(result, provider, mode)
    return ("deny" if result["outcome"] == "fail" and mode == "block" else "warn"), text


def gate(adapter: str, starts: list[str | None], bypass_inline: bool, trigger: str,
         workspaces: list[Path] | None = None) -> int:
    """Validate every adopted candidate repository; deny if any one denies.

    A command usually names one repository. When it names several (cd into a
    nested repo, `git -C`), each is checked, so a parsing gap can never let
    the pushed repository skip validation.
    """
    provider = adapter if adapter in ("claude", "codex", "cursor") else (
        os.environ.get("AGENT_PROVIDER", "unknown").strip() or "unknown")
    if provider not in ("claude", "codex", "cursor", "devin", "unknown"):
        provider = "unknown"
    roots = candidate_roots(starts, workspaces or [])
    # Candidates run concurrently, each capped below the host hook timeout, so
    # a slow (possibly mis-ranked) one can never starve the pushed repository.
    # A single repository keeps its configured timeout.
    cap = DEFAULT_TIMEOUT_SECONDS if len(roots) > 1 else None
    with ThreadPoolExecutor(max_workers=max(len(roots), 1)) as pool:
        results = list(pool.map(lambda root: gate_one(root, provider, bypass_inline, trigger, cap),
                                roots))
    outcomes = [(root, *result) for root, result in zip(roots, results)]
    messages = [(f"[{root}] " if len(roots) > 1 else "") + text
                for root, _decision, text in outcomes if text]
    text = "\n\n".join(messages)
    decisions = {decision for _root, decision, _text in outcomes}
    if "deny" in decisions:
        return emit(adapter, "deny", text)
    return emit(adapter, "warn" if text else "allow", text)


def git_pre_push() -> int:
    # stdin: "<local ref> <local sha> <remote ref> <remote sha>" per ref.
    lines = [line.split() for line in sys.stdin.read().splitlines() if line.strip()]
    if lines and all(len(p) >= 2 and set(p[1]) == {"0"} for p in lines):
        return 0  # every ref is a deletion
    return gate("git-pre-push", [os.getcwd()], False, "push")


# ── git shim ────────────────────────────────────────────────────────────────

SHIM = """#!/bin/sh
{marker}
# Runs the repository's finalise gate before every push. Remove this file to
# uninstall. Re-run `python3 scripts/qa/finalise-gate.py --install-git-hook`
# to refresh it.
# Discover only directory metadata. Native Git may parse protected config/ref/
# object aliases before Python can enforce a newly installed owner boundary.
ROOT="$PWD"
while [ "$ROOT" != "/" ] && [ ! -e "$ROOT/.git" ] && [ ! -L "$ROOT/.git" ]; do
  ROOT=$(dirname "$ROOT") || exit 1
done
GATE="$ROOT/scripts/qa/finalise-gate.py"
[ -f "$GATE" ] || GATE={fallback}
[ -f "$GATE" ] || exit 0
exec python3 "$GATE" --hook git-pre-push "$@"
"""


def _native_hook_overrides_unknown() -> bool:
    """Higher-precedence ambient config is not an admitted hook-routing input."""
    return (os.environ.get("GIT_CONFIG_COUNT", "0") != "0"
            or bool(os.environ.get("GIT_CONFIG_PARAMETERS"))
            or "GIT_CONFIG" in os.environ)


def _native_hook_nonlocal_config_absent(root: Path) -> bool:
    """Prove absence from path metadata only; never open external config bytes.

    Git's compiled system path is not a portable admitted input. An enabled
    implicit system route is unknown, rather than guessed to be /etc/gitconfig.
    No new environment override is applied to the user's native Git process.
    """
    def absent(path: str | Path) -> bool:
        if str(path) == os.devnull:
            return True
        try:
            (root / path).lstat()
        except FileNotFoundError:
            return True
        except (OSError, ValueError, TypeError):
            return False
        return False

    no_system = os.environ.get("GIT_CONFIG_NOSYSTEM", "").lower()
    if no_system not in ("1", "true", "yes", "on"):
        if no_system not in ("", "0", "false", "no", "off"):
            return False
        system = os.environ.get("GIT_CONFIG_SYSTEM")
        if system is None or not absent(system):
            return False
    if "GIT_CONFIG_GLOBAL" in os.environ:
        return absent(os.environ["GIT_CONFIG_GLOBAL"])
    home = os.environ.get("HOME")
    if not home:
        return False
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path(home) / ".config")
    return absent(Path(home) / ".gitconfig") and absent(Path(xdg) / "git/config")


def _native_hook_routing_hold() -> int:
    print("finalise-gate: native hook routing is unqualified; no hook installed or changed", file=sys.stderr)
    return UNAVAILABLE_EXIT


def _hook_target_hold(*, refresh: bool = False) -> int:
    reason = ("marked-hook refresh requires a qualified replacement" if refresh
              else "hook target is unqualified")
    print(f"finalise-gate: {reason}; installation not confirmed", file=sys.stderr)
    return UNAVAILABLE_EXIT


def _hook_file_identity(details) -> tuple:
    return (details.st_dev, details.st_ino, details.st_mode, details.st_nlink,
            details.st_size, details.st_mtime_ns, details.st_ctime_ns)


def _hook_directory_identity(details) -> tuple:
    return (details.st_dev, details.st_ino, details.st_mode, details.st_uid, details.st_gid)


def _hook_directory(target: Path, descriptors: list[int]) -> tuple:
    """Open every physical parent component without following aliases."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(target.anchor, flags)
    descriptors.append(parent)
    chain = []
    parts = target.parent.parts[1:]
    for index, name in enumerate(parts):
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if index != len(parts) - 1:
                raise ValueError("unqualified hook parent") from None
            os.mkdir(name, 0o755, dir_fd=parent)
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError("unqualified hook parent")
        child = os.open(name, flags, dir_fd=parent)
        descriptors.append(child)
        identity = _hook_directory_identity(os.fstat(child))
        if identity != _hook_directory_identity(before):
            raise ValueError("hook parent changed")
        chain.append((parent, name, child, identity))
        parent = child
    return parent, chain


def _hook_directory_unchanged(chain: list) -> None:
    for parent, name, child, identity in chain:
        if (_hook_directory_identity(os.fstat(child)) != identity
                or _hook_directory_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity):
            raise ValueError("hook parent changed")


def _hook_leaf(parent: int, name: str):
    try:
        details = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size > 65536:
        raise ValueError("unqualified hook leaf")
    return details


def _install_hook_target(target: Path, body: bytes) -> int:
    """Never replace an existing namespace or truncate an installed control."""
    if not _HOOK_DIRFD_AVAILABLE:
        return _hook_target_hold()
    descriptors: list[int] = []
    created = None
    parent = None
    try:
        # Both pathname ownership routes hold before any existing hook bytes.
        require_source_scope(target.parent)
        selected = _source_scope_resolved_path(target)
        require_source_scope(selected.parent)
        if selected.is_dir():
            require_source_scope(selected)
        _source_scope_control_metadata(target, optional=True)
        parent, chain = _hook_directory(selected, descriptors)
        require_source_scope(selected.parent)
        _hook_directory_unchanged(chain)
        before = _hook_leaf(parent, selected.name)
        if before is not None:
            descriptor = os.open(selected.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
            descriptors.append(descriptor)
            if _hook_file_identity(os.fstat(descriptor)) != _hook_file_identity(before):
                raise ValueError("hook leaf changed")
            raw = os.read(descriptor, 65537)
            after = os.fstat(descriptor)
            current = _hook_leaf(parent, selected.name)
            _hook_directory_unchanged(chain)
            if (len(raw) != before.st_size or len(raw) > 65536 or current is None
                    or _hook_file_identity(after) != _hook_file_identity(before)
                    or _hook_file_identity(current) != _hook_file_identity(before)):
                raise ValueError("hook leaf changed")
            if SHIM_MARKER.encode() not in raw:
                print("finalise-gate: existing hook is managed elsewhere; leaving it unchanged")
                return 0
            if raw != body or stat.S_IMODE(before.st_mode) != 0o755:
                return _hook_target_hold(refresh=True)
            print("finalise-gate: marked shim already matches; leaving it unchanged")
            return 0
        # An exclusive new leaf remains non-executable until its complete
        # body and the admitted name/ancestry have been checked. No existing
        # namespace is ever replaced, truncated or removed during recovery.
        created = os.open(selected.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        descriptors.append(created)
        offset = 0
        while offset < len(body):
            written = os.write(created, body[offset:])
            if written <= 0:
                raise OSError("hook write incomplete")
            offset += written
        os.fsync(created)
        require_source_scope(selected.parent)
        _hook_directory_unchanged(chain)
        staged = _hook_leaf(parent, selected.name)
        opened = os.fstat(created)
        os.lseek(created, 0, os.SEEK_SET)
        complete = os.read(created, len(body) + 1)
        if (complete != body or staged is None or _hook_file_identity(staged) != _hook_file_identity(opened)
                or _hook_file_identity(os.fstat(created)) != _hook_file_identity(opened)
                or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_size != len(body)):
            raise ValueError("hook publication changed")
        require_source_scope(selected.parent)
        _hook_directory_unchanged(chain)
        current = _hook_leaf(parent, selected.name)
        if current is None or _hook_file_identity(current) != _hook_file_identity(opened):
            raise ValueError("hook publication changed")
        os.fchmod(created, 0o755)
        require_source_scope(selected.parent)
        _hook_directory_unchanged(chain)
        installed = _hook_leaf(parent, selected.name)
        activated = os.fstat(created)
        # chmod legitimately changes mode/ctime. It cannot qualify a different
        # inode, link count, size, mtime or body than the prepared bytes.
        if (installed is None or _hook_file_identity(installed) != _hook_file_identity(activated)
                or stat.S_IMODE(activated.st_mode) != 0o755
                or (activated.st_dev, activated.st_ino, activated.st_nlink, activated.st_size, activated.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size, opened.st_mtime_ns)):
            raise ValueError("hook publication changed")
        os.lseek(created, 0, os.SEEK_SET)
        confirmed = os.read(created, len(body) + 1)
        require_source_scope(selected.parent)
        _hook_directory_unchanged(chain)
        current = _hook_leaf(parent, selected.name)
        if (confirmed != body or current is None
                or _hook_file_identity(os.fstat(created)) != _hook_file_identity(activated)
                or _hook_file_identity(current) != _hook_file_identity(activated)):
            raise ValueError("hook publication changed")
        return 0
    except SourceScopeHeld:
        raise
    except (OSError, ValueError, RuntimeError, TypeError):
        return _hook_target_hold()
    finally:
        active_exception = sys.exc_info()[0]
        close_error = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as error:
                close_error = error
        if close_error is not None and active_exception is None:
            raise close_error


def install_git_hook(root: Path) -> int:
    """Install a qualified new shim; retain existing managers and controls."""
    require_source_scope(root)
    if _native_hook_overrides_unknown():
        return _native_hook_routing_hold()
    environment = _source_scope_git_environment()
    try:
        configuration = subprocess.run(["git", "--no-replace-objects", "-c", "protocol.allow=never",
                                        "config", "--get", "core.hooksPath"], cwd=root,
                                       env=environment, capture_output=True, text=True, timeout=10)
        if configuration.returncode not in (0, 1):
            return _native_hook_routing_hold()
        if configuration.returncode == 0:
            print("finalise-gate: local core.hooksPath is managed elsewhere; "
                  "call `scripts/qa/finalise-gate.py --hook git-pre-push` from that manager's pre-push")
            return 0
        if not _native_hook_nonlocal_config_absent(root):
            return _native_hook_routing_hold()
        location = subprocess.run(["git", "--no-replace-objects", "-c", "protocol.allow=never",
                                   "rev-parse", "--git-path", "hooks"], cwd=root,
                                  env=environment, capture_output=True, text=True, timeout=10)
        hooks_dir = location.stdout.strip()
        if location.returncode or not hooks_dir or "\0" in hooks_dir or "\n" in hooks_dir:
            return _hook_target_hold()
        target = (_source_scope_resolved_path(root) / hooks_dir) / "pre-push"
        fallback = shlex.quote(str(Path(__file__).resolve()))
        body = SHIM.format(marker=SHIM_MARKER, fallback=fallback).encode("utf-8")
        return _install_hook_target(target, body)
    except SourceScopeHeld:
        raise
    except (OSError, ValueError, RuntimeError, TypeError, subprocess.SubprocessError):
        return _hook_target_hold()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--hook", choices=["claude", "codex", "cursor", "git-pre-push"])
    group.add_argument("--install-git-hook", action="store_true")
    group.add_argument("--query", choices=["replace-legacy-pre-push"])
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("remainder", nargs="*", help=argparse.SUPPRESS)  # git passes remote name/url
    args = parser.parse_args(argv)
    if args.install_git_hook:
        require_source_scope(args.repo_root or Path.cwd())
        root = git_toplevel(args.repo_root or Path.cwd())
        if root is None:
            print("finalise-gate: not a git repository", file=sys.stderr)
            return 1
        return install_git_hook(root)
    if args.query:
        require_source_scope(args.repo_root or Path.cwd())
        root = git_toplevel(args.repo_root or Path.cwd())
        try:
            section, _ = load_qa_config(root) if root else ({}, False)
        except ValueError:
            return 1
        return 0 if section.get("replace_legacy_pre_push") is True else 1
    if args.hook == "git-pre-push":
        return git_pre_push()
    payload = read_payload()
    trigger, starts, bypass, _command = hook_target(args.hook, payload)
    if trigger is None:
        return emit(args.hook, "allow", "")
    return gate(args.hook, starts, bypass, trigger, workspaces_for(args.hook, payload))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(arguments)
    except SourceScopeHeld as exc:
        if "--hook" in arguments:
            index = arguments.index("--hook") + 1
            if index < len(arguments):
                return emit(arguments[index], "deny", str(exc))
        print(str(exc), file=sys.stderr)
        return 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
