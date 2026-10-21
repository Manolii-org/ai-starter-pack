#!/usr/bin/env python3
"""Render one CI-economy policy onto native agent instruction surfaces.

Only the named block is managed; local ownership, release rules and settings
are preserved. Run --check in an existing CI job, never a new solo micro-job.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

BEGIN = "<!-- agent-validation:begin -->"
END = "<!-- agent-validation:end -->"


def outputs(root: Path, policy: str | None = None) -> dict[Path, str]:
    policy = (policy if policy is not None else (root / "docs/agent-validation.md").read_text(encoding="utf-8")).strip()
    block = f"{BEGIN}\n{policy}\n{END}"
    claude = Path("CLAUDE.md") if (root / "CLAUDE.md").is_file() else Path(".claude/CLAUDE.md")
    claude_paths = [claude]
    for rel in (Path("CLAUDE.md"), Path(".claude/CLAUDE.md")):
        path = root / rel
        if rel == claude or not path.exists():
            continue
        if path.resolve() != path or path.is_symlink():
            raise ValueError(f"linked instruction surface: {rel}")
        previous = path.read_text(encoding="utf-8")
        if BEGIN in previous or END in previous:
            claude_paths.append(rel)
    paths = [Path("AGENTS.md"), *claude_paths, Path(".cursor/rules/agent-validation.mdc")]
    result = {}
    for rel in paths:
        path = root / rel
        if path.resolve() != path or path.is_symlink():
            raise ValueError(f"linked instruction surface: {rel}")
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        if old.count(BEGIN) != old.count(END) or old.count(BEGIN) > 1:
            raise ValueError(f"incomplete/duplicate managed block: {rel}")
        if BEGIN in old:
            start, stop = old.index(BEGIN), old.index(END) + len(END)
            if stop < start:
                raise ValueError(f"reversed managed block: {rel}")
            new = old[:start] + block + old[stop:]
        else:
            # Put required guidance first, before large local instructions can
            # consume a host's instruction budget. Preserve YAML frontmatter.
            if rel.suffix == ".mdc" and not old:
                old = "---\ndescription: Shared local validation and economical CI\nalwaysApply: true\n---\n"
            if old.startswith("---\n"):
                boundary = old.find("\n---\n", 4)
                if boundary < 0:
                    raise ValueError(f"unterminated frontmatter: {rel}")
                boundary += 5
                new = old[:boundary] + "\n" + block + "\n\n" + old[boundary:]
            else:
                new = block + "\n\n" + old
        result[path] = new.rstrip() + "\n"
    ignore = root / ".gitignore"
    if ignore.is_symlink() or ignore.resolve() != ignore:
        raise ValueError("linked .gitignore")
    text = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    if ".ai/agent-checks/" not in text.splitlines():
        result[ignore] = text.rstrip() + "\n\n# Local agent validation receipts and dependency state\n.ai/agent-checks/\n"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        planned = outputs(root)  # validate the entire batch before writes
        stale = [p for p, data in planned.items() if not p.is_file() or p.read_text(encoding="utf-8") != data]
        for path in stale:
            print(f"{'STALE' if args.check else 'UPDATE'} {path.relative_to(root)}")
        if args.check or args.dry_run:
            return int(args.check and bool(stale))
        for path in stale:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(planned[path], encoding="utf-8")
        return 0
    except (OSError, ValueError) as exc:
        print(f"AGENT-VALIDATION-BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
