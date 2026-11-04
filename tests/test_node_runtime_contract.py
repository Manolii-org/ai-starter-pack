"""Execute the shared runtime/cache contract against isolated caller fixtures."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name):
    """Load a source workflow without rendering caller expressions."""
    return yaml.safe_load((WORKFLOWS / name).read_text())


def inputs(spec):
    """Read workflow-call inputs with either YAML boolean-key convention."""
    return (spec.get("on") or spec[True])["workflow_call"]["inputs"]


@pytest.mark.parametrize("name", ["ci-reusable.yml", "e2e-playwright-reusable.yml", "secret-scan-reusable.yml"])
def test_node24_default_keeps_explicit_node22_override(name):
    """All reusable setup steps honor the caller's runtime selection."""
    spec = workflow(name)
    assert inputs(spec)["node_version"]["default"] == "24"
    setup = [step for job in spec["jobs"].values() for step in job.get("steps", [])
             if step.get("uses", "").startswith("actions/setup-node@")]
    assert setup
    for step in setup:
        value = step["with"]["node-version"]
        assert value == "${{ inputs.node_version }}"


@pytest.mark.parametrize("name", ["ci-reusable.yml", "ci.yml"])
def test_cache_keys_bind_actual_runtime_abi_arch_and_lockfiles(name, tmp_path):
    """Execute runtime detection and compare every cache writer and reader."""
    spec = workflow(name)
    keys = set()
    for job in spec["jobs"].values():
        steps = job.get("steps", [])
        caches = [s for s in steps if s.get("uses", "").startswith("actions/cache")]
        if not caches:
            continue
        runtime = next(s for s in steps if s.get("id") == "node-runtime")
        output = tmp_path / "runtime.txt"
        output.unlink(missing_ok=True)
        subprocess.run(["bash", "-e", "-c", runtime["run"]], check=True,
                       env={**os.environ, "GITHUB_OUTPUT": str(output)}, timeout=10)
        actual = dict(line.split("=", 1) for line in output.read_text().splitlines())
        expected = json.loads(subprocess.check_output(
            ["node", "-p", "JSON.stringify({version:process.versions.node,abi:process.versions.modules})"], text=True))
        assert actual == expected
        for cache in caches:
            key = cache["with"]["key"]
            assert "${{ steps.node-runtime.outputs.version }}" in key
            assert "${{ steps.node-runtime.outputs.abi }}" in key
            assert "${{ runner.arch }}" in key
            for lock in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock"):
                assert lock in key
            assert "restore-keys" not in cache["with"]
            keys.add(key)
    assert len(keys) == 1  # producer and every downstream reader agree


@pytest.mark.parametrize("manager,expected", [("npm", ["ci"]), ("pnpm", ["install", "--frozen-lockfile"])])
def test_default_install_commands_keep_caller_manager(manager, expected, tmp_path):
    """Record install arguments without contacting package registries."""
    spec = workflow("ci-reusable.yml")
    step = next(s for s in spec["jobs"]["install"]["steps"] if s.get("name") == "Install")
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "argv"
    for name in ("npm", "pnpm"):
        tool = tools / name
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALL_RECORD"\n')
        tool.chmod(0o755)
    subprocess.run(["bash", "-e", "-c", step["run"]], check=True, cwd=tmp_path,
        env={"PATH": str(tools) + os.pathsep + os.environ["PATH"],
             "PACKAGE_MANAGER": manager, "INSTALL_COMMAND": "", "CALL_RECORD": str(log)}, timeout=10)
    assert log.read_text().splitlines() == expected
    pnpm_steps = [s for s in spec["jobs"]["install"]["steps"]
                  if s.get("uses", "").startswith("pnpm/action-setup@")]
    assert len(pnpm_steps) == 2
    assert any("version" not in s["with"] and "== ''" in s["if"] for s in pnpm_steps)


def test_non_node_caller_reports_no_package_without_install(tmp_path):
    """An empty non-Node caller keeps package-dependent jobs disabled."""
    spec = workflow("ci-reusable.yml")
    step = next(s for s in spec["jobs"]["detect"]["steps"] if s.get("id") == "detect")
    output = tmp_path / "detect.txt"
    subprocess.run(["bash", "-e", "-c", step["run"]], check=True, cwd=tmp_path,
                   env={**os.environ, "GITHUB_OUTPUT": str(output)}, timeout=10)
    assert output.read_text() == "has_pkg=false\n"
    assert spec["jobs"]["install"]["if"] == "needs.detect.outputs.has_pkg == 'true'"
    assert "inputs.skip_build == false" in spec["jobs"]["build"]["if"]
    assert "needs.install.result == 'success'" in spec["jobs"]["build"]["if"]


def test_library_floors_and_audit_optional_setup_remain_distinct():
    """Runtime defaults preserve package floors and optional audit setup."""
    assert json.loads((ROOT / "package.json").read_text())["engines"]["node"] == ">=20.19"
    for prefix in (".claude", "plugin/manolii-framework", "registry/platform/framework"):
        package = ROOT / prefix / "skills/exploratory-bug-bash/harness/package.json"
        assert json.loads(package.read_text())["engines"]["node"] == ">=22.12.0"
    audit = workflow("secret-scan-reusable.yml")["jobs"]["npm-audit"]
    setup = next(s for s in audit["steps"] if s.get("uses", "").startswith("actions/setup-node@"))
    assert setup["if"] == "steps.detect.outputs.found == 'true'"
    assert audit["name"] == "Dependency Vulnerability Audit"


def test_new_scaffold_pins_the_matching_framework_major_exactly():
    """The scaffold manifest and generated native receipt select the same release."""
    manifest = yaml.safe_load((ROOT / "ai-manifest.yaml").read_text())
    assert manifest["requires"] == [{"plugin": "platform/framework", "ref": "2.0.0"}]
    lock = json.loads((ROOT / ".ai/native-guidance-lock.json").read_text())
    assert lock["ref"] == "2.0.0"
    assert lock["resolved_version"] == "2.0.0"
