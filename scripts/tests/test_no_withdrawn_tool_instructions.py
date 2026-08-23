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

# Root instruction files sit under NO scanned directory, and they are the
# highest-impact model-facing text the pack ships — a consumer's generated
# CLAUDE.md/AGENTS.md is read on every turn. A reintroduction there passed this
# guard silently. (Codex review #71; the identical fix landed in
# manolii-org/master via Codex review #4433 and failed to propagate here — the
# two copies of this file have drifted and should be compared on every change.)
SCAN_ROOT_FILES = ("CLAUDE.md", "AGENTS.md")

# Prose that directs the model to use the tool.
#
# The verb set is an ALLOWLIST and the determiner gap is a closed set, so the
# pattern stays fail-loud on wording it has not seen rather than matching any
# sentence that happens to contain the name. Codex #71 measured the original
# `use|using|open|via|with` set against ordinary directive phrasing: "Invoke
# TodoWrite.", "Call TodoWrite.", "Run TodoWrite at each step.", "Update
# TodoWrite as you go.", "Create a TodoWrite list." and even "Use the TodoWrite
# tool to track progress." ALL scanned clean, because `use` had to sit
# immediately adjacent to the name. Only the bare "Use TodoWrite …" form was
# caught — the one phrasing the migration had already removed.
INSTRUCTION_VERBS = (
    r"use|using|invoke|invoking|call|calling|run|running|update|updating"
    r"|create|creating|add|adding|maintain|maintaining|open|opening"
    r"|keep|keeping|mirror|mirroring"
    # "…recorded in TodoWrite", "…belongs in TodoWrite" — the directive arrives as a
    # preposition rather than a bare verb. Same shape as the existing `track … with`
    # entry, so these are more of the same rule, not new machinery.
    r"|(?:track|record|log|captur)(?:e|ed|ing)?\s+(?:with|in|into|via)"
    r"|belongs?\s+in"
    r"|via|with"
)
# Determiners/adjectives that may sit between the verb and the name. Closed set:
# "call to TodoWrite" (descriptive prose about a removed call) must stay unmatched,
# so `to` is deliberately absent.
#
# KNOWN LIMITATION, stated rather than papered over. A verb separated from the name by
# a FREE NOUN PHRASE is not matched. Measured 2026-08-23, still bypassing:
#
#     "Add the remaining steps to a new TodoWrite entry."
#     "Mirror the plan into TodoWrite."
#     "Log each step in TodoWrite."
#     "Capture the plan in TodoWrite."
#
# Widening the gap to arbitrary words is the fix that looks obvious and is wrong: it is
# the unbounded-gap mistake the NEGATION allowlist exists to undo, and it would make any
# sentence mentioning the name near any verb a hit.
#
# INVERTING THE POLARITY was the other candidate — flag every mention unless it is
# negated or an inert token — and it was measured, not assumed: 0 hits across the real
# tree, so it is *safe today*. It was still rejected, because it flags legitimate
# documentation ABOUT the withdrawal, which `docs/` is entitled to contain and which the
# counter-cases below require to stay clean:
#
#     "TodoWrite was withdrawn in 2.1.233."
#     "Removed the call to TodoWrite from the hook."
#     "The TodoWrite tool no longer exists on Opus 5."
#
# That trades a bypass class for a false-positive class that blocks correct docs in CI —
# and a guard that cries wolf on accurate documentation gets disabled, which is a worse
# end state than a known floor. The trade-off is real and reasonable people could pick
# the other side; whoever revisits it should re-run both measurements rather than
# inherit this call.
#
# So: this guard is a FLOOR on the phrasings that occur in instruction files, not a
# parser. When a new shape appears in the tree, add it here with the line that
# motivated it.
INSTRUCTION_GAP = r"(?:(?:the|a|an|your|its|new|first|initial|running)\s+){0,3}"
INSTRUCTION = re.compile(
    rf"\b(?:{INSTRUCTION_VERBS})\s+{INSTRUCTION_GAP}`?TodoWrite`?"
    # A bare `TodoWrite items` alternative used to sit here and was dropped: it flagged
    # "TodoWrite items are no longer available." — documentation, not an instruction.
    # Measured before removing it: every positive case that reads as `… items` is
    # already caught through the verb path ("Open your TodoWrite items before
    # starting."), so it cost nothing and blocked accurate docs, which is the failure
    # mode this file rejects polarity inversion to avoid.
    r"|(?<!\w)`?TodoWrite`?\s+(?:for|to\s+track)",
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
    r"TodoWrite\(\*\)"  # permission allow-list entry
    # Include the NAME so overlap scoping exempts this occurrence only.
    r"|permissions\.allow\s*(?:(?:includes?|contains?|carries?)\s+|[:=]\s*)`?TodoWrite`?"
    r"|\|(?:Bash|Edit|Write|Read)\|`?TodoWrite`?\|"  # native-tool regex
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


# Markdown structure that must END a block. Joining across these welds two unrelated
# lines into a sentence neither of them makes. CodeRabbit #71 on 3e81ecd, measured:
#
#   ["## Use", "TodoWrite was withdrawn."]  -> "## Use TodoWrite was withdrawn."  FLAGGED
#   ["### Update", "TodoWrite items are …"] -> "### Update TodoWrite items are …" FLAGGED
#
# The second is a false positive THIS PR introduced, by adding `update` to the verb set
# one commit earlier — a heading named "Update" above prose about the withdrawal is
# ordinary Markdown, not a contrived case.
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_FENCE = re.compile(r"^\s*(?:```|~~~)")


def _blocks(lines: list[str]) -> list[tuple[int, str, dict[int, int]]]:
    """Group lines belonging to one prose unit -> (first line no, text, offset->line).

    Codex #71: `scan()` evaluated one line at a time, so ordinary Markdown prose
    wrapping split an instruction in half and it passed. Measured 2026-08-22 —
    "Track progress with\nTodoWrite.", "Open\nTodoWrite." and "Continue using\nTodoWrite."
    each matched when read whole and matched NOTHING line by line. Prose wraps for
    width, so this needs no ill intent to happen.

    A block ends at a blank line OR at Markdown structure — a heading, a list-item
    marker, or a fence delimiter (see `check_markdown_structure_ends_a_block`). Both
    exist for the same reason: joining across either invents an instruction out of two
    lines that never made one. Blank lines were the original terminator; the structural
    ones were added after joining welded "## Use" onto the prose beneath it.

    The offset map exists so a hit is still reported on the line the mention actually
    falls on, not the block head.
    """
    blocks: list[tuple[int, str, dict[int, int]]] = []
    buf: list[str] = []
    offsets: dict[int, int] = {}
    start = 0

    def flush() -> None:
        nonlocal buf, offsets, start
        if buf:
            blocks.append((start, " ".join(buf), offsets))
        buf, offsets, start = [], {}, 0

    for n, line in enumerate(lines, 1):
        if not line.strip():
            flush()
            continue
        if _FENCE.match(line):
            # A fence delimiter is structure, not content: end the block and drop it.
            flush()
            continue
        if _HEADING.match(line):
            # A heading stands ALONE — flushing is not enough, because the prose that
            # follows would then simply join the heading instead of the text above it,
            # which is the exact defect.
            flush()
            blocks.append((n, line, {i: n for i in range(len(line) + 1)}))
            continue
        if _LIST_ITEM.match(line):
            # A new item ends the previous one; its own continuation lines still join,
            # so an instruction wrapped inside one item is still caught.
            flush()
        if not buf:
            start = n
        base = sum(len(b) + 1 for b in buf)
        for i in range(len(line) + 1):
            offsets[base + i] = n
        buf.append(line)
    flush()
    return blocks


def _flag(path: Path, hits: list[str], repo: Path = REPO) -> None:
    """Append every affirmative withdrawn-tool instruction in `path` to `hits`.

    Factored out of `scan()` so root files and scanned directories share ONE code
    path. A second, hand-written loop for the root files would be free to drift out
    of block-awareness — which is precisely how the root-file gap arose in the first
    place.
    """
    if path.resolve() == SELF or path.suffix not in SCAN_SUFFIXES or not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        try:
            shown_path = path.relative_to(repo)
        except ValueError:
            shown_path = path
        hits.append(f"{shown_path}: unreadable: {type(exc).__name__}: {exc}")
        return
    for _start_line, block, offsets in _blocks(lines):
        # Case-INSENSITIVE, matching `INSTRUCTION`'s own re.IGNORECASE. Codex #71 on
        # 3e81ecd: this fast path was an exact-case `"TodoWrite" not in block`, so the
        # pattern's case-insensitivity never reached repository files. Measured —
        # is_affirmative("Use TODOWRITE to track progress.") was True while scan() on a
        # CLAUDE.md containing that exact line returned []. A prefilter stricter than
        # the predicate it guards silently narrows it.
        if "todowrite" not in block.lower():
            continue
        for span_start, span_end in affirmative_spans(block):
            n = offsets[span_start]
            shown = lines[n - 1].strip()
            if len(shown) > 120:
                shown = shown[:117] + "…"
            # `(wrapped)` means THIS MATCH straddles a line break, so the quoted source
            # line shows only part of it. The previous condition compared `len(offsets)`
            # with `len(block)`; those differ by exactly one for every block (the map
            # carries an end-of-line offset per line), so the tag was unconditional and
            # said nothing. Compare the lines the match's own endpoints land on instead.
            wrapped = "" if offsets[span_end - 1] == n else " (wrapped)"
            hits.append(f"{path.relative_to(repo)}:{n}:{wrapped} {shown}")
            break


def scan(repo: Path = REPO) -> list[str]:
    """Walk model-facing text and return every affirmative withdrawn-tool instruction.

    `repo` is a parameter solely so `check_root_instruction_files_are_scanned` can
    point the real walk at a fixture tree. Nothing in the pack passes it.
    """
    hits: list[str] = []
    for name in SCAN_ROOT_FILES:
        candidate = repo / name
        if candidate.is_file():
            _flag(candidate, hits, repo)
    for rel in SCAN_DIRS:
        root = repo / rel.replace("...", ".")
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            _flag(path, hits, repo)
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
        "permissions.allow: TodoWrite. Use TodoWrite to track progress.",
        "permissions.allow: Read. Use TodoWrite to track progress.",
        "Tool regex: |Bash|TodoWrite|. Use TodoWrite to track progress.",
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
        # Codex #71: the directive grammar recognised only `use|using|open|via|with`
        # AND required the verb to be immediately adjacent to the name. Every line
        # below scanned clean — ordinary phrasings of exactly the instruction this
        # guard exists to stop. Measured 2026-08-23.
        "Use the TodoWrite tool to track progress.",
        "Invoke TodoWrite.",
        "Call TodoWrite.",
        "Run TodoWrite at each step.",
        "Update TodoWrite as you go.",
        "Create a TodoWrite list.",
        "Maintain your TodoWrite list as the plan changes.",
        "Add a TodoWrite entry for each step.",
        "Open your TodoWrite items before starting.",
        # CodeRabbit #71 merge-risk on 3e81ecd: "some withdrawn-tool instructions may
        # still bypass the gate". True when measured — these three did. Each is the
        # directive arriving as a preposition or a state verb rather than an
        # imperative, which the verb list did not cover.
        "Keep TodoWrite up to date.",
        "Progress should be recorded in TodoWrite.",
        "Each step belongs in TodoWrite.",
    ):
        if not flagged(text):
            problems.append(f"affirmative instruction NOT flagged: {text!r}")
    # Counter-cases for the WIDER grammar specifically. Broadening a detector is only
    # safe if it is measured in both directions: each of these mentions the name near
    # a verb without directing the model at the tool, and each must stay clean.
    for text in (
        "Do not invoke TodoWrite.",
        "Never call TodoWrite on current models.",
        "No longer create TodoWrite entries — write to active-task.json.",
        "Do not update TodoWrite; it is withdrawn.",
        # the widened verb/preposition set must still lose to a negation
        "Do not keep TodoWrite up to date.",
        "Never record progress in TodoWrite.",
        # descriptive prose about code, not an instruction. `to` is deliberately
        # absent from INSTRUCTION_GAP so these stay unmatched.
        "Removed the call to TodoWrite from the hook.",
        "This migration drops every reference to TodoWrite.",
        "TodoWrite was withdrawn in 2.1.233.",
        "The TodoWrite tool no longer exists on Opus 5.",
        # the dropped bare-`items` alternative — documentation, not a directive
        "TodoWrite items are no longer available.",
        "The TodoWrite items were removed in 2.1.233.",
        "Recall TodoWrite was withdrawn.",
        "Recreate TodoWrite documentation.",
        "Reuse TodoWrite documentation.",
        "permissions.allow: TodoWrite for legacy clients.",
        "permissions.allow includes TodoWrite for legacy clients.",
        "Tool regex: |Bash|TodoWrite| for legacy clients.",
    ):
        if flagged(text):
            problems.append(f"inert mention wrongly flagged by widened grammar: {text!r}")
    return problems


