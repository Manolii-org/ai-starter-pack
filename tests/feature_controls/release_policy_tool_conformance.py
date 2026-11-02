import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/feature-release-check.py"


def check(root, *args):
    return subprocess.run([sys.executable, str(CHECKER), "--repo-root", str(root), *args],
                          text=True, capture_output=True, timeout=20)


def manifest(root, ready=False):
    path = root / ".feature-controls"
    path.mkdir()
    value = {"schema_version": 1, "features": {}}
    if ready:
        value["features"]["search"] = {"state": "ready", "owner": "team.search", "applications": ["web"],
                                           "surfaces": ["search"], "acceptance": ["tests/search.test.mjs"]}
    (path / "releases.json").write_text(json.dumps(value))
    return value


def test_source_validation_is_explicitly_not_runtime_acceptance(tmp_path):
    manifest(tmp_path)
    result = check(tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "source_validated", "runtime_status": "not_requested", "features": 0}
    result = check(tmp_path, "--mode", "runtime")
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "no_features"


def test_ready_feature_requires_repository_acceptance_reference(tmp_path):
    manifest(tmp_path, ready=True)
    assert check(tmp_path).returncode == 1
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/search.test.mjs").write_text("// synthetic acceptance reference\n")
    assert check(tmp_path).returncode == 0
    result = check(tmp_path, "--mode", "runtime", "--expected-source-sha", "1" * 40)
    assert result.returncode == 1 and "source_validated" not in result.stdout


def test_probe_failures_never_echo_secret_bearing_streams(tmp_path):
    manifest(tmp_path, ready=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/search.test.mjs").write_text("// synthetic\n")
    (tmp_path / "probe.py").write_text("import sys\nprint('synthetic-private-marker')\nprint('synthetic-private-marker', file=sys.stderr)\nsys.exit(1)\n")
    result = check(tmp_path, "--mode", "runtime", "--probe-script", "probe.py", "--expected-source-sha", "1" * 40)
    assert result.returncode == 1 and "synthetic-private-marker" not in result.stdout + result.stderr


@pytest.mark.parametrize("event,protected,run_event,run_repo,conclusion,allowed", [
    ("pull_request", "true", "", "", "", False),
    ("pull_request_target", "true", "", "", "", False),
    ("push", "false", "", "", "", False),
    ("push", "true", "", "", "", True),
    ("workflow_dispatch", "true", "", "", "", True),
    ("schedule", "true", "", "", "", True),
    ("workflow_run", "true", "pull_request", "Example/app", "success", False),
    ("workflow_run", "true", "push", "Other/fork", "success", False),
    ("workflow_run", "true", "push", "Example/app", "failure", False),
    # Downstream protected/default ref does not prove the upstream push was trusted.
    ("workflow_run", "true", "push", "Example/app", "success", False),
])
def test_consumed_runtime_action_guards(event, protected, run_event, run_repo, conclusion, allowed):
    action = (ROOT / ".github/actions/feature-release-policy/action.yml").read_text()
    script = textwrap.dedent(action.split("      run: |\n", 1)[1])
    # Exercise the actual admission guard, stopping before any installation/network.
    guard = script.split("policy_venv=", 1)[0] + "\nexit 0\n"
    env = {**os.environ, "POLICY_MODE": "runtime", "POLICY_EVENT": event, "POLICY_REF_PROTECTED": protected,
           "POLICY_RUN_EVENT": run_event, "POLICY_RUN_REPO": run_repo,
           "POLICY_REPOSITORY": "Example/app", "POLICY_RUN_CONCLUSION": conclusion}
    result = subprocess.run(["bash", "-c", guard], env=env, text=True, capture_output=True, timeout=5)
    assert (result.returncode == 0) is allowed
