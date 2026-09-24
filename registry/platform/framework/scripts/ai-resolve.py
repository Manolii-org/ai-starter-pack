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
    rb"\s+[^\n|&;`]*?scripts/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|sh|ts|js|mjs)\b"
    rb"|\./scripts/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|sh|ts|js|mjs)\b")
# Backticked `scripts/x.py` is NOT an invocation context — prose uses it for
# mentions. A real dependency that no interpreter/./ prefix expresses must be
# declared explicitly: `requires_scripts: [...]` in the file's frontmatter.
SCRIPT_DEP_KEYS = ("requires_scripts",)
# Frontmatter declaring the file's `scripts/x.py` references are CONSUMER-side
# (the consumer repo already owns them, or the file's own setup section has the
# consumer fetch them) — the only exemption that lets an unbundled script
# reference materialise instead of being skipped as an unsatisfiable dep.
CONSUMER_SCRIPT_KEYS = ("consumer_scripts",)
# Path extraction for SCRIPT_REF matches (relative to scripts/) — used to
# distinguish bundled plugin scripts (a real dependency) from
# consumer-repository commands.
SCRIPT_NAME = re.compile(
    rb"scripts/((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|sh|ts|js|mjs))\b")


_DQ_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
    "L": "\u2028", "P": "\u2029",
}


def _dq_decode(s: str) -> str:
    """YAML double-quoted scalar escapes — \\xNN, \\uNNNN, \\UNNNNNNNN and the
    single-char set. Unknown escapes raise rather than reinterpret."""
    out = []
    i = 0
    while i < len(s):
        if s[i] != "\\":
            out.append(s[i])
            i += 1
            continue
        i += 1
        if i >= len(s):
            raise ValueError("trailing backslash in double-quoted scalar")
        c = s[i]
        i += 1
        if c in _DQ_ESCAPES:
            out.append(_DQ_ESCAPES[c])
            continue
        n = {"x": 2, "u": 4, "U": 8}.get(c)
        if n is None:
            raise ValueError(f"unsupported escape \\{c}")
        hexs = s[i:i + n]
        if len(hexs) != n or not re.fullmatch(r"[0-9a-fA-F]+", hexs):
            raise ValueError(f"malformed escape \\{c}{hexs}")
        out.append(chr(int(hexs, 16)))
        i += n
    return "".join(out)


