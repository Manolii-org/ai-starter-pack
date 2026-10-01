"""Unit tests for scripts/lib/failure_class.py — classifier priority & fix #7."""
from __future__ import annotations

import re
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



# --- test-run failure classes -------------------------------------------------

from failure_class import (  # noqa: E402
    NO_TEST_FAILURE,
    TEST_FAILURE_CLASSES,
    classify_playwright_report,
    classify_test_failure_text,
    is_setup_location,
    normalize_test_failure_class,
    parse_playwright_text_output,
    summarize_test_run,
)


def test_test_failure_classes_reuse_session_vocabulary():
    shared = set(TEST_FAILURE_CLASSES) & set(FAILURE_CLASSES)
    assert shared == {"environment", "external-dependency", "unclassified"}
    assert normalize_test_failure_class("PRODUCT") == "product"
    assert normalize_test_failure_class("tooling") == DEFAULT_FAILURE_CLASS


def test_assertion_failure_is_product():
    msg = "Error: expect(received).toBe(expected)\n\nExpected: 200\nReceived: 500"
    assert classify_test_failure_text(msg) == "product"
    assert classify_test_failure_text(
        "\x1b[31mError: expect(locator).toBeVisible() failed\x1b[39m"
    ) == "product"


def test_selector_and_code_errors_are_test_defects():
    assert classify_test_failure_text(
        "Error: expect(locator).toBeVisible() failed\nstrict mode violation: "
        "getByRole('button') resolved to 2 elements"
    ) == "test-defect"
    assert classify_test_failure_text("ReferenceError: foo is not defined") == "test-defect"


def test_missing_config_and_credentials_are_environment():
    assert classify_test_failure_text("E2E_TEST_USER_EMAIL is not set") == "environment"
    assert classify_test_failure_text("Error: missing env var SUPABASE_URL") == "environment"
    assert classify_test_failure_text("Invalid login credentials") == "environment"


def test_setup_403_is_environment_but_test_403_is_not():
    assert classify_test_failure_text("Request failed: 403 Forbidden", setup=True) == "environment"
    assert classify_test_failure_text("Request failed: 403 Forbidden") == DEFAULT_FAILURE_CLASS


def test_network_and_health_failures_are_external():
    assert classify_test_failure_text(
        "page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:3000/"
    ) == "external-dependency"
    assert classify_test_failure_text("App failed to become healthy") == "external-dependency"
    assert classify_test_failure_text("Error: 503 Service Unavailable") == "external-dependency"


def test_bare_timeout_stays_unclassified():
    assert classify_test_failure_text("Test timeout of 30000ms exceeded.") == DEFAULT_FAILURE_CLASS
    assert classify_test_failure_text("") == DEFAULT_FAILURE_CLASS


def test_setup_location_detection():
    assert is_setup_location(project="setup")
    assert is_setup_location(file="e2e/global-setup.ts")
    assert is_setup_location(file="tests/auth.setup.ts")
    assert not is_setup_location(project="chromium", file="tests/setup-wizard.spec.ts")


def _report():
    def test(status, expected="passed", errors=(), retries=1, project="chromium"):
        results = [{"status": "failed"}] * (retries - 1) + [
            {"status": status, "errors": [{"message": m} for m in errors]}
        ]
        return {"expectedStatus": expected, "projectName": project, "results": results}

    return {
        "errors": [],
        "suites": [{
            "title": "home.spec.ts",
            "file": "home.spec.ts",
            "specs": [
                {"title": "renders", "tests": [test("passed")]},
                {"title": "flaky", "tests": [test("passed", retries=2)]},
                {"title": "skipped", "tests": [test("skipped")]},
                {"title": "expected fail", "tests": [test("failed", expected="failed")]},
                {"title": "asserts", "tests": [test("failed", errors=["expect(x).toBe(y)"])]},
            ],
            "suites": [{
                "title": "nested",
                "specs": [{"title": "times out", "tests": [test("timedOut")]}],
            }],
        }],
    }


def test_playwright_report_counts_final_failures_only():
    out = classify_playwright_report(_report())
    assert out["tests"] == 6
    assert out["flaky"] == 1
    assert [(f["title"], f["class"]) for f in out["failures"]] == [
        ("asserts", "product"),
        ("nested › times out", DEFAULT_FAILURE_CLASS),
    ]
    assert out["failures"][1]["file"] == "home.spec.ts"


def test_playwright_top_level_errors_are_setup_phase():
    out = classify_playwright_report(
        {"errors": [{"message": "Error: 401 Unauthorized while signing in"}], "suites": []}
    )
    assert out["failures"][0]["setup"] is True
    assert out["failures"][0]["class"] == "environment"


