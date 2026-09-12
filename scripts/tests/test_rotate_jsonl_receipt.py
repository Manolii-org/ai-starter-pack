import importlib.util
import json
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


def test_dry_run_does_not_move(tmp_path):
    source = tmp_path / "instruction-loads.jsonl"
    source.write_text("x" * 64, encoding="utf-8")
    assert MODULE.rotate_file(source, max_bytes=8, dry_run=True)
    assert source.exists()
    assert source.stat().st_size == 64