def _mini_yaml(text: str):
    """Stdlib-only YAML subset so the vendored resolver runs without PyYAML.

    Covers the ai-manifest + plugin-frontmatter grammar: block mappings,
    block sequences (scalar items and `- key: value` inline maps continued
    by deeper-indented keys), flow `[a, b]` lists, `{}` empty maps, comments,
    and plain/quoted/int/float/bool/null scalars. Anything richer (anchors,
    folded scalars, flow maps with content, multi-doc, tabs) raises
    ValueError — callers must fail closed, never guess."""
    def strip_comment(s: str) -> str:
        # ' #' starts a comment only outside quotes. Track quote state
        # properly — an apostrophe inside a double-quoted scalar (or a
        # backslash escape) must not corrupt the balance check.
        in_s = in_d = False
        i = 0
        while i < len(s):
            ch = s[i]
            if in_d:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_d = False
            elif in_s:
                if ch == "'":
                    if s[i + 1:i + 2] == "'":
                        i += 2   # doubled '' is an escaped quote
                        continue
                    in_s = False
            elif ch == '"':
                in_d = True
            elif ch == "'":
                in_s = True
            elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
                return s[:i]
            i += 1
        return s

    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        body = strip_comment(raw.rstrip())
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip())
        if "\t" in body[:indent]:
            raise ValueError("tab indentation is not supported")
        lines.append((indent, body.lstrip()))

    pos = [0]
    key_re = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?:\s+(.*))?$")

    def flow_items(inner: str) -> list[str]:
        # Split a flow list's item text on top-level commas only — a comma
        # inside a quoted scalar or a nested flow belongs to the item.
        items, depth = [], 0
        in_s = in_d = esc = False
        start = 0
        for i, ch in enumerate(inner):
            if esc:
                esc = False
            elif in_d and ch == "\\":
                esc = True
            elif in_d and ch == '"':
                in_d = False
            elif in_s and ch == "'":
                if inner[i + 1:i + 2] == "'":
                    esc = True  # '' escape — skip the second quote too
                else:
                    in_s = False
            elif ch == '"' and not in_s:
                # Quotes only open a quoted region at the START of an item —
                # a " inside an already-started plain scalar is plain text.
                if not inner[start:i].strip():
                    in_d = True
            elif ch == "'" and not in_d:
                # Same for ': YAML allows apostrophes in plain scalars, so
                # `editor's-tool` mid-item must not swallow the comma.
                if not inner[start:i].strip():
                    in_s = True
            elif not in_s and not in_d:
                if ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                elif ch == "," and depth == 0:
                    items.append(inner[start:i])
                    start = i + 1
        items.append(inner[start:])
        return items

    def scalar(tok: str):
        tok = tok.strip()
        if not tok:
            raise ValueError("empty scalar")
        if tok.startswith("[") and tok.endswith("]"):
            inner = tok[1:-1].strip()
            return ([] if not inner
                    else [scalar(p) for p in flow_items(inner)])
        if tok == "{}":
            return {}
        if tok == "[]":
            return []
        if tok[0] == '"':
            if not (len(tok) > 1 and tok.endswith('"')):
                raise ValueError(f"unterminated quoted scalar {tok!r}")
            return _dq_decode(tok[1:-1])
        if tok[0] == "'":
            if not (len(tok) > 1 and tok.endswith("'")):
                raise ValueError(f"unterminated quoted scalar {tok!r}")
            return tok[1:-1].replace("''", "'")
        if re.fullmatch(r"-?\d+", tok):
            return int(tok)
        if re.fullmatch(r"-?\d+\.\d+", tok):
            return float(tok)
        if tok in ("true", "false", "True", "False"):
            return tok.lower() == "true"
        if tok in ("null", "~"):
            return None
        if tok[0] in "[{|>&!%@`":
            raise ValueError(f"unsupported scalar {tok!r}")
        return tok

    def parse(indent: int):
        if lines[pos[0]][0] != indent:
            raise ValueError("inconsistent indentation")
        first = lines[pos[0]][1]
        if first == "-" or first.startswith("- "):
            seq = []
            while (pos[0] < len(lines)
                   and lines[pos[0]][0] == indent
                   and (lines[pos[0]][1] == "-"
                        or lines[pos[0]][1].startswith("- "))):
                item = lines[pos[0]][1][1:].lstrip()
                pos[0] += 1
                if not item:
                    seq.append(parse(lines[pos[0]][0])
                               if pos[0] < len(lines)
                               and lines[pos[0]][0] > indent else None)
                    continue
                km = key_re.match(item)
                if km:
                    d = {}
                    if km.group(2) is not None:
                        d[km.group(1)] = scalar(km.group(2))
                    elif (pos[0] < len(lines)
                          and lines[pos[0]][0] > indent):
                        d[km.group(1)] = parse(lines[pos[0]][0])
                    else:
                        d[km.group(1)] = None
                    while pos[0] < len(lines) and lines[pos[0]][0] > indent:
                        more = parse(lines[pos[0]][0])
                        if not isinstance(more, dict):
                            raise ValueError("nested sequence inside item map")
                        dup = d.keys() & more.keys()
                        if dup:
                            raise ValueError(
                                f"duplicate key {sorted(dup)[0]!r}")
                        d.update(more)
                    seq.append(d)
                else:
                    seq.append(scalar(item))
            return seq
        out = {}
        while pos[0] < len(lines) and lines[pos[0]][0] == indent:
            km = key_re.match(lines[pos[0]][1])
            if not km:
                raise ValueError(f"unsupported line {lines[pos[0]][1]!r}")
            k, v = km.group(1), km.group(2)
            if k in out:
                # Duplicate keys silently keep the last value in YAML — a
                # manifest that states a ref twice must be a parse error,
                # not a coin flip on which value won.
                raise ValueError(f"duplicate key {k!r}")
            pos[0] += 1
            if v is not None:
                if re.fullmatch(r"[>|][+-]?", v.strip()):
                    # Block scalar: every deeper-indented line is literal
                    # content (even lines shaped like keys or seq items).
                    vals = []
                    while (pos[0] < len(lines)
                           and lines[pos[0]][0] > indent):
                        vals.append(lines[pos[0]][1])
                        pos[0] += 1
                    out[k] = (" ".join(vals)
                              if v.strip().startswith(">") else "\n".join(vals))
                else:
                    out[k] = scalar(v)
                    # Plain scalars may continue on deeper-indented lines that
                    # are not a new key or seq item — YAML folds them with one
                    # space. A deeper key after a scalar is mixed content:
                    # reject.
                    while (pos[0] < len(lines)
                           and lines[pos[0]][0] > indent
                           and not key_re.match(lines[pos[0]][1])
                           and not lines[pos[0]][1].startswith("-")):
                        out[k] = str(out[k]) + " " + lines[pos[0]][1]
                        pos[0] += 1
                    if (pos[0] < len(lines) and lines[pos[0]][0] > indent):
                        raise ValueError("nested structure after scalar value")
            elif (pos[0] < len(lines)
                  and lines[pos[0]][0] == indent
                  and (lines[pos[0]][1] == "-"
                       or lines[pos[0]][1].startswith("- "))):
                # Indentationless block sequence: `key:` followed by `-`
                # items at the SAME indent is valid YAML (yaml.dump emits
                # it). The seq parser stops at the next non-dash line, so
                # sibling mapping keys are not consumed.
                out[k] = parse(indent)
            elif pos[0] < len(lines) and lines[pos[0]][0] > indent:
                # `key:` empty followed by deeper plain text is a folded
                # scalar in YAML; a deeper key/seq is a nested structure.
                if (not key_re.match(lines[pos[0]][1])
                        and not lines[pos[0]][1].startswith("-")):
                    vals = []
                    while (pos[0] < len(lines)
                           and lines[pos[0]][0] > indent
                           and not key_re.match(lines[pos[0]][1])
                           and not lines[pos[0]][1].startswith("-")):
                        vals.append(lines[pos[0]][1])
                        pos[0] += 1
                    out[k] = " ".join(vals)
                    if (pos[0] < len(lines)
                            and lines[pos[0]][0] > indent):
                        raise ValueError("nested structure after scalar")
                else:
                    out[k] = parse(lines[pos[0]][0])
            else:
                out[k] = None
        return out

    if not lines:
        return {}
    root = parse(lines[0][0])
    if pos[0] != len(lines):
        raise ValueError("trailing unparseable structure")
    return root


