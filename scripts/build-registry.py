#!/usr/bin/env python3
"""Regenerate registry/platform/* from the canonical `.claude/` template.

The registry holds one canonical copy of each shareable capability, organised
by scope. Platform scope is GENERATED — never hand-edited — from the same
Copier template that produces `plugin/manolii-*` (ADR-0023 single-source rule),
rendered with install_mode=unbranded so platform assets carry no org identity.

Each emitted plugin gets two manifests rewritten for its registry identity:
  .claude-plugin/plugin.json  — Claude Code manifest; `name` is re-scoped:
                                manolii-framework -> framework, manolii-om -> om
  .devin-plugin/plugin.json   — Devin manifest (git-subdir installable)

Usage:
    python3 scripts/build-registry.py [--install-mode branded|unbranded]
                                    [--check]        # rebuild to temp, diff, no writes
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "registry"

# source build -> (registry scope, registry plugin name)
PLUGIN_MAP = {
    "manolii-framework": ("platform", "framework"),
    "manolii-om": ("platform", "om"),
}

POLICY_SKILLS = ("adaptable-development", "design-feature-adoption", "compatibility-improvement")


def _policy_resolver():
    """Reuse the installed resolver's checked I/O, not a second file writer."""
    name = "policy_refresh_resolver"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "scripts/ai-resolve.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def refresh_policy(target: Path, check: bool = False) -> int:
    """Generate only public policy assets, preserving a mirror's other content.

    These three authored skills are scope-neutral and contain no Jinja. A
    bounded refresh avoids unrelated platform upgrades in private registries.
    """
    from lib.native_guidance import apply_outputs, file_snapshot
    resolver = _policy_resolver()
    if target.resolve() != target:
        raise ValueError("policy registry root must not contain a symlink")
    guard = contextlib.nullcontext() if check else resolver._repo_apply_mutex(target.parent)
    with guard:
        if resolver._read_source(target, "platform/framework/.claude-plugin/plugin.json") is None:
            raise ValueError("policy refresh requires an established framework registry")
        outputs, snapshots = {}, {}
        # Validate and capture ALL six sources and destinations before any
        # materialisation. A late conflict must leave earlier assets untouched.
        for name in POLICY_SKILLS:
            for leaf in ("SKILL.md", "evals/cases.json"):
                rel = f"platform/framework/skills/{name}/{leaf}"
                source = resolver._read_source(REPO, f".claude/skills/{name}/{leaf}")
                if source is None:
                    raise ValueError(f"unsafe or unreadable policy source: {name}/{leaf}")
                outputs[rel] = source[0]
                snapshots[rel] = file_snapshot(target, rel, resolver)
        stale = [rel for rel in outputs if snapshots[rel] is None or snapshots[rel][0] != outputs[rel]]
        if not check:
            apply_outputs(target, outputs, snapshots, resolver)
    print(f"{'STALE' if check and stale else 'OK'} policy assets: {len(stale)} {'differ' if check else 'updated'}")
    return int(check and bool(stale))

DEVIN_DESCRIPTIONS = {
    "framework": "Platform-scope engineering framework — PR review, delivery governance, and verification skills.",
    "om": "Platform-scope Operational Memory skills — propose-only fact capture, readiness, staff answers, handover. Requires a Knowledge Layer MCP backend.",
}


def build_one(source_plugin: str, scope: str, name: str, install_mode: str,
              registry_root: Path) -> Path:
    """Render+assemble one plugin into registry_root/<scope>/<name>/ and patch manifests."""
    out = registry_root / scope / name
    cmd = [
        sys.executable, str(REPO / "scripts" / "build-plugin.py"),
        "--plugin", source_plugin,
        "--out", str(out),
        "--install-mode", install_mode,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=300)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"FAIL: build-plugin --plugin {source_plugin} "
                         "timed out after 300s\n")
        sys.exit(2)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        sys.exit(proc.returncode)

    claude_manifest = out / ".claude-plugin" / "plugin.json"
    manifest = json.loads(claude_manifest.read_text(encoding="utf-8"))
    manifest["name"] = name  # scope-neutral registry identity
    claude_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    devin_dir = out / ".devin-plugin"
    devin_dir.mkdir(exist_ok=True)
    (devin_dir / "plugin.json").write_text(json.dumps({
        "name": name,
        "version": manifest.get("version", "0.0.0"),
        "description": DEVIN_DESCRIPTIONS[name],
    }, indent=2) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--install-mode", default="unbranded",
                    choices=["branded", "unbranded"])
    ap.add_argument("--check", action="store_true",
                    help="rebuild into a temp dir and diff against registry/ "
                         "(freshness gate — no writes)")
    ap.add_argument("--policy-only", action="store_true",
                    help="refresh only the three policy skills in an established registry")
    ap.add_argument("--registry-root", type=Path,
                    help="with --policy-only, an established private mirror's registry directory")
    args = ap.parse_args()
    if args.registry_root and not args.policy_only:
        ap.error("--registry-root requires --policy-only")
    if args.policy_only:
        try:
            status = refresh_policy((args.registry_root or REGISTRY).absolute(), args.check)
        except (OSError, ValueError) as exc:
            ap.error(str(exc))
        sys.exit(status)

    target_root = Path(tempfile.mkdtemp(prefix="registry-build-")) if args.check \
        else REGISTRY
    for source, (scope, name) in PLUGIN_MAP.items():
        out = build_one(source, scope, name, args.install_mode, target_root)
        if not args.check:
            print(f"OK  {scope}/{name} -> {out.relative_to(REPO)}")

    if args.check:
        failed = False
        for _source, (scope, name) in PLUGIN_MAP.items():
            a, b = REGISTRY / scope / name, target_root / scope / name
            try:
                proc = subprocess.run(
                    ["diff", "-r", "--exclude=.git", str(a), str(b)],
                    capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                shutil.rmtree(target_root, ignore_errors=True)
                sys.stderr.write(
                    f"FAIL: freshness diff timed out after 120s for {scope}/{name}\n")
                sys.exit(2)
            if proc.returncode != 0:
                failed = True
                sys.stderr.write(f"STALE {scope}/{name}:\n{proc.stdout[:4000]}\n")
        shutil.rmtree(target_root, ignore_errors=True)
        if failed:
            sys.stderr.write("FAIL: registry/ is stale — run scripts/build-registry.py\n")
            sys.exit(1)
        print("OK  registry/platform/* is fresh")


if __name__ == "__main__":
    main()
