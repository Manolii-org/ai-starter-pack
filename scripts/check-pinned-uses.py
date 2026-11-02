#!/usr/bin/env python3
"""Fail when a workflow or composite action uses code by a mutable reference.

Every ``uses: owner/repo[/path]@ref`` must pin a full 40-character commit SHA.
Keep a ``# vX.Y.Z`` trailer so Renovate can propose updates. A tag or branch
can be moved by anyone with write access to the source repository, so a tag pin
runs whatever that ref points at *today* — with the caller's secrets.

Exempt: local references (``./...``). ``docker://`` images must pin an
``@sha256:`` digest.

Usage:
    python3 scripts/check-pinned-uses.py                 # scan .github/ in cwd
    python3 scripts/check-pinned-uses.py --root caller   # scan another checkout
    python3 scripts/check-pinned-uses.py --allow-ref 'actions/.*@v4'  # escape hatch

Exit codes: 0 all pinned, 1 violations found, 2 usage error.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

USES_RE = re.compile(r"""^\s*(?:-\s*)?uses:\s*['"]?([^'"\s#]+)""")
SHA_REF_RE = re.compile(r"@[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def default_targets(root: Path) -> list[Path]:
    targets: list[Path] = []
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        targets += sorted(p for p in workflows.iterdir()
                          if p.suffix in {".yml", ".yaml"} and p.is_file())
    actions = root / ".github" / "actions"
    if actions.is_dir():
        targets += sorted(p for p in actions.rglob("action.y*ml") if p.is_file())
    return targets


def classify(ref: str, allow: list[re.Pattern[str]]) -> str | None:
    """Return a violation reason for ``ref``, or None when it is acceptable."""
    if ref.startswith("./") or "${{" in ref:
        return None
    if any(rx.fullmatch(ref) for rx in allow):
        return None
    if ref.startswith("docker://"):
        return None if DIGEST_RE.search(ref) else "docker image not pinned to an @sha256 digest"
    if "@" not in ref:
        return "no ref (resolves to the default branch)"
    if not SHA_REF_RE.search(ref):
        return "mutable ref (pin a 40-character commit SHA with a # vX.Y.Z trailer)"
    return None


def scan_file(path: Path, allow: list[re.Pattern[str]]) -> list[tuple[int, str, str]]:
    violations = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        m = USES_RE.match(line)
        if not m:
            continue
        reason = classify(m.group(1), allow)
        if reason:
            violations.append((lineno, m.group(1), reason))
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", type=Path,
                    help="Files to scan (default: workflows and composite actions under --root)")
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--allow-ref", action="append", default=[],
                    help="Full-match regex for a reference to exempt (repeatable)")
    args = ap.parse_args(argv)

    try:
        allow = [re.compile(p) for p in args.allow_ref]
    except re.error as exc:
        print(f"invalid --allow-ref: {exc}", file=sys.stderr)
        return 2

    targets = args.paths or default_targets(args.root)
    total = 0
    for path in targets:
        for lineno, ref, reason in scan_file(path, allow):
            total += 1
            rel = path.as_posix()
            print(f"::error file={rel},line={lineno}::{ref}: {reason}")
            print(f"{rel}:{lineno}: {ref}: {reason}", file=sys.stderr)
    if total:
        print(f"{total} unpinned reference(s) in {len(targets)} file(s)", file=sys.stderr)
        return 1
    print(f"OK: every uses: reference in {len(targets)} file(s) is SHA-pinned or local")
    return 0


if __name__ == "__main__":
    sys.exit(main())