def check_markdown_structure_ends_a_block() -> list[str]:
    """Headings, list items and fences must terminate a block, not join across it.

    CodeRabbit #71 on 3e81ecd. `_blocks()` joined every non-blank line, so ordinary
    Markdown welded two unrelated lines into a sentence neither made. Measured before
    the fix — both flagged:

        ["## Use", "TodoWrite was withdrawn."]
        ["### Update", "TodoWrite items are no longer available."]

    The second was a false positive introduced by THIS PR, one commit earlier, when
    `update` joined the verb set. A heading called "Update" above prose about the
    withdrawal is ordinary documentation.

    Both directions, as ever: the join must stop at structure, and must still happen
    inside a paragraph or a single list item, or the wrapped-instruction fix regresses.
    """
    problems: list[str] = []
    for lines in (
        ["## Use", "TodoWrite was withdrawn."],
        ["### Update", "TodoWrite items are no longer available."],
        ["- Keep", "- TodoWrite is gone."],
        ["1. Run", "2. TodoWrite is unavailable."],
        ["Run", "```", "TodoWrite", "```"],
    ):
        if any(is_affirmative(b) for _s, b, _o in _blocks(lines)):
            problems.append(f"joined across Markdown structure: {lines!r}")
    for lines in (
        ["Track progress with", "TodoWrite."],
        ["- Track progress with", "  TodoWrite."],
    ):
        if not any(is_affirmative(b) for _s, b, _o in _blocks(lines)):
            problems.append(f"wrapped instruction no longer caught: {lines!r}")
    return problems


