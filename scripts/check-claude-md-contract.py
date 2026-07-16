#!/usr/bin/env python3
"""Validate the per-repo CLAUDE.md interface contract (heading anchors).

CLAUDE.md is a wired interface, not free-form prose: agents landing in the
repo rely on specific sections existing (the six-section standalone
contract — what-this-is, stack/commands, critical rules, environments &
data, secrets, ecosystem pointer). This guard fails when a load-bearing
heading disappears, so a CLAUDE.md slim-down or restructure cannot silently
strand agents without environment/secrets context.

The contract lives in config/claude-md-contract.json:

  {
    "required_anchors": {
      "CLAUDE.md": [
        { "anchor": "Environments & Data", "referenced_by": ["..."] }
      ]
    },
    "required_markers": { "CLAUDE.md": ["<!-- CAPABILITY-SYNC-START"] }
  }

Anchor matching is deliberately tolerant: a required anchor passes if ANY
heading in the target file contains it (case-insensitive substring), so
"Stack" matches "Stack & Commands" and "Secrets" matches
"Secrets (Doppler my-project/prd)".

If the contract file is absent the check is a SKIP (exit 0) so the reusable
workflow can roll out fleet-wide before every repo has scaffolded a
contract; pass --require to turn a missing contract into a failure.

Stdlib-only. Exit codes: 0 = ok/skipped, 1 = contract violated,
2 = checker misconfigured (contract present but a target file is missing,
or contract JSON is invalid).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CONTRACT_REL = "config/claude-md-contract.json"
HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$")


def repo_root() -> Path:
    """Return the git repository root, or fall back to script parent dir."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if out:
            return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"[claude-md-contract] WARN: git repo-root detection failed: {exc}",
              file=sys.stderr)
    # Fallback: scripts/ is one level under the repo root.
    return Path(__file__).resolve().parent.parent


def _normalize(text: str) -> str:
    """Lowercase, strip backticks/emphasis, collapse whitespace."""
    text = text.replace("`", "").replace("*", "").replace("_", "")
    return re.sub(r"\s+", " ", text).strip().lower()


def extract_headings(path: Path) -> list[str]:
    """Extract markdown headings, normalized to lowercase.

    Fence-aware: lines inside ``` / ~~~ code blocks are ignored, so a heading
    shown in an example snippet cannot satisfy (or shadow) a real section.
    """
    headings: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            fence_marker = stripped[:3]
            continue
        m = HEADING_RE.match(line)
        if m:
            headings.append(_normalize(m.group(1)))
    return headings


def _anchor_present(anchor: str, headings: list[str]) -> bool:
    """Check if anchor substring (normalized) appears in any heading."""
    a = _normalize(anchor)
    return any(a in h for h in headings)


def check_contract(root: Path, contract_path: Path) -> list[str]:
    """Return a list of error strings. Raises FileNotFoundError/ValueError on misconfig."""
    errors: list[str] = []

    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"contract JSON invalid: {contract_path}: {exc}") from exc

    # 1. Required anchors must exist as headings in their target file.
    for rel, anchors in contract.get("required_anchors", {}).items():
        target = root / rel
        if not target.is_file():
            raise FileNotFoundError(f"contract target file missing: {rel}")
        headings = extract_headings(target)
        for entry in anchors:
            anchor = entry["anchor"]
            if not _anchor_present(anchor, headings):
                refs = ", ".join(entry.get("referenced_by", [])) or "(no ref listed)"
                errors.append(
                    f"{rel}: required heading '{anchor}' is MISSING — "
                    f"depended on by {refs}"
                )

    # 2. Required marker tokens must exist (raw substring) in their file.
    for rel, tokens in contract.get("required_markers", {}).items():
        target = root / rel
        if not target.is_file():
            raise FileNotFoundError(f"contract target file missing: {rel}")
        blob = target.read_text(encoding="utf-8")
        for token in tokens:
            if token not in blob:
                errors.append(
                    f"{rel}: required marker '{token}' is MISSING — "
                    "cross-repo sync tooling keys on it"
                )

    return errors


def main(argv=None) -> int:
    """Parse args, check contract against target files, emit errors."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--contract", default=None,
        help="Path to contract JSON (default: <repo>/" + CONTRACT_REL + ")",
    )
    ap.add_argument(
        "--require", action="store_true",
        help="Fail (exit 2) if the contract file is absent instead of skipping.",
    )
    ap.add_argument("--quiet", action="store_true", help="Suppress OK/skip output")
    args = ap.parse_args(argv)

    root = repo_root()
    contract_path = Path(args.contract) if args.contract else root / CONTRACT_REL

    if not contract_path.is_file():
        if args.require:
            print(f"[claude-md-contract] MISCONFIG: contract not found: {contract_path}",
                  file=sys.stderr)
            return 2
        if not args.quiet:
            print(f"[claude-md-contract] SKIP — no {CONTRACT_REL} in this repo.")
        return 0

    try:
        errors = check_contract(root, contract_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"[claude-md-contract] MISCONFIG: {e}", file=sys.stderr)
        return 2

    if errors:
        for e in errors:
            print(f"[claude-md-contract] ERROR: {e}", file=sys.stderr)
        print(
            f"[claude-md-contract] {len(errors)} contract violation(s). "
            f"Restore the heading/marker, or update {CONTRACT_REL} in the same commit.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print("[claude-md-contract] OK — all required anchors and markers present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
