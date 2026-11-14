"""Publish immutable annotated tags without moving retained legacy readers."""

from __future__ import annotations

import os
import re
import json
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/release-tag.yml").read_text(encoding="utf-8"))
STEPS = {s.get("name"): s for s in WORKFLOW["jobs"]["tag"]["steps"]}


def _validate(tag: str) -> int:
    script = STEPS["Validate tag format"]["run"]
    return subprocess.run(["bash", "-c", script], env={"PATH": os.defpath, "TAG": tag},
                          capture_output=True, text=True).returncode


@pytest.mark.parametrize("tag", [
    "v1.23.0", "v2.0.0", "email-capture-v0.2.6", "feature-controls-v0.1.0",
    "feature-controls-v0.2.0-rc.1", "email-capture-v1.0.0-beta.2", "v10.0.0",
    "feature-controls-v1.0.0-rc.0",
])
def test_accepts_pack_and_component_tags(tag: str) -> None:
    assert _validate(tag) == 0


@pytest.mark.parametrize("tag", [
    "v1", "v1.23", "1.23.0", "v1.23.0-rc.1", "email-capture-0.2.6", "other-v1.0.0",
    "email-capture-v0.2", "feature-controls-v0.1.0-dev.1", "v1.23.0\nv2", "",
    # Non-canonical numbers (Codex review, PR #237): the component release
    # tooling's semver regex rejects leading zeroes, so the tag must too.
    "feature-controls-v01.2.3", "feature-controls-v1.2.3-rc.01", "email-capture-v0.02.6", "v01.23.0",
])
def test_refuses_everything_else(tag: str) -> None:
    assert _validate(tag) != 0


_CURL_STUB = r'''
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
url = next(value for value in args if value.startswith('https://'))
method = args[args.index('-X') + 1] if '-X' in args else 'GET'
payload = json.loads(args[args.index('-d') + 1]) if '-d' in args else None
header = sys.stdin.read()
entry = {'method': method, 'url': url, 'payload': payload, 'argv': args,
         'header_valid': header == 'Authorization: Bearer ' + os.environ['GH_TOKEN'] + '\n'}
with Path(os.environ['FIXTURE_CALLS']).open('a') as out:
    out.write(json.dumps(entry) + '\n')
if not entry['header_valid']:
    raise SystemExit(22)
config = json.loads(os.environ['FIXTURE_RESPONSES'])
def git(*argv, data=None):
    return subprocess.run(['git', *argv], cwd=os.environ['FIXTURE_REPO'],
                          input=data, text=True, capture_output=True, check=True).stdout.strip()
try:
    if method == 'GET' and url.endswith('/commits/main'):
        if config.get('fail') == 'resolve':
            raise SystemExit(22)
        result = {'sha': config.get('resolve_sha', git('rev-parse', 'main'))}
    elif method == 'POST' and url.endswith('/git/tags'):
        if config.get('fail') == 'tag':
            raise SystemExit(22)
        if 'tag_sha' in config:
            result = {'sha': config['tag_sha']}
        else:
            value = ('object ' + payload['object'] + '\ntype commit\ntag ' + payload['tag']
                     + '\ntagger Release Fixture <release@example.test> 1760000000 +0000\n\n'
                     + payload['message'] + '\n')
            result = {'sha': git('mktag', data=value)}
    elif method == 'POST' and url.endswith('/git/refs'):
        if config.get('fail') == 'ref':
            raise SystemExit(22)
        # The actual GitHub create-ref API is new-only. A collision cannot
        # become an overwrite or be retried through PATCH.
        git('update-ref', payload['ref'], payload['sha'], '0' * 40)
        if config.get('fail') == 'ref_after_write':
            raise SystemExit(22)
        result = {'ref': payload['ref'], 'object': {'sha': payload['sha']}}
    elif method == 'PATCH' and '/git/refs/tags/' in url:
        # Deliberately model the former force writer for the negative control.
        ref = 'refs/tags/' + url.rsplit('/', 1)[1]
        git('update-ref', ref, payload['sha'])
        result = {'ref': ref, 'object': {'sha': payload['sha']}}
    else:
        raise SystemExit(22)
    print(json.dumps(result))
except (subprocess.CalledProcessError, KeyError, TypeError, ValueError):
    raise SystemExit(22)
'''


