#!/usr/bin/env python3
"""Guard against instructing the model to use tools the platform has withdrawn.

Claude Code 2.1.233 withdrew the todo/task tools (`TodoWrite`, and the
`TaskCreate/Get/Update/List` family) on Opus 4.8, Sonnet 5, Fable 5, Mythos 5 and
newer models, restorable only via `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`. Verified
absent from a live Opus 5 web session on 2026-08-22.

A stale *instruction* is worse than a stale allow-list entry: an allow rule for a
non-existent tool is inert, but a prompt that says "use TodoWrite" sends the model
at a tool that is not there and costs a wasted turn every invocation. 3 such
instructions were removed from this repo on 2026-08-22 (15 more in manolii-org/master).

Scope, deliberately narrow:
  * FLAGGED — prose telling the model to use the tool ("use TodoWrite",
    "track with TodoWrite", "open TodoWrite items").
  * ALLOWED — permission allow-lists, native-tool-name regexes, and this file.
    Those are inert, and stripping them has a small downside for pack
    consumers still running older models where the tools do exist.

`Task*` is NOT flagged. It is withdrawn by the same changelog entry, yet was
observed PRESENT in that same session with `CLAUDE_CODE_ENABLE_TODO_TOOLS` unset —
the web host appears to enable it out of band. Flagging it would produce false
positives; the standing advice is simply not to build required steps on it.

Run: python3 scripts/tests/test_no_withdrawn_tool_instructions.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# Directories holding model-facing text in the pack, including the generated
# plugin/ tree — a shipped plugin must not carry the stale instruction either.
SCAN_DIRS = ("...claude", "plugin", "docs")
SCAN_SUFFIXES = (".md",)

# Prose that directs the model to use the tool.
INSTRUCTION = re.compile(
    r"(?:use|using|track(?:ed|ing)?\s+(?:with|in|via)|open|via|with)\s+`?TodoWrite`?"
    r"|`?TodoWrite`?\s+(?:for|to\s+track|items)",
    re.IGNORECASE,
)

# Contexts where the bare name is inert and must NOT be flagged.
ALLOWED_CONTEXT = re.compile(
    r"TodoWrite\(\*\)"                 # permission allow-list entry
    r"|permissions\.allow"             # prose describing an allow-list
    r"|\|(?:Bash|Edit|Write|Read)\|"   # native-tool-name regex alternation
    r"|CLAUDE_CODE_ENABLE_TODO_TOOLS"  # discussing the restore flag
    r"|2\.1\.233"                      # citing the changelog entry
)

SELF = Path(__file__).resolve()


def scan() -> list[str]:
    hits: list[str] = []
    for rel in SCAN_DIRS:
        root = REPO / rel.replace("...", ".")
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.resolve() == SELF or path.suffix not in SCAN_SUFFIXES:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for n, line in enumerate(lines, 1):
                if "TodoWrite" not in line:
                    continue
                if ALLOWED_CONTEXT.search(line) or not INSTRUCTION.search(line):
                    continue
                shown = line.strip()
                if len(shown) > 120:
                    shown = shown[:117] + "…"
                hits.append(f"{path.relative_to(REPO)}:{n}: {shown}")
    return hits


def main() -> int:
    hits = scan()
    if hits:
        print("Withdrawn-tool instructions found:\n")
        for h in hits:
            print(f"  ✗ {h}")
        print(
            "\n`TodoWrite` was withdrawn on current models (Claude Code 2.1.233). "
            "Instructing the model to use it wastes a turn every time.\n"
            "Point at a durable mechanism instead — e.g. `.ai/sessions/active-task.json`.\n"
            "Permission allow-lists and tool-name regexes are fine and are not flagged.\n"
        )
        return 1
    print("✔ no withdrawn-tool instructions in model-facing text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
