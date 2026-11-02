"""Generic installed terminal CLI coverage; no private adapter or consumer data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_CONFIG_GLOBAL": os.devnull, "PYTHONDONTWRITEBYTECODE": "1"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], env=ENV,
                                   stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def retained_repo(tmp_path):
    root = tmp_path / "synthetic"
    root.mkdir()
    git(root, "init", "-q", "-b", "codex/retained")
    git(root, "config", "remote.origin.url", "https://github.com/example-org/example-app")
    (root / ".gitignore").write_text(".ai/\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "Generic fixture")
    path = root / ".ai/sessions/active-task.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "merged", "pr": "https://github.com/example-org/example-app/pull/7",
                               "steps": [{"id": "verify", "status": "pending"}]}) + "\n")
    return root, path


@pytest.fixture(params=["", "plugin/manolii-framework/", "registry/platform/framework/"])
def installed_checker(tmp_path, request):
    """Run shipped bytes outside the producer tree without local imports."""
    path = tmp_path / "installed-entrypoint/session-unfinished-work-check.py"
    path.parent.mkdir()
    shutil.copy2(ROOT / (request.param + "scripts/session-unfinished-work-check.py"), path)
    return path


@pytest.mark.parametrize("base", ["main", "master"])
def test_tracking_only_checkout_retains_parent_work(retained_repo, installed_checker, base):
    root, path = retained_repo
    git(root, "update-ref", f"refs/remotes/origin/{base}", "HEAD")
    before = path.read_bytes()
    result = subprocess.run([sys.executable, str(installed_checker),
                             "--skip-remote", "--json", "--strict"],
                            env={**ENV, "CLAUDE_PROJECT_DIR": str(root)}, cwd=root,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["base"] == base
    assert report["ok"] is False
    assert any("unfinished or unverified parent" in item for item in report["findings"])
    assert path.read_bytes() == before


@pytest.mark.parametrize("configured", [None, "main", "main~0"])
def test_unknown_default_is_not_inferred_from_a_tag(retained_repo, installed_checker, configured):
    root, path = retained_repo
    git(root, "tag", "main")
    if configured:
        git(root, "config", "init.defaultBranch", configured)
    before = path.read_bytes()
    result = subprocess.run([sys.executable, str(installed_checker),
                             "--skip-remote", "--json"],
                            env={**ENV, "CLAUDE_PROJECT_DIR": str(root)}, cwd=root,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert json.loads(result.stdout)["ok"] is False
    assert path.read_bytes() == before


def test_configured_custom_tracking_branch_is_verified(retained_repo, installed_checker):
    root, path = retained_repo
    git(root, "config", "init.defaultBranch", "release/next")
    git(root, "update-ref", "refs/remotes/origin/release/next", "HEAD")
    before = path.read_bytes()
    result = subprocess.run([sys.executable, str(installed_checker), "--skip-remote", "--json", "--strict"],
                            env={**ENV, "CLAUDE_PROJECT_DIR": str(root)}, cwd=root,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout)["base"] == "release/next"
    assert path.read_bytes() == before


def test_branch_and_same_named_tag_count_the_verified_branch(retained_repo, installed_checker):
    root, path = retained_repo
    git(root, "branch", "main")
    (root / "owned-source.txt").write_text("new task source\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "One owned task change")
    git(root, "tag", "main")
    before = path.read_bytes()
    result = subprocess.run([sys.executable, str(installed_checker), "--skip-remote", "--json", "--strict"],
                            env={**ENV, "CLAUDE_PROJECT_DIR": str(root)}, cwd=root,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout)["unique_commits"] == 1
    assert path.read_bytes() == before
