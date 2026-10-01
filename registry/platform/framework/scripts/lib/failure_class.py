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
      planning ← single file ≥3 edits, OR ≥5 distinct files, OR ≥3 files with one re-edited
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



# ---------------------------------------------------------------------------
# Test-run failure classes (Playwright and other E2E runners).
#
# Separate field (`test_failure_class`) from the session `failure_class`
# above; it reuses that vocabulary where the meaning is identical
# (environment, external-dependency, unclassified) and adds only the two
# classes a test run needs: product and test-defect. Evidence-only: an
# unmatched failure stays unclassified rather than being guessed.
# ---------------------------------------------------------------------------

TEST_FAILURE_CLASSES: tuple[str, ...] = (
    "product",
    "test-defect",
    "environment",
    "external-dependency",
    "unclassified",
)
TEST_FAILURE_CLASS_SET = frozenset(TEST_FAILURE_CLASSES)
# Run-level precedence: a blocked/infra cause outranks code failures because
# it can produce them; fix it first and re-run before chasing assertions.
TEST_RUN_PRECEDENCE: tuple[str, ...] = (
    "environment",
    "external-dependency",
    "test-defect",
    "product",
    "unclassified",
)
AUTOFIX_ELIGIBLE_TEST_CLASSES = frozenset({"product", "test-defect"})
RETRYABLE_TEST_CLASSES = frozenset({"external-dependency"})
NO_TEST_FAILURE = "none"

_ANSI_RX = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TEST_DEFECT_RX = re.compile(
    r"strict mode violation|is not a valid selector|SyntaxError|ReferenceError|"
    r"TypeError: [^\n]{0,80} is not a function|Cannot find module|"
    r"fixture \"[^\"]+\" (?:not found|has already been registered)|No tests found|"
    r"forbidOnly|focused item found",
    re.I,
)
_TEST_ENV_RX = re.compile(
    r"\b(?:missing|unset|not set|not configured|empty)\b[^\n]{0,60}"
    r"\b(?:env(?:ironment)? var(?:iable)?s?|secrets?|credentials?)\b|"
    r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b[^\n]{0,40}\b(?:is )?(?:missing|unset|not set|required|empty)\b|"
    r"\b(?:missing|unset)\b[^\n]{0,20}\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|"
    r"invalid (?:login )?credentials|invalid api key|authentication failed|"
    r"(?:login|sign[- ]?in) failed|storage ?state[^\n]{0,40}(?:ENOENT|not found|missing)|"
    r"ENOENT[^\n]{0,80}(?:storage[-_ ]?state|\.auth)|"
    r"seed(?:ed)? (?:data|users?|rows?|fixtures?) (?:missing|not found|absent)|"
    r"browserType\.launch: Executable doesn't exist|npx playwright install",
    re.I,
)
_TEST_SETUP_AUTH_RX = re.compile(r"\b(?:401|403)\b|unauthori[sz]ed|forbidden", re.I)
_TEST_EXTERNAL_RX = re.compile(
    r"net::ERR_[A-Z_]+|ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|"
    r"socket hang up|\b(?:502|503|504)\b|Bad Gateway|Service Unavailable|"
    r"Gateway Time-?out|failed to become healthy|health ?check (?:failed|timed out)|"
    r"Target crashed|Page crashed|Browser crashed|"
    r"rate[- ]?limit(?:ed)?|\b429\b|Too Many Requests|page\.goto: Timeout|"
    r"navigation timeout|config\.webServer|webServer[^\n]{0,40}(?:exited|timed out)",
    re.I,
)
_TEST_PRODUCT_RX = re.compile(
    r"expect\(|\.(?:toHave|toBe|toEqual|toContain|toMatch)[A-Za-z]*\(|"
    r"^\s*(?:Expected|Received)(?: [a-z ]+)?:",
    re.M,
)
_SETUP_LOCATION_RX = re.compile(
    r"(?:^|[/\\])(?:global[-_.]?(?:setup|teardown)|[\w.-]*\.setup)\.[cm]?[jt]sx?$",
    re.I,
)


def strip_ansi(text: str) -> str:
    return _ANSI_RX.sub("", text or "")


def is_setup_location(*, project: str = "", file: str = "") -> bool:
    """True for Playwright setup projects and global setup/teardown files."""
    return bool(re.search(r"setup", project or "", re.I)) or bool(
        _SETUP_LOCATION_RX.search(file or "")
    )


