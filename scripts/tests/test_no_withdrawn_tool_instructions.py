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

# A PROHIBITION is the opposite of an instruction, and must not be flagged.
# CodeRabbit #71: `INSTRUCTION` matches "Do not use TodoWrite" because it contains
# "use TodoWrite", so text that correctly forbids the withdrawn tool — exactly what a
# migrated instruction file should say — failed the guard and exited 1. Verified
# 2026-08-22: all four of "Do not use TodoWrite", "Do NOT use TodoWrite",
# "Never use TodoWrite" and the affirmative "Use TodoWrite to track progress" were
# flagged identically. Matched against the text immediately BEFORE the mention, so a
# negation elsewhere in a long line cannot launder an affirmative instruction.
NEGATION = re.compile(
    r"(?:do\s*n[o']?t|don't|never|no\s+longer|stop|avoid|without|instead\s+of"
    r"|rather\s+than|not)\s+(?:\w+\s+){0,3}$",
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
    """Walk model-facing text and return every affirmative withdrawn-tool instruction."""
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
                if ALLOWED_CONTEXT.search(line):
                    continue
                m = INSTRUCTION.search(line)
                if not m or NEGATION.search(line[: m.start()]):
                    continue
                shown = line.strip()
                if len(shown) > 120:
                    shown = shown[:117] + "…"
                hits.append(f"{path.relative_to(REPO)}:{n}: {shown}")
    return hits


def check_negated_instructions_are_not_flagged() -> list[str]:
    """A prohibition must pass; an affirmative instruction must still fail.

    CodeRabbit #71: `INSTRUCTION` matched "Do not use TodoWrite" via its "use TodoWrite"
    substring, so a file that correctly forbids the withdrawn tool tripped the guard and
    exited 1 — CI blocked by the very wording the guard wants to see. Measured
    2026-08-22: all four phrasings below matched identically before the NEGATION filter.
    """
    def flagged(line: str) -> bool:
        """True when this line would be reported as a violation."""
        if ALLOWED_CONTEXT.search(line):
            return False
        m = INSTRUCTION.search(line)
        return bool(m) and not NEGATION.search(line[: m.start()])

    problems: list[str] = []
    for text in (
        "Do not use TodoWrite.",
        "Do NOT use TodoWrite; it was withdrawn in 2.1.233.",
        "Never use TodoWrite for tracking.",
        "No longer use TodoWrite — write to .ai/sessions/active-task.json instead.",
        "Track state in active-task.json instead of using TodoWrite.",
    ):
        if flagged(text):
            problems.append(f"prohibition wrongly flagged: {text!r}")
    for text in (
        "Use TodoWrite to track progress.",
        "Track your steps with TodoWrite.",
        "TodoWrite for the remaining items.",
    ):
        if not flagged(text):
            problems.append(f"affirmative instruction NOT flagged: {text!r}")
    return problems


def main() -> int:
    """Check negation handling, then scan the tree. 0 = no live instructions found."""
    problems = check_negated_instructions_are_not_flagged()
    if problems:
        print("Negation handling is wrong:\n")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
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


def test_all_checks_pass() -> None:
    """pytest entry point — the only test this module exposes to collection.

    CI runs `coverage run -m pytest scripts/tests/test_*.py`
    (.github/workflows/quality-base.yml:203). Modules in this directory report through
    `main()`'s exit code rather than assertions, so without a collected test that
    checks it, pytest sees nothing to fail. Measured
    before this fix: `pytest -q test_fork_dispatch.py` reported **7 passed** while
    `python3 test_fork_dispatch.py` exited 1 with 4 real failures, and
    test_no_withdrawn_tool_instructions.py collected **zero** tests. Every guard in
    this directory was therefore inert in CI.

    Fix: the individual checks are named `check_*` (not collected), and this single
    collected test funnels them through `main()` so both invocation modes agree.
    Found by Codex review on PR #4433.
    """
    assert main() == 0, "see captured stdout for the failing checks"


if __name__ == "__main__":
    sys.exit(main())
