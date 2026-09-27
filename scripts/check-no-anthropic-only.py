#!/usr/bin/env python3
"""Anti-regression guard: fail if any current-policy file still references the
retired ``anthropic_only`` data-sensitivity tier as an *active* value.

Historical/tombstone lines (containing "retired", "no longer exists", or
"was retired") are explicitly allowed so that ADRs and changelogs can document
the removal without triggering this guard.

Scanned files (ALLOWLIST):
  CLAUDE.md, AGENTS.md, docs/**/*.md, .ai/pr-standards.yaml,
  schemas/**/*.json, .cursor/rules/*.mdc, .claude/**/*.md

Exit 0 + OK message when clean.
Exit 1 + file:line details for each violation found.

Importable scan function for use in unit tests — see ``scan_paths()``.
"""

import pathlib
import sys

# Repo root = directory of this script's parent
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Glob patterns relative to REPO_ROOT that define the current-policy surface.
# tests/, migrations/, templates/, and session logs are intentionally excluded.
SCAN_GLOBS = [
    "CLAUDE.md",
    "AGENTS.md",
    "docs/**/*.md",
    ".ai/pr-standards.yaml",
    "schemas/**/*.json",
    ".cursor/rules/*.mdc",
    ".claude/**/*.md",
]

# A line is a violation if it matches one of these substrings ...
# Only the underscore form (``anthropic_only``) is used as an actual tier identifier in
# YAML, JSON, and Python. The hyphenated form ("Anthropic-only routing") appears in docs
# as English routing prose referring to Anthropic-direct infrastructure — not the retired
# tier. Scanning only the underscore form avoids false positives on that prose.
FORBIDDEN = ("anthropic_only",)

# ... UNLESS it also contains one of these historical-allowance markers.
HISTORICAL_MARKERS = ("retired", "no longer exists", "was retired")


def scan_paths(root: pathlib.Path, globs: list[str]) -> list[tuple[pathlib.Path, int, str]]:
    """Return a list of (path, lineno, line) tuples for every violation found.

    ``root`` is the repository root; ``globs`` are glob patterns relative to it.
    This function is importable so tests can call it directly.
    """
    violations: list[tuple[pathlib.Path, int, str]] = []

    seen: set[pathlib.Path] = set()
    for pattern in globs:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                line_lower = line.lower()
                if any(f in line_lower for f in FORBIDDEN):
                    if not any(m in line_lower for m in HISTORICAL_MARKERS):
                        violations.append((path, lineno, line))

    return violations


def main() -> int:
    """Run the guard from the repo root. Returns exit code."""
    violations = scan_paths(REPO_ROOT, SCAN_GLOBS)

    if not violations:
        print("OK: no active anthropic_only references")
        return 0

    for path, lineno, line in violations:
        rel = path.relative_to(REPO_ROOT)
        print(f"{rel}:{lineno}: {line.rstrip()}")

    print(
        f"\nFAIL: {len(violations)} active 'anthropic_only' reference(s) found. "
        "Remove them or mark the line with 'retired'/'no longer exists'/'was retired'."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
