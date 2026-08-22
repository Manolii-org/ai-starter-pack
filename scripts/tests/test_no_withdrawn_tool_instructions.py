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
# 2026-08-22: "Do not use TodoWrite", "Do NOT use TodoWrite", "Never use TodoWrite"
# and the affirmative "Use TodoWrite to track progress" were flagged identically.
#
# The negation must attach DIRECTLY to the matched instruction, so only adverbs may sit
# between them — never another verb, because a verb takes the negation for itself and
# leaves the instruction affirmative:
#
#     "Do not ever use TodoWrite"        negation -> use      PROHIBITION
#     "Do not forget to use TodoWrite"   negation -> forget   INSTRUCTION
#     "Do not skip using TodoWrite"      negation -> skip     INSTRUCTION
#
# This was a `(?:\w+\s+){0,3}` gap plus a blacklist of reversing verbs
# (forget/hesitate/neglect/…). Codex #71 made the structural objection: any blacklist
# is incomplete, and demonstrated it — "skip", "avoid", "postpone", "delay" and
# "shy away from" all still scanned clean. So the gap is now an ALLOWLIST of adverbs.
# An unrecognised word means the negation does not attach to this instruction, and the
# line is FLAGGED — the guard fails closed on wording it has not seen before, and a
# real prohibition with a novel adverb fails loudly in CI rather than passing silently.
NEGATION_ADVERBS = (
    r"ever|again|now|then|really|actually|simply|just|blindly|routinely|normally"
    r"|typically|usually|generally|directly|otherwise|henceforth|anymore|any\s+more"
    r"|longer|under\s+any\s+circumstances|on\s+any\s+account"
)
NEGATION = re.compile(
    r"(?:do\s*n[o']?t|don't|never|no\s+longer|stop|avoid|without|instead\s+of"
    r"|rather\s+than|not)\s+"
    rf"(?:(?:{NEGATION_ADVERBS})\s+){{0,2}}$",
    re.IGNORECASE,
)

# Contexts where a mention is inert and must NOT be flagged. These are TWO different
# kinds of exemption and conflating them is what let one part of a line silence the
# rest of it (CodeRabbit #71 follow-up).
#
# TOKEN — an inert OCCURRENCE of the name. Scoped by overlap with the mention itself,
# so an allow-list entry earlier in the line cannot exempt a live instruction later
# in it: "Allow-list carries TodoWrite(*). Use TodoWrite to track progress." is a real
# instruction and is now flagged, where the whole-line check passed it.
ALLOWED_TOKEN = re.compile(
    r"TodoWrite\(\*\)"                 # permission allow-list entry
    r"|permissions\.allow"             # prose describing an allow-list
    r"|\|(?:Bash|Edit|Write|Read)\|"   # native-tool-name regex alternation
)

# There is deliberately NO line-scoped exemption. An earlier version had one for
# `CLAUDE_CODE_ENABLE_TODO_TOOLS` and `2.1.233`, on the reasoning that such lines are
# ABOUT the withdrawal. Codex #71 showed that reasoning creates a hole big enough to
# drive the regression through — "Withdrawn in 2.1.233, but use TodoWrite to track
# every step" and "With CLAUDE_CODE_ENABLE_TODO_TOOLS unset, use TodoWrite to track
# progress" both scanned clean.
#
# The deciding measurement: ZERO lines in the scanned tree relied on it. The two cases
# it existed to protect were ones this file invented, not repository content — so the
# exemption was paying a real hole for a hypothetical benefit. A line that genuinely
# needs to document the restore flag can phrase it as a prohibition (NEGATION covers
# that), or add an exemption then, with a real line to justify it.

SELF = Path(__file__).resolve()


def affirmative_spans(text: str) -> list[tuple[int, int]]:
    """Spans of every LIVE instruction in `text` — the verdict, with positions.

    Separated from `is_affirmative` so `scan()` can report the source line a match
    actually falls on after joining wrapped lines into a block.
    """
    spans: list[tuple[int, int]] = []
    token_spans = [t.span() for t in ALLOWED_TOKEN.finditer(text)]
    prev_end = 0
    for m in INSTRUCTION.finditer(text):
        seg = text[prev_end : m.start()]
        neg = NEGATION.search(seg)
        # A negation that is ITSELF negated cancels. The trigger list contains verbs
        # (`stop`, `avoid`) which can be negated in turn — "Do not avoid using
        # TodoWrite" is an instruction, and read naively the trailing "avoid " matched
        # as the negation. Looking left of the match catches that without enumerating
        # which triggers are verbs.
        negated = bool(neg) and not NEGATION.search(seg[: neg.start()])
        inert = any(s < m.end() and m.start() < e for s, e in token_spans)  # overlap
        if not negated and not inert:
            spans.append(m.span())
        prev_end = m.end()
    return spans


