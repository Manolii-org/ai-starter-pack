#!/usr/bin/env python3
"""Install/update the bounded validation capability from a reviewed pack checkout.

This reads the local branch to reject protected branches, performs no Git
mutations or network operations, and never updates settings or secrets.
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
        for path, text in module.outputs(root, (SOURCE / "docs/agent-validation.md").read_text(encoding="utf-8")).items():
            outputs[path] = text.encode()
        stale = [p for p, data in outputs.items() if not p.is_file() or p.read_bytes() != data]
        if args.check or args.dry_run:
            for path in stale:
                print(f"{'STALE' if args.check else 'UPDATE'} {path.relative_to(root)}")
            return int(args.check and bool(stale))
        runner_spec = importlib.util.spec_from_file_location("agent_check", SOURCE / "scripts/agent-check.py")
        if runner_spec is None or runner_spec.loader is None:
            raise ValueError("cannot load the canonical Git context helper")
        runner = importlib.util.module_from_spec(runner_spec)
        runner_spec.loader.exec_module(runner)
        branch = runner.git(root, "branch", "--show-current").decode("utf-8").strip()
        owner_configs = [config]
        if args.recipes and owned.is_file():
            owner_configs.append(json.loads(owned.read_text(encoding="utf-8")))
        remotes = set(runner.git(root, "remote").decode("utf-8").splitlines())
        def owner_branch_ref(ref):
            resolved = subprocess.run([b"git", b"rev-parse", b"--symbolic-full-name", b"--verify", b"--end-of-options", ref.encode("utf-8")],
                                      cwd=root, env=runner.git_environment(), capture_output=True)
            canonical = resolved.stdout.decode("utf-8").strip()
            if resolved.returncode == 0:
                if canonical.startswith(("refs/heads/", "refs/remotes/")) and "\n" not in canonical:
                    return canonical
                raise ValueError("owner base/protected references must identify branches, not revisions or tags")
            # Initial adoption may name a branch whose ref is not present yet.
            # Accept only literal branch names, never revision/symbolic aliases.
            literal = subprocess.run([b"git", b"check-ref-format", b"--branch", ref.encode("utf-8")], cwd=root,
                                     env=runner.git_environment(), capture_output=True)
            if (ref == "@" or ref.startswith("refs/tags/") or literal.returncode
                    or literal.stdout.decode("utf-8").strip() != ref):
                raise ValueError("owner base/protected references must be literal branch names")
            return ref

        if not branch or branch in protected_branches(owner_configs, remotes, owner_branch_ref):
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