def check_the_scan_prefilter_is_case_insensitive() -> list[str]:
    """`scan()`'s fast path must not be stricter than the predicate it guards.

    Codex #71 on 3e81ecd: the prefilter was an exact-case `"TodoWrite" not in block`
    while `INSTRUCTION` carries `re.IGNORECASE`, so the pattern's case-insensitivity
    never reached repository files. Measured — `is_affirmative` said True and `scan()`
    said nothing:

        is_affirmative("Use TODOWRITE to track progress.")  -> True
        scan(<CLAUDE.md containing that line>)              -> []

    Driven through the real `scan()`, because the defect lived in the walk rather than
    in the predicate; asserting on `is_affirmative` alone would have missed it entirely.
    """
    import tempfile

    problems: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for variant in ("TODOWRITE", "todowrite", "TodoWrite"):
            (root / "CLAUDE.md").write_text(
                f"Use {variant} to track progress.\n", encoding="utf-8"
            )
            if not scan(root):
                problems.append(f"scan() missed a cased variant: {variant!r}")
    return problems


def check_root_instruction_files_are_scanned() -> list[str]:
    """`CLAUDE.md` / `AGENTS.md` sit under no scanned directory and were skipped.

    Codex #71: the pack shipped both as agent-facing templates while `scan()` walked
    only `.claude/`, `plugin/` and `docs/`, so "Use TodoWrite to track progress." in a
    generated CLAUDE.md passed CI with the same runtime effect as one under `.claude/`.
    Verified 2026-08-23: this check fails against the previous `scan()`.

    Driven through the real `scan()` against a fixture tree rather than asserting on
    `SCAN_ROOT_FILES`'s contents — a constant can be present while nothing reads it,
    which is the shape of the bug being fixed.
    """
    import tempfile

    problems: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name in SCAN_ROOT_FILES:
            (root / name).write_text("Use TodoWrite to track progress.\n", encoding="utf-8")
        hits = scan(root)
        for name in SCAN_ROOT_FILES:
            if not any(h.startswith(f"{name}:") for h in hits):
                problems.append(f"root instruction file not scanned: {name}")
        # …and the fixture must not be flagging on a technicality: a clean root file
        # produces nothing.
        for name in SCAN_ROOT_FILES:
            (root / name).write_text("Do not use TodoWrite.\n", encoding="utf-8")
        if scan(root):
            problems.append("clean root instruction files were flagged")
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