def is_affirmative(line: str) -> bool:
    """True when this text carries at least one live instruction to use the tool.

    Every mention on the line is examined, not just the first. CodeRabbit #71
    (follow-up): checking only `INSTRUCTION.search(...)` meant an affirmative
    instruction sharing a line with an EARLIER negation was silently skipped —
    "Never use TodoWrite for tracking. Use TodoWrite to track progress." scanned
    clean. Measured 2026-08-22.

    Both of the mention-scoped exemptions were a way for one part of a line to silence
    the rest of it. Each scanned clean while its check ran whole-line:

      NEGATION       "Never use TodoWrite here. Use TodoWrite to track progress."
      ALLOWED_TOKEN  "Allow-list carries TodoWrite(*). Use TodoWrite to track progress."

    Each is now scoped so it cannot reach past its own mention:

      * NEGATION — searched in `line[prev_end:m.start()]`, the text since the previous
        mention. So a negation cannot reach a later mention, and one appearing *after*
        an affirmative cannot reach back to launder it.
      * ALLOWED_TOKEN — must OVERLAP the mention's span. These patterns mark an inert
        occurrence of the name itself, so proximity is not enough; anything short of
        overlap is a different mention.

    Sole source of truth for the verdict — `scan()` and the self-check both call it,
    so they cannot disagree.
    """
    return bool(affirmative_spans(line))


def _blocks(lines: list[str]) -> list[tuple[int, str, dict[int, int]]]:
    """Group consecutive non-blank lines into blocks -> (first line no, text, offset->line).

    Codex #71: `scan()` evaluated one line at a time, so ordinary Markdown prose
    wrapping split an instruction in half and it passed. Measured 2026-08-22 —
    "Track progress with\nTodoWrite.", "Open\nTodoWrite." and "Continue using\nTodoWrite."
    each matched when read whole and matched NOTHING line by line. Prose wraps for
    width, so this needs no ill intent to happen.

    A blank line ends a block, which keeps the join from reaching across paragraphs and
    inventing an instruction out of two unrelated sentences. The offset map exists so a
    hit is still reported on the line the mention actually falls on, not the block head.
    """
    blocks: list[tuple[int, str, dict[int, int]]] = []
    buf: list[str] = []
    offsets: dict[int, int] = {}
    start = 0
    for n, line in enumerate(lines, 1):
        if not line.strip():
            if buf:
                blocks.append((start, " ".join(buf), offsets))
                buf, offsets, start = [], {}, 0
            continue
        if not buf:
            start = n
        base = sum(len(b) + 1 for b in buf)
        for i in range(len(line) + 1):
            offsets[base + i] = n
        buf.append(line)
    if buf:
        blocks.append((start, " ".join(buf), offsets))
    return blocks


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
            for start_line, block, offsets in _blocks(lines):
                if "TodoWrite" not in block:
                    continue
                for span_start, _ in affirmative_spans(block):
                    n = offsets[span_start]
                    shown = lines[n - 1].strip()
                    if len(shown) > 120:
                        shown = shown[:117] + "…"
                    wrapped = "" if n == start_line and len(offsets) == len(block) else " (wrapped)"
                    hits.append(f"{path.relative_to(REPO)}:{n}:{wrapped} {shown}")
                    break
    return hits