def _git(repo: Path, *args: str) -> str:
    env = {"PATH": os.defpath, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repository(root: Path) -> tuple[Path, str, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.name", "Release Fixture")
    _git(repo, "config", "user.email", "release@example.test")
    (repo / "reader.txt").write_text("retained reader\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "retained")
    retained = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v1")
    _git(repo, "tag", "-a", "v1.0.0", "-m", "Retained immutable release")
    (repo / "reader.txt").write_text("new reader\n")
    _git(repo, "commit", "-qam", "selected")
    return repo, retained, _git(repo, "rev-parse", "HEAD")


def _publish(root: Path, repo: Path, tag: str, *, sha: str = "", notes: str = "",
             responses: dict | None = None, workflow: dict | None = None) -> dict:
    root.mkdir(exist_ok=True)
    calls = root / "calls.jsonl"
    output = root / "output"
    output.write_text("")
    stub = root / "curl"
    stub.write_text("#!" + sys.executable + "\n" + _CURL_STUB)
    stub.chmod(0o700)
    # Generated at runtime: a complete synthetic token-shaped sentinel,
    # never a real credential or a static exception in the secret scanner.
    token = "gh" + "s_" + "q" * 36
    env = {"PATH": str(root) + os.pathsep + os.defpath, "TAG": tag, "SHA": sha,
           "NOTES": notes, "REPO": "fixture/release", "GH_TOKEN": token,
           "GITHUB_OUTPUT": str(output), "FIXTURE_REPO": str(repo),
           "FIXTURE_CALLS": str(calls), "FIXTURE_RESPONSES": json.dumps(responses or {}),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    results = []
    for step in (WORKFLOW if workflow is None else workflow)["jobs"]["tag"]["steps"]:
        if "run" not in step:
            raise AssertionError("unreviewed non-shell tag writer")
        proc = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True,
                              text=True, timeout=15)
        results.append(proc)
        if proc.returncode:
            break
        for line in output.read_text().splitlines():
            if line.startswith("sha="):
                env["SHA"] = line.removeprefix("sha=")
    records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    text = "".join(p.stdout + p.stderr for p in results)
    if token in text or token in json.dumps(records):
        raise AssertionError("synthetic bearer exposed outside stdin header")
    return {"returncode": results[-1].returncode, "calls": records,
            "output": output.read_text(), "stdout_stderr": text}


@pytest.mark.parametrize("tag", ["v1.2.3", "v2.0.0", "email-capture-v0.3.0",
                                  "feature-controls-v0.3.0-rc.1"])
def test_pack_and_component_publication_preserve_all_retained_readers(tmp_path, tag):
    repo, retained, selected = _repository(tmp_path)
    old_tag = _git(repo, "rev-parse", "refs/tags/v1.0.0")
    result = _publish(tmp_path / "api", repo, tag)
    assert result["returncode"] == 0
    assert result["output"] == f"sha={selected}\n"
    assert _git(repo, "cat-file", "-t", "refs/tags/" + tag) == "tag"
    assert _git(repo, "rev-parse", tag + "^{}") == selected
    assert _git(repo, "rev-parse", "refs/tags/v1") == retained
    assert _git(repo, "rev-parse", "refs/tags/v1.0.0") == old_tag
    assert _git(repo, "show", "v1:reader.txt") == "retained reader"
    assert _git(repo, "tag", "-l", "v2") == ""
    assert [c["method"] for c in result["calls"]] == ["GET", "POST", "POST"]
    assert result["calls"][-1]["payload"]["ref"] == "refs/tags/" + tag
    assert all(c["header_valid"] for c in result["calls"])


def test_explicit_source_and_literal_notes_are_not_reinterpreted(tmp_path):
    repo, retained, _ = _repository(tmp_path)
    marker = tmp_path / "must-not-exist"
    notes = f"quote '\" and newline\n$(touch {marker})"
    result = _publish(tmp_path / "api", repo, "v3.0.0", sha=retained, notes=notes)
    assert result["returncode"] == 0 and not marker.exists()
    assert [c["method"] for c in result["calls"]] == ["POST", "POST"]
    assert result["calls"][0]["payload"] == {
        "tag": "v3.0.0", "message": notes, "object": retained, "type": "commit"}
    assert _git(repo, "rev-parse", "v3.0.0^{}") == retained


def test_existing_immutable_tag_collision_never_overwrites_or_falls_back(tmp_path):
    repo, retained, selected = _repository(tmp_path)
    old = _git(repo, "rev-parse", "refs/tags/v1.0.0")
    result = _publish(tmp_path / "api", repo, "v1.0.0", sha=selected)
    assert result["returncode"] != 0 and result["output"] == ""
    assert _git(repo, "rev-parse", "refs/tags/v1.0.0") == old
    assert _git(repo, "rev-parse", "refs/tags/v1") == retained
    assert [c["method"] for c in result["calls"]] == ["POST", "POST"]


@pytest.mark.parametrize("bad", ["a" * 39, "A" * 40, "a" * 40 + "\n",
                                 "a" * 40 + "\n" + "b" * 40, "main", "gh" + "s_" + "q" * 36],
                         ids=["short", "uppercase", "trailing-newline", "two-lines", "branch", "synthetic-token"])
def test_supplied_sha_must_be_one_complete_token_before_any_api_call(tmp_path, bad):
    repo, _, _ = _repository(tmp_path)
    result = _publish(tmp_path / "api", repo, "v1.2.3", sha=bad)
    assert result["returncode"] != 0 and result["calls"] == [] and result["output"] == ""


def test_invalid_tag_never_echoes_an_accidentally_supplied_synthetic_token(tmp_path):
    repo, _, _ = _repository(tmp_path)
    result = _publish(tmp_path / "api", repo, "gh" + "s_" + "q" * 36)
    assert result["returncode"] != 0 and result["calls"] == [] and result["output"] == ""


@pytest.mark.parametrize("field", ["resolve_sha", "tag_sha"])
@pytest.mark.parametrize("bad", [None, True, "a" * 39, "a" * 40 + "\n",
                                 "a" * 40 + "\n" + "b" * 40, "gh" + "s_" + "q" * 36],
                         ids=["null", "boolean", "short", "trailing-newline", "two-lines", "synthetic-token"])
def test_api_identity_requires_full_raw_token_before_shell_normalization(tmp_path, field, bad):
    repo, retained, _ = _repository(tmp_path)
    result = _publish(tmp_path / "api", repo, "v1.2.3", responses={field: bad})
    assert result["returncode"] != 0 and result["output"] == ""
    assert _git(repo, "tag", "-l", "v1.2.3") == ""
    assert _git(repo, "rev-parse", "refs/tags/v1") == retained
    assert not any(c["url"].endswith("/git/refs") for c in result["calls"])


@pytest.mark.parametrize("phase", ["resolve", "tag", "ref"])
def test_api_failure_is_failed_evidence_without_alias_fallback(tmp_path, phase):
    repo, retained, _ = _repository(tmp_path)
    result = _publish(tmp_path / "api", repo, "v1.2.3", responses={"fail": phase})
    assert result["returncode"] != 0 and result["output"] == ""
    assert _git(repo, "tag", "-l", "v1.2.3") == ""
    assert _git(repo, "rev-parse", "refs/tags/v1") == retained
    assert not any(c["method"] in {"PATCH", "DELETE"} for c in result["calls"])


def test_ambiguous_ref_response_never_becomes_pass_or_overwrite_recovery(tmp_path):
    repo, retained, selected = _repository(tmp_path)
    first = _publish(tmp_path / "first", repo, "v1.2.3", responses={"fail": "ref_after_write"})
    assert first["returncode"] != 0 and first["output"] == ""
    published = _git(repo, "rev-parse", "refs/tags/v1.2.3")
    assert _git(repo, "rev-parse", "v1.2.3^{}") == selected
    second = _publish(tmp_path / "second", repo, "v1.2.3", notes="changed retry")
    assert second["returncode"] != 0 and second["output"] == ""
    assert _git(repo, "rev-parse", "refs/tags/v1.2.3") == published
    assert _git(repo, "rev-parse", "refs/tags/v1") == retained
    assert not any(c["method"] in {"PATCH", "DELETE"} for c in first["calls"] + second["calls"])


def test_dispatch_and_outcome_contract_remain_automatic_and_scoped():
    inputs = WORKFLOW[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"tag", "sha", "notes"}
    assert inputs["tag"]["required"] is True
    assert inputs["sha"]["default"] == inputs["notes"]["default"] == ""
    tag = WORKFLOW["jobs"]["tag"]
    assert set(WORKFLOW["jobs"]) == {"tag", "security-diff", "outcome"}
    assert tag["concurrency"]["cancel-in-progress"] is False
    assert tag["concurrency"]["group"].endswith("inputs.tag }}")
    assert tag["timeout-minutes"] == 5
    assert [s["name"] for s in tag["steps"]] == ["Validate tag format", "Create tag ref"]
    assert "env" not in tag and "GH_TOKEN" not in STEPS["Validate tag format"]["env"]
    assert "secrets.GITHUB_TOKEN" in STEPS["Create tag ref"]["env"]["GH_TOKEN"]
    assert WORKFLOW["jobs"]["security-diff"]["permissions"] == {"contents": "read"}
    assert WORKFLOW["jobs"]["outcome"]["needs"] == "tag"
    assert "FAILED: 'failure'" in WORKFLOW["jobs"]["outcome"]["steps"][0]["with"]["script"]
    contract = json.loads((ROOT / "config/workflow-outcomes.json").read_text())
    release = contract["workflows"][".github/workflows/release-tag.yml"]
    assert release["load_bearing_steps"] == {"tag": ["Validate tag format", "Create tag ref"]}
    assert release["load_bearing_jobs"] == ["tag"]
    assert release["outcome_reporter_job"] == "outcome"
    assert release["outcomes"] == ["EXECUTED", "FAILED"]


def test_owning_release_check_selects_publication_inputs_without_preparation(tmp_path):
    spec = importlib.util.spec_from_file_location("release_selection_core", ROOT / "scripts/agent-check.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    config = json.loads((ROOT / "config/agent-checks.json").read_text())
    recipe = next(c for c in config["checks"] if c["id"] == "release-manifest-contract")
    assert recipe["timeout_seconds"] == 180
    assert recipe["environment"] == "release-contract-python"
    assert any("test_release_*.py" in command for command in recipe["commands"])
    assert any("tests/test_release_tag_namespaces.py" in command for command in recipe["commands"])
    paths = [".github/workflows/release-tag.yml", "tests/test_release_tag_namespaces.py",
             "tests/test_hydrate_assessment.py", "tests/test_workflow_security.py",
             "tests/test_renovate_config.py", "README.md", "default.json",
             ".github/workflows/REUSABLE-WORKFLOWS.md", "docs/renovate-preset.md",
             "docs/node-runtime-support.md", "docs/contracts/integration-admission.md",
             "config/workflow-outcomes.json", ".github/workflows/integration-admission-reusable.yml",
             "scripts/tests/test_lint_workflow_outcomes.py"]
    repo = tmp_path / "selection"
    repo.mkdir()
    for name in [*paths, "unrelated.txt"]:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unchanged synthetic source\n")
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.name", "Selection Fixture")
    _git(repo, "config", "user.email", "selection@example.test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic inputs")
    for name in [*paths, "unrelated.txt"]:
        path = repo / name
        path.write_text("changed synthetic source\n")
        plan = core.plan_for(repo, config, "HEAD")
        assert plan["paths"] == [name]
        selected = {c["id"] for c in plan["checks"]}
        assert ("release-manifest-contract" in selected) is (name != "unrelated.txt")
        path.write_text("unchanged synthetic source\n")
    assert not (repo / ".ai").exists(), "selection cannot mint a validation or preparation receipt"


def test_tag_is_annotated_with_optional_notes() -> None:
    run = STEPS["Create tag ref"]["run"]
    assert "/git/tags" in run and "os.environ['NOTES'] or 'Release '" in run
    assert STEPS["Create tag ref"]["env"]["NOTES"] == "${{ inputs.notes }}"


def test_documented_shadow_exception_matches_actual_tag_gate_and_paired_commit(tmp_path):
    assessment = yaml.safe_load((ROOT / ".github/workflows/pr-assessment-reusable.yml").read_text())
    inputs = assessment[True]["workflow_call"]["inputs"]
    assert inputs["jev_judge_shadow"]["default"] == "off"
    steps = [step for job in assessment["jobs"].values() for step in job.get("steps", [])]
    gate = next(step for step in steps if step.get("id") == "jev_gate")
    assert "inputs.jev_judge_shadow == 'on'" in gate["if"]
    assert "vars.JEV_ENABLED_JUDGE_FINDING_SHADOW == 'true'" in gate["if"]
    assert "env" in gate and gate["env"] == {"PACK_REF": "${{ inputs.pack_ref }}"}
    for index, (ref, expected) in enumerate((("a" * 40, False), ("v1.25.1", True), ("main", False))):
        output = tmp_path / ("shadow-gate-" + str(index))
        result = subprocess.run(["bash", "-c", gate["run"]], capture_output=True, text=True, timeout=10,
                                env={"PATH": os.defpath, "PACK_REF": ref, "GITHUB_OUTPUT": str(output)})
        assert result.returncode == 0
        assert (output.exists() and output.read_text() == "ok=true\n") is expected
    # Real Git distinguishes a verified immutable tag/uses pair from an alias mismatch.
    repo, retained, selected = _repository(tmp_path)
    _git(repo, "tag", "-a", "v1.25.1", "-m", "Synthetic immutable patch release", selected)
    assert _git(repo, "rev-parse", "v1.25.1^{}") == selected
    assert _git(repo, "rev-parse", "v1^{}") == retained != selected
    assert _validate("v1.25.1") == 0 and _validate("v1") != 0
    guide = (ROOT / ".github/workflows/REUSABLE-WORKFLOWS.md").read_text()
    exception = " ".join(guide.split("#### Opt-in Jev shadow exception", 1)[1].split("The hydration list", 1)[0].split())
    for claim in ("`off` by default", "rejects a raw", "owning adoption agent must automatically",
                  "full patch release tag", "same commit", "Hold a mismatched pair",
                  "raw-SHA path is not implemented"):
        assert claim in exception
    assert "pack_ref: v1.25.1" in exception
    assert "321810f558463b2363d3372d669efcd3ea8283ec" in exception
    assert "legacy gate accepts their shape" in exception


def test_legacy_preset_guidance_requires_automatic_paired_adoption_without_manager_promise():
    preset = json.loads((ROOT / "default.json").read_text())
    manager = next(manager for manager in preset["customManagers"]
                   if "pack_ref" in manager["description"])
    pattern = re.compile(re.sub(r"\(\?<([A-Za-z][A-Za-z0-9_]*)>", r"(?P<\1>", manager["matchStrings"][0]))
    line = "pack_ref: " + "a" * 40 + " # v1.25.1"
    assert pattern.search(line).groupdict() == {"currentDigest": "a" * 40, "currentValue": "v1.25.1"}
    assert not pattern.search("pack_ref: v1.25.1")
    # A preset without this manager cannot promise a paired hydration-pin update.
    legacy_managers = [item for item in preset["customManagers"] if item is not manager]
    assert not any("pack_ref" in item.get("description", "") for item in legacy_managers)
    readme = (ROOT / "README.md").read_text()
    pins = " ".join(readme.split("## Consumer pins", 1)[1].split("### Tag namespaces", 1)[0].split())
    for claim in ("preset revision containing the `pack_ref` manager", "older `#v1.25.1` preset does not include",
                  "a workflow-pin update alone does not update", "owning adoption agent must automatically",
                  "both pins together in one adoption PR", "same release commit", "not evidence that every consumer"):
        assert claim in pins
    assert "full patch release-tag exception" in pins and "shadow stays off by default" in pins
    assert "#opt-in-jev-shadow-exception" in pins and "For ordinary SHA-form inputs" in pins
    assert not re.search(r"(?i)(?:must|need to|should) update[^.]*manually", pins)
