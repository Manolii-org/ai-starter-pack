from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / ".github/actions/validate-database-migrations/validate.py"
spec = importlib.util.spec_from_file_location("migration_validator", VALIDATOR)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def test_drizzle_accepts_complete_journal(tmp_path):
    (tmp_path / "meta").mkdir()
    (tmp_path / "0000_first.sql").write_text("select 1;\n")
    (tmp_path / "meta/_journal.json").write_text(json.dumps({"entries": [
        {"idx": 0, "when": 1700000000000, "tag": "0000_first"},
    ]}))
    assert validator.validate_drizzle(tmp_path, None) == []


def test_drizzle_rejects_duplicate_prefix_and_unjournaled_file(tmp_path):
    (tmp_path / "meta").mkdir()
    (tmp_path / "0000_first.sql").write_text("select 1;\n")
    (tmp_path / "0000_second.sql").write_text("select 2;\n")
    (tmp_path / "meta/_journal.json").write_text(json.dumps({"entries": [
        {"idx": 0, "when": 1700000000000, "tag": "0000_first"},
    ]}))
    errors = validator.validate_drizzle(tmp_path, None)
    assert any("duplicate migration identifier 0000" in error for error in errors)
    assert any("0000_second.sql: missing" in error for error in errors)


def test_alembic_accepts_one_connected_head(tmp_path):
    (tmp_path / "one.py").write_text('revision = "one"\ndown_revision = None\n')
    (tmp_path / "two.py").write_text('revision = "two"\ndown_revision = "one"\n')
    assert validator.validate_alembic(tmp_path) == []


@pytest.mark.parametrize("second_parent", [None, "missing"])
def test_alembic_rejects_multiple_heads_or_missing_parent(tmp_path, second_parent):
    (tmp_path / "one.py").write_text('revision = "one"\ndown_revision = None\n')
    (tmp_path / "two.py").write_text(
        f"revision = \"two\"\ndown_revision = {second_parent!r}\n"
    )
    errors = validator.validate_alembic(tmp_path)
    expected = "expected one Alembic head" if second_parent is None else "missing Alembic parent"
    assert any(expected in error for error in errors)


def test_alembic_rejects_disconnected_cycle_hidden_behind_one_head(tmp_path):
    (tmp_path / "root.py").write_text('revision = "root"\ndown_revision = None\n')
    (tmp_path / "head.py").write_text('revision = "head"\ndown_revision = "root"\n')
    (tmp_path / "cycle_a.py").write_text('revision = "cycle_a"\ndown_revision = "cycle_b"\n')
    (tmp_path / "cycle_b.py").write_text('revision = "cycle_b"\ndown_revision = "cycle_a"\n')
    assert any("contains a cycle" in error for error in validator.validate_alembic(tmp_path))


def test_alembic_rejects_repeated_metadata_assignments(tmp_path):
    (tmp_path / "revision.py").write_text(
        'revision = "first"\nrevision = "effective"\ndown_revision = None\n'
    )
    assert any(
        "revision must be a non-empty string literal" in error
        for error in validator.validate_alembic(tmp_path)
    )


def test_alembic_allows_annotation_only_declarations_before_bindings(tmp_path):
    (tmp_path / "revision.py").write_text(
        'revision: str\nrevision = "one"\ndown_revision: str | None\ndown_revision = None\n'
    )
    assert validator.validate_alembic(tmp_path) == []


def test_supabase_accepts_legacy_and_timestamp_identifiers(tmp_path):
    (tmp_path / "00165_legacy.sql").write_text("select 1;\n")
    (tmp_path / "20260902120000_native.sql").write_text("select 2;\n")
    assert validator.validate_names(tmp_path, validator.SUPABASE_NAME) == []


def test_supabase_baseline_allows_only_exact_historical_duplicate_group(tmp_path):
    first = tmp_path / "00165_first.sql"
    second = tmp_path / "00165_second.sql"
    first.write_text("select 1;\n")
    second.write_text("select 2;\n")
    allowed = {"00165": [first.name, second.name]}
    assert validator.validate_names(tmp_path, validator.SUPABASE_NAME, allowed_duplicates=allowed) == []
    (tmp_path / "00165_third.sql").write_text("select 3;\n")
    assert any(
        "duplicate migration identifier 00165" in error
        for error in validator.validate_names(tmp_path, validator.SUPABASE_NAME, allowed_duplicates=allowed)
    )


def test_supabase_ignores_rollback_companions(tmp_path):
    (tmp_path / "00165_forward.sql").write_text("select 1;\n")
    (tmp_path / "00165_forward.rollback.sql").write_text("select 1;\n")
    assert validator.validate_names(
        tmp_path,
        validator.SUPABASE_NAME,
        ignored_suffixes=(".rollback.sql", ".down.sql"),
    ) == []


def test_prisma_requires_unique_timestamp_directories_with_sql(tmp_path):
    valid = tmp_path / "20260902120000_create_users"
    valid.mkdir()
    (valid / "migration.sql").write_text("select 1;\n")
    invalid = tmp_path / "manual_change"
    invalid.mkdir()
    errors = validator.validate_prisma(tmp_path)
    assert any("malformed Prisma migration directory" in error for error in errors)
    assert any("migration.sql is missing" in error for error in errors)


def test_composite_action_invokes_bundled_validator():
    action = (VALIDATOR.parent / "action.yml").read_text()
    assert 'python3 "${{ github.action_path }}/validate.py"' in action
    assert "--adapter" in action and "--path" in action


def test_cli_rejects_baseline_for_flyway(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"allowed_duplicate_identifiers": {}}))
    assert validator.main([
        "--adapter", "flyway", "--path", str(tmp_path), "--baseline", str(baseline),
    ]) == 1
    assert "supported only for the supabase adapter" in capsys.readouterr().err