def check_workflow_triggers_cover_every_scanned_path() -> list[str]:
    """Every path this guard READS must also TRIGGER it, or it is not a gate.

    Codex #71, immediately after the root-file fix landed: `scan()` was extended to
    `CLAUDE.md` / `AGENTS.md`, and the workflow's `paths:` filters were not. Verified
    2026-08-23 — neither filter listed `CLAUDE.md`, `AGENTS.md` or `docs/**`, so a PR
    touching only those files never ran this check and a withdrawn-tool directive in
    any of them could merge with the gate simply absent (`docs/**` had been in that
    state since before this PR; the root-file fix widened an existing gap).

    That is a nastier failure than a scanner bug: the guard reports success by never
    running. Nothing in CI distinguishes "passed" from "not triggered".

    Both filters are checked. `pull_request` alone would leave the push-to-branch
    route uncovered, and vice versa.
    """
    problems: list[str] = []
    wf = REPO / ".github/workflows/plugin-eval-gate.yml"
    if not wf.is_file():
        return [f"workflow not found: {wf.relative_to(REPO)}"]
    try:
        import yaml
    except ImportError:  # pragma: no cover - pyyaml is a CI build dep
        return ["pyyaml unavailable — cannot verify trigger coverage"]
    # `on:` is the YAML 1.1 boolean True, not the string "on".
    triggers = yaml.safe_load(wf.read_text(encoding="utf-8")).get(True) or {}

    required = {rel.replace("...", ".").rstrip("/") + "/**" for rel in SCAN_DIRS}
    required |= set(SCAN_ROOT_FILES)
    for event in ("push", "pull_request"):
        listed = set((triggers.get(event) or {}).get("paths") or [])
        if listed & {"**", "**/*"}:  # universal path filters cover every scanned input
            continue
        for pattern in sorted(required - listed):
            problems.append(f"{event}.paths does not cover scanned path {pattern!r}")
    return problems


