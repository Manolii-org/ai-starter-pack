"""Canonical failure_class taxonomy for retrospective / issue records (WS1).

Single source of truth — import or read FAILURE_CLASSES elsewhere; do not redefine.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

# Fixed enum (AgentDebug-style). Additive field on capture records; default unclassified.
FAILURE_CLASSES: tuple[str, ...] = (
    "instruction-gap",
    "tooling",
    "environment",
    "planning",
    "memory-context",
    "external-dependency",
    "unclassified",
)

FAILURE_CLASS_SET = frozenset(FAILURE_CLASSES)
DEFAULT_FAILURE_CLASS = "unclassified"

_INSTRUCTION_RX = re.compile(
    r"\b(should have|you forgot|as instructed|per (the )?rules?|claude\.md|"
    r"persistent.?instructions|don'?t do|never |always )\b",
    re.I,
)
_TOOLING_RX = re.compile(
    r"\b(tool (call|retry|error)|mcp|hook|command failed|pretooluse|posttooluse)\b",
    re.I,
)
_ENV_RX = re.compile(
    r"\b(401|403|unauthori[sz]ed|forbidden|credential|token|env(ironment)?|"
    r"doppler|missing key|not set|permission denied)\b",
    re.I,
)
_PLANNING_RX = re.compile(
    r"\b(wrong (approach|direction)|edit churn|re-?plan|should have (planned|checked))\b",
    re.I,
)
_MEMORY_RX = re.compile(
    r"\b(re-?read|context (lost|reset)|after compact|forgot|memory|session.?context)\b",
    re.I,
)
_EXTERNAL_RX = re.compile(
    r"\b(timeout|timed out|5\d\d|network|unreachable|rate.?limit|vercel|fly\.io|"
    r"supabase|external.?api|dependency)\b",
    re.I,
)


def normalize_failure_class(value: Optional[str]) -> str:
    """Return a valid enum member; unknown/empty → unclassified."""
    if not value:
        return DEFAULT_FAILURE_CLASS
    v = str(value).strip().lower()
    return v if v in FAILURE_CLASS_SET else DEFAULT_FAILURE_CLASS


def classify_from_text(text: str) -> str:
    """Deterministic keyword classify; first match wins; else unclassified."""
    if not text:
        return DEFAULT_FAILURE_CLASS
    if _INSTRUCTION_RX.search(text):
        return "instruction-gap"
    if _ENV_RX.search(text):
        return "environment"
    if _EXTERNAL_RX.search(text):
        return "external-dependency"
    if _TOOLING_RX.search(text):
        return "tooling"
    if _MEMORY_RX.search(text):
        return "memory-context"
    if _PLANNING_RX.search(text):
        return "planning"
    return DEFAULT_FAILURE_CLASS


def classify_from_signals(
    *,
    user_corrections: Optional[Iterable[str]] = None,
    ai_confusion_events: Optional[Iterable[str]] = None,
    tool_retries: Optional[Mapping[str, int]] = None,
    edit_churn: Optional[Mapping[str, int]] = None,
    file_reads: Optional[Mapping[str, int]] = None,
    error_count: int = 0,
    kinds: Optional[Mapping[str, int]] = None,
    samples: Optional[Iterable[str]] = None,
) -> str:
    """Derive failure_class from retrospective / issue-log signals.

    Priority (deterministic):
      instruction-gap ← user corrections matching instruction patterns
      tooling ← heavy tool retries (≥3) or kinds containing tool
      environment ← auth/credential kinds or env-pattern corrections
      external-dependency ← timeout kinds / external patterns
      planning ← heavy edit churn (≥3 files or any file ≥3 edits)
      memory-context ← re-reads without edit
      unclassified ← default
    """
    corrections = list(user_corrections or [])
    confusion = list(ai_confusion_events or [])
    retries = dict(tool_retries or {})
    churn = dict(edit_churn or {})
    reads = dict(file_reads or {})
    kind_map = dict(kinds or {})
    sample_list = list(samples or [])

    joined_corrections = "\n".join(corrections)
    if corrections and _INSTRUCTION_RX.search(joined_corrections):
        return "instruction-gap"

    if any(v >= 3 for v in retries.values()) or kind_map.get("tool", 0) > 0:
        return "tooling"

    if kind_map.get("auth", 0) > 0 or (corrections and _ENV_RX.search(joined_corrections)):
        return "environment"

    if kind_map.get("timeout", 0) > 0 or error_count >= 6:
        return "external-dependency"

    # Planning failures need corroborating dysfunction, not just breadth.
    # Codex P2 2026-07-19: `len(churn) >= 3` alone tagged every routine
    # source+test+config change as "planning". Require EITHER:
    #   - Depth: a single file rewritten >= 3 times (real iteration churn), OR
    #   - Wide breadth: >= 5 distinct files touched (uncommon in scoped work), OR
    #   - Breadth + iteration: >= 3 distinct files AND at least one re-edited
    # so ordinary three-file implementations are NOT misclassified.
    if (
        any(v >= 3 for v in churn.values())
        or len(churn) >= 5
        or (len(churn) >= 3 and any(v >= 2 for v in churn.values()))
    ):
        return "planning"

    # Memory-context: a file re-read enough times WITHOUT edits happening in
    # between (i.e. the agent forgot what it saw, not that it was iterating on
    # a change). If we also edited the same file, that's normal iterative
    # work, not a memory gap. The "Re-read: <path> xN" confusion cue emitted
    # by extract_signals mirrors the same file_reads count, so apply the
    # SAME edit-churn guard when reading it back — otherwise ordinary
    # edit-and-verify sessions would still trip memory-context via the
    # confusion channel (Codex P2 2026-07-19).
    reread_without_edit = any(
        read_count >= 2 and churn.get(path, 0) == 0
        for path, read_count in reads.items()
    )
    reread_cue_without_edit = False
    for cue in confusion:
        if not isinstance(cue, str) or not cue.startswith("Re-read: "):
            continue
        rest = cue[len("Re-read: "):]
        # Format: "Re-read: <path> xN" — strip trailing " xN" if present.
        idx = rest.rfind(" x")
        cue_path = rest[:idx] if idx > 0 else rest
        if cue_path and churn.get(cue_path, 0) == 0:
            reread_cue_without_edit = True
            break
    if reread_without_edit or reread_cue_without_edit:
        return "memory-context"

    # Fall through to text classification on corrections / samples / confusion.
    # Strip "Re-read:" cues from the blob — they were ALREADY consulted above
    # (and gated by edit-churn). Feeding them to _MEMORY_RX here would let a
    # confused-and-edited session trip memory-context via the text path even
    # though the structured guard cleared it (Codex P2 follow-up).
    text_confusion = [c for c in confusion if not (isinstance(c, str) and c.startswith("Re-read: "))]
    blob = "\n".join(
        corrections + text_confusion + sample_list + [f"kinds:{sorted(kind_map)}"]
    )
    return classify_from_text(blob)
