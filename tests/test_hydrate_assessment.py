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
    """Caller-owned copies must win on trusted triggers, or a consumer's
    customised prompt is clobbered. On pull_request the opposite holds:
    caller-shipped runtime files are attacker-controlled and hydration must
    overwrite them."""
    steps = _workflow()["jobs"]["classify"]["steps"]
    body = next(
        s["run"] for s in steps if str(s.get("name", "")) == "Hydrate assessment runtime"
    )
    assert '"$FORCE" != "true" && -e "$f"' in body, (
        "hydration must skip files the caller already provides on non-PR triggers"
    )
    assert '"${{ github.event_name }}" == "pull_request"' in body, (
        "hydration must force-overwrite runtime files on pull_request triggers"
    )


def test_probe_forces_hydration_on_pull_request() -> None:
    """The probe may not skip hydration when a PR ships the full runtime —
    that is exactly the caller-controlled-code attack the forced copy prevents."""
    steps = _workflow()["jobs"]["classify"]["steps"]
    body = next(
        s["run"] for s in steps if str(s.get("name", "")) == "Probe assessment runtime"
    )
    assert '"${{ github.event_name }}" == "pull_request"' in body
    assert body.index('"${{ github.event_name }}" == "pull_request"') < body.index(
        "elif [[ -n \"$missing\" ]]"
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


def test_protected_helper_and_sanitizer_are_hydrated_and_in_sparse_checkout():
    listed = _hydration_list()
    assert {"scripts/lib/protected_review.py", ".ai/security/token-shapes.json"} <= set(listed)
    for job in RUNNER_JOBS:
        checkout = next(s for s in _workflow()["jobs"][job]["steps"]
                        if s.get("name") == "Check out pack for hydration")
        sparse = checkout["with"]["sparse-checkout"].split()
        assert all(any(path == prefix or path.startswith(prefix + "/") for prefix in sparse)
                   for path in listed), f"{job}: sparse checkout omits a runtime dependency"


@pytest.mark.parametrize("job", sorted(RUNNER_JOBS))
@pytest.mark.parametrize("trigger", ["pull_request", "workflow_dispatch"])
def test_real_hydration_step_closes_dependencies_preserving_trusted_overrides(tmp_path, job, trigger):
    import hashlib
    import os
    import shutil
    import subprocess
    import sys
    listed = _hydration_list()
    scratch = tmp_path / ".pack-hydrate"
    for relative in listed:
        destination = scratch / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)
    helper = tmp_path / "scripts/lib/protected_review.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("# Existing trusted consumer override\n")
    steps = _workflow()["jobs"][job]["steps"]
    body = next(s["run"] for s in steps if s.get("name") == "Hydrate assessment runtime")
    body = body.replace("${{ github.event_name }}", trigger).replace("${{ inputs.pack_ref }}", "offline-frozen-source")
    env = {"PATH": os.defpath, "ASSESSMENT_RUNTIME_FILES": " ".join(listed)}
    subprocess.run(["bash", "-c", body], cwd=tmp_path, env=env, check=True,
                   capture_output=True, text=True, timeout=15)
    assert not scratch.exists()
    assert (tmp_path / ".ai/security/token-shapes.json").read_bytes() == (REPO_ROOT / ".ai/security/token-shapes.json").read_bytes()
    for relative in listed:
        if relative == "scripts/lib/protected_review.py" and trigger != "pull_request":
            assert helper.read_text() == "# Existing trusted consumer override\n"
        else:
            assert hashlib.sha256((tmp_path / relative).read_bytes()).digest() == hashlib.sha256((REPO_ROOT / relative).read_bytes()).digest()
    if trigger == "pull_request":
        # Load the real hydrated modules from their new consumer root. Definitions
        # only: no credentials, CLI invocation, model calls or workflow dispatch.
        probe = '''import importlib.util, pathlib, sys
for name in ("run-specialists.py", "run-broad-agents.py", "run-judge.py"):
    path=pathlib.Path("scripts")/name
    spec=importlib.util.spec_from_file_location(name.replace("-","_"),path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    exec(compile(path.read_bytes(),str(path),"exec"),module.__dict__)
    if name=="run-judge.py": module._require_safe_packet({"source":"ordinary synthetic input"})
'''
        subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env,
                       check=True, capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("job", sorted(RUNNER_JOBS))
def test_native_workflow_restores_sanitizer_with_trusted_runtime(job):
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/pr-assessment.yml").read_text())
    step = next(s for s in workflow["jobs"][job]["steps"]
                if s.get("name") == "Restore trusted runtime from PR base")
    assert "for p in .claude scripts .ai/security CLAUDE.md AGENTS.md; do" in step["run"]
    assert 'git checkout "$BASE_SHA" -- "$p"' in step["run"]
    assert 'rm -rf "$p"' in step["run"]


@pytest.mark.parametrize("job", sorted(RUNNER_JOBS))
@pytest.mark.parametrize(("component", "kind"), [
    (p, "symlink") for p in [".ai", ".ai/security", ".ai/security/token-shapes.json",
                            "scripts", "scripts/lib", "scripts/lib/protected_review.py"]
] + [(p, "file") for p in [".ai", ".ai/security", "scripts", "scripts/lib"]]
  + [(p, "directory") for p in [".ai/security/token-shapes.json", "scripts/lib/protected_review.py"]])
