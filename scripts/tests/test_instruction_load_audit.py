import hashlib
import importlib.util
import json
import sys
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "instruction-load-audit.py"
SPEC = importlib.util.spec_from_file_location("instruction_load_audit", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_build_receipt_hashes_the_bytes_observed_by_the_handler(tmp_path):
    instruction = tmp_path / "CLAUDE.md"
    instruction.write_text("reviewed instruction\n", encoding="utf-8")
    receipt = MODULE.build_receipt(
        {
            "session_id": "session-1",
            "file_path": str(instruction),
            "memory_type": "Project",
            "load_reason": "session_start",
        },
        root=tmp_path,
    )
    assert receipt["file_path"] == "CLAUDE.md"
    assert receipt["observed_sha256"] == hashlib.sha256(instruction.read_bytes()).hexdigest()
    assert receipt["load_reason"] == "session_start"


def test_main_appends_a_bounded_receipt(tmp_path, monkeypatch):
    instruction = tmp_path / "rules.md"
    instruction.write_text("do the safe thing\n", encoding="utf-8")
    destination = tmp_path / "receipt.jsonl"
    monkeypatch.setenv("INSTRUCTION_LOAD_AUDIT_PATH", str(destination))
    monkeypatch.setenv("INSTRUCTION_LOAD_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(
            json.dumps(
                {
                    "hook_event_name": "InstructionsLoaded",
                    "session_id": "session-2",
                    "file_path": str(instruction),
                    "memory_type": "Project",
                    "load_reason": "include",
                    "parent_file_path": "CLAUDE.md",
                }
            )
        ),
    )
    assert MODULE.main([]) == 0
    stored = json.loads(destination.read_text(encoding="utf-8"))
    assert stored["session_id"] == "session-2"
    assert stored["observed_sha256"]
    assert "do the safe thing" not in destination.read_text(encoding="utf-8")


def test_main_ignores_other_hook_events(tmp_path, monkeypatch):
    destination = tmp_path / "receipt.jsonl"
    monkeypatch.setenv("INSTRUCTION_LOAD_AUDIT_PATH", str(destination))
    monkeypatch.setenv("INSTRUCTION_LOAD_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps({"hook_event_name": "SessionStart"})),
    )
    assert MODULE.main([]) == 0
    assert not destination.exists()


def test_dry_run_prints_receipt_without_writing(tmp_path, monkeypatch, capsys):
    instruction = tmp_path / "CLAUDE.md"
    instruction.write_text("reviewed\n", encoding="utf-8")
    destination = tmp_path / "receipt.jsonl"
    monkeypatch.setenv("INSTRUCTION_LOAD_AUDIT_PATH", str(destination))
    monkeypatch.setenv("INSTRUCTION_LOAD_ROOT", str(tmp_path))
    monkeypatch.setattr(
        sys, "stdin", __import__("io").StringIO(json.dumps({
            "hook_event_name": "InstructionsLoaded", "file_path": str(instruction),
        })),
    )
    assert MODULE.main(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["observed_sha256"]
    assert not destination.exists()


def test_external_paths_are_sanitised(tmp_path):
    rendered = MODULE.sanitise_path("/private/customer/project/CLAUDE.md", root=tmp_path)
    assert rendered.startswith("<external:")
    assert rendered.endswith(">/CLAUDE.md")
    assert "/private/customer" not in rendered


def test_out_of_repo_files_are_not_hashed(tmp_path):
    outside = tmp_path / "secret.txt"
    instruction_root = tmp_path / "repo"
    instruction_root.mkdir()
    outside.write_text("customer-secret\n", encoding="utf-8")
    receipt = MODULE.build_receipt(
        {"file_path": str(outside), "hook_event_name": "InstructionsLoaded"},
        root=instruction_root,
    )
    assert receipt["observed_sha256"] is None
    assert "customer-secret" not in json.dumps(receipt)


def test_main_ignores_non_object_payloads(tmp_path, monkeypatch):
    destination = tmp_path / "receipt.jsonl"
    monkeypatch.setenv("INSTRUCTION_LOAD_AUDIT_PATH", str(destination))
    monkeypatch.setenv("INSTRUCTION_LOAD_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("[]"))
    assert MODULE.main([]) == 0
    assert not destination.exists()


def test_main_waits_for_sidecar_lock(tmp_path, monkeypatch):
    import fcntl
    import multiprocessing
    import time

    instruction = tmp_path / "CLAUDE.md"
    instruction.write_text("observed\n", encoding="utf-8")
    destination = tmp_path / "instruction-loads.jsonl"
    lock_path = destination.with_name(destination.name + ".lock")
    monkeypatch.setenv("INSTRUCTION_LOAD_ROOT", str(tmp_path))
    payload = json.dumps({
        "hook_event_name": "InstructionsLoaded",
        "session_id": "lock-test",
        "file_path": str(instruction),
        "load_reason": "session_start",
    })
    monkeypatch.setenv("INSTRUCTION_LOAD_AUDIT_PATH", str(destination))
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        process = multiprocessing.get_context("spawn").Process(
            target=_append_from_child,
            args=(str(PATH), str(destination), payload, str(tmp_path)),
        )
        process.start()
        time.sleep(0.15)
        assert process.is_alive(), "appender did not wait for the sidecar lock"
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    process.join(timeout=5)
    assert process.exitcode == 0
    stored = json.loads(destination.read_text(encoding="utf-8"))
    assert stored["session_id"] == "lock-test"


def _append_from_child(script_path: str, destination: str, payload: str, root: str = "") -> None:
    import importlib.util
    import io
    import os
    import sys

    os.environ["INSTRUCTION_LOAD_AUDIT_PATH"] = destination
    if root:
        os.environ["INSTRUCTION_LOAD_ROOT"] = root
    spec = importlib.util.spec_from_file_location("instruction_load_audit_child", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    sys.stdin = io.StringIO(payload)
    raise SystemExit(module.main([]))