def classify_test_failure_text(text: str, *, setup: bool = False) -> str:
    """Classify one failure's message/stack; first match wins; else unclassified.

    `setup` marks setup-phase evidence (setup project, global setup, or a
    pre-test step such as an app health check), where a 401/403 means the
    run was blocked by credentials rather than an asserted product response.
    """
    body = strip_ansi(text)
    if not body.strip():
        return DEFAULT_FAILURE_CLASS
    if _TEST_DEFECT_RX.search(body):
        return "test-defect"
    if _TEST_ENV_RX.search(body) or (setup and _TEST_SETUP_AUTH_RX.search(body)):
        return "environment"
    if _TEST_EXTERNAL_RX.search(body):
        return "external-dependency"
    if _TEST_PRODUCT_RX.search(body):
        return "product"
    return DEFAULT_FAILURE_CLASS


def normalize_test_failure_class(value: Optional[str]) -> str:
    """Return a valid TEST_FAILURE_CLASSES member; unknown/empty → unclassified."""
    if not value:
        return DEFAULT_FAILURE_CLASS
    v = str(value).strip().lower()
    return v if v in TEST_FAILURE_CLASS_SET else DEFAULT_FAILURE_CLASS


def _walk_playwright_suites(suites, file_hint: str = "", path: tuple[str, ...] = ()):
    for suite in suites or []:
        file = str(suite.get("file") or file_hint)
        title = str(suite.get("title") or "")
        chain = path + ((title,) if title and title != file else ())
        for spec in suite.get("specs") or []:
            spec_file = str(spec.get("file") or file)
            name = " › ".join(chain + (str(spec.get("title") or "unnamed"),))
            for test in spec.get("tests") or []:
                yield spec_file, name, test
        yield from _walk_playwright_suites(suite.get("suites"), file, chain)


def _result_error_text(result: Mapping) -> str:
    parts: list[str] = []
    errors = list(result.get("errors") or [])
    if result.get("error"):
        errors.append(result["error"])
    for err in errors:
        if isinstance(err, Mapping):
            parts.append(str(err.get("message") or ""))
            parts.append(str(err.get("stack") or ""))
        else:
            parts.append(str(err))
    return "\n".join(p for p in parts if p)


def classify_playwright_report(payload: Mapping) -> dict:
    """Classify every final failure in a Playwright JSON-reporter payload.

    Returns {"failures": [...], "flaky": n, "tests": n}. A test counts as a
    failure only when its final attempt did not reach its expected status;
    retried-then-passed tests are flaky, not failures. Top-level `errors`
    (config / globalSetup) are setup-phase failures.
    """
    failures: list[dict] = []
    flaky = 0
    total = 0
    for err in payload.get("errors") or []:
        text = _result_error_text({"errors": [err]})
        failures.append({
            "title": "(global setup / config)",
            "file": "",
            "project": "",
            "status": "error",
            "setup": True,
            "class": classify_test_failure_text(text, setup=True),
            "message": text,
        })
    for file, title, test in _walk_playwright_suites(payload.get("suites")):
        total += 1
        results = list(test.get("results") or [])
        if not results:
            continue
        final = results[-1]
        status = str(final.get("status") or "")
        expected = str(test.get("expectedStatus") or "passed")
        if status in ("skipped", expected):
            if len(results) > 1 and status == "passed":
                flaky += 1
            continue
        project = str(test.get("projectName") or "")
        setup = is_setup_location(project=project, file=file)
        text = _result_error_text(final)
        if status == "timedOut" and not text:
            text = "Test timeout exceeded"
        failures.append({
            "title": title,
            "file": file,
            "project": project,
            "status": status,
            "setup": setup,
            "class": classify_test_failure_text(text, setup=setup),
            "message": text,
        })
    return {"failures": failures, "flaky": flaky, "tests": total}


