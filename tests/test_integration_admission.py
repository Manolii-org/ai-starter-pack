"""Tests for the portable Integration Admission planner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/ci/integration_admission.py"
SPEC = importlib.util.spec_from_file_location("integration_admission", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def config() -> dict:
    return {
        "schema_version": 1,
        "required_context": "Integration Admission",
        "unknown_paths": "block",
        "global_invalidators": ["lock"],
        "surfaces": {
            "api": {
                "paths": ["api/**"], "commands": ["test-api"], "depends_on": ["schema"],
                "invalidates": [], "inputs": ["shared/**"], "lane": "node",
                "working_directory": ".",
            },
            "schema": {
                "paths": ["db/**"], "commands": ["test-db"], "depends_on": [],
                "invalidates": ["api"], "inputs": [], "lane": "database",
                "working_directory": ".",
            },
        },
    }


def write_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name, content in (("api/a.py", "x"), ("db/a.sql", "y"), ("shared/a", "z"), ("lock", "1")):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)


def test_dependency_and_invalidation_closure() -> None:
    affected, unknown = module.affected_surfaces(config(), ["db/new.sql"])
    assert affected == {"schema", "api"}
    assert unknown == []


def test_global_invalidator_selects_every_surface() -> None:
    affected, unknown = module.affected_surfaces(config(), ["lock"])
    assert affected == {"schema", "api"}
    assert unknown == []


def test_unknown_path_blocks() -> None:
    affected, unknown = module.affected_surfaces(config(), ["mystery/file"])
    assert affected == set()
    assert unknown == ["mystery/file"]


def test_unknown_path_can_conservatively_select_all() -> None:
    value = config()
    value["unknown_paths"] = "all"
    affected, unknown = module.affected_surfaces(value, ["mystery/file"])
    assert affected == {"schema", "api"}
    assert unknown == []


def test_config_rejects_unknown_surface_reference(tmp_path: Path) -> None:
    value = config()
    value["surfaces"]["api"]["depends_on"] = ["missing"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    with pytest.raises(module.AdmissionError, match="unknown surfaces"):
        module.load_config(path)


def test_config_rejects_dependency_cycle(tmp_path: Path) -> None:
    value = config()
    value["surfaces"]["schema"]["depends_on"] = ["api"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    with pytest.raises(module.AdmissionError, match="cycle"):
        module.load_config(path)


def test_exact_and_composed_evidence_are_distinct(tmp_path: Path) -> None:
    write_repo(tmp_path)
    value = config()
    contract = module.contract_digest(value)
    files = module.git_files(tmp_path)
    input_digest = module.surface_input_digest(tmp_path, value, "api", files)
    evidence = {
        "schema_version": 1, "repository": "o/r", "installation_id": "7",
        "tree_oid": "old", "test_contract_digest": contract, "producer_version": "v1",
        "surface": "api", "input_closure_digest": input_digest, "verdict": "executed_pass",
    }
    composed = module.validate_evidence(
        evidence, repository="o/r", installation_id="7", tree_oid="new", contract=contract,
        surface="api", input_digest=input_digest, accepted_producers={"v1"},
    )
    exact = module.validate_evidence(
        {**evidence, "tree_oid": "new"}, repository="o/r", installation_id="7",
        tree_oid="new", contract=contract, surface="api", input_digest=input_digest,
        accepted_producers={"v1"},
    )
    assert composed == module.EvidenceDecision(True, "composed input closure")
    assert exact == module.EvidenceDecision(True, "exact tree")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("repository", "other/r", "repository mismatch"),
        ("installation_id", "8", "installation mismatch"),
        ("test_contract_digest", "old", "test contract mismatch"),
        ("producer_version", "bad", "producer not accepted"),
        ("input_closure_digest", "old", "input closure mismatch"),
        ("verdict", "reused_exact_pass", "evidence is not an executed pass"),
    ],
)
def test_evidence_mismatch_fails_closed(field: str, value: str, reason: str) -> None:
    evidence = {
        "schema_version": 1, "repository": "o/r", "installation_id": "7",
        "tree_oid": "tree", "test_contract_digest": "contract", "producer_version": "v1",
        "surface": "api", "input_closure_digest": "input", "verdict": "executed_pass",
    }
    evidence[field] = value
    decision = module.validate_evidence(
        evidence, repository="o/r", installation_id="7", tree_oid="tree",
        contract="contract", surface="api", input_digest="input", accepted_producers={"v1"},
    )
    assert decision == module.EvidenceDecision(False, reason)


def test_plan_reuses_exact_evidence_and_executes_remaining_lane(tmp_path: Path) -> None:
    write_repo(tmp_path)
    value = config()
    files = module.git_files(tmp_path)
    evidence = [{
        "schema_version": 1, "repository": "o/r", "installation_id": "7",
        "tree_oid": "tree", "test_contract_digest": module.contract_digest(value),
        "producer_version": "v1", "surface": "api",
        "input_closure_digest": module.surface_input_digest(tmp_path, value, "api", files),
        "verdict": "executed_pass",
    }]
    plan = module.build_plan(
        tmp_path, value, ["db/a.sql"], evidence, repository="o/r", installation_id="7",
        tree_oid="tree", accepted_producers={"v1"},
    )
    assert plan["surfaces"]["api"]["verdict"] == "reused_exact_pass"
    assert plan["surfaces"]["schema"]["verdict"] == "execution_required"
    assert set(plan["lanes"]) == {"database"}


def test_input_change_invalidates_composed_evidence(tmp_path: Path) -> None:
    write_repo(tmp_path)
    value = config()
    files = module.git_files(tmp_path)
    evidence = {
        "schema_version": 1, "repository": "o/r", "installation_id": "7",
        "tree_oid": "old", "test_contract_digest": module.contract_digest(value),
        "producer_version": "v1", "surface": "api",
        "input_closure_digest": module.surface_input_digest(tmp_path, value, "api", files),
        "verdict": "executed_pass",
    }
    (tmp_path / "shared/a").write_text("changed")
    plan = module.build_plan(
        tmp_path, value, ["api/a.py"], [evidence], repository="o/r", installation_id="7",
        tree_oid="new", accepted_producers={"v1"},
    )
    assert plan["surfaces"]["api"]["verdict"] == "execution_required"
    assert "node" in plan["lanes"]


def test_dependency_change_invalidates_composed_evidence(tmp_path: Path) -> None:
    write_repo(tmp_path)
    value = config()
    files = module.git_files(tmp_path)
    evidence = {
        "schema_version": 1, "repository": "o/r", "installation_id": "7",
        "tree_oid": "old", "test_contract_digest": module.contract_digest(value),
        "producer_version": "v1", "surface": "api",
        "input_closure_digest": module.surface_input_digest(tmp_path, value, "api", files),
        "verdict": "executed_pass",
    }
    (tmp_path / "db/a.sql").write_text("changed")
    plan = module.build_plan(
        tmp_path, value, ["db/a.sql"], [evidence], repository="o/r", installation_id="7",
        tree_oid="new", accepted_producers={"v1"},
    )
    assert plan["surfaces"]["api"]["verdict"] == "execution_required"
    assert set(plan["lanes"]) == {"node", "database"}
