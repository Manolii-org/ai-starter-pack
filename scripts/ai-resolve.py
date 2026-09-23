#!/usr/bin/env python3
"""Resolve ai-manifest.yaml requirements into repo-local agent surfaces.

Reads the consumer repo's ai-manifest.yaml, enforces scope rules fail-closed
(a repo may only require platform/*, <its-own-universe>/*, and repo/personal
assets), then materialises plugin components into the paths agents already
read — materialise-in-place: distribution changes, consumption paths don't.

    skills/<n>/SKILL.md  ->  .claude/skills/<n>/SKILL.md
    agents/*.md          ->  .claude/agents/*.md
    commands/*.md        ->  .claude/commands/*.md

Every materialised file is recorded in .ai/capability-lock.json so
`--check` can detect drift and hand-edited files are never silently clobbered.
Hooks and MCP wiring are reported as advisory output only — they need a
settings.json merge step that is a later-phase concern.

Usage:
    ai-resolve.py [--manifest ai-manifest.yaml] [--registry <path>]
                  [--repo-root .] [--apply [--prune] | --check]

  Default mode is a dry run — prints the plan, writes nothing.
  --apply   materialise the resolved set
  --prune   with --apply, also remove lockfile-tracked files no longer required
  --check   exit 1 if materialised files differ from the registry source

Registry source: a local checkout containing a top-level registry/ dir (the
ai-starter-pack repo works). Remote fetch (github:org/repo@ref) lands in P1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("FAIL: PyYAML required (pip install pyyaml)\n")
    sys.exit(2)

SCOPES = ("platform", "manolii", "buro", "impaktful", "cpdcheck", "repo", "personal")
LOCAL_SCOPES = {"repo", "personal"}
COMPONENT_TARGETS = {
    "skills": ".claude/skills",
    "agents": ".claude/agents",
    "commands": ".claude/commands",
}
# Resolver-owned subtrees — prune/removal may only ever touch paths rooted at
# one of these (e.g. .claude/settings.json is NOT resolver-owned and must
# never be unlinked by a lockfile entry).
OWNED_ROOTS = frozenset(COMPONENT_TARGETS.values())
LOCK_PATH = ".ai/capability-lock.json"
REQUIRES_RE = re.compile(
    r"^(platform|manolii|buro|impaktful|cpdcheck|repo|personal)/([a-z0-9][a-z0-9-]*)$"
)
SEMVER_REF = re.compile(r"v?\d+(?:\.\d+){0,2}")
# A materialised file that INVOKES a sibling script — the resolver does not
# materialise scripts/, so real invocations can never run in resolver mode.
# Only executable-invocation shapes match (interpreter call or ./exec); a
# bare `scripts/x.py` mention in prose or sample output is not a dependency.
SCRIPT_REF = re.compile(
    rb"(?:python3?|bash|sh|zsh|node|npx|tsx|deno|ruby|perl|uv\s+run|pipenv\s+run)"
    rb"\s+[^\n|&;`]*?scripts/[A-Za-z0-9_.-]+\.(?:py|sh|ts|js|mjs)\b"
    rb"|\./scripts/[A-Za-z0-9_.-]+\.(?:py|sh|ts|js|mjs)\b")
# Backticked `scripts/x.py` is NOT an invocation context — prose uses it for
# mentions. A real dependency that no interpreter/./ prefix expresses must be
# declared explicitly: `requires_scripts: [...]` in the file's frontmatter.
SCRIPT_DEP_KEYS = ("requires_scripts",)
# Frontmatter declaring the file's `scripts/x.py` references are CONSUMER-side
# (the consumer repo already owns them, or the file's own setup section has the
# consumer fetch them) — the only exemption that lets an unbundled script
# reference materialise instead of being skipped as an unsatisfiable dep.
CONSUMER_SCRIPT_KEYS = ("consumer_scripts",)
# Basename extraction for SCRIPT_REF matches — used to distinguish bundled
# plugin scripts (a real dependency) from consumer-repository commands.
SCRIPT_NAME = re.compile(rb"scripts/([A-Za-z0-9_.-]+\.(?:py|sh|ts|js|mjs))\b")


def _frontmatter(src_bytes: bytes) -> dict:
    m = re.match(rb"\A---\s*\n(.*?)\n---\s*\n", src_bytes, re.S)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1).decode("utf-8", errors="ignore")) or {}
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def declares_script_deps(src_bytes: bytes) -> bool:
    """True when the file's YAML frontmatter declares script dependencies —
    explicit metadata, since prose heuristics can't distinguish
    "run `scripts/x.py`" from "routing uses `scripts/x.py`"."""
    fm = _frontmatter(src_bytes)
    return any(fm.get(k) for k in SCRIPT_DEP_KEYS)


def declared_consumer_scripts(src_bytes: bytes) -> set[str]:
    """The set of script paths the file declares are consumer-repository
    provided (consumer_scripts: [...]) — every unbundled scripts/x.py
    invocation must be explicitly listed here to be exempt."""
    fm = _frontmatter(src_bytes)
    out: set[str] = set()
    for k in CONSUMER_SCRIPT_KEYS:
        v = fm.get(k)
        if isinstance(v, (list, tuple)):
            out |= {str(x) for x in v}
    return out


def script_dep_block(plugin_dir: Path, src_bytes: bytes) -> bool:
    """True when the file's script usage cannot run under a resolver install:
    a bundled plugin script (scripts/ isn't materialised), an explicit
    requires_scripts dep, or an unbundled invocation that isn't listed in
    consumer_scripts."""
    sdir = plugin_dir / "scripts"
    declared = declared_consumer_scripts(src_bytes)
    for m in SCRIPT_REF.finditer(src_bytes):
        # Only names inside actual invocations count — a bare `scripts/x.py`
        # mention in prose is not a dependency and must not gate materialise.
        n = SCRIPT_NAME.search(m.group(0))
        if not n:
            continue
        name = n.group(1).decode("utf-8", errors="ignore")
        if (sdir / name).is_file():
            return True  # bundled dep — resolver cannot satisfy it
        if (f"scripts/{name}" not in declared
                and name not in declared):
            return True  # unbundled + undeclared — would ship broken
    return False


@dataclass
class Plan:
    writes: list[tuple[Path, Path]] = field(default_factory=list)   # (src, dst)
    skips: list[tuple[Path, str]] = field(default_factory=list)     # (dst, why)
    conflicts: list[tuple[Path, str]] = field(default_factory=list) # (dst, why)
    removals: list[Path] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    resolved: list[dict] = field(default_factory=list)
    # rel_dst -> (src_sha256, plugin_req) for every planned write — cross-plugin
    # output-path collision detection (the last writer must never win silently)
    planned: dict[str, tuple[str, str]] = field(default_factory=dict)


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        sys.stderr.write(f"FAIL: manifest not found: {path}\n")
        sys.exit(2)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        sys.stderr.write("FAIL: ai-manifest.yaml must be a mapping\n")
        sys.exit(2)
    for key in ("version", "universe", "requires"):
        if key not in data:
            sys.stderr.write(f"FAIL: ai-manifest.yaml missing required key: {key}\n")
            sys.exit(2)
    if data["version"] != 1:
        sys.stderr.write(f"FAIL: unsupported manifest version: {data['version']}\n")
        sys.exit(2)
    if not isinstance(data["requires"], list):
        sys.stderr.write("FAIL: requires must be a list\n")
        sys.exit(2)
    return data


def find_registry_root(path: Path) -> Path:
    """Accept a bare registry/ dir or a checkout that contains one."""
    for cand in (path, path / "registry"):
        if (cand / "plugins.json").is_file():
            return cand
    sys.stderr.write(f"FAIL: no registry/plugins.json under {path}\n")
    sys.exit(2)


def load_plugins_index(registry_root: Path) -> dict[tuple[str, str], dict]:
    index = json.loads((registry_root / "plugins.json").read_text(encoding="utf-8"))
    return {(p["scope"], p["name"]): p for p in index.get("plugins", [])}


def version_satisfies(version: str, ref: str) -> bool:
    """Exact semver or caret range (^x.y -> same major, >=).

    tag:/sha: pins never reach here — they are verified against the actual
    checkout revision in plan_requirement, so a checkout at the wrong commit
    cannot silently satisfy a pin."""
    def parse(v: str) -> tuple[int, ...]:
        parts = v.lstrip("v").split(".")
        return tuple(int(p) for p in parts if p.isdigit())

    def pad(t: tuple[int, ...]) -> tuple[int, int, int]:
        return (t + (0, 0, 0))[:3]

    if ref.startswith("^"):
        want = parse(ref[1:])
        # SemVer caret: the upper bound is the first NONZERO component +1 —
        # ^1.4 → <2.0.0, ^0.1 → <0.2.0, ^0.0.3 → <0.0.4. All-zero constraints
        # bound at their declared width: ^0 → <1.0.0, ^0.0 → <0.1.0.
        padded = pad(want)
        upper = None
        for i, c in enumerate(padded):
            if c:
                upper = padded[:i] + (c + 1,)
                break
        if upper is None:
            upper = tuple(1 if j == len(want) - 1 else 0 for j in range(3))
        have = pad(parse(version))
        return pad(want) <= have < upper
    # Exact refs are padded like caret bounds — the grammar accepts x[.y[.z]],
    # so '1.14' must satisfy a plugin reporting '1.14.0'.
    return pad(parse(version)) == pad(parse(ref))


def git_rev(repo: Path, rev: str) -> str | None:
    """Resolve `rev` to a commit sha inside `repo`, or None when the path is
    not a git checkout (or the rev does not exist)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{rev}^{{commit}}"],
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def collect_component_files(plugin_dir: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for comp in COMPONENT_TARGETS:
        d = plugin_dir / comp
        if d.is_dir():
            out[comp] = sorted(p for p in d.rglob("*") if p.is_file())
    return out


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_replace(dst: Path, fill) -> None:
    """Install `dst` through an exclusively-created sibling temp + rename.

    tempfile.mkstemp picks a random name with O_EXCL — a consumer cannot
    pre-plant a symlink or hard link there, so writes can never follow a
    link out of the tree. os.replace then unlinks any existing dst entry,
    so a destination hard-linked to a file outside the owned tree keeps
    its shared inode (and the external peer) untouched."""
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent,
                                    prefix=f".{dst.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            fill(f)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def plan_requirement(req: str, ref: str, universe: str, registry_root: Path,
                     index: dict, repo_root: Path, locked: dict, plan: Plan,
                     write_components: bool = True) -> None:
    m = REQUIRES_RE.match(req)
    if not m:
        plan.conflicts.append((repo_root / req, "malformed plugin reference"))
        return
    scope, name = m.group(1), m.group(2)

    # Fail-closed scope check — a repo can only reach platform, its own
    # universe, and the local scopes.
    allowed = {"platform", universe} | LOCAL_SCOPES
    if scope not in allowed:
        plan.conflicts.append((
            repo_root / req,
            f"scope '{scope}' is not reachable from universe '{universe}' "
            f"(allowed: platform, {universe}, repo, personal) — refusing",
        ))
        return
    if scope in LOCAL_SCOPES:
        plan.advisories.append(
            f"{req}: repo/personal assets are materialised in place — nothing to fetch")
        return

    pinned = ref.startswith(("sha:", "tag:"))
    if pinned:
        # Verify the pin BEFORE trusting the index — plugins.json is itself a
        # resolution input, and a dirty index could retarget a scope/name.
        want = ref.split(":", 1)[1]
        if ref.startswith("sha:"):
            # sha: must be a literal commit id — rev-parse accepts arbitrary
            # expressions, so sha:HEAD would otherwise always pass.
            if not re.fullmatch(r"[0-9a-fA-F]{7,40}", want):
                plan.conflicts.append((repo_root / req,
                    f"pinned ref '{ref}' — sha: requires a hexadecimal "
                    "commit id"))
                return
            pin_rev = want
        else:
            # git rev-parse accepts revision operators — tag:v2~1 would
            # resolve to refs/tags/v2's PARENT, not a tag named v2~1.
            # check-ref-format rejects ~ ^ : ? * [ \ spaces and "..".
            bad_name = subprocess.run(
                ["git", "check-ref-format", f"tags/{want}"],
                capture_output=True, timeout=10)
            if bad_name.returncode != 0:
                plan.conflicts.append((
                    repo_root / req,
                    f"pinned ref '{ref}' — tag: requires a valid git tag "
                    "name (no revision operators, '..', or spaces)",
                ))
                return
            # tag: resolves strictly under refs/tags/ — tag:main must not
            # satisfy against a branch.
            pin_rev = f"refs/tags/{want}"
        head = git_rev(registry_root, "HEAD")
        if head is None:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' needs a verifiable git checkout — "
                "the registry source is not a git repository",
            ))
            return
        pinned_sha = git_rev(registry_root, pin_rev)
        if pinned_sha is None:
            kind = "tag" if ref.startswith("tag:") else "commit"
            plan.conflicts.append((
                repo_root / req,
                f"pinned {kind} '{want}' does not resolve in the registry "
                "checkout",
            ))
            return
        if pinned_sha != head:
            plan.conflicts.append((
                repo_root / req,
                f"registry checkout is not at the pinned ref '{want}' "
                f"(HEAD {head[:12]}) — check out the pin or use a version range",
            ))
            return
        # HEAD may equal the pin while the worktree is dirty — materialising
        # would copy uncommitted bytes while recording the pinned ref. The
        # check covers the whole registry: plugins.json and every plugin
        # tree are pin inputs.
        dirty = subprocess.run(
            ["git", "-C", str(registry_root), "status", "--porcelain",
             "--untracked-files=all", "--", "."],
            capture_output=True, text=True, timeout=10)
        if dirty.returncode != 0:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' cannot verify worktree cleanliness — "
                "git status failed; refusing to record a pin over "
                "unverifiable bytes",
            ))
            return
        if dirty.stdout.strip():
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' requires a clean worktree — uncommitted "
                "changes under the registry (index and plugin content are "
                "pin inputs)",
            ))
            return
    else:
        body = ref[1:] if ref.startswith("^") else ref
        if not SEMVER_REF.fullmatch(body):
            plan.conflicts.append((repo_root / req, f"malformed ref '{ref}' — "
                "supported: ^x[.y[.z]], x[.y[.z]], tag:<tag>, sha:<sha>"))
            return

    entry = index.get((scope, name))
    if entry is None:
        plan.conflicts.append((repo_root / req, "not in registry/plugins.json"))
        return
    plugin_dir = registry_root.parent / entry["path"] if not Path(entry["path"]).is_absolute() else Path(entry["path"])
    if not plugin_dir.is_dir():
        plan.conflicts.append((repo_root / req, f"missing plugin dir: {entry['path']}"))
        return
    try:
        indexed_rel = plugin_dir.relative_to(registry_root)
    except ValueError:
        plan.conflicts.append((repo_root / req,
            f"indexed path '{entry['path']}' is outside the registry"))
        return
    if indexed_rel.parts != (scope, name):
        plan.conflicts.append((repo_root / req,
            f"indexed path '{entry['path']}' does not match the requested "
            f"scope/name '{scope}/{name}' — refusing to alias"))
        return
    if plugin_dir.resolve() != plugin_dir:
        # A symlinked plugin dir (or ancestor) aliases another scope's tree —
        # materialising it would distribute the target's content under this
        # plugin's name. Registry lint rejects the same shape at the gate.
        plan.conflicts.append((
            repo_root / req,
            "plugin dir contains a symlink — refusing to materialise through it",
        ))
        return

    manifest_file = plugin_dir / ".claude-plugin" / "plugin.json"
    if pinned:
        # The manifest feeds resolved_version + the lock's sha256 — it is a
        # pin input like any component file, so verify it against the pinned
        # git object too (skip-worktree on THIS file passes every other check
        # while recording modified metadata under the pin's name).
        rel_man = manifest_file.relative_to(registry_root).as_posix()
        try:
            man_blob = subprocess.run(
                ["git", "-C", str(registry_root), "show", f"HEAD:./{rel_man}"],
                capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            man_blob = None
        if (man_blob is None or man_blob.returncode != 0
                or not manifest_file.is_file()
                or man_blob.stdout != manifest_file.read_bytes()):
            plan.conflicts.append((
                repo_root / req,
                f"{req}: {rel_man} differs from or is absent at the pinned "
                "revision — refusing to record pin metadata from unverifiable bytes",
            ))
            return
    version = "0.0.0"
    if manifest_file.is_file():
        try:
            version = json.loads(manifest_file.read_text(encoding="utf-8"))["version"]
            if not isinstance(version, str):
                version = "0.0.0"
        except (json.JSONDecodeError, KeyError):
            pass
    if not pinned and not version_satisfies(version, ref):
        plan.conflicts.append((
            repo_root / req,
            f"ref '{ref}' not satisfied by registry version {version}",
        ))
        return

    materialised: dict[str, str] = {}
    component_files = (collect_component_files(plugin_dir)
                       if write_components else {})
    if pinned:
        # Worktree enumeration alone is not authoritative under a pin: a
        # tracked component DELETED while marked skip-worktree leaves status
        # clean and simply never appears in the rglob — the per-file git show
        # would never run on it, materialising an incomplete plugin under the
        # pin's name. Compare the git TREE's file list for each component dir
        # against what the worktree actually holds — enumerated independently
        # of component_files since surface selection may leave that empty.
        # Extra worktree files are already refused per-file below (untracked
        # → no pinned object).
        verify_files = collect_component_files(plugin_dir)
        for comp in COMPONENT_TARGETS:
            rel_dir = (plugin_dir / comp).relative_to(registry_root).as_posix()
            try:
                tree = subprocess.run(
                    ["git", "-C", str(registry_root), "ls-tree", "-r",
                     "--name-only", "HEAD", "--", rel_dir],
                    capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                tree = None
            if tree is None or tree.returncode != 0:
                plan.conflicts.append((
                    repo_root / req,
                    f"pinned ref '{ref}' cannot enumerate the tracked plugin "
                    "tree — refusing to record a pin over unverifiable state",
                ))
                return
            tracked = {ln for ln in tree.stdout.splitlines() if ln}
            present = {
                src.relative_to(registry_root).as_posix()
                for src in verify_files.get(comp, [])
            }
            missing = sorted(tracked - present)
            if missing:
                plan.conflicts.append((
                    repo_root / req,
                    f"{req}: {missing[0]} is tracked at the pinned revision "
                    "but absent from the worktree (skip-worktree deletion?) "
                    "— refusing an incomplete pin",
                ))
                return
    for comp, files in component_files.items():
        target_root = repo_root / COMPONENT_TARGETS[comp]
        for src in files:
            rel = src.relative_to(plugin_dir / comp)
            dst = target_root / rel
            rel_dst = dst.relative_to(repo_root).as_posix()
            resolved_src = src.resolve()
            if (resolved_src != src
                    or not resolved_src.is_relative_to(plugin_dir)):
                # A symlinked component file (or one behind a linked dir)
                # copies the link target's bytes into the consumer — e.g.
                # agents/leak.md -> /etc/passwd ships /etc/passwd.
                plan.conflicts.append((
                    dst,
                    f"{req}: {rel} is or resolves through a symlink — "
                    "refusing to materialise the link target"))
                continue
            src_bytes = src.read_bytes()
            if pinned:
                # Compare against the pinned git OBJECT, not the index or
                # status output: --untracked-files=all is blind to ignored
                # files, and skip-worktree / assume-unchanged index flags
                # let a tracked file's modified worktree bytes pass both
                # checks while not being what the pinned revision holds.
                rel_src = src.relative_to(registry_root).as_posix()
                try:
                    blob = subprocess.run(
                        ["git", "-C", str(registry_root), "show",
                         f"HEAD:./{rel_src}"],
                        capture_output=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    plan.conflicts.append((
                        dst,
                        f"{req}: {rel} cannot be verified against the "
                        "pinned revision — git show failed",
                    ))
                    continue
                if blob.returncode != 0:
                    plan.conflicts.append((
                        dst,
                        f"{req}: {rel} is not tracked at the pinned revision "
                        "(ignored or untracked) — refusing to materialise "
                        "bytes outside the pin",
                    ))
                    continue
                if blob.stdout != src_bytes:
                    plan.conflicts.append((
                        dst,
                        f"{req}: {rel} differs from the pinned git object "
                        "(index flags like skip-worktree can hide the "
                        "divergence) — refusing to materialise",
                    ))
                    continue
            if (b"CLAUDE_PLUGIN_ROOT" in src_bytes
                    or declares_script_deps(src_bytes)
                    or script_dep_block(plugin_dir, src_bytes)):
                # Files depending on the plugin install root or on sibling
                # scripts/ cannot run in a resolver install — the resolver
                # does not materialise scripts (surface wiring is a later
                # phase). Shipping them would document commands/skills that
                # fail on invoke.
                plan.advisories.append(
                    f"{req}: {rel} depends on the plugin root or scripts/ — "
                    f"not runnable in resolver mode (needs marketplace install "
                    f"or script wiring); not materialised")
                continue
            # Any symlink in the destination chain — dst itself or an ancestor
            # — makes mkdir/copy2 write through it: outside the repo or across
            # to another locked capability. resolve() != dst proves a link
            # exists regardless of where it points; refuse to write through it.
            if dst.resolve() != dst:
                plan.conflicts.append((
                    dst,
                    "destination path contains a symlink — refusing to materialise "
                    "through it (replace the link with a real directory)",
                ))
                continue
            # Type-check ancestors too — .claude/agents as a plain FILE is
            # not a symlink, passes the checks above, and dst.exists() is
            # False for its children: --apply would copy every earlier file
            # then crash at mkdir(), leaving them materialised without a
            # lock. Reject non-directory ancestors at plan time.
            file_ancestor = None
            for anc in dst.parents:
                if anc == repo_root:
                    break
                if anc.exists() and not anc.is_dir():
                    file_ancestor = anc
                    break
            if file_ancestor is not None:
                plan.conflicts.append((
                    dst,
                    f"destination ancestor {file_ancestor.relative_to(repo_root)} "
                    "is not a directory — refusing to materialise through it",
                ))
                continue
            src_sha = hashlib.sha256(src_bytes).hexdigest()
            prior = plan.planned.get(rel_dst)
            if prior is not None:
                prior_sha, prior_req = prior
                if prior_sha != src_sha:
                    plan.conflicts.append((
                        dst,
                        f"output-path collision: {req} provides different content for this "
                        f"path than {prior_req} — refusing to pick a winner",
                    ))
                else:
                    plan.skips.append((dst, f"identical — already provided by {prior_req}"))
                    materialised[rel_dst] = src_sha
                continue
            if dst.exists() and not dst.is_file():
                # A directory (or FIFO/socket) at the destination —
                # read_bytes() would crash IsADirectoryError instead of
                # reporting a fail-closed conflict.
                plan.conflicts.append((
                    dst,
                    "destination exists as a non-regular file — refusing to "
                    "overwrite it (remove the directory and re-resolve)",
                ))
                continue
            if dst.exists():
                if dst.read_bytes() == src_bytes:
                    plan.skips.append((dst, "identical"))
                    plan.planned[rel_dst] = (src_sha, req)
                elif rel_dst not in locked:
                    plan.conflicts.append((
                        dst,
                        "exists and differs — not lockfile-tracked, refusing to clobber a hand edit",
                    ))
                    continue
                elif locked[rel_dst] is None or sha256(dst) != locked[rel_dst]:
                    plan.conflicts.append((
                        dst,
                        "materialised file modified since install — refusing to clobber a hand "
                        "edit (restore it or delete it and re-resolve)",
                    ))
                    continue
                else:
                    plan.writes.append((src, dst))  # registry drift — update
                    plan.planned[rel_dst] = (src_sha, req)
                materialised[rel_dst] = src_sha
            else:
                plan.writes.append((src, dst))
                plan.planned[rel_dst] = (src_sha, req)
                materialised[rel_dst] = src_sha

    for comp in ("hooks", "scripts", "data", "telemetry"):
        if (plugin_dir / comp).is_dir():
            plan.advisories.append(
                f"{req}: '{comp}/' needs surface wiring (settings merge) — "
                f"not materialised by --apply; see docs/registry.md")

    plan.resolved.append({
        "plugin": req,
        "scope": scope,
        "ref": ref,
        "resolved_version": version,
        "source": entry["path"],
        "files": materialised,
        "sha256": sha256(manifest_file) if manifest_file.is_file() else None,
    })


def load_lock(repo_root: Path) -> tuple[dict, str | None]:
    """(lock doc, error) — a lock that exists but can't be parsed must never
    masquerade as an empty ownership map: --check would report OK while
    resolver-installed files stay behind, and the next --apply would
    overwrite the lock and lose their ownership permanently."""
    p = repo_root / LOCK_PATH
    if not p.is_file():
        return {}, None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {}, f"unparseable JSON: {e}"
    if not isinstance(doc, dict):
        return {}, "not a JSON object"
    # Structure validation — a syntactically valid but wrongly-typed lock
    # ({"files": null}, resolved:[1,2]) must fail closed with a repairable
    # conflict, not a TypeError traceback deep in locked_digests.
    files = doc.get("files")
    if "files" in doc:
        if isinstance(files, dict):
            if not all(isinstance(k, str)
                       and (v is None or isinstance(v, str))
                       for k, v in files.items()):
                return {}, "'files' entries must map paths to digests"
        elif isinstance(files, list):
            if not all(isinstance(f, str) for f in files):
                return {}, "'files' list entries must be path strings"
        else:
            return {}, "'files' must be an object or a list"
    resolved = doc.get("resolved")
    if "resolved" in doc and (
            not isinstance(resolved, list)
            or not all(isinstance(r, dict) for r in resolved)):
        return {}, "'resolved' must be a list of plugin objects"
    if isinstance(resolved, list):
        # Nested 'files' maps inside resolved entries feed locked_digests —
        # {"resolved":[{"files":null}]} is valid JSON that TypeErrors there.
        for r in resolved:
            if "files" not in r:
                continue
            rf = r["files"]
            if isinstance(rf, dict):
                ok = all(isinstance(k, str)
                         and (v is None or isinstance(v, str))
                         for k, v in rf.items())
            elif isinstance(rf, list):
                ok = all(isinstance(f, str) for f in rf)
            else:
                ok = False
            if not ok:
                return {}, ("resolved entry 'files' must map paths to "
                            "digests or list path strings")
    return doc, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default="ai-manifest.yaml")
    ap.add_argument("--registry", required=True,
                    help="path to a checkout containing registry/ (e.g. an ai-starter-pack clone)")
    ap.add_argument("--repo-root", default=".")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="with --apply, also remove lockfile-tracked files no longer required")
    args = ap.parse_args()
    if args.prune and not args.apply:
        ap.error("--prune requires --apply")

    repo_root = Path(args.repo_root).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path
    manifest = load_manifest(manifest_path)
    universe = str(manifest["universe"])
    registry_root = find_registry_root(Path(args.registry).resolve())
    index = load_plugins_index(registry_root)
    lock, lock_err = load_lock(repo_root)

    locked_dig = locked_digests(lock)
    plan = Plan()
    # claude-code is the only implemented surface — anything else the
    # manifest selects is advisory until its renderer lands, and a manifest
    # that selects no claude-code must materialise no .claude/ output at all.
    surfaces = manifest.get("surfaces")
    claude_selected = not isinstance(surfaces, list) or "claude-code" in surfaces
    if isinstance(surfaces, list):
        for s in surfaces:
            if s != "claude-code":
                plan.advisories.append(
                    f"surface '{s}' is advisory-only — no renderer yet "
                    "(claude-code is the only implemented surface)")
    for req in manifest["requires"]:
        plan_requirement(req["plugin"], str(req["ref"]), universe,
                         registry_root, index, repo_root, locked_dig, plan,
                         write_components=claude_selected)

    # Orphan detection: lockfile files no longer required. Without --prune an
    # orphan is simply kept (and stays lockfile-tracked) — modified or not.
    # Under --prune a file whose on-disk digest differs from the installed
    # digest (or whose install digest is unknown — v1 locks) may be a hand
    # edit: never unlink it silently, surface it as a conflict.
    current = set()
    for r in plan.resolved:
        current.update(r["files"])
    for f in sorted(locked_dig):
        if f in current:
            continue
        # Containment: the lockfile is data, not authority — a poisoned or
        # legacy-v1 entry (absolute path, .. escape, or anything outside the
        # resolver-owned subtrees — .claude/{skills,agents,commands}) must
        # never steer an unlink outside them. .claude/settings.json and
        # friends are not resolver-owned. Resolve it and fail closed.
        lexical = repo_root / f
        candidate = lexical.resolve()
        # The entry itself may still be a symlink — unlink removes the link,
        # never its target. But a symlink ANCESTOR (.claude/skills/alias ->
        # ../agents) makes unlink traverse the link and delete another
        # capability's file; the parent chain must resolve to itself.
        parent_resolved = lexical.parent.resolve()
        try:
            rel_c = candidate.relative_to(repo_root)
        except ValueError:
            rel_c = None
        if (Path(f).is_absolute() or parent_resolved != lexical.parent
                or rel_c is None
                or "/".join(rel_c.parts[:2]) not in OWNED_ROOTS):
            plan.conflicts.append((
                lexical,
                "lockfile path outside resolver-owned roots or behind a "
                "symlinked directory (.claude/{skills,agents,commands}) — "
                "refusing to act on it "
                "(repair .ai/capability-lock.json manually)",
            ))
            continue
        digest = locked_dig[f]
        # Digest checks read THROUGH a symlink (that's what the lock recorded),
        # but removals act on the lexical path — unlinking a symlink entry must
        # remove the link, never its target.
        if (args.prune and lexical.is_file()
                and (digest is None or sha256(lexical) != digest)):
            plan.conflicts.append((
                lexical,
                "prune candidate modified since install — refusing to remove a "
                "possibly hand-edited file (delete or restore it manually, then re-resolve)",
            ))
        else:
            plan.removals.append(lexical)

    # A symlinked .ai dir or lock file makes write_text follow the link out of
    # the repo — refuse before any materialisation applies.
    lock_file = repo_root / LOCK_PATH
    if lock_file.resolve() != lock_file:
        plan.conflicts.append((
            lock_file,
            "lockfile destination contains a symlink — refusing to write through it",
        ))
    # Type-check the destination too — --apply copies every planned file
    # BEFORE the lock write; if .ai is a plain file or the lock path is a
    # directory the copy succeeds and the lock fails, leaving materialised
    # files with no ownership record. Reject it at plan time.
    if lock_file.parent.exists() and not lock_file.parent.is_dir():
        plan.conflicts.append((
            lock_file,
            "lockfile parent .ai is not a directory — refusing to materialise "
            "without an ownership path",
        ))
    elif lock_file.exists() and not lock_file.is_file():
        plan.conflicts.append((
            lock_file,
            "lockfile destination is not a regular file — refusing to "
            "materialise without an ownership path",
        ))
    if lock_err:
        plan.conflicts.append((
            lock_file,
            f"capability lock is malformed ({lock_err}) — refusing to infer an "
            f"empty ownership map (repair or delete {LOCK_PATH})",
        ))

    # ---- report ----
    print(f"universe={universe}  registry={registry_root}")
    for r in plan.resolved:
        print(f"  resolve {r['plugin']}@{r['resolved_version']}  ({len(r['files'])} files)")
    for _, dst in plan.writes:
        print(f"  write   {dst}")
    for dst, why in plan.skips[:20]:
        print(f"  skip    {dst} ({why})")
    if len(plan.skips) > 20:
        print(f"  skip    … {len(plan.skips) - 20} more identical")
    for dst in plan.removals:
        print(f"  prune   {dst} {'(will remove)' if args.prune else '(kept — --prune to remove)'}")
    for a in plan.advisories:
        print(f"  note    {a}")
    for dst, why in plan.conflicts:
        print(f"  CONFLICT {dst}: {why}")

    if plan.conflicts:
        print(f"\n{len(plan.conflicts)} conflict(s) — fail-closed, nothing applied")
        return 1

    if args.check:
        drift = [d for _, d in plan.writes]
        # Verify the lock itself, not just file bytes — a consumer whose
        # files match the registry but whose lock is missing or stale has
        # no ownership record: CI would pass, then a later registry update
        # reads the files as untracked hand edits and refuses to update.
        # Expected doc mirrors exactly what --apply would write.
        expected_files = {rel: digest for r in plan.resolved
                          for rel, digest in r["files"].items()}
        for f in plan.removals:
            rel = f.relative_to(repo_root).as_posix()
            expected_files[rel] = locked_dig[rel]
        expected_resolved = [{k: v for k, v in r.items() if k != "files"}
                             for r in plan.resolved]
        lock_missing = not lock_file.is_file()
        expected_lock = {
            "version": 1,
            "universe": universe,
            "resolved": expected_resolved,
            "files": expected_files,
        }
        lock_stale = lock != expected_lock
        if drift or plan.removals or lock_missing or lock_stale:
            if lock_missing:
                print("\nDRIFT: capability lock missing — run --apply to "
                      "establish ownership")
            elif lock_stale:
                print("\nDRIFT: capability lock is stale — run --apply to "
                      "refresh ownership")
            print("\nDRIFT: materialised state differs from registry source")
            return 1
        print("\nOK: materialised state matches registry source")
        return 0

    if args.apply:
        for src, dst in plan.writes:
            dst.parent.mkdir(parents=True, exist_ok=True)
            atomic_replace(dst, lambda f, s=src: shutil.copyfileobj(
                s.open("rb"), f))
            try:
                shutil.copystat(src, dst)   # keep copy2's mode/mtime semantics
            except OSError:
                pass
        if args.prune:
            for f in plan.removals:
                if f.is_file() or f.is_symlink():
                    f.unlink()
            for f in plan.removals:
                d = f.parent
                while d != repo_root and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
        new_files = {rel: digest for r in plan.resolved
                     for rel, digest in r["files"].items()}
        if not args.prune:
            # Kept orphans stay lockfile-tracked so a later --apply --prune
            # (or --check) still knows they were installed by the resolver.
            for f in plan.removals:
                rel = f.relative_to(repo_root).as_posix()
                new_files[rel] = locked_dig[rel]
        lock_doc = {
            "version": 1,
            "universe": universe,
            "resolved": [{k: v for k, v in r.items() if k != "files"} for r in plan.resolved],
            "files": new_files,
        }
        lock_file = repo_root / LOCK_PATH
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic like the component writes — write_text truncates a
        # hard-linked lock's shared inode (external peer), and a crash
        # mid-write would leave a partial ownership record.
        atomic_replace(
            lock_file,
            lambda f: f.write(json.dumps(lock_doc, indent=2).encode("utf-8")
                              + b"\n"))
        print(f"\napplied: {len(plan.writes)} write(s), lock -> {LOCK_PATH}")
        return 0

    print(f"\ndry-run: {len(plan.writes)} write(s), {len(plan.skips)} identical, "
          f"{len(plan.removals)} orphaned — pass --apply to materialise")
    return 0


def locked_digests(lock: dict) -> dict[str, str | None]:
    """All lockfile-tracked repo-relative paths -> installed sha256 (or None
    for v1 locks that recorded paths without digests)."""
    out: dict[str, str | None] = {}
    if not lock:
        return out
    files = lock.get("files", {})
    if isinstance(files, dict):
        out.update(files)
    else:
        out.update((f, None) for f in files)
    for r in lock.get("resolved", []):
        rf = r.get("files", {})
        if isinstance(rf, dict):
            for k in rf:
                out.setdefault(k, rf[k])
        else:
            for k in rf:
                out.setdefault(k, None)
    return out


if __name__ == "__main__":
    sys.exit(main())
