"""RULE 1 must not route every workflow change to systems-consistency.

Regression (#252): CI-only workflow PRs ran systems-consistency without any
deployment files, so every check was "could not be evaluated"; the Judge then
truncated adjudicating those notes and the PR got "assessment error".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = [ROOT / ".claude/agents/pr-classifier.md",
          ROOT / "plugin/manolii-framework/agents/pr-classifier.md",
          ROOT / "registry/platform/framework/agents/pr-classifier.md"]


def _rule1(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^RULE 1:.*?(?=^RULE 2:)", text, re.M | re.S)
    assert match, f"RULE 1 not found in {path}"
    return match.group(0)


@pytest.mark.parametrize("path", [p for p in COPIES if p.exists()], ids=lambda p: str(p.relative_to(ROOT)))
def test_rule1_requires_a_deploy_step_for_workflow_changes(path: Path) -> None:
    rule = _rule1(path)
    first_line = rule.splitlines()[0]
    rule = " ".join(rule.split())  # phrase checks must not depend on line wrapping
    assert ".github/workflows" not in first_line, "workflow paths alone must not trigger RULE 1"
    assert "Skip RULE 1 only for a workflow that clearly does not deploy" in rule
    # Codex review: deploys through a reusable workflow or action count, and so
    # do edits to an existing deploy workflow's triggers or conditions.
    assert "`uses:` of an action or reusable workflow that" in rule
    assert "triggers, conditions, env or secrets" in rule
    assert "When unsure" in rule and "apply RULE 1" in rule
    assert 'add "systems-consistency"' in rule
    # CodeRabbit review: RULE 1 must narrow routing, never widen it (a root
    # fly.toml was not routed before this change and still is not).
    assert first_line.endswith("deploy/**/*.{sh,toml,yaml}")
    # Codex review: installing flyctl deploys nothing.
    assert "Installing a CLI" in rule and "is not a deploy step" in rule


def test_agent_trigger_description_matches_rule1() -> None:
    text = (ROOT / ".claude/agents/systems-consistency.md").read_text(encoding="utf-8")
    assert "A workflow that clearly does not deploy does not trigger it." in text
    assert "`.github/workflows/**`, or `config.yaml`" not in text


def _classifier():
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_pr_classifier", ROOT / "scripts/run-pr-classifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_floor_reads_the_whole_workflow_not_just_the_hunk(tmp_path: Path) -> None:
    """CodeRabbit review: a CI-looking hunk in a workflow that deploys elsewhere."""
    cls = _classifier()
    wf = tmp_path / ".github/workflows/release.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("jobs:\n  test:\n    steps:\n      - run: npm test\n"
                  "  ship:\n    steps:\n      - run: flyctl deploy --remote-only\n", encoding="utf-8")
    hunk = "@@ -4 +4 @@\n-      - run: npm test\n+      - run: npm test -- --ci\n"
    found = cls._deploying_workflows({".github/workflows/release.yml": [hunk]}, tmp_path)
    assert found == [".github/workflows/release.yml"]
    manifest = cls._apply_deploy_floor({"invoke_skills": [], "invoke_agents": [], "depth": "narrow",
                                        "reason": "ci only"}, found)
    assert manifest["invoke_agents"] == ["systems-consistency"] and manifest["depth"] == "broad"


@pytest.mark.parametrize("body", [
    "- uses: ./.github/workflows/deploy-reusable.yml\n",   # deploy via reusable workflow
    "- run: fly secrets set FOO=bar\n",                     # changes a running app
    "- run: ./scripts/deploy.sh\n",
    # Codex review: config applied to a running service, no "deploy" word.
    "- run: kubectl apply -f k8s/\n",
    "- run: helm upgrade --install app ./chart\n",
    "- run: terraform apply -auto-approve\n",
    "- run: az containerapp update -n app\n",
    # Codex review: global options before the operation.
    "- run: terraform -chdir=infra apply\n",
    "- run: aws --region us-east-1 ecs update-service --cluster c\n",
    "- run: kubectl --context prod apply -f k8s/\n",
])
def test_floor_catches_every_deploy_shape(tmp_path: Path, body: str) -> None:
    cls = _classifier()
    found = cls._deploying_workflows({".github/workflows/x.yml": ["+      " + body]}, tmp_path)
    assert found == [".github/workflows/x.yml"]


def test_floor_catches_a_removed_deploy_step(tmp_path: Path) -> None:
    cls = _classifier()
    found = cls._deploying_workflows({".github/workflows/x.yml": ["-      - run: fly deploy\n"]}, tmp_path)
    assert found == [".github/workflows/x.yml"]


def test_floor_catches_a_deploy_workflow_renamed_away(tmp_path: Path) -> None:
    # Pure rename: no hunk lines, and nothing left at the old path.
    cls = _classifier()
    (tmp_path / "disabled").mkdir()
    (tmp_path / "disabled/deploy.yml").write_text("jobs:\n  d:\n    steps:\n      - run: fly deploy\n")
    section = (" a/.github/workflows/deploy.yml b/disabled/deploy.yml\nsimilarity index 100%\n"
               "rename from .github/workflows/deploy.yml\nrename to disabled/deploy.yml\n")
    found = cls._deploying_workflows({".github/workflows/deploy.yml": [section],
                                      "disabled/deploy.yml": [section]}, tmp_path)
    assert found == [".github/workflows/deploy.yml"]


def test_floor_leaves_ci_only_and_non_workflow_changes_alone(tmp_path: Path) -> None:
    cls = _classifier()
    wf = tmp_path / ".github/workflows/scan.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("# deploy is mentioned only in a comment\njobs:\n  scan:\n"
                  "    steps:\n      - uses: superfly/flyctl-actions/setup-flyctl@v1\n"
                  "      - run: gitleaks detect\n", encoding="utf-8")
    sections = {".github/workflows/scan.yml": ["+      - run: gitleaks detect\n"],
                "docs/deploy.md": ["+fly deploy\n"]}
    assert cls._deploying_workflows(sections, tmp_path) == []
    manifest = {"invoke_skills": [], "invoke_agents": ["architecture-impact"], "depth": "broad", "reason": "r"}
    assert cls._apply_deploy_floor(manifest, []) is manifest


def test_the_real_ci_only_workflow_is_not_floored() -> None:
    """The #271 case: secret-scan-reusable.yml must not force the agent."""
    cls = _classifier()
    path = ".github/workflows/secret-scan-reusable.yml"
    assert cls._deploying_workflows({path: ["+      - run: echo audit\n"]}, ROOT) == []
