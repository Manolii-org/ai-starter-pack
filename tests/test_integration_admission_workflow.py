"""Structural safety tests for the portable Integration Admission reusable."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/integration-admission-reusable.yml"


def load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_one_stable_aggregate_and_runtime_lane_matrix() -> None:
    jobs = load()["jobs"]
    assert jobs["integration-admission"]["name"] == "Integration Admission"
    assert jobs["execute"]["strategy"]["matrix"]["item"]
    assert jobs["execute"]["if"].startswith("${{")


def test_candidate_config_is_never_the_command_authority() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'git show "${BASE_SHA}:${CONFIG_PATH}"' in text
    assert "trusted-config.json" in text
    assert "repository: Manolii-org/ai-starter-pack" in text
    assert "${{ inputs.pack_ref }}" in text
    assert "sparse-checkout-cone-mode: false" in text
    assert "bootstrap=true" in text
    assert "BOOTSTRAP" in text
    assert "full 40-character Git object IDs" in text
    assert "persist-credentials: false" in text


def test_deployment_events_and_secrets_are_absent() -> None:
    data = load()
    # PyYAML's YAML 1.1 resolver parses the unquoted `on` key as boolean true.
    assert set(data.get("on", data.get(True))) == {"workflow_call"}
    assert data["permissions"] == {"contents": "read"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "deployment" not in text.lower()
    assert "secrets." not in text


def test_shadow_does_not_hide_test_failure() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'if [ "$EXECUTE_RESULT" != "success" ]' in text
    assert "verdict=executed_fail" in text