def _yaml_load(text: str):
    """One YAML grammar in every environment: the restricted _mini_yaml
    subset — the vendored resolver has no runtime deps and parses the same
    installed set whether or not PyYAML happens to be present. Richer YAML
    fails closed, never parses differently."""
    return _mini_yaml(text)


def _frontmatter(src_bytes: bytes) -> dict:
    m = re.match(rb"\A---\s*\n(.*?)\n---\s*\n", src_bytes, re.S)
    if not m:
        return {}
    try:
        fm = _yaml_load(m.group(1).decode("utf-8", errors="ignore")) or {}
    except Exception:
        # Frontmatter that fails to parse must not read as 'no deps declared' —
        # fail closed so the file is skipped rather than shipped half-gated.
        return {"_unparseable": True}
    return fm if isinstance(fm, dict) else {"_unparseable": True}


def declares_script_deps(src_bytes: bytes) -> bool:
    """True when the file's YAML frontmatter declares script dependencies —
    explicit metadata, since prose heuristics can't distinguish
    "run `scripts/x.py`" from "routing uses `scripts/x.py`"."""
    fm = _frontmatter(src_bytes)
    return fm.get("_unparseable", False) or any(
        fm.get(k) for k in SCRIPT_DEP_KEYS)


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


def script_dep_block(plugin_dir: Path, src_bytes: bytes,
                     pinned_scripts: set[str] | None = None) -> bool:
    """True when the file's script usage cannot run under a resolver install:
    a bundled plugin script (scripts/ isn't materialised), an explicit
    requires_scripts dep, or an unbundled invocation that isn't listed in
    consumer_scripts.

    Under a tag:/sha: pin, `pinned_scripts` carries the scripts/-relative
    paths the PINNED git tree holds — the worktree's is_file() would honour
    ignored/untracked plants and index-hidden deletions the pin never saw."""
    sdir = plugin_dir / "scripts"
    declared = declared_consumer_scripts(src_bytes)
    for m in SCRIPT_REF.finditer(src_bytes):
        # Only names inside actual invocations count — a bare `scripts/x.py`
        # mention in prose is not a dependency and must not gate materialise.
        n = SCRIPT_NAME.search(m.group(0))
        if not n:
            continue
        name = n.group(1).decode("utf-8", errors="ignore")
        bundled = (name in pinned_scripts if pinned_scripts is not None
                   else (sdir / name).is_file())
        if bundled:
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
    # rel_dst -> (src_sha256, plugin_req, src exec mask) for every planned
    # write — cross-plugin output-path collision detection: the last writer
    # must never win silently, and identical bytes with different modes are
    # not the same file.
    planned: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    # rel_dst -> verified source bytes, captured at plan time so --apply
    # writes what was checksummed instead of re-reading a mutable registry.
    payload: dict[str, bytes] = field(default_factory=dict)


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        sys.stderr.write(f"FAIL: manifest not found: {path}\n")
        sys.exit(2)
    try:
        data = _yaml_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        sys.stderr.write(f"FAIL: cannot parse ai-manifest.yaml: {e}\n")
        sys.exit(2)
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
    for i, entry in enumerate(data["requires"]):
        # Each entry must be a {plugin: str, ref: str} map — a non-mapping
        # crashes plan_requirement later, and an unquoted `ref: 1.10` parses
        # as the float 1.1, silently resolving a different requirement.
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("plugin"), str)
                or not isinstance(entry.get("ref"), str)):
            sys.stderr.write(
                f"FAIL: requires[{i}] must map string 'plugin' and 'ref' "
                "(quote numeric-looking refs, e.g. ref: \"1.10\")\n")
            sys.exit(2)
    surfaces = data.get("surfaces")
    if surfaces is not None and (
            not isinstance(surfaces, list)
            or not all(isinstance(s, str) for s in surfaces)):
        sys.stderr.write("FAIL: surfaces must be a list of strings\n")
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


