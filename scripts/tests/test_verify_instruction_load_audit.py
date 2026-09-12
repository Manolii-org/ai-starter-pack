import hashlib
import importlib.util
import json
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "verify-instruction-load-audit.py"
SPEC = importlib.util.spec_from_file_location("verify_instruction_load_audit", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_load_rows_filters_session_and_schema(tmp_path):
    receipt = tmp_path / "loads.jsonl"
    receipt.write_text("\n".join([
        json.dumps({"schema": "other", "session_id": "wanted"}),
        json.dumps({"schema": "manolii.instruction-load/v1", "session_id": "other"}),
        json.dumps({"schema": "manolii.instruction-load/v1", "session_id": "wanted"}),
    ]), encoding="utf-8")
    assert len(MODULE.load_rows(receipt, "wanted")) == 1


def test_verify_requires_matching_reason_and_current_digest(tmp_path):
    instruction = tmp_path / "CLAUDE.md"
    instruction.write_text("rules\n", encoding="utf-8")
    digest = hashlib.sha256(instruction.read_bytes()).hexdigest()
    rows = [{
        "file_path": "CLAUDE.md", "load_reason": "session_start",
        "observed_sha256": digest,
    }]
    assert MODULE.verify(rows, [("CLAUDE.md", "session_start")], root=tmp_path) == []
    assert MODULE.verify(rows, [("CLAUDE.md", "compact")], root=tmp_path) == ["missing CLAUDE.md:compact"]


def test_verify_rejects_stale_digest(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("current\n", encoding="utf-8")
    rows = [{
        "file_path": "CLAUDE.md", "load_reason": "session_start",
        "observed_sha256": "0" * 64,
    }]
    assert MODULE.verify(rows, [("CLAUDE.md", "session_start")], root=tmp_path) == [
        "digest mismatch CLAUDE.md:session_start"
    ]


def test_load_rows_requires_a_session_and_reads_rotated_archives(tmp_path):
    """Rotation must not make a current-session observation look missing."""
    receipt = tmp_path / "instruction-loads.jsonl"
    archive = tmp_path / "archive" / "2026-09"
    archive.mkdir(parents=True)
    row = {
        "schema": "manolii.instruction-load/v1", "session_id": "wanted",
        "file_path": "CLAUDE.md", "load_reason": "session_start",
    }
    (archive / "instruction-loads.20260907T000000Z.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8"
    )
    receipt.write_text("", encoding="utf-8")
    assert MODULE.load_rows(receipt, "wanted") == [row]
    # An empty session id must never behave as "match every session".
    assert MODULE.load_rows(receipt, "") == []


def test_main_fails_closed_without_a_session_id(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    receipt = tmp_path / "instruction-loads.jsonl"
    receipt.write_text("", encoding="utf-8")
    assert MODULE.main(["--receipt", str(receipt)]) == 1
    assert "no session id" in capsys.readouterr().err


def test_invalid_canary_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "wanted")
    receipt = tmp_path / "instruction-loads.jsonl"
    receipt.write_text("", encoding="utf-8")
    canary = tmp_path / "config" / "instruction-load-canary.json"
    canary.parent.mkdir()
    canary.write_text("[]", encoding="utf-8")
    assert MODULE.main(["--receipt", str(receipt), "--root", str(tmp_path), "--session-id", "wanted"]) == 1
    assert "INSTRUCTION-AUDIT-FAIL" in capsys.readouterr().err


def test_null_requirement_fails_closed(tmp_path, capsys):
    receipt = tmp_path / "instruction-loads.jsonl"
    receipt.write_text("", encoding="utf-8")
    canary = tmp_path / "config" / "instruction-load-canary.json"
    canary.parent.mkdir()
    canary.write_text('{"requirements":[null]}', encoding="utf-8")
    assert MODULE.main(["--receipt", str(receipt), "--root", str(tmp_path), "--session-id", "wanted"]) == 1
    assert "INSTRUCTION-AUDIT-FAIL" in capsys.readouterr().err


def test_archive_discovery_matches_real_rotation_naming(tmp_path, monkeypatch):
    """Pin the verifier's archive glob to the names rotation actually writes."""
    import importlib.util

    scripts = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "rotate_jsonl_receipt", scripts / "rotate-jsonl-receipt.py"
    )
    rm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rm)

    memory = tmp_path / ".ai" / "memory"
    memory.mkdir(parents=True)
    receipt = memory / "instruction-loads.jsonl"
    receipt.write_text("", encoding="utf-8")
    destination = rm._archive_destination(receipt)
    destination.write_text("", encoding="utf-8")

    assert destination in MODULE.receipt_sources(receipt), (
        f"verifier does not look where rotation writes: {destination}"
    )
