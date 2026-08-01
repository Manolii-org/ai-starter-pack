"""The hydration list in pr-assessment-reusable.yml must match what the runners read.

`pr-assessment-reusable.yml` hydrates a fixed list of runtime files into consumer
repos that do not ship their own. That list is a hand-maintained duplicate of paths
the four runner scripts hard-code. If a runner starts reading a new agent or skill
prompt and the list is not updated, hydration succeeds and the runner then dies on a
FileNotFoundError inside a consumer repo — far from the cause. These tests derive the
required set from the runners themselves so the two cannot drift apart.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REUSABLE = REPO_ROOT / ".github/workflows/pr-assessment-reusable.yml"
SCRIPTS = REPO_ROOT / "scripts"

# Pack-only: consumers receive `pr-assessment.yml` (a caller), never the
# reusable itself. copier.yml excludes this file from rendering, but a skip
# guard means an older instance that already has a copy degrades to a skip
# instead of a hard failure in someone else's CI.
pytestmark = pytest.mark.skipif(
    not REUSABLE.exists(),
    reason="pr-assessment-reusable.yml is pack-only; nothing to check in a consumer",
)

RUNNERS = [
    "run-pr-classifier.py",
    "run-specialists.py",
    "run-broad-agents.py",
    "run-judge.py",
]

# Jobs that execute a runner script and therefore need the runtime present.
# `sast` runs Semgrep/Bandit only and must stay un-hydrated.
RUNNER_JOBS = {"classify", "specialists", "broad-agents", "judge"}


def _workflow() -> dict:
    return yaml.safe_load(REUSABLE.read_text())


def _hydration_list() -> list[str]:
    env = _workflow().get("env", {})
    raw = env.get("ASSESSMENT_RUNTIME_FILES")
    assert raw, "ASSESSMENT_RUNTIME_FILES missing from workflow-level env"
    return raw.split()


def _literal_set(source: str, name: str) -> set[str]:
    """Return a module-level set/list literal by name, without importing."""
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name in targets:
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found")


def test_every_runner_is_hydrated() -> None:
    listed = _hydration_list()
    for runner in RUNNERS:
        assert f"scripts/{runner}" in listed, f"{runner} is not in the hydration list"


def test_classifier_agent_prompt_is_hydrated() -> None:
    listed = _hydration_list()
    assert ".claude/agents/pr-classifier.md" in listed


def test_every_broad_agent_prompt_is_hydrated() -> None:
    """run-broad-agents.py loads .claude/agents/<name>.md for each BROAD_AGENTS entry."""
    agents = _literal_set((SCRIPTS / "run-broad-agents.py").read_text(), "BROAD_AGENTS")
    listed = _hydration_list()
    missing = {a for a in agents if f".claude/agents/{a}.md" not in listed}
    assert not missing, f"broad agent prompts absent from hydration list: {sorted(missing)}"


def test_every_valid_skill_prompt_is_hydrated() -> None:
    """run-specialists.py loads .claude/skills/<name>/SKILL.md for each routed skill.

    The classifier's _VALID_SKILLS is the allowlist of what it can ever route,
    so it is the authoritative set the specialists job must be able to run.
    """
    skills = _literal_set((SCRIPTS / "run-pr-classifier.py").read_text(), "_VALID_SKILLS")
    listed = _hydration_list()
    missing = {s for s in skills if f".claude/skills/{s}/SKILL.md" not in listed}
    assert not missing, f"skill prompts absent from hydration list: {sorted(missing)}"


def test_hydration_list_has_no_dead_entries() -> None:
    """Every listed path must exist in the pack — hydration fails loudly otherwise."""
    for rel in _hydration_list():
        assert (REPO_ROOT / rel).exists(), f"{rel} is hydrated but not shipped by the pack"


@pytest.mark.parametrize("job", sorted(RUNNER_JOBS))
def test_runner_jobs_hydrate(job: str) -> None:
    steps = _workflow()["jobs"][job]["steps"]
    names = [str(s.get("name", "")) for s in steps]
    assert "Probe assessment runtime" in names, f"{job} does not probe for the runtime"
    assert "Hydrate assessment runtime" in names, f"{job} does not hydrate"


def test_sast_job_is_not_hydrated() -> None:
    """sast runs no runner; hydrating it would fetch the pack for nothing."""
    steps = _workflow()["jobs"]["sast"]["steps"]
    names = [str(s.get("name", "")) for s in steps]
    assert "Hydrate assessment runtime" not in names


def test_hydration_never_overwrites_caller_files() -> None:
    """Caller-owned copies must win, or a consumer's customised prompt is clobbered."""
    steps = _workflow()["jobs"]["classify"]["steps"]
    body = next(
        s["run"] for s in steps if str(s.get("name", "")) == "Hydrate assessment runtime"
    )
    assert "[[ -e \"$f\" ]] && continue" in body, (
        "hydration must skip files the caller already provides"
    )


def test_pack_checkout_is_conditional() -> None:
    """An unreachable pack_ref must not break repos that need no hydration."""
    steps = _workflow()["jobs"]["classify"]["steps"]
    checkout = next(
        s
        for s in steps
        if str(s.get("name", "")) == "Check out pack for hydration"
    )
    assert checkout.get("if") == "steps.hydrate_probe.outputs.needed == 'true'"


def test_pack_ref_input_exists_and_defaults_to_major_alias() -> None:
    inputs = _workflow()[True]["workflow_call"]["inputs"]
    assert "pack_ref" in inputs
    # The moving major alias is only safe because release-tag.yml moves it.
    assert inputs["pack_ref"]["default"] == "v1"
    release = (REPO_ROOT / ".github/workflows/release-tag.yml").read_text()
    assert "Move major-version alias" in release, (
        "pack_ref defaults to the v1 alias, so release-tag.yml must move it"
    )


def test_scratch_checkout_is_cleaned_up() -> None:
    """.pack-hydrate must not survive into later steps that walk the workspace."""
    for job in RUNNER_JOBS:
        steps = _workflow()["jobs"][job]["steps"]
        body = next(
            s["run"]
            for s in steps
            if str(s.get("name", "")) == "Hydrate assessment runtime"
        )
        assert re.search(r"rm -rf \.pack-hydrate", body), f"{job} leaves .pack-hydrate behind"