def atomic_replace(dst: Path, fill, src: Path | None = None) -> None:
    """Install `dst` through an exclusively-created sibling temp + rename.

    tempfile.mkstemp picks a random name with O_EXCL — a consumer cannot
    pre-plant a symlink or hard link there, so writes can never follow a
    link out of the tree. os.replace then unlinks any existing dst entry,
    so a destination hard-linked to a file outside the owned tree keeps
    its shared inode (and the external peer) untouched. With `src`, the
    source's mode/mtime land on the temp BEFORE the swap — a metadata
    failure then leaves the old destination intact rather than
    publishing a 0600 temp."""
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent,
                                    prefix=f".{dst.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            fill(f)
        if src is not None:
            shutil.copystat(src, tmp)   # keep copy2's mode/mtime semantics
            # Never propagate privileged mode bits — a 4755/2755 source under
            # a root-run --apply would publish setuid/setgid on the output.
            os.chmod(tmp, tmp.stat().st_mode & 0o777)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def plan_requirement(req: str, ref: str, universe: str, registry_root: Path,
                     index: dict, repo_root: Path, locked: dict, plan: Plan,
                     write_components: bool = True,
                     locked_exec: dict | None = None) -> None:
    locked_exec = locked_exec or {}
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
        # porcelain is blind to skip-worktree / assume-unchanged edits —
        # the catalog is a resolution input, so verify the worktree copy
        # byte-for-byte against the pinned object before trusting `index`.
        cat = subprocess.run(
            ["git", "-C", str(registry_root), "show", "HEAD:./plugins.json"],
            capture_output=True, timeout=10)
        if cat.returncode != 0:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' cannot verify the plugin catalog — "
                "git show HEAD:./plugins.json failed; refusing to resolve "
                "an index the pin cannot vouch for",
            ))
            return
        try:
            live_catalog = (registry_root / "plugins.json").read_bytes()
        except OSError:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}': registry/plugins.json unreadable",
            ))
            return
        if cat.stdout != live_catalog:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}': registry/plugins.json differs from "
                "the pinned object (skip-worktree/assume-unchanged hides "
                "worktree edits from git status) — refusing to install a "
                "catalog the pin never published",
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
    version = None
    if manifest_file.is_file():
        try:
            version = json.loads(manifest_file.read_text(encoding="utf-8"))["version"]
        except (json.JSONDecodeError, KeyError):
            version = None
    # The manifest version is a resolution input: absent/malformed/non-semver
    # values must conflict — silently defaulting to 0.0.0 lets `ref: "0"`
    # satisfy a manifest-less plugin, and parse()'s permissive digit filter
    # would let "1.bad.2" satisfy "1.2".
    if (not isinstance(version, str)
            or not SEMVER_REF.fullmatch(version)):
        plan.conflicts.append((
            repo_root / req,
            f"{req}: .claude-plugin/plugin.json missing, unreadable, or "
            f"carries non-semver version {version!r} — refusing unverifiable "
            "resolution",
        ))
        return
    if not pinned and not version_satisfies(version, ref):
        plan.conflicts.append((
            repo_root / req,
            f"ref '{ref}' not satisfied by registry version {version}",
        ))
        return

    materialised: dict[str, str] = {}
    exec_modes: dict[str, int] = {}
    component_files = (collect_component_files(plugin_dir)
                       if write_components else {})
    pinned_scripts: set[str] | None = None
    pinned_modes: dict[str, str] = {}
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
                     "HEAD", "--", rel_dir],
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
            # '<mode> <type> <sha>\t<path>' — modes are pin inputs too: a
            # skip-worktree exec-bit flip passes the blob comparison while
            # copystat ships the wrong permissions under the pin's name.
            tracked = set()
            for ln in tree.stdout.splitlines():
                if not ln:
                    continue
                meta, _, path = ln.partition("\t")
                tracked.add(path)
                pinned_modes[path] = meta.split(" ", 1)[0]
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
        # The bundled-script check inside script_dep_block is a dep decision
        # the pin must also own: read scripts/ from the pinned git tree so an
        # ignored worktree plant or index-hidden deletion cannot flip it
        # while the lock records the same pin.
        rel_sdir = (plugin_dir / "scripts").relative_to(registry_root).as_posix()
        try:
            stree = subprocess.run(
                ["git", "-C", str(registry_root), "ls-tree", "-r",
                 "--name-only", "HEAD", "--", rel_sdir],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            stree = None
        if stree is None or stree.returncode != 0:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' cannot enumerate the pinned scripts tree "
                "— refusing to make dep decisions on unverifiable state",
            ))
            return
        pinned_scripts = {
            ln[len(rel_sdir) + 1:] for ln in stree.stdout.splitlines()
            if ln and ln.startswith(rel_sdir + "/")
        }
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
                if ((pinned_modes.get(rel_src) == "100755")
                        != bool(src.stat().st_mode & 0o111)):
                    plan.conflicts.append((
                        dst,
                        f"{req}: {rel} exec bit differs from the pinned git "
                        "tree (a skip-worktree mode flip hides it from "
                        "status) — refusing to materialise",
                    ))
                    continue
            if (b"CLAUDE_PLUGIN_ROOT" in src_bytes
                    or declares_script_deps(src_bytes)
                    or script_dep_block(plugin_dir, src_bytes,
                                        pinned_scripts)):
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
            src_exec = src.stat().st_mode & 0o111
            prior = plan.planned.get(rel_dst)
            if prior is not None:
                prior_sha, prior_req, prior_exec = prior
                # Exec compares on the any-exec state: git reproduces only
                # 100644/100755 and the umask picks the actual bits, so a
                # partial-mask difference is a checkout artifact, not content.
                if prior_sha != src_sha or bool(prior_exec) != bool(src_exec):
                    plan.conflicts.append((
                        dst,
                        f"output-path collision: {req} provides different content or "
                        f"mode for this path than {prior_req} — refusing to pick a winner",
                    ))
                else:
                    plan.skips.append((dst, f"identical — already provided by {prior_req}"))
                    materialised[rel_dst] = src_sha
                    exec_modes[rel_dst] = 0o111 if src_exec else 0
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
                # 'identical' means bytes AND the any-exec state match — a
                # registry file that gained/lost +x must fall through to the
                # locked drift-repair path, not skip with stale permissions.
                # The comparison is any-exec only: git stores 100644/100755
                # and the umask selects the bits, so the mask itself is not a
                # portable signal.
                dst_exec = dst.stat().st_mode & 0o111
                same_exec = bool(dst_exec) == bool(src_exec)
                bytes_match = dst.read_bytes() == src_bytes
                # For a tracked destination the on-disk mode must also match
                # the installed-mode record — a consumer chmod that
                # coincides with a registry chmod is still a local edit.
                rec_exec = locked_exec.get(rel_dst)
                exec_consistent = (rel_dst not in locked or rec_exec is None
                                   or _exec_matches(rec_exec,
                                                    dst.stat().st_mode))
                if bytes_match and same_exec and exec_consistent:
                    plan.skips.append((dst, "identical"))
                    plan.planned[rel_dst] = (src_sha, req, src_exec)
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
                elif not same_exec or not exec_consistent:
                    # Bytes match the install record but the exec state
                    # differs from the source and/or the record — the lock's
                    # installed-mode record distinguishes a local chmod
                    # (refuse) from a registry mode change (repair by
                    # rewriting). No record means the drift cannot be
                    # attributed — fail closed.
                    if (rec_exec is None
                            or not _exec_matches(rec_exec,
                                                 dst.stat().st_mode)):
                        plan.conflicts.append((
                            dst,
                            "exec mode differs from the installed-mode record "
                            "— refusing to clobber a possible local chmod "
                            "(restore the mode or delete the file and "
                            "re-resolve)",
                        ))
                        continue
                    else:
                        # dst mode still matches the install record — the
                        # registry changed mode; repair by rewriting.
                        plan.writes.append((src, dst))
                        plan.planned[rel_dst] = (src_sha, req, src_exec)
                        plan.payload[rel_dst] = src_bytes
                else:
                    plan.writes.append((src, dst))  # registry drift — update
                    plan.planned[rel_dst] = (src_sha, req, src_exec)
                    plan.payload[rel_dst] = src_bytes
                materialised[rel_dst] = src_sha
                exec_modes[rel_dst] = 0o111 if src_exec else 0
            else:
                plan.writes.append((src, dst))
                plan.planned[rel_dst] = (src_sha, req, src_exec)
                plan.payload[rel_dst] = src_bytes
                materialised[rel_dst] = src_sha
                exec_modes[rel_dst] = 0o111 if src_exec else 0

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
        "exec": exec_modes,
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
    provenance = doc.get("provenance")
    if "provenance" in doc and (
            not isinstance(provenance, dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in provenance.items())):
        return {}, "'provenance' must map repo-relative paths to plugin names"
    execmap = doc.get("exec")
    if "exec" in doc and (
            not isinstance(execmap, dict)
            or not all(isinstance(k, str) and isinstance(v, int)
                       for k, v in execmap.items())):
        return {}, "'exec' must map repo-relative paths to exec masks (int)"
    return doc, None