def test_run_precedence_infra_over_code():
    run = summarize_test_run([{"class": "product"}, {"class": "external-dependency"}])
    assert run["test_failure_class"] == "external-dependency"
    assert run["retryable"] is True and run["autofix_eligible"] is False
    assert run["counts"]["product"] == 1


def test_run_product_is_autofix_eligible():
    run = summarize_test_run([{"class": "product"}, {"class": "test-defect"}])
    assert run["test_failure_class"] == "test-defect"
    assert run["autofix_eligible"] is True


def test_run_pre_test_log_evidence_and_noise():
    run = summarize_test_run([], log_texts=["booting\nApp failed to become healthy\n"], job_failed=True)
    assert run["test_failure_class"] == "external-dependency"
    noisy = summarize_test_run([{"class": "product"}], log_texts=["expect(x) noise\nReferenceError"])
    assert noisy["test_failure_class"] == "product"


def test_run_green_and_unexplained_red():
    assert summarize_test_run([])["test_failure_class"] == NO_TEST_FAILURE
    assert summarize_test_run([], job_failed=True)["test_failure_class"] == DEFAULT_FAILURE_CLASS



_LIST_OUTPUT = """
Running 4 tests using 1 worker

  \u2718  1 [chromium] \u203a fmt.spec.ts:2:5 \u203a asserts (562ms)

  1) [chromium] \u203a fmt.spec.ts:2:5 \u203a asserts \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    Error: page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:3000/

    Retry #1 \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    Error: expect(locator).toHaveText(expected) failed

    Expected: "bye"
    Received: "hi"

  2) [setup] \u203a auth.setup.ts:3:5 \u203a sign in \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    Error: 401 Unauthorized

  3) [chromium] \u203a fmt.spec.ts:4:5 \u203a flaky \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    Error: expect(received).toBe(expected) // Object.is equality

  2 failed
    [chromium] \u203a fmt.spec.ts:2:5 \u203a asserts \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    [setup] \u203a auth.setup.ts:3:5 \u203a sign in \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  1 flaky
    [chromium] \u203a fmt.spec.ts:4:5 \u203a flaky \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  1 passed (6.2s)
"""


def test_text_output_final_attempt_and_flaky_excluded():
    rows = parse_playwright_text_output(_LIST_OUTPUT)
    assert [(r["project"], r["file"], r["title"], r["class"]) for r in rows] == [
        ("chromium", "fmt.spec.ts", "asserts", "product"),
        ("setup", "auth.setup.ts", "sign in", "environment"),
    ]
    assert rows[1]["setup"] is True


def test_text_output_without_summary_counts_every_block():
    head = _LIST_OUTPUT.split("  2 failed")[0]
    assert len(parse_playwright_text_output(head)) == 3
    assert parse_playwright_text_output("no failures here") == []


def _as_github_annotations(output: str) -> str:
    def enc(msg: str) -> str:
        return msg.replace("%", "%25").replace("\n", "%0A")

    blocks = re.split(r"(?m)^(?=  \d+\) )|^(?=  \d+ failed)", output.strip("\n"))
    lines = []
    for block in filter(None, blocks):
        if re.match(r"  \d+ failed", block):
            lines.append("::notice title=Playwright Run Summary::" + enc(block))
            continue
        title = block.splitlines()[0]
        for attempt in re.split(r"(?m)^(?=\s+Retry #\d+)", block):
            body = attempt if attempt.startswith("  ") and ")" in attempt[:6] else title + "\n" + attempt
            lines.append(f"::error file=x.spec.ts,title={title.strip()},line=2,col=1::" + enc(body))
    return "\n".join(lines) + "\n"


def test_text_output_github_annotations_only():
    annotated = _as_github_annotations(_LIST_OUTPUT)
    assert not re.search(r"(?m)^  \d+\) ", annotated)
    rows = parse_playwright_text_output(annotated)
    assert [(r["project"], r["title"], r["class"]) for r in rows] == [
        ("chromium", "asserts", "product"),
        ("setup", "sign in", "environment"),
    ]


def test_text_output_github_reporter_mixed_stdout_not_double_counted():
    mixed = _LIST_OUTPUT + _as_github_annotations(_LIST_OUTPUT)
    assert len(parse_playwright_text_output(mixed)) == 2


def test_missing_browser_binary_is_environment():
    assert classify_test_failure_text(
        "Error: browserType.launch: Executable doesn't exist at /x/chrome"
    ) == "environment"
