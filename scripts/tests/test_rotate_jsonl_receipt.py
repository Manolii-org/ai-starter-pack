import importlib.util
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "rotate-jsonl-receipt.py"
SPEC = importlib.util.spec_from_file_location("rotate_jsonl_receipt", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_same_second_archive_is_unique(tmp_path):
    source = tmp_path / "instruction-loads.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    first = MODULE._archive_destination(source)
    first.write_text("first\n", encoding="utf-8")
    second = MODULE._archive_destination(source)
    assert second != first


def test_default_path_uses_claude_project_dir(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin-install"
    consumer = tmp_path / "consumer"
    plugin_root.mkdir()
    receipt_dir = consumer / ".ai" / "memory"
    receipt_dir.mkdir(parents=True)
    source = receipt_dir / "instruction-loads.jsonl"
    source.write_text("x" * 64, encoding="utf-8")
    monkeypatch.delenv("INSTRUCTION_LOAD_AUDIT_PATH", raising=False)
    monkeypatch.delenv("INSTRUCTION_LOAD_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(consumer))
    monkeypatch.setattr(MODULE, "ROOT", plugin_root)
    assert MODULE.main(["--max-bytes", "8"]) == 0
    assert source.stat().st_size == 0
    archives = list((receipt_dir / "archive").rglob("instruction-loads.*.jsonl"))
    assert archives


def test_dry_run_does_not_move(tmp_path):
    source = tmp_path / "instruction-loads.jsonl"
    source.write_text("x" * 64, encoding="utf-8")
    assert MODULE.rotate_file(source, max_bytes=8, dry_run=True)
    assert source.exists()
    assert source.stat().st_size == 64