def lock_provenance(lock: dict) -> dict[str, str]:
    """rel path -> the plugin req that installed it, for prune attribution.

    The 'provenance' map is the authority when present — locks written by
    provenance-aware code deliberately omit adopted-on-match files (a file
    the resolver never wrote is never prune-eligible). Backfill from
    resolved[].files only for locks that predate the field. A path present
    only in the top-level 'files' map (a forged or hand-written entry) has
    NO provenance — it is released from tracking, never pruned."""
    prov = lock.get("provenance")
    if isinstance(prov, dict):
        return dict(prov)
    out: dict[str, str] = {}
    for r in lock.get("resolved", []):
        rf = r.get("files", {}) if isinstance(r, dict) else {}
        who = str(r.get("plugin", "?")) if isinstance(r, dict) else "?"
        keys = rf.keys() if isinstance(rf, dict) else (
            rf if isinstance(rf, list) else ())
        for k in keys:
            if isinstance(k, str):
                out.setdefault(k, who)
    # Locks written by the shipped resolver before 'provenance' existed hold
    # all ownership in the top-level 'files' map — their resolved[] entries
    # were already serialised without 'files'. Attribute those entries
    # (bounded to resolver-owned roots) so their files stay prune-eligible.
    # The shape is gated on a non-empty resolved[] so a minimal files-only
    # lock (v0/v1 or forged claim) keeps the fail-closed release treatment.
    files_map = lock.get("files")
    resolved_entries = lock.get("resolved")
    if (isinstance(files_map, dict) and isinstance(resolved_entries, list)
            and resolved_entries):
        for k in files_map:
            if (isinstance(k, str)
                    and "/".join(k.split("/")[:2]) in OWNED_ROOTS):
                out.setdefault(k, "unknown")
    return out