def check_unreadable_files_fail_closed() -> list[str]:
    """A declared scan target must not disappear behind a read error."""
    from unittest import mock

    import tempfile

    failures = (
        OSError("permission denied"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    )
    problems: list[str] = []
    with tempfile.TemporaryDirectory(dir=REPO / "docs") as td:
        directory = Path(td) / "not-a-file.md"
        directory.mkdir()
        directory_hits: list[str] = []
        _flag(directory, directory_hits)
        if directory_hits:
            problems.append("directory with a Markdown suffix was treated as an unreadable file")

        candidate = Path(td) / "unreadable-fixture.md"
        candidate.write_text("fixture exists before the simulated read failure\n")
        for failure in failures:
            hits: list[str] = []
            original_read_text = Path.read_text

            def fail_candidate(path: Path, *args: object, **kwargs: object) -> str:
                if path == candidate:
                    raise failure
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(
                Path, "read_text", autospec=True, side_effect=fail_candidate
            ) as read_text:
                _flag(candidate, hits)
            if not read_text.called:
                problems.append(f"{type(failure).__name__} fixture did not reach file read")
            elif not any(f"unreadable: {type(failure).__name__}:" in hit for hit in hits):
                problems.append(f"{type(failure).__name__} was silently skipped")
    return problems


def main() -> int:
    """Check negation handling, then scan the tree. 0 = no live instructions found."""
    problems = check_exemptions_are_scoped_to_the_mention()
    problems += check_wrapped_instructions_are_not_split()
    problems += check_root_instruction_files_are_scanned()
    problems += check_workflow_triggers_cover_every_scanned_path()
    problems += check_markdown_structure_ends_a_block()
    problems += check_the_scan_prefilter_is_case_insensitive()
    problems += check_unreadable_files_fail_closed()
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
