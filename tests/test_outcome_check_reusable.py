from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/outcome-check-reusable.yml"


def test_reusable_exposes_all_terminal_outcomes_and_neutral_holds() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for outcome in ("EXECUTED", "NO_CHANGES", "DEFERRED", "POLICY_HELD", "FAILED"):
        assert outcome in text
    assert "DEFERRED: 'neutral'" in text
    assert "POLICY_HELD: 'neutral'" in text
    assert "FAILED: 'failure'" in text


def test_reusable_has_bounded_permissions_and_immutable_action_pin() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"checks": "write", "contents": "read"}
    step = workflow["jobs"]["publish"]["steps"][0]
    assert step["uses"].startswith("actions/github-script@")
    assert len(step["uses"].split("@", 1)[1].split()[0]) == 40
