#!/usr/bin/env python3
"""Install/update the bounded validation capability from a reviewed pack checkout.

This performs no Git/network operations and never updates settings or secrets.
Repository owners supply recipes once; later updates preserve those recipes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
FILES = (
    "scripts/agent-check.py", "scripts/sync-agent-validation.py",
    "scripts/install-agent-validation.py", "scripts/tests/test_agent_check.py",
    "docs/agent-validation.md", ".claude/skills/agent-validation/SKILL.md",
)


def safe(root: Path, rel: str) -> Path:
    path = root / rel
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError(f"linked destination: {rel}")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--recipes", type=Path, help="explicit owner-reviewed recipes for initial adoption or update")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        owned = safe(root, "config/agent-checks.json")
        recipe = args.recipes.read_bytes() if args.recipes else owned.read_bytes()
        config = json.loads(recipe)
        if config.get("schema_version") != 1 or not config.get("base") or "checks" not in config:
            raise ValueError("owner recipes require schema_version=1, base and checks")
        def source_file(rel):
            # Plugin/registry layouts use skills/, Copier checkouts .claude/skills/.
            direct = SOURCE / rel
            return direct if direct.is_file() else SOURCE / rel.removeprefix(".claude/")

        outputs = {safe(root, rel): source_file(rel).read_bytes() for rel in FILES}
        if args.recipes:
            outputs[owned] = recipe
        provenance = {"schema": "agent-validation-source/v1", "source": "ai-starter-pack",
                      "files": {str(path.relative_to(root)): hashlib.sha256(data).hexdigest()
                                for path, data in outputs.items() if path != owned},
                      "recipes": "repository-owned"}
        outputs[safe(root, ".ai/agent-validation-source.json")] = (json.dumps(provenance, indent=2) + "\n").encode()
        spec = importlib.util.spec_from_file_location("sync_agent_validation", SOURCE / "scripts/sync-agent-validation.py")
        if spec is None or spec.loader is None:
            raise ValueError("cannot load the canonical instruction generator")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Preflight ALL managed instruction surfaces before the first write.
        for path, text in module.outputs(root, (SOURCE / "docs/agent-validation.md").read_text()).items():
            outputs[path] = text.encode()
        stale = [p for p, data in outputs.items() if not p.is_file() or p.read_bytes() != data]
        if args.check or args.dry_run:
            for path in stale:
                print(f"{'STALE' if args.check else 'UPDATE'} {path.relative_to(root)}")
            return int(args.check and bool(stale))
        branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=root, text=True).strip()
        if not branch or branch in {"main", "master", "staging", "prod", "develop", "work"}:
            raise ValueError("install on an isolated task branch")
        # Validate every destination before writing; keep other local surfaces
        # untouched. Callers must keep these bounded paths quiescent.
        for path in stale:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(outputs[path])
            if path.parent.name == "scripts" and path.suffix == ".py":
                path.chmod(0o755)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"AGENT-VALIDATION-INSTALL-BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
