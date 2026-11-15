"""Autofix patch contract and interoperability with the accepted publisher."""

import base64
import importlib.util
import os
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import codex_automation_patch as patch
import codex_automation_worker as worker


def git(cwd, *args):
    return subprocess.check_output(["git", "-C", str(cwd), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q")
    git(src, "config", "user.email", "fixture@example.invalid")
    git(src, "config", "user.name", "fixture")
    (src / "policy.py").write_text("def allowed(origin):\n    return origin in ('null', '*')\n")
    git(src, "add", ".")
    git(src, "commit", "-qm", "head")
    bare = tmp_path / "source.git"
    subprocess.check_call(["git", "clone", "-q", "--bare", str(src), str(bare)])
    return bare, git(src, "rev-parse", "HEAD")


FIX = ("diff --git a/policy.py b/policy.py\n--- a/policy.py\n+++ b/policy.py\n@@ -1,2 +1,2 @@\n"
       " def allowed(origin):\n-    return origin in ('null', '*')\n+    return origin == 'null'\n")


def test_instruction_surface_rule_matches_the_accepted_publisher():
    source = (ROOT / "scripts/ci/autofix-publish.py").read_text()
    match = re.search(r'^INSTRUCTIONS = re\.compile\((r".*")\)$', source, re.MULTILINE)
    assert match and ast.literal_eval(match.group(1)) == patch.INSTRUCTIONS.pattern


@pytest.mark.parametrize("text", [
    "not json", '{"summary": "x"}', '{"summary": "x", "patch": "p", "extra": 1}',
    '{"summary": "x", "summary": "y", "patch": "p"}', '{"summary": "a\\nb", "patch": "p"}',
    '{"summary": "' + "x" * 101 + '", "patch": "p"}', '{"summary": "x", "patch": ""}',
    # Review P3: no bidi/line separators/C1 controls, no issue-closing keywords.
    '{"summary": "a\\u202eb", "patch": "p"}', '{"summary": "a\\u2028b", "patch": "p"}',
    '{"summary": "a\\u0085b", "patch": "p"}', '{"summary": "Fixes #12", "patch": "p"}',
    '{"summary": "closes 3", "patch": "p"}', '{"summary": "ping @org", "patch": "p"}',
])
def test_malformed_proposals_are_refused(text):
    with pytest.raises(patch.PatchRefused):
        patch.parse_proposal(text)


@pytest.mark.parametrize("body", [
    "diff --git a/a.py b/a.py\ndeleted file mode 100644\n",
    "diff --git a/a.py b/b.py\n",
    "diff --git a/a.py b/a.py\nold mode 100644\nnew mode 100755\n",
    "diff --git a/a.py b/a.py\nrename from a.py\nrename to b.py\n",
    "diff --git a/a.bin b/a.bin\nGIT binary patch\n",
    "diff --git a/link b/link\nnew file mode 120000\n",
    "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n",
    "diff --git a/CLAUDE.md b/CLAUDE.md\n",
    "diff --git a/.claude/settings.json b/.claude/settings.json\n",
    "diff --git a/sub/AGENTS.md b/sub/AGENTS.md\n",
    "diff --git a/../x b/../x\n",
    "diff --git a/.git/config b/.git/config\n",
    "diff --git a/a.py b/a.py\n--- a/other.py\n+++ b/a.py\n",
    "".join(f"diff --git a/f{i}.py b/f{i}.py\n" for i in range(21)),
    "no header at all\n",
    # Review P2: case-insensitive and wider protected surface.
    "diff --git a/.Claude/settings.json b/.Claude/settings.json\n",
    "diff --git a/Claude.md b/Claude.md\n",
    "diff --git a/agents.md b/agents.md\n",
    "diff --git a/.MCP.json b/.MCP.json\n",
    "diff --git a/.cursor/rules/x.mdc b/.cursor/rules/x.mdc\n",
    "diff --git a/.github/copilot-instructions.md b/.github/copilot-instructions.md\n",
    "diff --git a/GEMINI.md b/GEMINI.md\n",
    "diff --git a/.codex/config.toml b/.codex/config.toml\n",
    "diff --git a/.github/CODEOWNERS b/.github/CODEOWNERS\n",
    "diff --git a/.gitattributes b/.gitattributes\n",
    "diff --git a/.pre-commit-config.yaml b/.pre-commit-config.yaml\n",
    "diff --git a/tests/conftest.py b/tests/conftest.py\n",
    "diff --git a/.vscode/tasks.json b/.vscode/tasks.json\n",
])
def test_patch_operations_outside_the_contract_are_refused(body):
    with pytest.raises(patch.PatchRefused):
        patch.validate_patch(body)


def test_candidate_is_a_single_autofix_commit_on_the_exact_head(repo, tmp_path):
    bare, head = repo
    work = tmp_path / "work"
    work.mkdir()
    result = patch.build_candidate(bare, work, head, {"summary": "Reject wildcard origins", "patch": FIX})
    edit = work / "edit"
    assert git(edit, "log", "--format=%s", f"{head}..{result['candidate_sha']}") == "[autofix] Reject wildcard origins"
    assert git(edit, "rev-parse", f"{result['candidate_sha']}^") == head
    assert result["paths"] == ["policy.py"]
    heads = git(work, "bundle", "list-heads", str(result["bundle"]))
    assert heads == f"{result['candidate_sha']} refs/heads/implementation"


def test_patch_that_does_not_apply_is_refused_not_repaired(repo, tmp_path):
    bare, head = repo
    stale = FIX.replace("('null', '*')", "('null', '?')")
    with pytest.raises(patch.PatchRefused, match="git_apply_failed"):
        patch.build_candidate(bare, tmp_path, head, {"summary": "x", "patch": stale})


def test_packet_round_trips_through_the_accepted_publisher_import(repo, tmp_path, monkeypatch):
    """The controller's packet must be importable by autofix-publish.py unchanged;
    only its network fetch of the admitted base is redirected to a local repo."""
    bare, head = repo
    work = tmp_path / "work"
    work.mkdir()
    built = patch.build_candidate(bare, work, head, {"summary": "Reject wildcard origins", "patch": FIX})
    target = {"repository": "example/automation", "pull_request": 7, "head_sha": head, "head_ref": "feature"}
    output = {"candidate_sha": built["candidate_sha"], "paths": built["paths"], "summary": "s",
              "bundle_b64": base64.b64encode(built["bundle"].read_bytes()).decode()}
    packet_root = tmp_path / "controller"
    packet_root.mkdir()
    result = worker.write_packet(target, packet_root, output)
    assert result["outcome"] == "packet"
    packet = Path(result["packet_dir"])
    assert {p.name for p in packet.iterdir()} == {"receipt.json", "implementation.bundle"}

    spec = importlib.util.spec_from_file_location("autofix_publish", ROOT / "scripts/ci/autofix-publish.py")
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    real_git = publisher.git

    def local_git(args, cwd, *, env=None, timeout=30):
        if args and args[0] == "fetch":
            args = ["fetch", "--no-tags", str(bare), head]
        return real_git(args, cwd, env=env, timeout=timeout)
    monkeypatch.setattr(publisher, "git", local_git)
    for key, value in {"AUTOFIX_PACKET_DIR": str(packet), "GITHUB_REPOSITORY": "example/automation",
                       "PR_NUMBER": "7", "HEAD_REF": "feature", "EXPECTED_HEAD": head,
                       "GITHUB_SERVER_URL": "https://github.com", "READ_TOKEN": "unused"}.items():
        monkeypatch.setenv(key, value)
    imported = tmp_path / "import"
    imported.mkdir()
    workdir = publisher.import_packet(imported)
    assert git(workdir, "rev-parse", "refs/heads/implementation") == built["candidate_sha"]
    subjects = git(workdir, "log", "--format=%s", f"{head}..{built['candidate_sha']}").splitlines()
    assert subjects and all(s.startswith("[autofix]") for s in subjects)


@pytest.mark.parametrize("change", [{"candidate_sha": "x"}, {"bundle_b64": "!!"}, {"head_ref": "main"}])
def test_packet_identity_is_validated_before_any_file_is_written(tmp_path, change):
    target = {"repository": "example/automation", "pull_request": 7, "head_sha": "a" * 40,
              "head_ref": change.get("head_ref", "feature")}
    output = {"candidate_sha": change.get("candidate_sha", "b" * 40), "bundle_b64": change.get("bundle_b64", "")}
    result = worker.write_packet(target, tmp_path, output)
    assert result["outcome"] == "failed"
    assert not (tmp_path / "packet" / "receipt.json").exists()



def test_existing_symlink_target_cannot_be_rewritten(tmp_path):
    """Review P3: a regular-looking patch to an existing symlink is refused."""
    src = tmp_path / "s"
    src.mkdir()
    git(src, "init", "-q")
    git(src, "config", "user.email", "f@example.invalid")
    git(src, "config", "user.name", "f")
    (src / "a.txt").write_text("a\n")
    os.symlink("a.txt", src / "lnk")
    git(src, "add", ".")
    git(src, "commit", "-qm", "head")
    bare = tmp_path / "s.git"
    subprocess.check_call(["git", "clone", "-q", "--bare", str(src), str(bare)])
    head = git(src, "rev-parse", "HEAD")
    body = ("diff --git a/lnk b/lnk\n--- a/lnk\n+++ b/lnk\n@@ -1 +1 @@\n-a.txt\n\\ No newline at end of file\n"
            "+b.txt\n\\ No newline at end of file\n")
    work = tmp_path / "w"
    work.mkdir()
    with pytest.raises(patch.PatchRefused):
        patch.build_candidate(bare, work, head, {"summary": "Retarget", "patch": body})
