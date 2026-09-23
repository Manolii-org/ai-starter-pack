#!/usr/bin/env python3
"""Fail closed when pack Autofix+relay is paired with master auto-address-review.

Dual-ownership XOR (pack >= v1.9.2 effectiveness contract, enforced from v1.9.3):

  Pack shape:  pr-autofix-loop(-reusable) ACCEPTS github-actions[bot] comments
               whose body starts with '**[@' (from bot-review-relay.yml).
  Master shape: pr-autofix-loop SKIPS those comments to auto-address-review.yml.

Installing BOTH pack Autofix (relay-accept) AND auto-address-review.yml in the
same repository creates a double-fixer race on every relayed bot review.

Usage:
  python3 scripts/lint-autofix-xor.py [REPO_ROOT]
  Exit 0 = OK / N/A; exit 1 = XOR violation.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

AUTO_ADDRESS = "auto-address-review.yml"
RELAY = "bot-review-relay.yml"
AUTOFIX_NAMES = ("pr-autofix-loop.yml", "pr-autofix-loop-reusable.yml")
RELAY_ACCEPT = re.compile(
    r"github\.actor\s*==\s*'github-actions\[bot\]'[\s\S]{0,200}"
    r"startsWith\(\s*github\.event\.comment\.body\s*,\s*'\*\*\[@'",
    re.MULTILINE,
)


def _workflow_dir(root: Path) -> Path:
    return root / ".github" / "workflows"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return ""


def pack_accepts_relay(workflows: Path) -> bool:
    """True if any Autofix workflow accepts relayed **[@ comments."""
    for name in AUTOFIX_NAMES:
        path = workflows / name
        if not path.is_file():
            continue
        text = _read(path)
        if RELAY_ACCEPT.search(text):
            return True
        # Thin callers that only `uses:` the pack reusable still own the XOR
        # when paired with a local bot-review-relay.yml.
        if "pr-autofix-loop-reusable.yml" in text and (workflows / RELAY).is_file():
            return True
    return False


def has_auto_address(workflows: Path) -> bool:
    return (workflows / AUTO_ADDRESS).is_file()


def has_literal_redacted(workflows: Path) -> list[str]:
    """Return workflow paths that contain the literal string [REDACTED]."""
    bad: list[str] = []
    if not workflows.is_dir():
        return bad
    for path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        if "[REDACTED]" in _read(path):
            bad.append(str(path))
    return bad


def check(root: Path) -> int:
    workflows = _workflow_dir(root)
    violations: list[str] = []

    if pack_accepts_relay(workflows) and has_auto_address(workflows):
        violations.append(
            "XOR violation: pack Autofix relay-accept (+ bot-review-relay) must not "
            f"coexist with {AUTO_ADDRESS}. Remove one ownership path."
        )

    for path in has_literal_redacted(workflows):
        violations.append(
            f"Literal [REDACTED] in {path} — recover real secret/var identifiers "
            "from live main via API; never commit redaction placeholders."
        )

    if violations:
        for v in violations:
            print(f"ERROR: {v}", file=sys.stderr)
        return 1

    # Informative OK line for CI logs
    shape = []
    if pack_accepts_relay(workflows):
        shape.append("pack-autofix-relay-accept")
    if has_auto_address(workflows):
        shape.append("master-auto-address")
    if not shape:
        shape.append("neither")
    print(f"OK: autofix ownership={'+'.join(shape)} root={root}")
    return 0


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2
    return check(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