def test_hydration_refuses_redirected_new_dependency_without_touching_outside(tmp_path, job, component, kind):
    import os
    import subprocess
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("retain outside target unchanged")
    destination = workspace / component
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        destination.symlink_to(sentinel if component.endswith(".json") or component.endswith(".py") else outside)
    elif kind == "file":
        destination.write_text("parent must remain unchanged")
    else:
        destination.mkdir()
    body = next(s["run"] for s in _workflow()["jobs"][job]["steps"]
                if s.get("name") == "Hydrate assessment runtime")
    body = body.replace("${{ github.event_name }}", "pull_request").replace("${{ inputs.pack_ref }}", "offline")
    # Provide real source bytes at the new destinations. Without the refusal,
    # cp would reach the external target instead of merely failing on missing
    # scratch input, so the sentinel assertion exercises the actual risk.
    for relative in ["scripts/lib/protected_review.py", ".ai/security/token-shapes.json"]:
        source = workspace / ".pack-hydrate" / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("replacement source bytes")
    result = subprocess.run(["bash", "-c", body], cwd=workspace,
        env={"PATH": os.defpath, "ASSESSMENT_RUNTIME_FILES": ".ai/security/token-shapes.json scripts/lib/protected_review.py"},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 1 and "::error::Assessment dependency" in result.stderr
    assert sentinel.read_text() == "retain outside target unchanged"
    assert {p for p in outside.rglob("*") if p.is_file()} == {sentinel}


@pytest.mark.parametrize("job", sorted(RUNNER_JOBS))
@pytest.mark.parametrize("layout", ["symlink", "file"])
def test_native_nested_catalog_restore_refuses_unsafe_state_root(tmp_path, job, layout):
    import os
    import subprocess
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    (outside / "security").mkdir(parents=True)
    sentinel = outside / "security/sentinel"
    sentinel.write_text("retain outside subtree")
    if layout == "symlink":
        (workspace / ".ai").symlink_to(outside)
    else:
        (workspace / ".ai").write_text("not a directory")
    tools = tmp_path / "tools"
    tools.mkdir()
    # No network or Git authentication: execute the real restore body against
    # a controlled fake Git. Old rm -rf .ai/security deletes the external
    # subtree through the symlink; the corrected body exits before any Git.
    (tools / "git").write_text("#!/bin/sh\nif [ \"$1\" = cat-file ]; then exit 1; fi\nexit 0\n")
    (tools / "git").chmod(0o755)
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/pr-assessment.yml").read_text())
    body = next(s["run"] for s in workflow["jobs"][job]["steps"]
                if s.get("name") == "Restore trusted runtime from PR base")
    body = body.replace("${{ github.event.pull_request.base.sha }}", "a" * 40)
    result = subprocess.run(["bash", "-c", body], cwd=workspace,
        env={"PATH": str(tools) + os.pathsep + os.defpath}, capture_output=True,
        text=True, timeout=15)
    assert result.returncode == 1 and "Assessment state root" in result.stderr
    assert sentinel.read_text() == "retain outside subtree"


def test_native_judge_checkout_contains_exact_head_and_merge_base(tmp_path):
    """A shallow synthetic merge plus a base fetch omits the PR head object."""
    import shlex
    import subprocess

    def git(cwd, *args, check=True):
        return subprocess.run(["git", *args], cwd=cwd, check=check,
                              capture_output=True, text=True, timeout=15)

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    git(origin, "config", "user.name", "Offline fixture")
    git(origin, "config", "user.email", "fixture@example.invalid")
    (origin / "source.py").write_text("original = True\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "common ancestor")
    git(origin, "checkout", "-b", "feature")
    (origin / "source.py").write_text("corrected = True\n")
    git(origin, "commit", "-am", "PR head")
    head = git(origin, "rev-parse", "HEAD").stdout.strip()
    git(origin, "checkout", "main")
    (origin / "other.txt").write_text("base advanced\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "current base")
    base = git(origin, "rev-parse", "HEAD").stdout.strip()
    git(origin, "merge", "--no-ff", "feature", "-m", "synthetic PR merge")
    merge = git(origin, "rev-parse", "HEAD").stdout.strip()

    def checkout(name, depth, base_fetch):
        target = tmp_path / name
        target.mkdir()
        git(target, "init")
        git(target, "remote", "add", "origin", origin.as_uri())
        git(target, "fetch", *(["--depth", str(depth)] if depth else []), "origin", merge)
        git(target, "checkout", "--detach", "FETCH_HEAD")
        # The trusted-runtime restore fetch alone cannot supply the head.
        git(target, *base_fetch)
        return target

    shallow = checkout("old-default", 1, ["fetch", "--depth=1", "origin", base])
    assert git(shallow, "rev-parse", head + "^{tree}", check=False).returncode != 0
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/pr-assessment.yml").read_text())
    step = next(s for s in workflow["jobs"]["judge"]["steps"]
                if s.get("uses", "").startswith("actions/checkout@"))
    restore = next(s for s in workflow["jobs"]["judge"]["steps"]
                   if s.get("name") == "Restore trusted runtime from PR base")
    fetch_line = next(line.strip() for line in restore["run"].splitlines()
                      if line.strip().startswith("git fetch "))
    base_fetch = shlex.split(fetch_line.replace('"$BASE_SHA"', base))[1:]
    complete = checkout("configured-checkout", step.get("with", {}).get("fetch-depth", 1), base_fetch)
    assert git(complete, "rev-parse", head + "^{tree}").stdout.strip()
    assert git(complete, "show", head + ":source.py").stdout == "corrected = True\n"
    assert "+corrected = True" in git(complete, "diff", base + "..." + head).stdout
