"""New shared-gate workflow/action files must parse and stay callable."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_migration_run_gate_reusable_parses() -> None:
    wf = ROOT / ".github/workflows/migration-run-gate-reusable.yml"
    text = wf.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        assert not line.startswith("**"), f"{wf.name}:{i} bare '**' breaks YAML"
    spec = yaml.safe_load(text)
    assert "jobs" in spec
    on = spec.get("on") or spec.get(True) or {}
    assert "workflow_call" in on
    inputs = on["workflow_call"]["inputs"]
    for required in ("db_image", "db_ports_json", "db_env_json", "migrations_env_json", "install_command"):
        assert inputs[required]["required"] is True


def test_check_guarded_paths_action_parses() -> None:
    action = ROOT / ".github/actions/check-guarded-paths/action.yml"
    spec = yaml.safe_load(action.read_text(encoding="utf-8"))
    assert spec["runs"]["using"] == "composite"
    assert (action.parent / "check.py").exists()


def test_schemas_parse_as_json() -> None:
    import json

    for name in ("automation-registry", "deployment-contract"):
        doc = json.loads(
            (ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8")
        )
        assert doc["type"] == "object"


def test_guards_domain_example_parses() -> None:
    import json

    doc = json.loads((ROOT / ".ai/guards.domain.example.json").read_text(encoding="utf-8"))
    ids = [g["id"] for g in doc["guards"]]
    assert len(ids) == len(set(ids)), "guard ids must be unique"
    assert "guards-config" in ids
