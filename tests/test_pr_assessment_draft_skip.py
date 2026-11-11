"""Drafts qualify source inputs without invoking optional model review."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ("pr-assessment.yml", "pr-assessment-reusable.yml")
MODEL_JOBS = ("review-routing", "specialists", "broad-agents", "judge")


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def _truth(value):
    return not (value is None or value is False or value == "" or value == 0)


def _condition(expression, context):
    """Evaluate the actual YAML subset; unknown syntax fails the test."""
    expression = re.sub(r"\bneeds\['([A-Za-z0-9_-]+)'\]", r"needs.\1", expression)
    values = {}
    parts = re.split(r"('(?:[^']|'')*')", expression.strip())
    for i in range(0, len(parts), 2):
        def variable(match):
            name = "context_" + str(len(values))
            values[name] = context.get(match.group(), "")
            return name
        text = re.sub(r"\b(?:github|needs|inputs)\.[A-Za-z0-9_.*-]+", variable, parts[i])
        text = text.replace("&&", " and ").replace("||", " or ")
        text = re.sub(r"!(?!=)", " not ", text)
        for old, new in (("true", "True"), ("false", "False"), ("null", "None")):
            text = re.sub(r"\b" + old + r"\b", new, text)
        parts[i] = text

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not _truth(visit(node.operand))
        if isinstance(node, ast.BoolOp):
            for item in node.values:
                value = visit(item)
                if isinstance(node.op, ast.And) and not _truth(value):
                    return value
                if isinstance(node.op, ast.Or) and _truth(value):
                    return value
            return value
        if isinstance(node, ast.Compare):
            assert len(node.ops) == len(node.comparators) == 1
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(left, str) and isinstance(right, str):
                left, right = left.casefold(), right.casefold()
            assert isinstance(node.ops[0], (ast.Eq, ast.NotEq))
            return left == right if isinstance(node.ops[0], ast.Eq) else left != right
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name) and not node.keywords
            args = [visit(arg) for arg in node.args]
            if node.func.id == "always":
                assert args == []
                return True
            if node.func.id == "cancelled":
                assert args == []
                return context.get("_cancelled", False)
            if node.func.id == "contains":
                haystack, needle = args
                if isinstance(haystack, list):
                    return str(needle).casefold() in [str(v).casefold() for v in haystack]
                return str(needle).casefold() in str(haystack).casefold()
            if node.func.id == "startsWith":
                return str(args[0]).casefold().startswith(str(args[1]).casefold())
        raise AssertionError(ast.dump(node))

    return _truth(visit(ast.parse("(" + "".join(parts) + ")", mode="eval").body))


def _context(**changes):
    result = {
        "github.event_name": "pull_request",
        "github.event.pull_request.draft": False,
        "github.event.pull_request.labels.*.name": [],
        "github.event.pull_request.title": "Source repair",
        "github.head_ref": "codex/source-repair",
        "github.repository": "example/fixture",
        "github.event.pull_request.head.repo.full_name": "example/fixture",
        "github.event.pull_request.user.id": 1,
        "inputs.trusted_sync_author_id": 2,
        "needs.classify.result": "success",
        "needs.classify.outputs.sast_required": "true",
        "needs.classify.outputs.diff_files": "1",
        "needs.review-routing.result": "success",
        "needs.review-routing.outputs.diff_files": "1",
        "needs.review-routing.outputs.depth": "broad",
        "needs.review-routing.outputs.invoke_skills": "scope-adherence",
        "needs.sast.result": "success",
    }
    result.update(changes)
    return result


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_drafts_qualify_source_without_credentials_or_model_jobs(workflow):
    jobs = _workflow(workflow)["jobs"]
    assert "review-routing" in jobs
    context = _context(**{"github.event.pull_request.draft": True,
                         "github.event.pull_request.labels.*.name": ["skip-ai-review"],
                         "github.event.pull_request.title": "chore(sync): source repair"})
    assert _condition(jobs["classify"]["if"], context)
    assert _condition(jobs["sast"]["if"], context)
    assert all(not _condition(jobs[name]["if"], context) for name in MODEL_JOBS)
    cheap = jobs["classify"]
    assert cheap["permissions"] == {"contents": "read"}
    assert jobs["sast"]["permissions"] == {"contents": "read"}
    serialized = json.dumps(cheap)
    assert not re.search(r"\$\{\{[^}]*\bsecrets\.", serialized)
    assert all(token not in serialized for token in
               ("ANTHROPIC_", "LITELLM_", "Validate provider secrets",
                "Post merge danger", "Retire stale merge danger"))


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_classify_failure_or_skip_blocks_sast_and_all_optional_models(workflow, result):
    jobs = _workflow(workflow)["jobs"]
    assert "review-routing" in jobs
    # Stale positive outputs cannot override the failed job result.
    context = _context(**{"needs.classify.result": result})
    assert all(not _condition(jobs[name]["if"], context) for name in ("sast", *MODEL_JOBS))


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_optional_route_failure_blocks_models_without_blocking_source_scan(workflow, result):
    jobs = _workflow(workflow)["jobs"]
    context = _context(**{"needs.review-routing.result": result})
    assert _condition(jobs["sast"]["if"], context)
    assert all(not _condition(jobs[name]["if"], context)
               for name in ("specialists", "broad-agents", "judge"))


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("required", ["false", ""])
def test_sast_requires_actual_deterministic_selection(workflow, required):
    jobs = _workflow(workflow)["jobs"]
    assert not _condition(jobs["sast"]["if"],
                          _context(**{"needs.classify.outputs.sast_required": required}))


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_only_pull_request_context_runs_source_classification(workflow):
    spec = _workflow(workflow)
    assert not _condition(spec["jobs"]["classify"]["if"],
                          _context(**{"github.event_name": "workflow_dispatch"}))
    if workflow == "pr-assessment.yml":
        pr = (spec.get("on") or spec.get(True))["pull_request"]
        assert {"ready_for_review", "converted_to_draft", "edited"} <= set(pr["types"])
        assert not pr.get("paths") and not pr.get("paths-ignore")


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_models_keep_explicit_draft_and_both_classification_guards(workflow):
    jobs = _workflow(workflow)["jobs"]
    for name in MODEL_JOBS:
        condition = jobs[name]["if"]
        assert "github.event.pull_request.draft != true" in condition
        assert "needs.classify.result == 'success'" in condition
        if name != "review-routing":
            assert "needs.review-routing.result == 'success'" in condition
            assert "review-routing" in jobs[name]["needs"]
    assert jobs["sast"]["needs"] == ["classify"]


def _source_step(workflow):
    return next(step for step in _workflow(workflow)["jobs"]["classify"]["steps"]
                if step.get("name") == "Classify source inputs without a model")


def _literal_manifests():
    module = ast.parse((ROOT / "scripts/run-pr-classifier.py").read_text())
    return {target.id: ast.literal_eval(node.value)
            for node in module.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
            and target.id in {"_FALLBACK_MANIFEST", "_EMPTY_DIFF_MANIFEST"}}


def _run_source_step(tmp_path, workflow, diff, count, *, dynamic=False, shadow=None):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    manifests = _literal_manifests()
    assignments = []
    for name, value in manifests.items():
        expression = repr(value)
        if dynamic and name == "_FALLBACK_MANIFEST":
            expression = "dict(" + expression + ")"
        assignments.append(name + " = " + expression)
    source = "\n".join(assignments)
    # Importing/executing this source would cross the model-code boundary.
    source += "\nfrom pathlib import Path\nPath('MODEL_EXECUTED').touch()\nraise RuntimeError('model invoked')\n"
    (scripts / "run-pr-classifier.py").write_text(source)
    path = tmp_path / "pr.diff"
    if diff is not None:
        path.write_text(diff)
    output = tmp_path / "outputs"
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env.update(GITHUB_OUTPUT=str(output), DIFF_FILES=count)
    if shadow:
        kind, name = shadow
        directory = tmp_path if kind == "cwd" else tmp_path / "injected-module-path"
        directory.mkdir(exist_ok=True)
        (directory / (name + ".py")).write_text(
            "from pathlib import Path\nPath('UNTRUSTED_IMPORT').touch()\n"
            "raise RuntimeError('untrusted module executed')\n")
        if kind == "pythonpath":
            env["PYTHONPATH"] = str(directory)
    command = _source_step(workflow)["run"].replace("/tmp/pr.diff", str(path))
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert not (tmp_path / "MODEL_EXECUTED").exists()
    values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return result, values, manifests


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("diff,count,depth,scan", [
    ("diff --git a/app.py b/app.py\n+changed\n", "1", "broad", "true"),
    ("diff --git a/README.md b/README.md\n+unknown input\n", "1", "broad", "true"),
    ("diff --git a/image.bin b/image.bin\nBinary files differ\n", "1", "broad", "true"),
    ("diff --git a/script b/script\nold mode 100644\nnew mode 100755\n", "1", "broad", "true"),
    ("", "0", "narrow", "false"),
])
def test_actual_deterministic_step_never_executes_model_source(
        tmp_path, workflow, diff, count, depth, scan):
    result, values, manifests = _run_source_step(tmp_path, workflow, diff, count)
    assert result.returncode == 0, result.stderr
    assert values["depth"] == depth and values["sast_required"] == scan
    expected = manifests["_FALLBACK_MANIFEST" if scan == "true" else "_EMPTY_DIFF_MANIFEST"]
    assert values["invoke_skills"] == ",".join(expected["invoke_skills"])
    assert values["invoke_agents"] == ",".join(expected["invoke_agents"])
    assert json.loads(values["merge_danger"]) == {
        "door": "unknown", "blast_radius": "unknown", "danger_reason": ""}


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("diff,count", [(None, "1"), ("changed", ""), ("changed", "-1"),
                                        ("changed", "0"), ("", "1")])
def test_missing_or_inconsistent_source_inputs_fail_before_outputs(tmp_path, workflow, diff, count):
    result, values, _ = _run_source_step(tmp_path, workflow, diff, count)
    assert result.returncode != 0 and values == {}


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_dynamic_manifest_definitions_are_not_executed(tmp_path, workflow):
    result, values, _ = _run_source_step(tmp_path, workflow, "changed", "1", dynamic=True)
    assert result.returncode != 0 and values == {}


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("job", ["classify", "review-routing"])
@pytest.mark.parametrize("phase", ["diff", "inventory"])
def test_diff_inventory_failure_cannot_publish_empty_source_success(
        tmp_path, workflow, job, phase):
    step = next(item for item in _workflow(workflow)["jobs"][job]["steps"]
                if item.get("name") == "Get PR diff")
    git = tmp_path / "git"
    git.write_text("#!/usr/bin/env python3\nimport os,sys\n"
                   "inventory = '--name-only' in sys.argv\n"
                   "if inventory == (os.environ['FAIL_PHASE'] == 'inventory'): raise SystemExit(13)\n")
    git.chmod(0o755)
    output = tmp_path / "outputs"
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env.update(PATH=str(tmp_path) + os.pathsep + env["PATH"],
               PR_BASE_REF="main", BASE_REF="main", FAIL_PHASE=phase,
               GITHUB_OUTPUT=str(output))
    command = step["run"].replace("/tmp/pr.diff", str(tmp_path / "pr.diff"))
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("job", ["classify", "review-routing"])
def test_diff_inventory_outputs_support_native_paths_with_spaces(tmp_path, workflow, job):
    step = next(item for item in _workflow(workflow)["jobs"][job]["steps"]
                if item.get("name") == "Get PR diff")
    git = tmp_path / "git"
    git.write_text("#!/usr/bin/env python3\nprint('fixture.py')\n")
    git.chmod(0o755)
    output = tmp_path / "outputs with spaces"
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env.update(PATH=str(tmp_path) + os.pathsep + env["PATH"],
               PR_BASE_REF="main", BASE_REF="main", GITHUB_OUTPUT=str(output))
    command = step["run"].replace("/tmp/pr.diff", str(tmp_path / "pr.diff"))
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "lines": "1", "files": "1"}


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("selected,sast,rejected", [
    ("true", "skipped", True), ("true", "success", False),
    ("true", "failure", True), ("true", "cancelled", True),
    ("false", "failure", True), ("false", "cancelled", True),
])
def test_judge_refuses_missing_selected_sast_before_model_execution(
        tmp_path, workflow, selected, sast, rejected):
    judge = _workflow(workflow)["jobs"]["judge"]
    # Narrow routing with no invoked specialists must not hide a missing scan.
    context = _context(**{"needs.classify.outputs.sast_required": selected,
                         "needs.review-routing.outputs.depth": "narrow",
                         "needs.review-routing.outputs.invoke_skills": "",
                         "needs.specialists.result": "skipped",
                         "needs.broad-agents.result": "skipped",
                         "needs.sast.result": sast})
    assert _condition(judge["if"], context)
    guard = next(step for step in judge["steps"]
                 if step.get("name") == "Fail when an upstream assessment stage failed")
    model = next(step for step in judge["steps"] if step.get("name") == "Run judge")
    assert judge["steps"].index(guard) < judge["steps"].index(model)
    assert model.get("if") is None  # Native default requires prior step success.
    assert _condition(guard["if"], context) is rejected
    if rejected:
        marker = tmp_path / "JUDGE_STARTED"
        result = subprocess.run(["bash", "-e", "-c", guard["run"] + "\ntouch JUDGE_STARTED\n"],
                                cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0 and not marker.exists()


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_intentional_empty_diff_sast_skip_retains_metadata_review(workflow):
    judge = _workflow(workflow)["jobs"]["judge"]
    context = _context(**{"needs.classify.outputs.sast_required": "false",
                         "needs.classify.outputs.diff_files": "0",
                         "needs.review-routing.outputs.diff_files": "0",
                         "needs.review-routing.outputs.depth": "narrow",
                         "needs.review-routing.outputs.invoke_skills": "scope-adherence",
                         "needs.specialists.result": "success",
                         "needs.broad-agents.result": "skipped",
                         "needs.sast.result": "skipped"})
    assert _condition(judge["if"], context)
    guard = next(step for step in judge["steps"]
                 if step.get("name") == "Fail when an upstream assessment stage failed")
    assert not _condition(guard["if"], context)


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("selected,source_files,routing_files,sast,allowed", [
    ("true", "1", "0", "skipped", False),
    ("true", "1", "0", "failure", False),
    ("true", "1", "0", "cancelled", False),
    ("true", "1", "0", "success", True),
    ("false", "0", "0", "skipped", True),
    ("false", "0", "0", "failure", False),
    ("false", "0", "0", "cancelled", False),
    ("false", "1", "0", "skipped", False),
    ("", "", "0", "skipped", False),
    ("false", "0", "1", "skipped", False),
])
def test_specialists_cannot_use_optional_routing_to_bypass_required_source_scan(
        workflow, selected, source_files, routing_files, sast, allowed):
    job = _workflow(workflow)["jobs"]["specialists"]
    context = _context(**{"needs.classify.outputs.sast_required": selected,
                         "needs.classify.outputs.diff_files": source_files,
                         "needs.review-routing.outputs.diff_files": routing_files,
                         "needs.review-routing.outputs.depth": "narrow",
                         "needs.review-routing.outputs.invoke_skills": "scope-adherence",
                         "needs.sast.result": sast})
    assert _condition(job["if"], context) is allowed


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("shadow", [("cwd", "ast"), ("cwd", "json"),
                                    ("pythonpath", "ast"), ("pythonpath", "json")])
def test_deterministic_classifier_isolated_from_root_and_pythonpath_modules(
        tmp_path, workflow, shadow):
    result, values, _ = _run_source_step(tmp_path, workflow, "changed", "1", shadow=shadow)
    assert result.returncode == 0, result.stderr
    assert values["sast_required"] == "true"
    assert not (tmp_path / "UNTRUSTED_IMPORT").exists()


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_cheap_classification_uses_only_one_isolated_stdlib_interpreter(workflow):
    job = _workflow(workflow)["jobs"]["classify"]
    python_steps = [step for step in job["steps"]
                    if re.search(r"(?m)^\s*python3\b", step.get("run", ""))]
    assert [step["name"] for step in python_steps] == ["Classify source inputs without a model"]
    assert _source_step(workflow)["run"].startswith("python3 -I <<'PY'")


def test_cheap_reusable_probe_never_imports_root_yaml(tmp_path):
    step = next(item for item in _workflow("pr-assessment-reusable.yml")["jobs"]["classify"]["steps"]
                if item.get("name") == "Probe assessment runtime")
    # The successful shadow import prevents any old fallback pip execution.
    (tmp_path / "yaml.py").write_text(
        "from pathlib import Path\nPath('UNTRUSTED_YAML_IMPORT').touch()\n")
    output = tmp_path / "outputs"
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env.update(ASSESSMENT_EVENT="pull_request", ASSESSMENT_RUNTIME_FILES="",
               GITHUB_OUTPUT=str(output))
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert output.read_text().strip() == "needed=true"
    assert not (tmp_path / "UNTRUSTED_YAML_IMPORT").exists()


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("selected,count,routing_count,sast,refuse", [
    ("", "1", "0", "skipped", True),
    ("unknown", "1", "0", "skipped", True),
    ("false", "1", "0", "skipped", True),
    ("false", "0", "", "skipped", True),
    ("false", "0", "1", "skipped", True),
    ("false", "0", "0", "skipped", False),
    ("", "", "", "success", False),
])
def test_judge_accepts_only_success_or_exact_designed_empty_sast(
        workflow, selected, count, routing_count, sast, refuse):
    judge = _workflow(workflow)["jobs"]["judge"]
    context = _context(**{"needs.classify.outputs.sast_required": selected,
                         "needs.classify.outputs.diff_files": count,
                         "needs.review-routing.outputs.diff_files": routing_count,
                         "needs.review-routing.outputs.depth": "narrow",
                         "needs.review-routing.outputs.invoke_skills": "",
                         "needs.specialists.result": "skipped",
                         "needs.broad-agents.result": "skipped",
                         "needs.sast.result": sast})
    guard = next(step for step in judge["steps"]
                 if step.get("name") == "Fail when an upstream assessment stage failed")
    assert _condition(judge["if"], context)
    assert _condition(guard["if"], context) is refuse
