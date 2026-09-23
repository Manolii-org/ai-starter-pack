#!/usr/bin/env python3
"""CI-side .ai/guards.json enforcement.

Session hooks (scripts/guard_check.py) guard local agent edits; this script
guards the merge decision: diff base..head, match changed paths against each
guard's globs, and fail when a guard without a bypass was touched.

Bypass channels (each yields a guard id):
  - --bypass "id1,id2"          explicit list (caller derives from PR labels)
  - PR label  "guard-ok:<id>"   caller extracts names, passes via --bypass
  - commit trailer "Guarded-Path: <id>"  found in any commit in the range

Guard fields honoured here: id, paths (fnmatch), mode ("block"|"warn",
default "block"), reason. Region-level (json_path) guards are session-hook
granularity and are treated as file-level here.

Policy source: the guards file is read from the BASE ref, not the PR head —
the merge gate must enforce the policy as it stood before the PR, so a PR
cannot weaken or delete the policy it is checked against. Independently of
that, any change to the guards file itself requires an explicit bypass (the
hard-coded 'guards-config' self-protection).

Exit 0 when no blocked guard matches; 1 otherwise; 2 on bad policy file
(fail-closed, matching guard_check.py's unparseable-policy semantics).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    )
    return out.stdout


GUARDS_SELF_GUARD = "guards-config"


def load_guards(base: str, guards_file: str) -> list[dict]:
    """Load the policy from the BASE ref. When no policy existed at base there
    is nothing to enforce — a PR that ADDS the policy must not be evaluated by
    the very policy it introduces (the guards-config self-guard covers that
    change); its guards take effect for subsequent PRs once merged.
    """
    try:
        text = _git("show", f"{base}:{guards_file}")
    except subprocess.CalledProcessError:
        # No policy at base → nothing to enforce. A PR that ADDS the policy
        # must not be checked by the policy it introduces (only the
        # guards-config self-guard gates that change).
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        print(f"::error::{guards_file}@{base} could not be parsed ({exc}); failing closed.", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print(f"::error::{guards_file}: root must be a JSON object; failing closed.", file=sys.stderr)
        sys.exit(2)
    guards = data.get("guards")
    if not isinstance(guards, list):
        print(f"::error::{guards_file}: 'guards' must be a list; failing closed.", file=sys.stderr)
        sys.exit(2)
    for i, g in enumerate(guards):
        if (not isinstance(g, dict)
                or not isinstance(g.get("id"), str)
                or not isinstance(g.get("paths"), list)
                or not all(isinstance(p, str) for p in g["paths"])
                or g.get("mode", "block") not in ("block", "warn")):
            print(f"::error::{guards_file}: guards[{i}] is malformed "
                  "(needs id:str, paths:list[str], mode in block|warn) — failing closed.",
                  file=sys.stderr)
            sys.exit(2)
    return guards


def changed_files(base: str, head: str) -> list[str]:
    """Files changed base..head INCLUDING rename sources — a rename of a
    guarded file out of its guarded path must still match (and renaming
    .ai/guards.json away must still trip the self-guard)."""
    try:
        merge_base = _git("merge-base", base, head).strip()
    except subprocess.CalledProcessError:
        merge_base = base
    # -z: NUL-separated raw paths — without it git C-quotes non-ASCII names
    # ("alembic/\303\251.py"), and the retained quotes never match a guard glob.
    out = _git("diff", "--name-status", "-M", "-z", f"{merge_base}...{head}")
    fields = [f for f in out.split("\x00") if f]
    files: list[str] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        if status.startswith("R") or status.startswith("C"):
            files.extend(fields[i + 1:i + 3])   # source AND destination
            i += 3
        else:
            files.append(fields[i + 1])
            i += 2
    return files


def trailer_bypasses(base: str, head: str) -> set[str]:
    # Only commits UNIQUE TO THE HEAD side may bypass: `base...head` in log
    # includes base-side commits when head diverged before base advanced, which
    # would let a merged base commit's trailer clear an unrelated stale PR.
    try:
        merge_base = _git("merge-base", base, head).strip()
    except subprocess.CalledProcessError:
        merge_base = base
    # git interpret-trailers is the AUTHORITATIVE trailer parser — a
    # `Guarded-Path:` line stranded in prose (no trailer block) yields nothing,
    # so only a real trailing Key: value block authorises a bypass.
    try:
        shas = _git("log", "--format=%H", f"{merge_base}..{head}").split()
    except subprocess.CalledProcessError:
        return set()
    found: set[str] = set()
    for sha in shas:
        try:
            body = _git("show", "-s", "--format=%B", sha)
            out = subprocess.run(["git", "interpret-trailers", "--parse"],
                                 input=body, capture_output=True, text=True)
        except subprocess.CalledProcessError:
            continue
        for line in out.stdout.splitlines():
            if line.lower().startswith("guarded-path:"):
                found.update(g.strip() for g in line.split(":", 1)[1].split(",") if g.strip())
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--guards-file", default=".ai/guards.json")
    ap.add_argument("--bypass", default="", help="comma-separated guard ids")
    args = ap.parse_args()

    files = changed_files(args.base, args.head)
    bypasses = {b.strip() for b in args.bypass.split(",") if b.strip()}
    bypasses |= trailer_bypasses(args.base, args.head)

    if args.guards_file in files and GUARDS_SELF_GUARD not in bypasses:
        print("guarded-paths: BLOCKED — the guards policy itself changed without a bypass")
        print(f"Bypass: PR label 'guard-ok:{GUARDS_SELF_GUARD}' or commit trailer 'Guarded-Path: {GUARDS_SELF_GUARD}'.")
        return 1

    guards = load_guards(args.base, args.guards_file)
    if not guards:
        print("guarded-paths: no guards defined at base — nothing to check.")
        return 0

    blocked: list[tuple[dict, list[str]]] = []
    warned: list[tuple[dict, list[str]]] = []
    for guard in guards:
        gid = guard.get("id", "<unnamed>")
        if gid in bypasses:
            continue
        hits = sorted({f for f in files for p in guard.get("paths", []) if fnmatch.fnmatch(f, p)})
        if not hits:
            continue
        if guard.get("mode", "block") == "warn":
            warned.append((guard, hits))
        else:
            blocked.append((guard, hits))

    for guard, hits in warned:
        print(f"::warning::guard '{guard['id']}' (warn) touched: {', '.join(hits[:10])}")

    if not blocked:
        print(f"guarded-paths: {len(files)} changed files, no blocked guard matched.")
        return 0

    print("guarded-paths: BLOCKED — guarded paths changed without bypass")
    for guard, hits in blocked:
        print(f"  guard '{guard['id']}': {guard.get('reason', 'no reason given')}")
        for h in hits[:10]:
            print(f"    - {h}")
    print("Bypass: apply PR label 'guard-ok:<id>' or commit trailer 'Guarded-Path: <id>'.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
