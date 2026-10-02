"""Tests for .github/actions/classify-playwright-failures/classify.py."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/actions/classify-playwright-failures/classify.py"


def _report(errors=(), status="failed", project="chromium", file="a.spec.ts"):
    return {
        "config": {},
        "errors": [],
        "suites": [{
            "title": file,
            "file": file,
            "specs": [{
                "title": "case",
                "tests": [{
                    "expectedStatus": "passed",
                    "projectName": project,
                    "results": [{"status": status, "errors": [{"message": m} for m in errors]}],
                }],
            }],
        }],
    }


def _run(tmp_path, *args):
    summary = tmp_path / "summary.md"
    outputs = tmp_path / "out.txt"
    record = tmp_path / "rec" / "class.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--summary", str(summary), "--github-output", str(outputs),
         "--json", str(record), *args],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    kv = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    return kv, json.loads(record.read_text()), summary.read_text(), proc.stdout


def test_product_failure_from_nested_report_dir(tmp_path):
    d = tmp_path / "test-results" / "core"
    d.mkdir(parents=True)
    (d / "results.json").write_text(json.dumps(_report(["Error: expect(page).toHaveURL(expected)"])))
    (d / "other.json").write_text("{\"not\": \"a report\"}")
    kv, rec, md, out = _run(tmp_path, "--results", str(tmp_path / "test-results"), "--job-status", "failure")
    assert kv == {"test_failure_class": "product", "autofix_eligible": "true",
                  "retryable": "false", "failed_tests": "1"}
    assert rec["reports"] == [str(d / "results.json")]
    assert "`product`" in md and "a.spec.ts › case" in md
    assert "::notice title=test_failure_class::product" in out


def test_green_run_reports_none_without_notice(tmp_path):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(_report(status="passed")))
    kv, rec, _, out = _run(tmp_path, "--results", str(p), "--job-status", "success")
    assert kv["test_failure_class"] == "none"
    assert rec["tests"] == 1 and rec["failed"] == 0
    assert "::notice" not in out


def test_pre_test_log_only(tmp_path):
    log = tmp_path / "pre.log"
    log.write_text("Waiting for app...\nApp failed to become healthy\n")
    kv, _rec, md, _ = _run(tmp_path, "--results", str(tmp_path / "missing"), "--log", str(log),
                          "--job-status", "failure")
    assert kv["test_failure_class"] == "external-dependency"
    assert kv["retryable"] == "true"
    assert "No Playwright JSON report or test output found" in md


def test_newline_lists_redaction_and_pipe_escaping(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    token = "A" * 40
    a.write_text(json.dumps(_report([f"expect(x).toBe(y) | token {token}"])))
    b.write_text(json.dumps(_report(["E2E_TEST_USER_EMAIL is not set"], project="setup", file="auth.setup.ts")))
    kv, rec, md, _ = _run(tmp_path, "--results", f"{a}\n{b}", "--job-status", "failure")
    assert kv["test_failure_class"] == "environment"
    assert rec["counts"]["product"] == 1 and rec["counts"]["environment"] == 1
    assert token not in md and "[redacted]" in md and "\\|" in md


def test_unreadable_inputs_never_fail(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    kv, rec, _, _ = _run(tmp_path, "--results", str(bad), "--job-status", "failure")
    assert kv["test_failure_class"] == "unclassified"
    assert rec["reports"] == []


def test_stdout_fallback_only_without_json(tmp_path):
    out = tmp_path / "pw.log"
    out.write_text(
        "  1) [chromium] \u203a a.spec.ts:1:1 \u203a case \u2500\u2500\u2500\u2500\n\n"
        "    Error: expect(page).toHaveTitle(expected) failed\n\n"
        "  1 failed\n    [chromium] \u203a a.spec.ts:1:1 \u203a case \u2500\u2500\u2500\u2500\n"
    )
    kv, rec, md, _ = _run(tmp_path, "--results", str(tmp_path / "none"), "--test-log", str(out),
                          "--job-status", "failure")
    assert kv["test_failure_class"] == "product" and rec["source"] == "stdout"
    assert "classified from the reporter's stdout" in md
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps(_report(status="passed")))
    kv, rec, _, _ = _run(tmp_path, "--results", str(rep), "--test-log", str(out), "--job-status", "failure")
    assert rec["source"] == "json" and kv["test_failure_class"] == "unclassified"
