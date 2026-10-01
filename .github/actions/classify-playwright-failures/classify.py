#!/usr/bin/env python3
"""Classify a Playwright run's failures into the pack test_failure_class.

Reads Playwright JSON-reporter files (any `*.json` under the given paths
that has a top-level `suites` array) plus optional plain-text logs from
pre-test steps, folds them with scripts/lib/failure_class.py, and writes a
step-summary table, a JSON record and GITHUB_OUTPUT keys. Advisory only:
always exits 0 so it can never change a job's conclusion.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "lib"))
from failure_class import (
    TEST_FAILURE_CLASSES,
    classify_playwright_report,
    parse_playwright_text_output,
    strip_ansi,
    summarize_test_run,
)

MAX_ROWS = 25
MAX_MESSAGE = 200
MAX_REPORT_BYTES = 50 * 1024 * 1024
_TOKENISH_RX = re.compile(r"[A-Za-z0-9_\-+/=.]{32,}")


def _redact(text: str) -> str:
    return _TOKENISH_RX.sub("[redacted]", text)


def first_line(text: str) -> str:
    for line in strip_ansi(text).splitlines():
        line = line.strip()
        if line:
            return _redact(line)[:MAX_MESSAGE]
    return ""


def find_reports(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            found.append(p)
        elif p.is_dir():
            found.extend(sorted(q for q in p.rglob("*.json") if q.is_file()))
    return found


def load_report(path: Path):
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("suites"), list):
        return data
    return None


def read_logs(paths: list[str]) -> list[str]:
    texts: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            try:
                texts.append(p.read_text(encoding="utf-8", errors="replace")[-200_000:])
            except OSError:
                continue
    return texts


def classify(
    results: list[str], logs: list[str], job_failed: bool, test_logs: list[str] = ()
) -> dict:
    failures: list[dict] = []
    reports: list[str] = []
    tests = flaky = 0
    for path in find_reports(results):
        payload = load_report(path)
        if payload is None:
            continue
        reports.append(str(path))
        out = classify_playwright_report(payload)
        tests += out["tests"]
        flaky += out["flaky"]
        failures.extend(out["failures"])
    source = "json" if reports else "none"
    if not reports:
        for text in read_logs(list(test_logs)):
            failures.extend(parse_playwright_text_output(text))
            source = "stdout"
    run = summarize_test_run(failures, log_texts=read_logs(logs), job_failed=job_failed)
    rows = [
        {
            "class": f["class"],
            "title": f["title"],
            "file": f["file"],
            "project": f["project"],
            "status": f["status"],
            "setup": f["setup"],
            "message": first_line(f["message"]),
        }
        for f in failures
    ]
    return {
        "schema_version": 1,
        **run,
        "source": source,
        "reports": reports,
        "tests": tests,
        "flaky": flaky,
        "failed": len(failures),
        "failures": rows[:MAX_ROWS],
        "failures_truncated": max(0, len(rows) - MAX_ROWS),
    }


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def markdown(data: dict, title: str) -> str:
    cls = data["test_failure_class"]
    lines = [f"### {title}", ""]
    lines.append(
        f"**test_failure_class:** `{cls}` · autofix_eligible: `{str(data['autofix_eligible']).lower()}`"
        f" · retryable: `{str(data['retryable']).lower()}`"
    )
    lines.append("")
    lines.append(
        f"Tests: {data['tests']} · failed: {data['failed']} · flaky: {data['flaky']}"
        f" · reports: {len(data['reports'])} · pre-test evidence: `{data['pre_test_evidence']}`"
    )
    counts = ", ".join(f"{k}={v}" for k, v in data["counts"].items() if v)
    if counts:
        lines += ["", f"By class: {counts}"]
    if data["failures"]:
        lines += ["", "| Class | Test | Project | First error line |", "| --- | --- | --- | --- |"]
        for f in data["failures"]:
            test = f"{f['file']} › {f['title']}" if f["file"] else f["title"]
            lines.append(
                f"| `{f['class']}` | {_cell(test)} | {_cell(f['project'])} | {_cell(f['message'])} |"
            )
        if data["failures_truncated"]:
            lines.append(f"\n…and {data['failures_truncated']} more.")
    if data["source"] == "stdout":
        lines += ["", "_No Playwright JSON report found; classified from the reporter's stdout._"]
    elif data["source"] == "none" and cls != "none":
        lines += ["", "_No Playwright JSON report or test output found; class is from pre-test log evidence only._"]
    return "\n".join(lines) + "\n"


def write_outputs(path: str, data: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"test_failure_class={data['test_failure_class']}\n")
        fh.write(f"autofix_eligible={str(data['autofix_eligible']).lower()}\n")
        fh.write(f"retryable={str(data['retryable']).lower()}\n")
        fh.write(f"failed_tests={data['failed']}\n")


def _lines(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        out.extend(x.strip() for x in v.splitlines() if x.strip())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", action="append", default=[], help="Report file or directory (repeatable, newline lists ok)")
    ap.add_argument("--log", action="append", default=[], help="Pre-test log file (repeatable, newline lists ok)")
    ap.add_argument("--test-log", action="append", default=[], help="Captured Playwright stdout; used only when no JSON report is found")
    ap.add_argument("--job-status", default="", help="job.status of the caller (failure/cancelled → red run)")
    ap.add_argument("--summary", default="", help="Append markdown here (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--json", default="", help="Write the JSON record here")
    ap.add_argument("--github-output", default="", help="Append key=value outputs here")
    ap.add_argument("--title", default="Playwright failure classification")
    args = ap.parse_args(argv)
    job_failed = args.job_status.strip().lower() in ("failure", "failed", "cancelled")
    data = classify(_lines(args.results), _lines(args.log), job_failed, _lines(args.test_log))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(markdown(data, args.title))
    if args.github_output:
        write_outputs(args.github_output, data)
    cls = data["test_failure_class"]
    if cls != "none":
        print(f"::notice title=test_failure_class::{cls} ({data['failed']} failed; classes: {', '.join(TEST_FAILURE_CLASSES)})")
    print(json.dumps({k: data[k] for k in ("test_failure_class", "autofix_eligible", "retryable", "failed", "flaky")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