def summarize_test_run(
    failures: Iterable[Mapping],
    *,
    log_texts: Iterable[str] = (),
    job_failed: bool = False,
) -> dict:
    """Fold per-test classes (plus pre-test log evidence) into one run class.

    `log_texts` is evidence from steps that can fail before any test runs
    (health checks, browser install, webServer start). Only lines matching
    environment / external-dependency count, so ordinary log noise cannot
    override the report.
    """
    rows = list(failures)
    counts = {cls: 0 for cls in TEST_FAILURE_CLASSES}
    for row in rows:
        counts[normalize_test_failure_class(row.get("class"))] += 1
    log_class = DEFAULT_FAILURE_CLASS
    for text in log_texts:
        for line in strip_ansi(text).splitlines():
            cls = classify_test_failure_text(line, setup=True)
            if cls in ("environment", "external-dependency") and (
                TEST_RUN_PRECEDENCE.index(cls) < TEST_RUN_PRECEDENCE.index(log_class)
            ):
                log_class = cls
    present = {cls for cls, n in counts.items() if n}
    if log_class != DEFAULT_FAILURE_CLASS:
        present.add(log_class)
    if not present:
        run_class = DEFAULT_FAILURE_CLASS if job_failed else NO_TEST_FAILURE
    else:
        run_class = next(cls for cls in TEST_RUN_PRECEDENCE if cls in present)
    return {
        "test_failure_class": run_class,
        "autofix_eligible": run_class in AUTOFIX_ELIGIBLE_TEST_CLASSES,
        "retryable": run_class in RETRYABLE_TEST_CLASSES,
        "counts": counts,
        "pre_test_evidence": log_class,
    }


_PW_BLOCK_RX = re.compile(r"^  (\d+)\) (.+?)\s*─{3,}", re.M)
_PW_COUNT_RX = re.compile(
    r"^  (\d+) (failed|flaky|interrupted|did not run|skipped|passed)\b", re.M
)
_PW_RETRY_RX = re.compile(r"^\s+Retry #\d+\s*─{3,}", re.M)
_PW_TITLE_RX = re.compile(r"^(?:\[(?P<project>[^\]]+)\] › )?(?P<file>[^›:]+?)(?::\d+:\d+)? › (?P<rest>.+)$")
_GH_ANNOTATION_RX = re.compile(r"^::(?:error|notice)(?: .*?)?::(.*)$", re.M)


def _decode_github_annotations(body: str) -> str:
    """Join `::error` / `::notice` workflow-command messages, %-decoded."""
    return "\n".join(
        m.group(1).replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")
        for m in _GH_ANNOTATION_RX.finditer(body)
    )


def parse_playwright_text_output(text: str) -> list[dict]:
    """Classify failures from Playwright list/line/dot/github stdout.

    Fallback for runs without a JSON reporter. Uses the numbered failure
    blocks every built-in terminal reporter prints, the final attempt of
    each block (after the last `Retry #N`), and the end-of-run
    `N failed` / `N flaky` lists so flaky tests are not counted as failures.
    When only the `github` reporter's `::error` / `::notice` annotations
    survive, their decoded messages are parsed the same way (the last
    annotation per test is its final attempt).
    """
    body = strip_ansi(text)
    headers = list(_PW_BLOCK_RX.finditer(body))
    if not headers:
        decoded = _decode_github_annotations(body)
        if not decoded or not _PW_BLOCK_RX.search(decoded):
            return []
        body = decoded
        headers = list(_PW_BLOCK_RX.finditer(body))
    counts = list(_PW_COUNT_RX.finditer(body))
    failed_titles: Optional[set[str]] = None
    if counts:
        failed_titles = set()
        for i, m in enumerate(counts):
            if m.group(2) not in ("failed", "interrupted"):
                continue
            end = counts[i + 1].start() if i + 1 < len(counts) else len(body)
            for line in body[m.end():end].splitlines():
                title = re.sub(r"\s*─+.*$", "", line).strip()
                if title:
                    failed_titles.add(title)
    out: dict[str, dict] = {}
    summary_start = counts[0].start() if counts else len(body)
    for i, m in enumerate(headers):
        title = m.group(2).strip()
        if failed_titles is not None and title not in failed_titles:
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else summary_start
        block = body[m.end():max(end, m.end())]
        attempts = _PW_RETRY_RX.split(block)
        final = attempts[-1]
        tm = _PW_TITLE_RX.match(title)
        project = (tm.group("project") or "") if tm else ""
        file = tm.group("file").strip() if tm else ""
        setup = is_setup_location(project=project, file=file)
        out[title] = {
            "title": tm.group("rest").strip() if tm else title,
            "file": file,
            "project": project,
            "status": "failed",
            "setup": setup,
            "class": classify_test_failure_text(final, setup=setup),
            "message": final.strip(),
        }
    return list(out.values())