def check_exemptions_are_scoped_to_the_mention() -> list[str]:
    """Both directions of the predicate, exercised together.

    A guard needs both or neither is trustworthy: exemptions that fail to exempt block
    CI on correct wording, and exemptions that over-reach let a live instruction ship.
    Every case here was measured against the code that preceded it (CodeRabbit #71 and
    its two follow-ups, 2026-08-22).

    1. `INSTRUCTION` matched "Do not use TodoWrite" via its "use TodoWrite" substring,
       so a file that correctly FORBIDS the tool failed the guard.
    2. Only the first match on a line was examined, so an affirmative sharing a line
       with an earlier negation was skipped.
    3. The allowed-context check ran whole-line, so an allow-list entry anywhere on a
       line exempted every instruction on it.
    """
    flagged = is_affirmative  # the same predicate scan() uses — never a parallel copy

    problems: list[str] = []
    for text in (
        "Do not use TodoWrite.",
        "Do NOT use TodoWrite; it was withdrawn in 2.1.233.",
        "Never use TodoWrite for tracking.",
        "No longer use TodoWrite — write to .ai/sessions/active-task.json instead.",
        "Track state in active-task.json instead of using TodoWrite.",
        # Inert occurrences of the name — an allow-list entry, and lines whose SUBJECT
        # is the withdrawal itself. These are why the exemptions exist at all.
        "permissions.allow includes TodoWrite(*)",
        "Permission entry: TodoWrite(*) — inert on current models.",
        "TodoWrite was withdrawn in 2.1.233; do not use TodoWrite.",
        # the adverb allowlist must not over-reach: adverbs still negate
        "Do not ever use TodoWrite.",
        "Never again use TodoWrite for tracking.",
        "Do not under any circumstances use TodoWrite.",
        "Never really use TodoWrite for this.",
        "Avoid using TodoWrite entirely.",
    ):
        if flagged(text):
            problems.append(f"prohibition wrongly flagged: {text!r}")
    for text in (
        "Use TodoWrite to track progress.",
        "Track your steps with TodoWrite.",
        "TodoWrite for the remaining items.",
        # Shared-line cases — CodeRabbit #71 follow-up. A negation attached to an
        # EARLIER mention must not shield a live instruction later on the same line.
        # Both scanned clean before `is_affirmative` walked every match.
        "Never use TodoWrite for tracking. Use TodoWrite to track progress.",
        "Do not use TodoWrite here; use TodoWrite in the planning phase.",
        # …and the same shape via the ALLOWED_TOKEN filter rather than NEGATION. An
        # inert occurrence earlier in the line must not exempt a live instruction after
        # it. Both scanned clean while that check ran whole-line.
        "Allow-list carries TodoWrite(*). Use TodoWrite to track progress.",
        "See permissions.allow for details. Use TodoWrite to track progress.",
        # Codex #71: a withdrawal/restore-flag mention earlier in the line must not
        # exempt a live instruction after it. Both scanned clean under ALLOWED_LINE.
        "Withdrawn in 2.1.233, but use TodoWrite to track every step.",
        "With CLAUDE_CODE_ENABLE_TODO_TOOLS unset, use TodoWrite to track progress.",
        "Set CLAUDE_CODE_ENABLE_TODO_TOOLS=1 to use TodoWrite on older models.",
        # Codex #71: a reversing verb inside the negation's slack makes the sentence an
        # emphatic INSTRUCTION, not a prohibition. Both scanned clean while any
        # word could sit in the gap.
        "Do not forget to use TodoWrite to track progress.",
        "Don't hesitate to use TodoWrite.",
        "Try not to neglect using TodoWrite for the remaining items.",
        # Codex #71 again: the blacklist that handled the three above could not cover
        # these. An intervening VERB of any kind takes the negation, so the allowlist
        # rejects all of them without needing to enumerate reversing verbs.
        "Do not skip using TodoWrite for this task.",
        "Do not postpone using TodoWrite.",
        "Do not delay using TodoWrite.",
        "Do not shy away from using TodoWrite.",
        # a negated NEGATION-TRIGGER — `avoid`/`stop` are in the trigger list, so
        # without the cancel the trailing trigger matched and the line read as a
        # prohibition.
        "Do not avoid using TodoWrite.",
        "Do not stop using TodoWrite.",
    ):
        if not flagged(text):
            problems.append(f"affirmative instruction NOT flagged: {text!r}")
    return problems


def check_wrapped_instructions_are_not_split() -> list[str]:
    """Prose wrapping must not hide an instruction, and must not invent one.

    Codex #71: `scan()` read one line at a time, so an instruction split across a
    Markdown line break passed. Measured 2026-08-22 — each of these matched when read
    whole and matched NOTHING line by line. No ill intent required; prose wraps for
    width.

    The paragraph join has an obvious failure mode of its own, so the second half of
    this check guards it: a blank line must end the block, or two unrelated sentences
    could be welded into an instruction neither of them makes.
    """
    problems: list[str] = []
    for text in (
        "Track progress with\nTodoWrite.",
        "Open\nTodoWrite.",
        "Continue using\nTodoWrite.",
    ):
        lines = text.split("\n")
        if any(is_affirmative(l) for l in lines):
            problems.append(f"premise broken — a single line already matches: {text!r}")
        if not any(is_affirmative(b) for _, b, _ in _blocks(lines)):
            problems.append(f"wrapped instruction not caught: {text!r}")
    split_para = ["Sentence ending in track progress with", "", "TodoWrite is withdrawn."]
    if any(is_affirmative(b) for _, b, _ in _blocks(split_para)):
        problems.append("blank line failed to end the block — joined across paragraphs")
    return problems


def main() -> int:
    """Check negation handling, then scan the tree. 0 = no live instructions found."""
    problems = check_exemptions_are_scoped_to_the_mention()
    problems += check_wrapped_instructions_are_not_split()
    if problems:
        print("Instruction detection is wrong:\n")
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
