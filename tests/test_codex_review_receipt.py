"""Execution-state tests: a missing reviewer never turns into a clean verdict."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("receipt", ROOT / "scripts/ci/codex_review_receipt.py")
assert SPEC is not None and SPEC.loader is not None
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)
READY = {"RANGE_STATE": "ready", "AUTH_PRESENT": "true", "INSTALL_OUTCOME": "success", "SCAN_OUTCOME": "success", "SCAN_RC": "0"}


@pytest.mark.parametrize("change,expected", [
    ({"RANGE_STATE": "unresolved"}, "failed_range"),
    ({"RANGE_STATE": "empty"}, "exempt"),
    ({"RANGE_STATE": "security_exempt"}, "failed_range"),
    ({"AUTH_PRESENT": "false"}, "blocked_credentials"),
    ({"AUTH_PRESENT": "failed"}, "blocked_credentials"),
    ({"INSTALL_OUTCOME": "failure"}, "failed_install"),
    ({"SCAN_OUTCOME": "cancelled"}, "cancelled"),
    ({"SCAN_OUTCOME": "failure"}, "failed_scan"),
    ({"SCAN_RC": "124"}, "failed_scan"),
    ({"SCAN_WITHHELD": "true"}, "failed_output"),
])
def test_operational_outcomes(change, expected):
    assert receipt.classify("code", READY | change, "codex\nNo defects found.")[0] == expected


@pytest.mark.parametrize("raw", [None, "", "INFO startup\nexec\ncat file\noutput", "codex\n"])
def test_no_model_turn_never_completes(raw):
    assert receipt.classify("code", READY, raw)[0] == "failed_output"


def test_model_turn_and_quota_are_distinct():
    assert receipt.classify("code", READY, "exec\ntrace\ncodex\nNo actionable issues.\ntokens used\n123")[0] == "completed"
    assert receipt.classify("code", READY, "codex\nYou have reached your Codex usage limits for code reviews.")[0] == "blocked_quota"
    assert receipt.classify("code", READY | {"SCAN_RC": "1"}, "Error: quota exhausted")[0] == "blocked_quota"
    assert receipt.classify("code", READY, "codex\nThe patch correctly handles quota exhaustion.")[0] == "completed"


@pytest.mark.parametrize("raw", [None, "", "{", "{}", '{"findings":null}', '{"findings":[1]}'])
def test_missing_and_invalid_security_findings_never_clean(raw):
    assert receipt.classify("security", READY, raw)[0] == "failed_output"


def test_security_zero_and_nonzero_findings_are_completed_not_clean_permissions():
    assert receipt.classify("security", READY, '{"findings":[]}') == ("completed", "findings_validated", 0)
    assert receipt.classify("security", READY, '[{"title":"an issue"}]') == ("completed", "findings_validated", 1)


@pytest.mark.parametrize("path", ["src/auth/session.ts", "supabase/migrations/a.sql", ".github/workflows/test.yml", "src/middleware.ts", "src/jwt.ts", "package-lock.json", "src/tenants/access.py", "deploy/app.toml", ".ai/entity-boundaries.yml", "prisma/schema.prisma", "src/billing/charge.ts", "pyproject.toml", "composer.lock", "Cargo.toml", "scripts/ci/codex_review_receipt.py", "scripts/assessment/sanitiser.py", "lib/redaction.py"])
def test_sensitive_paths_require_security(path):
    assert receipt.security_required([path])


def test_ordinary_paths_can_be_explicitly_exempted():
    assert not receipt.security_required(["docs/style.md", "src/components/Button.tsx"])
    assert receipt.classify("security", READY | {"RANGE_STATE": "security_exempt"}, None)[0] == "exempt"


def test_receipt_identity_and_no_model_text():
    env = READY | {"GITHUB_REPOSITORY": "example/project", "PR_NUM": "42", "HEAD_SHA": "a" * 40, "BASE_SHA": "b" * 40}
    result = receipt.build_receipt("code", env, "codex\nprivate model text")
    assert result["head_sha"] == "a" * 40
    assert "private model text" not in str(result)
    with pytest.raises(ValueError):
        receipt.build_receipt("code", env | {"HEAD_SHA": "a" * 7}, "codex\nok")


def test_artifact_redaction_and_fragment_withholding(tmp_path):
    import base64
    import json
    fixture_value = "synthetic-credential-fixture-" * 4
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": fixture_value}}))
    out = tmp_path / "out"
    out.mkdir()
    result = out / "findings.json"
    result.write_text(json.dumps({"findings": [{"title": base64.b64encode(fixture_value.encode()).decode()}]}))
    assert not receipt.redact_directory(out, auth)
    assert fixture_value not in result.read_text()
    assert "REDACTED-CREDENTIAL" in result.read_text()
    result.write_text("-".join(fixture_value[:60]))
    assert receipt.redact_directory(out, auth)
    assert "withheld" in result.read_text()