def lock_exec_modes(lock: dict) -> dict[str, int]:
    """rel path -> installed exec mask (st_mode & 0o111), for chmod-drift
    attribution: the lock records the mode the resolver materialised, so a
    local chmod (dst mode != record) is distinguishable from a registry
    mode change (dst mode == record != src mode). Legacy bool values are
    kept as-is — they cannot distinguish a partial-mask chmod, so exec
    drift against them conflicts rather than guesses."""
    execmap = lock.get("exec")
    return dict(execmap) if isinstance(execmap, dict) else {}


def _exec_matches(rec, mode: int) -> bool:
    """Any-exec state comparison — git only stores 100644/100755, so the
    umask decides which bits actually land and the exact mask is not a
    portable signal. A nonzero mask records 'has an exec bit', which is
    also all a legacy bool record can express."""
    return bool(mode & 0o111) == bool(rec)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default="ai-manifest.yaml")
    ap.add_argument("--registry", required=True,
                    help="path to a checkout containing registry/ (e.g. an ai-starter-pack clone)")
    ap.add_argument("--repo-root", default=".")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    # Explicit no-mutate spelling for wrappers/CI — dry run is the default
    # (absence of --apply/--check), but callers must not have to rely on an
    # implicit mode.
    mode.add_argument("--dry-run", action="store_true")
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
    install_prov = lock_provenance(lock)
    locked_exec = lock_exec_modes(lock)
    for req in manifest["requires"]:
        plan_requirement(req["plugin"], req["ref"], universe,
                         registry_root, index, repo_root, locked_dig, plan,
                         write_components=claude_selected,
                         locked_exec=locked_exec)

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
        if f not in install_prov:
            # Never resolver-installed: a pre-existing identical file adopted
            # on sight, a forged claim, or a v1-lock entry. It is the
            # consumer's file — never a prune candidate, never an orphan the
            # resolver tracks; the next lock simply stops watching it.
            plan.advisories.append(
                f"{f}: not resolver-installed — releasing lock tracking "
                "(file kept)")
            continue
        digest = locked_dig[f]
        # Digest checks read THROUGH a symlink (that's what the lock recorded),
        # but removals act on the lexical path — unlinking a symlink entry must
        # remove the link, never its target.
        if (args.prune and install_prov[f] == "unknown"
                and os.path.lexists(lexical)):
            # Backfilled from a pre-provenance legacy lock — the top-level
            # files map never recorded installed-vs-adopted, so the entry may
            # be a consumer file the resolver never wrote. Deletion would be
            # irreversible: refuse and let a human remove it. An already-
            # deleted path falls through to plan.removals — clearing its lock
            # entry unlinks nothing.
            plan.conflicts.append((
                lexical,
                "legacy lock entry with unverifiable provenance — may be an "
                "adopted consumer file; refusing to prune (delete it "
                "manually, then re-resolve)",
            ))
            continue
        if args.prune:
            if (os.path.lexists(lexical) and not lexical.is_file()
                    and not lexical.is_symlink()):
                plan.conflicts.append((
                    lexical,
                    "lockfile-tracked path exists as a non-regular file "
                    "(directory/socket/…) — refusing to prune it",
                ))
            elif (lexical.is_file()
                    and (digest is None or sha256(lexical) != digest)):
                plan.conflicts.append((
                    lexical,
                    "prune candidate modified since install — refusing to remove a "
                    "possibly hand-edited file (delete or restore it manually, then re-resolve)",
                ))
            elif (lexical.is_file() and not lexical.is_symlink()
                    and f in locked_exec
                    and not _exec_matches(locked_exec[f],
                                          lexical.stat().st_mode)):
                plan.conflicts.append((
                    lexical,
                    "prune candidate's exec mode differs from the installed-mode "
                    "record — refusing to remove a possibly hand-chmodded file "
                    "(delete or restore it manually, then re-resolve)",
                ))
            else:
                plan.removals.append(lexical)
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
        expected_resolved = [{k: v for k, v in r.items()
                              if k not in ("files", "exec")}
                             for r in plan.resolved]
        expected_written = {dst.relative_to(repo_root).as_posix()
                            for _, dst in plan.writes}
        expected_prov = {}
        for r in plan.resolved:
            for rel in r["files"]:
                if rel in expected_written:
                    expected_prov[rel] = r["plugin"]
                elif rel in install_prov:
                    # Carried record — including 'unknown' backfill, which
                    # must never silently upgrade to a real plugin name.
                    expected_prov[rel] = install_prov[rel]
        for f in plan.removals:
            rel = f.relative_to(repo_root).as_posix()
            if rel in install_prov:
                expected_prov[rel] = install_prov[rel]
        expected_exec = {rel: mode for r in plan.resolved
                         for rel, mode in r["exec"].items()}
        for f in plan.removals:
            rel = f.relative_to(repo_root).as_posix()
            if rel in locked_exec:
                expected_exec[rel] = locked_exec[rel]
        lock_missing = not lock_file.is_file()
        expected_lock = {
            "version": 1,
            "universe": universe,
            "resolved": expected_resolved,
            "files": expected_files,
            "provenance": expected_prov,
            "exec": expected_exec,
        }
        lock_stale = lock != expected_lock
        # Kept orphans are still lockfile-tracked: a modified or deleted one
        # is drift, not a pass — the expected-lock comparison is
        # self-referential, so verify on-disk bytes against the recorded
        # digest. A v1-lock orphan with no recorded digest can only be
        # existence-verified; --apply upgrades it to a full record.
        orphan_drift = []
        for f in plan.removals:
            rel = f.relative_to(repo_root).as_posix()
            digest = locked_dig[rel]
            if not os.path.lexists(f):
                orphan_drift.append(f"{rel} (missing)")
                continue
            if f.is_symlink():
                # The lock records a resolver-written regular file — a link in
                # its place is a type change even when it resolves to identical
                # bytes (the digest check follows links; the exec check skips
                # them).
                orphan_drift.append(f"{rel} (replaced by a symlink)")
                continue
            if digest is not None:
                try:
                    on_disk = sha256(f)
                except OSError:
                    on_disk = None
                if on_disk != digest:
                    orphan_drift.append(f"{rel} (modified)")
            rec = locked_exec.get(rel)
            if (rec is not None and f.is_file() and not f.is_symlink()
                    and not _exec_matches(rec, f.stat().st_mode)):
                orphan_drift.append(f"{rel} (exec mode changed)")
        if drift or orphan_drift or lock_missing or lock_stale:
            for od in orphan_drift:
                print(f"  drift   {od}")
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
            rel_dst = dst.relative_to(repo_root).as_posix()
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Write the bytes the plan checksummed, not a fresh read of src —
            # a registry file swapped between plan and apply would otherwise
            # ship content the lock never digested.
            atomic_replace(dst,
                           lambda f, b=plan.payload[rel_dst]: f.write(b),
                           src=src)
            # And the exec mask the plan recorded — copystat would copy the
            # source's CURRENT mode, which may have drifted since planning;
            # the lock must describe the file that was actually installed.
            planned = plan.planned.get(rel_dst)
            if planned is not None:
                os.chmod(dst, (dst.stat().st_mode & ~0o111) | planned[2])
        if args.prune:
            for f in plan.removals:
                if f.is_file() or f.is_symlink():
                    f.unlink()
                    print(f"  removed {f.relative_to(repo_root).as_posix()}")
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
        written = {dst.relative_to(repo_root).as_posix()
                   for _, dst in plan.writes}
        # Provenance = files this resolver wrote THIS run or carried from an
        # earlier install record. A pre-existing file that merely matched the
        # registry content is adopted for drift-watching only — never
        # provenanced, so --prune can never unlink a file the resolver did
        # not put there.
        new_prov = {}
        for r in plan.resolved:
            for rel in r["files"]:
                if rel in written:
                    new_prov[rel] = r["plugin"]
                elif rel in install_prov:
                    # Carried record — 'unknown' legacy backfill stays
                    # 'unknown' until this resolver actually writes the file.
                    new_prov[rel] = install_prov[rel]
        if not args.prune:
            for f in plan.removals:
                rel = f.relative_to(repo_root).as_posix()
                if rel in install_prov:
                    new_prov[rel] = install_prov[rel]
        new_exec = {rel: mode for r in plan.resolved
                    for rel, mode in r["exec"].items()}
        if not args.prune:
            for f in plan.removals:
                rel = f.relative_to(repo_root).as_posix()
                if rel in locked_exec:
                    new_exec[rel] = locked_exec[rel]
        lock_doc = {
            "version": 1,
            "universe": universe,
            "resolved": [{k: v for k, v in r.items()
                          if k not in ("files", "exec")} for r in plan.resolved],
            "files": new_files,
            "provenance": new_prov,
            "exec": new_exec,
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
