"""Synthetic execution acceptance for the opt-in layout contract; no real repos."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAYOUT = {"schema": "agent-validation-layout/v1", "aliases": {
    ".claude/skills": ".github/skills", ".cursor/skills": ".github/skills"}}
NAMES = ("scripts/agent-check.py", "scripts/sync-agent-validation.py",
         "scripts/install-agent-validation.py", "scripts/tests/test_agent_check.py",
         "docs/agent-validation.md", ".claude/skills/agent-validation/SKILL.md")
LAYOUT_NAME = "config/agent-validation-layout.json"
RECEIPT = ".ai/agent-validation-source.json"
RECOVERY = ".ai/agent-checks/layout-recovery.json"
ENV = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_CONFIG_GLOBAL": os.devnull, "PYTHONDONTWRITEBYTECODE": "1"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], env=ENV, stderr=subprocess.PIPE).strip()


def commit(root, message="fixture"):
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", message)


def snapshot(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            result[str(path.relative_to(root))] = ("link", path.lstat().st_mode, os.readlink(path))
        elif path.is_file():
            result[str(path.relative_to(root))] = ("file", path.stat().st_mode, path.read_bytes())
        elif path.is_dir():
            result[str(path.relative_to(root))] = ("directory", path.stat().st_mode)
    return result


@pytest.fixture(scope="module")
def producers(tmp_path_factory):
    """Immutable minimal source/plugin/registry producer Git checkouts."""
    result = {}
    for kind, prefix in [("source", ""), ("plugin", "plugin/manolii-framework"),
                         ("registry", "registry/platform/framework")]:
        top = tmp_path_factory.mktemp("producer-" + kind)
        source = top / prefix
        for logical in NAMES:
            name = logical if kind == "source" else logical.removeprefix(".claude/")
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            bundled = ROOT / logical if kind == "source" else ROOT / prefix / name
            shutil.copy2(bundled, path)
        git(top, "init", "-q", "-b", "main")
        commit(top)
        result[kind] = (source, top)
    return result


@pytest.fixture
def consumer(tmp_path):
    root = tmp_path / "consumer"
    for rel in ("config", ".claude", ".cursor", ".github/skills"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "config/agent-checks.json").write_text(json.dumps(
        {"schema_version": 1, "base": "origin/main", "checks": []}) + "\n")
    (root / LAYOUT_NAME).write_text(json.dumps(LAYOUT) + "\n")
    (root / "AGENTS.md").write_text("Owner AGENTS text.\n")
    (root / "CLAUDE.md").write_text("Owner CLAUDE text.\n")
    (root / ".gitignore").write_text("node_modules/\n")
    (root / ".ai").mkdir()
    (root / ".ai/.gitignore").write_text("agent-checks/\n")
    (root / ".github/skills/owner.md").write_text("Other skill owner text.\n")
    for name in (".claude", ".cursor"):
        (root / name / "skills").symlink_to("../.github/skills", target_is_directory=True)
    git(root, "init", "-q", "-b", "codex/synthetic-layout")
    commit(root)
    return root


def invoke(source, root, *flags, layout=True, env=None):
    args = [sys.executable, str(source / "scripts/install-agent-validation.py"), "--repo-root", str(root)]
    if layout:
        args += ["--layout", LAYOUT_NAME]
    return subprocess.run([*args, *flags], env=ENV if env is None else env, cwd=root.parent,
                          capture_output=True, text=True, timeout=30)


def imported(source):
    spec = importlib.util.spec_from_file_location("layout_installer", ROOT / "scripts/install-agent-validation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SOURCE = source
    return module


@pytest.mark.parametrize("kind", ["source", "plugin", "registry"])
def test_apply_check_dry_run_idempotence_and_installed_entrypoint(producers, consumer, kind):
    """Producer and installed entrypoints retain the same six-byte bundle and origin."""
    source, top = producers[kind]
    before = snapshot(consumer)
    aliases = git(consumer, "ls-files", "--stage", "--", ".claude/skills", ".cursor/skills")
    identities = {name: (consumer / name).lstat().st_ino for name in LAYOUT["aliases"]}
    result = invoke(source, consumer, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("UPDATE .github/skills/agent-validation/SKILL.md") == 1
    assert "UPDATE .cursor/skills/" not in result.stdout
    assert snapshot(consumer) == before
    assert invoke(source, consumer, "--check").returncode == 1
    result = invoke(source, consumer)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((consumer / RECEIPT).read_text())
    assert set(receipt["files"]) == set(NAMES)
    assert receipt["layout"]["source"]["commit"] == git(top, "rev-parse", "HEAD").decode()
    assert receipt["layout"]["layout_file"]["sha256"] == hashlib.sha256((consumer / LAYOUT_NAME).read_bytes()).hexdigest()
    for logical in NAMES:
        actual = consumer / receipt["layout"]["destinations"][logical]["canonical"]
        src = source / (logical if kind == "source" else logical.removeprefix(".claude/"))
        assert actual.read_bytes() == src.read_bytes()
        assert hashlib.sha256(actual.read_bytes()).hexdigest() == receipt["files"][logical]
    assert git(consumer, "ls-files", "--stage", "--", ".claude/skills", ".cursor/skills") == aliases
    assert {name: (consumer / name).lstat().st_ino for name in LAYOUT["aliases"]} == identities
    assert "Owner AGENTS text.\n" in (consumer / "AGENTS.md").read_text()
    assert "Owner CLAUDE text.\n" in (consumer / "CLAUDE.md").read_text()
    assert (consumer / ".github/skills/owner.md").read_text() == "Other skill owner text.\n"
    assert json.loads((consumer / RECOVERY).read_text())["status"] == "complete"
    after = snapshot(consumer)
    for flags in [("--check",), ("--dry-run",), ()]:
        result = invoke(source, consumer, *flags)
        assert result.returncode == 0, result.stderr
        assert snapshot(consumer) == after
    unavailable = top.with_name(top.name + "-unavailable")
    top.rename(unavailable)
    try:
        # The actual producer checkout is unavailable; no source path/PYTHONPATH
        # is supplied. Installed code must use its own six-file bundle/receipt.
        env = {k: v for k, v in ENV.items() if k != "PYTHONDONTWRITEBYTECODE"}
        for flags in [("--check",), ("--dry-run",)]:
            result = invoke(consumer, consumer, *flags, env=env)
            assert result.returncode == 0, result.stderr
            assert snapshot(consumer) == after
        assert json.loads((consumer / RECEIPT).read_text()) == receipt
    finally:
        unavailable.rename(top)


@pytest.mark.parametrize("bad", ["external", "escape", "cycle", "dangling", "nested", "case", "root-link",
                                 "unstaged-layout", "staged-layout", "untracked-layout", "unknown-field", "unknown-alias", "duplicate-field"])
def test_unsafe_layout_refuses_without_writes(producers, consumer, tmp_path, bad):
    source, _ = producers["source"]
    root = consumer
    link = consumer / ".claude/skills"
    if bad in ("external", "escape", "cycle", "dangling"):
        outside = tmp_path / "outside"
        outside.mkdir()
        link.unlink()
        target = {"external": str(outside), "escape": "../../outside", "cycle": "skills", "dangling": "../missing"}[bad]
        link.symlink_to(target, target_is_directory=True)
    elif bad == "nested":
        (consumer / ".github/skills").rename(consumer / ".github/original")
        (consumer / ".github/skills").symlink_to("original", target_is_directory=True)
    elif bad == "case":
        (consumer / ".github/Skills").mkdir()
    elif bad == "root-link":
        root = tmp_path / "root-alias"
        root.symlink_to(consumer, target_is_directory=True)
    elif bad == "untracked-layout":
        git(consumer, "rm", "--cached", LAYOUT_NAME)
    else:
        path = consumer / LAYOUT_NAME
        value = json.loads(path.read_text())
        if bad == "unknown-field":
            value["allow_any_symlink"] = True
        elif bad == "unknown-alias":
            value["aliases"]["scripts"] = ".github/skills"
        if bad == "duplicate-field":
            path.write_text('{"schema":"agent-validation-layout/v1","schema":"agent-validation-layout/v1","aliases":{}}')
        else:
            path.write_text(json.dumps(value) + "\n\n")
        if bad == "staged-layout":
            git(consumer, "add", LAYOUT_NAME)
        elif bad not in ("unstaged-layout", "staged-layout"):
            commit(consumer, "bad layout fixture")
    before = snapshot(consumer)
    result = invoke(source, root)
    assert result.returncode == 2, result.stdout + result.stderr
    assert snapshot(consumer) == before


@pytest.mark.parametrize("rel", [RECEIPT, RECOVERY, ".cursor/rules", "scripts"])
def test_unsafe_late_destination_has_zero_writes(producers, consumer, tmp_path, rel):
    outside = tmp_path / "outside"
    outside.mkdir()
    path = consumer / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    before = snapshot(consumer)
    result = invoke(producers["source"][0], consumer)
    assert result.returncode == 2, result.stderr
    assert snapshot(consumer) == before
    assert not list(outside.iterdir())


@pytest.mark.parametrize("changed", ["link", "canonical", "layout", "recipe", "prose"])
def test_change_after_plan_refuses_without_clobbering(producers, consumer, monkeypatch, changed):
    module = imported(producers["source"][0])
    original = module.build_plan
    observed = {}

    def alter(args):
        value = original(args)
        if changed == "link":
            p = consumer / ".claude/skills"
            p.unlink()
            p.symlink_to("../.github/skills", target_is_directory=True)
        elif changed == "canonical":
            p = consumer / ".github/skills"
            p.rename(consumer / ".github/old-skills")
            p.mkdir()
        else:
            p = consumer / {"layout": LAYOUT_NAME, "recipe": "config/agent-checks.json", "prose": "AGENTS.md"}[changed]
            p.write_bytes(p.read_bytes() + b"\nwriter\n")
        observed["snapshot"] = snapshot(consumer)
        return value
    monkeypatch.setattr(module, "build_plan", alter)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert snapshot(consumer) == observed["snapshot"]


def test_partial_batch_keeps_recovery_and_never_restores_over_writer(producers, consumer, monkeypatch, capsys):
    module = imported(producers["source"][0])
    original = module.OutputPlan.replace
    fired = []

    def after_first(plan, path, data, mode):
        original(plan, path, data, mode)
        if path == consumer / "scripts/agent-check.py" and not fired:
            fired.append(True)
            (consumer / "AGENTS.md").write_text("Concurrent owner prose.\n")
    monkeypatch.setattr(module.OutputPlan, "replace", after_first)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert (consumer / "AGENTS.md").read_text() == "Concurrent owner prose.\n"
    assert (consumer / "scripts/agent-check.py").is_file()
    journal = json.loads((consumer / RECOVERY).read_text())
    assert journal["status"] == "prepared"
    assert "scripts/agent-check.py" in journal["before"]
    assert "AGENT-VALIDATION-RECOVERY" in capsys.readouterr().err
    assert invoke(producers["source"][0], consumer, "--check").returncode == 2


@pytest.mark.parametrize("field", ["extra", "hash", "mapping", "source", "alias", "layout-digest"])
def test_malformed_provenance_fails_closed(producers, consumer, field):
    source = producers["source"][0]
    result = invoke(source, consumer)
    assert result.returncode == 0, result.stderr
    path = consumer / RECEIPT
    value = json.loads(path.read_text())
    if field == "extra":
        value["bypass"] = True
    elif field == "hash":
        value["files"][NAMES[0]] = "bad"
    elif field == "mapping":
        value["layout"]["destinations"][NAMES[0]]["canonical"] = "../escape"
    elif field == "source":
        value["layout"]["source"]["commit"] = "floating-main"
    elif field == "alias":
        value["layout"]["aliases"][".cursor/skills"]["mode"] = "100644"
    else:
        value["layout"]["layout_file"]["sha256"] = "0" * 64
    path.write_text(json.dumps(value))
    before = snapshot(consumer)
    result = invoke(source, consumer)
    assert result.returncode == 2
    assert snapshot(consumer) == before


def test_conflicting_output_plan_refuses_before_writes(producers, consumer, monkeypatch):
    module = imported(producers["source"][0])
    original = module.load_module

    def patched(name, path, data=None):
        loaded = original(name, path, data)
        if name == "sync_agent_validation":
            prior = loaded.outputs
            loaded.outputs = lambda root, policy: {**prior(root, policy),
                root / ".github/skills/agent-validation/SKILL.md": "Conflicting owner"}
        return loaded
    monkeypatch.setattr(module, "load_module", patched)
    before = snapshot(consumer)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert snapshot(consumer) == before


def test_foreign_git_cannot_select_identity(producers, consumer, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-q", "-b", "main")
    (foreign / "owner.txt").write_text("foreign")
    commit(foreign)
    before = snapshot(foreign)
    env = {**ENV, "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
           "GIT_INDEX_FILE": str(foreign / ".git/index"), "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "fixture.marker", "GIT_CONFIG_VALUE_0": "retained"}
    result = invoke(producers["source"][0], consumer, env=env)
    assert result.returncode == 0, result.stderr
    assert snapshot(foreign) == before
    git(consumer, "checkout", "-qb", "main")
    after = snapshot(consumer)
    assert invoke(producers["source"][0], consumer, env=env).returncode == 2
    assert snapshot(consumer) == after


def test_default_behavior_keeps_paths_and_six_key_provenance(producers, consumer):
    source = producers["source"][0]
    before = snapshot(consumer)
    assert invoke(source, consumer, layout=False).returncode == 2
    assert snapshot(consumer) == before
    for rel in (".claude/skills", ".cursor/skills"):
        p = consumer / rel
        p.unlink()
        p.mkdir()
    result = invoke(source, consumer, layout=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((consumer / RECEIPT).read_text())
    assert set(receipt) == {"schema", "source", "files", "recipes"}
    assert set(receipt["files"]) == set(NAMES)
    assert (consumer / ".claude/skills/agent-validation/SKILL.md").is_file()
    assert not (consumer / RECOVERY).exists()
    assert invoke(source, consumer, "--check", layout=False).returncode == 0


def test_changed_prose_during_render_is_not_clobbered(producers, consumer, monkeypatch):
    module = imported(producers["source"][0])
    original = module.load_module
    observed = {}

    def patched(name, path, data=None):
        loaded = original(name, path, data)
        if name == "sync_agent_validation":
            prior = loaded.outputs
            def render(root, policy):
                result = prior(root, policy)
                (root / "CLAUDE.md").write_text("Writer after render.\n")
                observed["snapshot"] = snapshot(root)
                return result
            loaded.outputs = render
        return loaded
    monkeypatch.setattr(module, "load_module", patched)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert snapshot(consumer) == observed["snapshot"]


def test_atomic_replacement_failure_retains_original_and_recovery(producers, consumer, monkeypatch):
    module = imported(producers["source"][0])
    original = module.os.replace

    def fail(src, dst, **kwargs):
        assert isinstance(kwargs["src_dir_fd"], int)
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        if dst == "CLAUDE.md":
            raise OSError("synthetic failure before atomic replacement")
        return original(src, dst, **kwargs)
    monkeypatch.setattr(module.os, "replace", fail)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert (consumer / "CLAUDE.md").read_text() == "Owner CLAUDE text.\n"
    record = json.loads((consumer / RECOVERY).read_text())
    assert record["status"] == "prepared"
    assert "scripts/agent-check.py" in record["completed"]
    assert not list(consumer.rglob(".agent-validation-*"))
    git(consumer, "add", ".")
    assert not git(consumer, "ls-files", "--", RECOVERY)


def test_case_ambiguity_added_after_planning_refuses(producers, consumer, monkeypatch):
    module = imported(producers["source"][0])
    original = module.build_plan

    def altered(args):
        plan = original(args)
        (consumer / ".github/Skills").mkdir()
        return plan
    monkeypatch.setattr(module, "build_plan", altered)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert not (consumer / "scripts").exists()
    assert not (consumer / RECOVERY).exists()


def test_malformed_managed_block_is_a_prewrite_hold(producers, consumer):
    (consumer / "CLAUDE.md").write_text("<!-- agent-validation:begin -->\nUnclosed owner block\n")
    before = snapshot(consumer)
    assert invoke(producers["source"][0], consumer).returncode == 2
    assert snapshot(consumer) == before


def test_matching_generated_installers_are_byte_identical():
    expected = (ROOT / "scripts/install-agent-validation.py").read_bytes()
    for prefix in ("plugin/manolii-framework", "registry/platform/framework"):
        assert (ROOT / prefix / "scripts/install-agent-validation.py").read_bytes() == expected


def test_gitless_initial_source_is_not_given_an_invented_origin(producers, consumer, tmp_path):
    """A missing source identity is a hold, not a floating-reference fallback."""
    source = tmp_path / "unqualified-gitless-source"
    shutil.copytree(producers["source"][0], source, ignore=shutil.ignore_patterns(".git"))
    before = snapshot(consumer)
    result = invoke(source, consumer)
    assert result.returncode == 2
    assert snapshot(consumer) == before


def test_regular_default_retains_portable_replacement(producers, consumer, monkeypatch):
    module = imported(producers["source"][0])
    monkeypatch.setattr(module, "DIRFD_ATOMIC", False)
    before = snapshot(consumer)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert snapshot(consumer) == before
    for rel in LAYOUT["aliases"]:
        path = consumer / rel
        path.unlink()
        path.mkdir()
    assert module.main(["--repo-root", str(consumer)]) == 0
    assert module.main(["--repo-root", str(consumer), "--check"]) == 0
    assert not (consumer / RECOVERY).exists()


@pytest.mark.parametrize("bad", ["unignored", "index-tracked", "head-tracked", "changed-policy", "global-only", "tracked-excludes-file", "inner-policy", "file-only-policy"])
def test_recovery_privacy_is_a_prewrite_requirement(producers, consumer, bad):
    policy = consumer / ".ai/.gitignore"
    if bad in ("unignored", "global-only", "tracked-excludes-file", "inner-policy", "file-only-policy"):
        policy.unlink()
        if bad == "tracked-excludes-file":
            (consumer / "config/.gitignore").write_text(".ai/agent-checks/\n")
            git(consumer, "config", "core.excludesFile", "config/.gitignore")
        elif bad == "inner-policy":
            policy = consumer / ".ai/agent-checks/.gitignore"
            policy.parent.mkdir()
            policy.write_text("layout-recovery.json\n")
        elif bad == "file-only-policy":
            policy.write_text("agent-checks/layout-recovery.json\n")
        commit(consumer, "remove private ignore")
        if bad == "global-only":
            (consumer / ".git/info/exclude").write_text(RECOVERY + "\n")
    elif bad == "changed-policy":
        policy.write_text("agent-checks/\n# unstaged\n")
    else:
        path = consumer / RECOVERY
        path.parent.mkdir()
        path.write_text('{"private":"before-image"}\n')
        git(consumer, "add", "-f", RECOVERY)
        if bad == "head-tracked":
            commit(consumer, "tracked private journal")
            git(consumer, "rm", "--cached", RECOVERY)
    before = snapshot(consumer)
    result = invoke(producers["source"][0], consumer)
    assert result.returncode == 2, result.stderr
    assert snapshot(consumer) == before


def test_recovery_ignore_change_after_plan_refuses(producers, consumer, monkeypatch):
    module = imported(producers["source"][0])
    original = module.build_plan
    observed = {}
    def altered(args):
        result = original(args)
        (consumer / ".ai/.gitignore").write_text("# writer removed ignore\n")
        observed["snapshot"] = snapshot(consumer)
        return result
    monkeypatch.setattr(module, "build_plan", altered)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert snapshot(consumer) == observed["snapshot"]


def test_unrepresentable_source_subdirectory_refuses_before_writes(producers, consumer, tmp_path):
    top = tmp_path / "producer"
    source = top / "source with space"
    shutil.copytree(producers["source"][0], source, ignore=shutil.ignore_patterns(".git"))
    git(top, "init", "-q", "-b", "main")
    commit(top)
    before = snapshot(consumer)
    result = invoke(source, consumer)
    assert result.returncode == 2, result.stderr
    assert snapshot(consumer) == before
