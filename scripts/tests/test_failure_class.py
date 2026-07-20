"""Unit tests for scripts/lib/failure_class.py — classifier priority & fix #7."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from failure_class import (  # noqa: E402
    DEFAULT_FAILURE_CLASS,
    FAILURE_CLASSES,
    classify_from_signals,
    classify_from_text,
    normalize_failure_class,
)


def test_normalize_unknown_returns_default():
    assert normalize_failure_class("bogus") == DEFAULT_FAILURE_CLASS
    assert normalize_failure_class(None) == DEFAULT_FAILURE_CLASS
    assert normalize_failure_class("") == DEFAULT_FAILURE_CLASS


def test_normalize_known_passes_through():
    for cls in FAILURE_CLASSES:
        assert normalize_failure_class(cls) == cls


def test_classify_instruction_gap_wins_first():
    assert classify_from_signals(
        user_corrections=["you should have followed CLAUDE.md rules"],
        tool_retries={"Bash": 5},  # would otherwise flag tooling
    ) == "instruction-gap"


def test_classify_tooling_from_retries():
    assert classify_from_signals(tool_retries={"Bash": 3}) == "tooling"


def test_classify_environment_from_kinds():
    assert classify_from_signals(kinds={"auth": 1}) == "environment"


def test_classify_external_from_timeouts():
    assert classify_from_signals(kinds={"timeout": 1}) == "external-dependency"
    assert classify_from_signals(error_count=6) == "external-dependency"


def test_planning_depth_still_flagged():
    """Existing behaviour: one file rewritten many times."""
    assert classify_from_signals(edit_churn={"foo.py": 4}) == "planning"


def test_planning_ordinary_three_file_change_is_not_planning():
    """Codex P2 2026-07-19: source + test + config each edited once is the
    normal shape of a routine multi-file change, NOT a planning failure.
    The classifier must require iteration or wider breadth as evidence."""
    result = classify_from_signals(edit_churn={"src.py": 1, "test.py": 1, "cfg.py": 1})
    assert result != "planning", f"got {result!r}"


def test_planning_breadth_plus_iteration_flagged():
    """Three files touched AND at least one re-edited — iteration signal
    combined with breadth is planning."""
    assert classify_from_signals(
        edit_churn={"a.py": 2, "b.py": 1, "c.py": 1}
    ) == "planning"


def test_planning_wide_breadth_alone_flagged():
    """Five or more distinct files is broad enough to flag on its own."""
    assert classify_from_signals(
        edit_churn={"a.py": 1, "b.py": 1, "c.py": 1, "d.py": 1, "e.py": 1}
    ) == "planning"


def test_planning_two_files_still_below_threshold():
    """Guard against overshooting: two files edited once are ordinary work."""
    result = classify_from_signals(edit_churn={"a.py": 1, "b.py": 1})
    assert result != "planning"


def test_memory_context_from_rereads():
    assert classify_from_signals(file_reads={"foo.py": 3}) == "memory-context"


def test_reread_confusion_cue_still_gated_by_edit_churn():
    """Codex P2 2026-07-19: the 'Re-read: <path> xN' confusion cue emitted
    by extract_signals mirrors the same file_reads count. It must be gated
    by the SAME edit-churn guard — otherwise edit-and-verify still trips
    memory-context via the confusion channel."""
    # Cue + edit on the same file → NOT memory-context
    assert classify_from_signals(
        ai_confusion_events=["Re-read: foo.py x3"],
        edit_churn={"foo.py": 1},
    ) != "memory-context"
    # Cue with NO edit → still memory-context
    assert classify_from_signals(
        ai_confusion_events=["Re-read: foo.py x3"],
    ) == "memory-context"
    # Cue path unedited, another file edited → still memory-context
    assert classify_from_signals(
        ai_confusion_events=["Re-read: foo.py x3"],
        edit_churn={"bar.py": 1},
    ) == "memory-context"


def test_memory_context_requires_reread_without_edit():
    """CodeRabbit fix: a re-read paired with an edit is iterative work,
    not a memory gap. Only unaccompanied rereads flag memory-context."""
    assert classify_from_signals(
        file_reads={"foo.py": 2},
        edit_churn={"foo.py": 1},
    ) != "memory-context"
    # But a re-read of a DIFFERENT file (untouched) still flags.
    assert classify_from_signals(
        file_reads={"foo.py": 2},
        edit_churn={"bar.py": 1},
    ) == "memory-context"


def test_unclassified_default():
    assert classify_from_signals() == DEFAULT_FAILURE_CLASS


def test_classify_from_text_priority():
    assert classify_from_text("Unauthorized 401") == "environment"
    assert classify_from_text("timed out talking to vercel") == "external-dependency"
    assert classify_from_text("hook fired PreToolUse tool call") == "tooling"
    assert classify_from_text("") == DEFAULT_FAILURE_CLASS
