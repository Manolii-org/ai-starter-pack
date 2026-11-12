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
import subprocess
import sys
from pathlib import Path

import yaml

SELF_WORKFLOW_RE = re.compile(r"\$/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")
SHA_REF_RE = re.compile(r"@[0-9a-f]{40}$")
# Mapping keys whose values are data, not step/job definitions: a `uses` key
# nested under these is an input value, never an action reference.
DATA_KEYS = {"with", "env", "inputs", "outputs", "secrets", "vars"}
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
    if ref.startswith("./") or SELF_WORKFLOW_RE.fullmatch(ref) or "${{" in ref:
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


def iter_uses(node: yaml.Node):
    """Yield (line, ref) for every `uses:` key in the composed YAML tree.

    Walking parsed nodes (not text lines) covers every YAML spelling —
    flow mappings (`- {uses: x@v1}`), quoted keys (`"uses": x@v1`) and
    multi-document files — while node marks keep exact line numbers.

    Keys directly under `jobs:` are job IDs, not data keys, so a job named
    `env` or `with` is still checked.
    """
    stack: list[tuple[yaml.Node, str | None]] = [(node, None)]
    while stack:
        cur, parent = stack.pop()
        if isinstance(cur, yaml.MappingNode):
            for key, value in cur.value:
                name = key.value if isinstance(key, yaml.ScalarNode) else None
                if name == "uses" and isinstance(value, yaml.ScalarNode):
                    yield value.start_mark.line + 1, str(value.value).strip()
                elif parent == "jobs" or name not in DATA_KEYS:
                    stack.append((value, name))
        elif isinstance(cur, yaml.SequenceNode):
            stack.extend((item, parent) for item in cur.value)


def local_action_files(path: Path, root: Path) -> list[Path]:
    """Composite-action files referenced from ``path`` as `uses: ./dir`.

    A local action can live anywhere in the repository (`./actions/build`),
    not only under `.github/actions`; follow each reference to its
    `action.yml` so the actions a workflow actually runs are always checked.
    """
    try:
        docs = [d for d in yaml.compose_all(path.read_text(encoding="utf-8", errors="replace"))
                if d is not None]
    except yaml.YAMLError:
        return []
    found = []
    for doc in docs:
        for _, ref in iter_uses(doc):
            if not ref.startswith("./") or ref.endswith((".yml", ".yaml")):
                continue  # remote ref, or a local reusable workflow (already scanned)
            base = (root / ref).resolve()
            for name in ("action.yml", "action.yaml"):
                candidate = base / name
                if candidate.is_file():
                    found.append(candidate)
                    break
    return found


def scan_file(path: Path, allow: list[re.Pattern[str]]) -> list[tuple[int, str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        docs = [d for d in yaml.compose_all(text) if d is not None]
    except yaml.YAMLError as exc:
        line = getattr(getattr(exc, "problem_mark", None), "line", -1) + 1
        return [(line, "<unparseable>", f"YAML parse error: {exc}")]
    violations = []
    for doc in docs:
        for lineno, ref in iter_uses(doc):
            reason = classify(ref, allow)
            if reason:
                violations.append((lineno, ref, reason))
    return sorted(violations)


def actionlint_view(text: str) -> str:
    """Validation-only support for GitHub's documented $/ workflow syntax.

    actionlint 1.7.7 and latest 1.7.12 reject it. Change only the first byte of
    actual job-level uses scalars to the documented equivalent ./ syntax in
    stdin, retaining filenames, columns, duplicates, inputs and diagnostics.
    No source file is rewritten and no scanner finding is ignored.
    """
    edits = set()
    for doc in yaml.compose_all(text):
        if not isinstance(doc,yaml.MappingNode):
            continue
        for key,jobs in doc.value:
            if key.value != 'jobs' or not isinstance(jobs,yaml.MappingNode):
                continue
            for _,job in jobs.value:
                if not isinstance(job,yaml.MappingNode):
                    continue
                for field,value in job.value:
                    if field.value != 'uses' or not isinstance(value,yaml.ScalarNode):
                        continue
                    if not SELF_WORKFLOW_RE.fullmatch(value.value.strip()):
                        continue  # Invalid or other new syntax still fails actionlint.
                    start,end = value.start_mark.index,value.end_mark.index
                    offset = text.find('$/',start,end)
                    if offset < 0:
                        raise ValueError('encoded self-workflow reference is unsupported')
                    edits.add(offset)
    for offset in sorted(edits,reverse=True):
        text = text[:offset]+'.'+text[offset+1:]
    return text


def lint_workflows(root: Path, paths: list[Path]) -> int:
    paths = paths or sorted(p for p in (root/'.github/workflows').glob('*') if p.suffix in {'.yml','.yaml'} and p.is_file())
    normal = []; status = 0
    for path in paths:
        text = path.read_text(encoding='utf-8')
        view = actionlint_view(text) if '$/' in text else text
        if view == text:
            normal.append(str(path))
            continue
        result = subprocess.run(['actionlint','-shellcheck=','-pyflakes=','-stdin-filename',str(path),'-'],
            input=view,text=True,timeout=120)
        status = max(status,result.returncode if result.returncode >= 0 else 2)
    if normal:
        result = subprocess.run(['actionlint','-shellcheck=','-pyflakes=',*normal],timeout=120)
        status = max(status,result.returncode if result.returncode >= 0 else 2)
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", type=Path,
                    help="Files to scan (default: workflows and composite actions under --root)")
    ap.add_argument("--actionlint",action="store_true",help="Lint workflows with narrow self-reference syntax adaptation; never suppress diagnostics")
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--allow-ref", action="append", default=[],
                    help="Full-match regex for a reference to exempt (repeatable)")
    args = ap.parse_args(argv)

    if args.actionlint:
        try:
            return lint_workflows(args.root,args.paths)
        except (ValueError,yaml.YAMLError,OSError,subprocess.TimeoutExpired) as exc:
            print(f'actionlint validation failed: {type(exc).__name__}',file=sys.stderr)
            return 2
    try:
        allow = [re.compile(p) for p in args.allow_ref]
    except re.error as exc:
        print(f"invalid --allow-ref: {exc}", file=sys.stderr)
        return 2

    targets = list(args.paths or default_targets(args.root))
    # Follow local `uses: ./dir` references to their action files, wherever
    # they live, so every composite action a workflow runs is scanned too.
    seen = {p.resolve() for p in targets}
    i = 0
    while i < len(targets):
        for extra in local_action_files(targets[i], args.root):
            if extra.resolve() not in seen:
                seen.add(extra.resolve())
                targets.append(extra)
        i += 1
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
