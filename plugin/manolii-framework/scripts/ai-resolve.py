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
                  [--repo-root .] [--apply | --check | --prune]

  Default mode is a dry run — prints the plan, writes nothing.
  --apply   materialise the resolved set
  --check   exit 1 if materialised files differ from the registry source
  --prune   also remove lockfile-tracked files no longer required

Registry source: a local checkout containing a top-level registry/ dir (the
ai-starter-pack repo works). Remote fetch (github:org/repo@ref) lands in P1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
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
LOCK_PATH = ".ai/capability-lock.json"
REQUIRES_RE = re.compile(
    r"^(platform|manolii|buro|impaktful|cpdcheck|repo|personal)/([a-z0-9][a-z0-9-]*)$"
)


@dataclass
class Plan:
    writes: list[tuple[Path, Path]] = field(default_factory=list)   # (src, dst)
    skips: list[tuple[Path, str]] = field(default_factory=list)     # (dst, why)
    conflicts: list[tuple[Path, str]] = field(default_factory=list) # (dst, why)
    removals: list[Path] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    resolved: list[dict] = field(default_factory=list)


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
    """Exact semver, caret range (^x.y -> same major, >=), tag:, or sha: pin."""
    def parse(v: str) -> tuple[int, ...]:
        parts = v.lstrip("v").split(".")
        return tuple(int(p) for p in parts if p.isdigit())
    if ref.startswith("sha:") or ref.startswith("tag:"):
        # Resolved at the fetch layer; the local checkout is the pinned ref.
        return True
    if ref.startswith("^"):
        want = parse(ref[1:])
        have = parse(version)
        return have[:1] == want[:1] and have >= want
    return parse(version) == parse(ref)


def collect_component_files(plugin_dir: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for comp in COMPONENT_TARGETS:
        d = plugin_dir / comp
        if d.is_dir():
            out[comp] = sorted(p for p in d.rglob("*") if p.is_file())
    return out


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_requirement(req: str, ref: str, universe: str, registry_root: Path,
                     index: dict, repo_root: Path, lock: dict, plan: Plan) -> None:
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

    entry = index.get((scope, name))
    if entry is None:
        plan.conflicts.append((repo_root / req, "not in registry/plugins.json"))
        return
    plugin_dir = registry_root.parent / entry["path"] if not Path(entry["path"]).is_absolute() else Path(entry["path"])
    if not plugin_dir.is_dir():
        plan.conflicts.append((repo_root / req, f"missing plugin dir: {entry['path']}"))
        return

    manifest_file = plugin_dir / ".claude-plugin" / "plugin.json"
    version = "0.0.0"
    if manifest_file.is_file():
        try:
            version = json.loads(manifest_file.read_text(encoding="utf-8"))["version"]
        except (json.JSONDecodeError, KeyError):
            pass
    if not version_satisfies(version, ref):
        plan.conflicts.append((
            repo_root / req,
            f"ref '{ref}' not satisfied by registry version {version}",
        ))
        return

    locked_files = set(lock.get("files", [])) if lock else set()
    materialised: list[str] = []
    for comp, files in collect_component_files(plugin_dir).items():
        target_root = repo_root / COMPONENT_TARGETS[comp]
        for src in files:
            rel = src.relative_to(plugin_dir / comp)
            dst = target_root / rel
            if dst.exists():
                if dst.read_bytes() == src.read_bytes():
                    plan.skips.append((dst, "identical"))
                elif dst.as_posix() in locked_files:
                    plan.writes.append((src, dst))  # tracked drift — update
                else:
                    plan.conflicts.append((
                        dst,
                        "exists and differs — not lockfile-tracked, refusing to clobber a hand edit",
                    ))
                materialised.append(dst.as_posix())
            else:
                plan.writes.append((src, dst))
                materialised.append(dst.as_posix())

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
        "files": sorted(materialised),
        "sha256": sha256(manifest_file) if manifest_file.is_file() else None,
    })


def load_lock(repo_root: Path) -> dict:
    p = repo_root / LOCK_PATH
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default="ai-manifest.yaml")
    ap.add_argument("--registry", required=True,
                    help="path to a checkout containing registry/ (e.g. an ai-starter-pack clone)")
    ap.add_argument("--repo-root", default=".")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--prune", action="store_true")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path
    manifest = load_manifest(manifest_path)
    universe = str(manifest["universe"])
    registry_root = find_registry_root(Path(args.registry).resolve())
    index = load_plugins_index(registry_root)
    lock = load_lock(repo_root)

    plan = Plan()
    for req in manifest["requires"]:
        plan_requirement(req["plugin"], str(req["ref"]), universe,
                         registry_root, index, repo_root, lock, plan)

    # Orphan detection: lockfile files no longer required.
    current = set()
    for r in plan.resolved:
        current.update(r["files"])
    for f in locked_files_all(lock):
        if f not in current:
            plan.removals.append(Path(f))

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
        if drift or plan.removals:
            print("\nDRIFT: materialised state differs from registry source")
            return 1
        print("\nOK: materialised state matches registry source")
        return 0

    if args.apply or args.prune:
        for src, dst in plan.writes:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        if args.prune:
            for f in plan.removals:
                if f.is_file():
                    f.unlink()
        lock_doc = {
            "version": 1,
            "universe": universe,
            "resolved": [{k: v for k, v in r.items() if k != "files"} for r in plan.resolved],
            "files": sorted(current),
        }
        lock_file = repo_root / LOCK_PATH
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(json.dumps(lock_doc, indent=2) + "\n", encoding="utf-8")
        print(f"\napplied: {len(plan.writes)} write(s), lock -> {LOCK_PATH}")
        return 0

    print(f"\ndry-run: {len(plan.writes)} write(s), {len(plan.skips)} identical, "
          f"{len(plan.removals)} orphaned — pass --apply to materialise")
    return 0


def locked_files_all(lock: dict) -> list[str]:
    if not lock:
        return []
    files = set(lock.get("files", []))
    for r in lock.get("resolved", []):
        files.update(r.get("files", []))
    return sorted(files)


if __name__ == "__main__":
    sys.exit(main())
