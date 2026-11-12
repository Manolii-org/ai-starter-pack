"""Synthetic execution acceptance for the opt-in layout contract; no real repos."""
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import re
import shutil
import subprocess
import sys

import pytest
import yaml

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


def make_case_variant(root):
    """Require distinct case-only siblings without adding a planning probe."""
    original, variant = root / ".github/skills", root / ".github/Skills"
    if variant.exists() and not variant.is_symlink() and variant.samefile(original):
        pytest.skip("filesystem cannot represent case-ambiguous siblings")
    variant.mkdir()
    return variant


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


def test_module_loader_uses_snapshot_even_with_valid_disk_bytecode(tmp_path):
    module = imported(ROOT)
    path = tmp_path / "bundled.py"
    path.write_text("VALUE = 'disk'\n")
    py_compile.compile(str(path), doraise=True)
    before = snapshot(tmp_path)
    loaded = module.load_module("frozen_bundle", path, b"VALUE = 'snapshot'\n")
    assert loaded.VALUE == "snapshot"
    assert loaded.__name__ == "frozen_bundle"
    assert loaded.__file__ == str(path)
    assert loaded.__spec__.origin == str(path)
    assert snapshot(tmp_path) == before


def test_module_loader_reads_once_and_ignores_subsequent_disk_change(tmp_path, monkeypatch):
    module = imported(ROOT)
    path = tmp_path / "bundled.py"
    path.write_bytes(b"VALUE = 'captured'\n")
    original = Path.read_bytes
    reads = []

    def capture(candidate):
        data = original(candidate)
        if candidate == path:
            reads.append(candidate)
            path.write_bytes(b"raise RuntimeError('changed disk must not execute')\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", capture)
    assert module.load_module("captured_bundle", path).VALUE == "captured"
    assert reads == [path]


@pytest.mark.parametrize("initial", [False, True])
def test_module_loader_restores_bytecode_policy_on_error(tmp_path, monkeypatch, initial):
    module = imported(ROOT)
    path = tmp_path / "bundled.py"
    monkeypatch.setattr(sys, "dont_write_bytecode", initial)
    before = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="synthetic source failure"):
        module.load_module("failed_bundle", path, b"raise RuntimeError('synthetic source failure')\n")
    assert sys.dont_write_bytecode is initial
    assert "failed_bundle" not in sys.modules
    assert snapshot(tmp_path) == before


def test_snapshot_loader_rejects_another_module_name(tmp_path):
    module = imported(ROOT)
    loader = module._SnapshotSourceLoader("expected", str(tmp_path / "bundled.py"), b"VALUE = 1\n")
    with pytest.raises(ImportError, match="another module"):
        loader.get_code("another")


def test_alias_git_identity_declares_nonsecurity_sha1(producers, consumer, monkeypatch):
    module = imported(producers["source"][0])
    original = module.hashlib.sha1
    calls = []

    def fips_sha1(data, **kwargs):
        if kwargs.get("usedforsecurity") is not False:
            raise ValueError("security SHA-1 unavailable in synthetic FIPS host")
        calls.append(kwargs)
        return original(data, **kwargs)

    monkeypatch.setattr(module.hashlib, "sha1", fips_sha1)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 0
    assert calls and all(value == {"usedforsecurity": False} for value in calls)
    record = json.loads((consumer / RECEIPT).read_text())
    raw = b"../.github/skills"
    expected = subprocess.check_output(["git", "hash-object", "--stdin"], cwd=consumer,
                                       input=raw, env=ENV, timeout=15).decode().strip()
    for value in record["layout"]["aliases"].values():
        assert value["blob"] == expected
        assert value["sha256"] == hashlib.sha256(raw).hexdigest()


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
        make_case_variant(consumer)
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


def test_sha256_repository_provenance_is_accepted_on_rerun(producers, tmp_path):
    """A SHA-256 consumer records 64-hex alias blobs; validation must accept them."""
    root = tmp_path / "consumer256"
    for rel in ("config", ".claude", ".cursor", ".github/skills", ".ai"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "config/agent-checks.json").write_text(json.dumps(
        {"schema_version": 1, "base": "origin/main", "checks": []}) + "\n")
    (root / LAYOUT_NAME).write_text(json.dumps(LAYOUT) + "\n")
    (root / ".ai/.gitignore").write_text("agent-checks/\n")
    (root / ".github/skills/owner.md").write_text("Other skill owner text.\n")
    for name in (".claude", ".cursor"):
        (root / name / "skills").symlink_to("../.github/skills", target_is_directory=True)
    try:
        git(root, "init", "-q", "--object-format=sha256", "-b", "codex/synthetic-layout")
    except subprocess.CalledProcessError:
        pytest.skip("git without SHA-256 repository support")
    commit(root)
    source = producers["source"][0]
    result = invoke(source, root)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / RECEIPT).read_text())
    assert len(receipt["layout"]["aliases"][".claude/skills"]["blob"]) == 64
    commit(root, "install")
    result = invoke(source, root)
    assert result.returncode == 0, result.stderr


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


def test_atomic_replacement_failure_retains_original_and_recovery(producers, consumer, monkeypatch, capsys):
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
    # Failed writes now retain their private owned inode instead of using
    # a racy pathname unlink. Bind recovery evidence to this owned orphan.
    leftovers = list(consumer.rglob(".agent-validation-*"))
    assert len(leftovers) == 1 and leftovers[0].is_file() and not leftovers[0].is_symlink()
    owned = leftovers[0].stat()
    assert owned.st_mode & 0o777 == 0o600
    error = capsys.readouterr().err
    temporary_record = json.loads(error.split("AGENT-VALIDATION-TEMPORARY-RECOVERY: ", 1)[1].splitlines()[0])
    assert temporary_record["held_identity"] == {"device": owned.st_dev, "inode": owned.st_ino}
    assert temporary_record["held_mode"] == "0600" and temporary_record["private"] is True
    assert temporary_record["name"] == leftovers[0].name
    git(consumer, "add", ".")
    assert not git(consumer, "ls-files", "--", RECOVERY)


@pytest.fixture(params=["source", "plugin", "registry"])
def replacement_kernel(producers, request):
    source = producers[request.param][0]
    spec = importlib.util.spec_from_file_location(
        "replacement_installer", source / "scripts/install-agent-validation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.DIRFD_ATOMIC:
        pytest.skip("directory-descriptor replacement unavailable")
    return module


@pytest.mark.parametrize("code", [errno.EACCES, errno.ENOSPC, errno.EROFS, errno.EDQUOT])
def test_temp_creation_failure_preserves_original_error_and_destination(
        replacement_kernel, tmp_path, monkeypatch, capsys, code):
    module = replacement_kernel
    root = tmp_path / "replacement"
    root.mkdir()
    path = root / "owner.txt"
    path.write_bytes(b"Owner bytes.\n")
    plan = module.OutputPlan(root)
    plan.add(path, b"Replacement.\n", "fixture")
    before = snapshot(root)
    original = module.os.open
    failure = OSError(code, "synthetic temporary-file creation failure")
    descriptors = []

    def fail(name, flags, *args, **kwargs):
        if flags & os.O_EXCL and str(name).startswith(".agent-validation-"):
            descriptors.append(kwargs["dir_fd"])
            raise failure
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", fail)
    with pytest.raises(OSError) as caught:
        plan.apply([path])
    assert caught.value is failure
    assert snapshot(root) == before
    assert plan.completed == []
    report = capsys.readouterr().err.split("AGENT-VALIDATION-RECOVERY: ", 1)[1]
    assert json.loads(report)["reason"] == str(failure)
    assert len(descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF


def test_temp_name_collision_preserves_other_writer_file(
        replacement_kernel, tmp_path, monkeypatch):
    module = replacement_kernel
    root = tmp_path / "collision"
    root.mkdir()
    path = root / "owner.txt"
    path.write_bytes(b"Owner bytes.\n")
    collision = root / (".agent-validation-" + bytes([17] * 16).hex())
    collision.write_bytes(b"Other writer's temporary bytes.\n")
    plan = module.OutputPlan(root)
    plan.add(path, b"Replacement.\n", "fixture")
    before = snapshot(root)
    monkeypatch.setattr(module.os, "urandom", lambda count: bytes([17] * count))
    with pytest.raises(FileExistsError):
        plan.apply([path])
    assert snapshot(root) == before
    assert plan.completed == []


def test_failed_replace_retains_owned_private_temp_and_closes_parent(
        replacement_kernel, tmp_path, monkeypatch, capsys):
    module = replacement_kernel
    root = tmp_path / "cleanup"
    root.mkdir()
    path = root / "owner.txt"
    path.write_bytes(b"Owner bytes.\n")
    plan = module.OutputPlan(root)
    plan.add(path, b"Replacement.\n", "fixture")
    primary = OSError(errno.ENOSPC, "synthetic replacement failure")
    descriptors = []

    def fail_replace(*args, **kwargs):
        descriptors.append(kwargs["src_dir_fd"])
        raise primary

    def fail_unlink(*args, **kwargs):
        raise AssertionError("failure cleanup must never unlink a pathname")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    monkeypatch.setattr(module.os, "unlink", fail_unlink)
    with pytest.raises(OSError) as caught:
        plan.apply([path])
    assert caught.value is primary
    assert path.read_bytes() == b"Owner bytes.\n"
    assert plan.completed == []
    error = capsys.readouterr().err
    record = json.loads(error.split("AGENT-VALIDATION-TEMPORARY-RECOVERY: ", 1)[1].splitlines()[0])
    assert record["private"] is True
    assert record["held_mode"] == "0600"
    assert record["held_links"] == 1
    assert json.loads(error.split("AGENT-VALIDATION-RECOVERY: ", 1)[1])["reason"] == str(primary)
    leftovers = list(root.glob(".agent-validation-*"))
    assert len(leftovers) == 1
    assert leftovers[0].stat().st_mode & 0o777 == 0o600
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("kind", ["source", "plugin", "registry"])
@pytest.mark.parametrize("explicit", [False, True])
def test_execute_only_ancestor_preserves_cli_modes(producers, consumer, tmp_path, kind, explicit):
    """A caller-selected ancestor needs search permission, not enumeration."""
    if not explicit:
        for alias in LAYOUT["aliases"]:
            (consumer / alias).unlink()
        commit(consumer)
    ancestor = tmp_path / "search-only"
    ancestor.mkdir()
    target = ancestor / "consumer"
    consumer.rename(target)
    source, _ = producers[kind]
    before = snapshot(target)
    ancestor.chmod(0o111)
    try:
        if os.access(ancestor, os.R_OK):
            pytest.skip("permission bits cannot deny enumeration for this user")
        with pytest.raises(PermissionError):
            list(ancestor.iterdir())
        for flag, expected in [("--dry-run", 0), ("--check", 1)]:
            result = invoke(source, target, flag, layout=explicit)
            assert result.returncode == expected, result.stderr
            assert snapshot(target) == before
        result = invoke(source, target, layout=explicit)
        assert result.returncode == 0, result.stderr
        after = snapshot(target)
        for flag in ["--check", "--dry-run"]:
            result = invoke(source, target, flag, layout=explicit)
            assert result.returncode == 0, result.stderr
            assert snapshot(target) == after
    finally:
        ancestor.chmod(0o700)


def test_unavailable_permission_precondition_skips_without_installing(
        producers, consumer, tmp_path, monkeypatch):
    """Unavailable permission isolation is a skip, with ancestor mode restored."""
    monkeypatch.setattr(os, "access", lambda *args, **kwargs: True)
    monkeypatch.setitem(globals(), "invoke", lambda *args, **kwargs: pytest.fail(
        "installer must not run when permission isolation is unavailable"))
    with pytest.raises(pytest.skip.Exception, match="permission bits cannot deny"):
        test_execute_only_ancestor_preserves_cli_modes(
            producers, consumer, tmp_path, "source", True)
    assert (tmp_path / "search-only").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("error", ["timeout", "failed-git"])
def test_git_validation_failure_reports_partial_batch(
        replacement_kernel, tmp_path, capsys, error):
    """A bounded Git failure reports known writes without clobbering later files."""
    module = replacement_kernel
    root = tmp_path / "partial-batch"
    root.mkdir()
    first, later = root / "first.txt", root / "later.txt"
    first.write_bytes(b"First owner bytes.\n")
    later.write_bytes(b"Later owner bytes.\n")
    plan = module.OutputPlan(root)
    plan.add(first, b"Qualified first replacement.\n", "fixture:first")
    plan.add(later, b"Unapplied later replacement.\n", "fixture:later")
    failure = (subprocess.TimeoutExpired(["git", "status"], 15) if error == "timeout"
               else subprocess.CalledProcessError(1, ["git", "status"]))

    def fail_after_first():
        if plan.completed:
            raise failure

    plan.validators.append(fail_after_first)
    with pytest.raises(subprocess.SubprocessError) as caught:
        plan.apply([first, later])
    assert caught.value is failure
    assert first.read_bytes() == b"Qualified first replacement.\n"
    assert later.read_bytes() == b"Later owner bytes.\n"
    assert plan.completed == ["first.txt"]
    report = json.loads(capsys.readouterr().err.split("AGENT-VALIDATION-RECOVERY: ", 1)[1])
    assert report["completed"] == plan.completed
    assert report["reason"] == str(failure)
    assert not list(root.glob(".agent-validation-*"))


def test_case_ambiguity_added_after_planning_refuses(producers, consumer, monkeypatch):
    module = imported(producers["source"][0])
    original = module.build_plan

    def altered(args):
        plan = original(args)
        make_case_variant(consumer)
        return plan
    monkeypatch.setattr(module, "build_plan", altered)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
    assert not (consumer / "scripts").exists()
    assert not (consumer / RECOVERY).exists()


@pytest.mark.parametrize("phase", ["before-plan", "after-plan"])
def test_unavailable_case_fixture_skips_without_writes(
        producers, consumer, tmp_path, monkeypatch, phase):
    """Model case-folding host APIs through each actual fixture caller."""
    original = consumer / ".github/skills"
    variant = consumer / ".github/Skills"
    exists, samefile, mkdir = Path.exists, Path.samefile, Path.mkdir
    monkeypatch.setattr(Path, "exists", lambda path: True if path == variant else exists(path))
    monkeypatch.setattr(Path, "samefile", lambda path, other:
                        True if path == variant and Path(other) == original else samefile(path, other))

    def reject_duplicate(path, *args, **kwargs):
        if path == variant:
            raise FileExistsError(errno.EEXIST, "case-folded fixture already exists", str(path))
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", reject_duplicate)
    before = snapshot(consumer)
    with pytest.raises(pytest.skip.Exception, match="case-ambiguous siblings"):
        if phase == "before-plan":
            test_unsafe_layout_refuses_without_writes(producers, consumer, tmp_path, "case")
        else:
            test_case_ambiguity_added_after_planning_refuses(producers, consumer, monkeypatch)
    assert snapshot(consumer) == before


def test_existing_distinct_case_sibling_is_not_an_unavailable_precondition(consumer):
    """A real existing sibling remains a fixture error, with its bytes intact."""
    variant = make_case_variant(consumer)
    (variant / "owner.txt").write_bytes(b"Existing sibling owner bytes.\n")
    before = snapshot(consumer)
    with pytest.raises(FileExistsError):
        make_case_variant(consumer)
    assert snapshot(consumer) == before


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


@pytest.mark.parametrize("kind", ["ancestor", "leaf"])
def test_default_explicit_recipe_input_retains_link_compatibility(producers, consumer, tmp_path, kind):
    for rel in (".claude/skills", ".cursor/skills"):
        (consumer / rel).unlink()
        (consumer / rel).mkdir()
    recipes = tmp_path / "owner-recipes"
    recipes.mkdir()
    raw = b'{"schema_version":1,"base":"origin/main","checks":[]}\n'
    (recipes / "checks.json").write_bytes(raw)
    alias = tmp_path / "recipe-input"
    alias.symlink_to(recipes if kind == "ancestor" else recipes / "checks.json")
    supplied = alias / "checks.json" if kind == "ancestor" else alias
    source = producers["source"][0]
    before = snapshot(consumer)
    flags = ("--recipes", str(supplied))
    assert invoke(source, consumer, *flags, "--dry-run", layout=False).returncode == 0
    assert invoke(source, consumer, *flags, "--check", layout=False).returncode == 1
    assert snapshot(consumer) == before
    result = invoke(source, consumer, *flags, layout=False)
    assert result.returncode == 0, result.stderr
    assert (consumer / "config/agent-checks.json").read_bytes() == raw
    after = snapshot(consumer)
    for extra in [("--check",), ("--dry-run",), ()]:
        result = invoke(source, consumer, *flags, *extra, layout=False)
        assert result.returncode == 0, result.stderr
        assert snapshot(consumer) == after
    assert alias.is_symlink()


@pytest.mark.parametrize("flags", [(), ("--check",), ("--dry-run",)])
def test_explicit_layout_still_rejects_linked_recipe_input(producers, consumer, tmp_path, flags):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "checks.json").write_bytes((consumer / "config/agent-checks.json").read_bytes())
    alias = tmp_path / "recipe-input"
    alias.symlink_to(recipes, target_is_directory=True)
    before = snapshot(consumer)
    result = invoke(producers["source"][0], consumer, "--recipes", str(alias / "checks.json"), *flags)
    assert result.returncode == 2, result.stderr
    assert snapshot(consumer) == before


@pytest.mark.parametrize("changed", ["alias", "canonical"])
def test_default_recipe_mapping_and_bytes_are_revalidated(producers, consumer, tmp_path, monkeypatch, changed):
    for rel in (".claude/skills", ".cursor/skills"):
        (consumer / rel).unlink()
        (consumer / rel).mkdir()
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    recipe = recipes / "checks.json"
    recipe.write_bytes((consumer / "config/agent-checks.json").read_bytes())
    alias = tmp_path / "recipe-input"
    alias.symlink_to(recipes, target_is_directory=True)
    module = imported(producers["source"][0])
    original = module.build_plan
    altered_inputs = []
    def altered(args):
        plan = original(args)
        if changed == "alias":
            other = tmp_path / "other-recipes"
            other.mkdir()
            (other / "checks.json").write_bytes(recipe.read_bytes())
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
        else:
            recipe.write_bytes(recipe.read_bytes() + b"\n")
        altered_inputs.append(changed)
        return plan
    monkeypatch.setattr(module, "build_plan", altered)
    before = snapshot(consumer)
    assert module.main(["--repo-root", str(consumer), "--recipes", str(alias / "checks.json")]) == 2
    assert altered_inputs == [changed]
    assert snapshot(consumer) == before


@pytest.mark.parametrize("probe", [b"rev-parse", b"check-ref-format"])
@pytest.mark.parametrize("explicit", [False, True])
def test_owner_branch_probe_timeout_is_bounded_and_prewrite(producers, consumer, monkeypatch, capsys, probe, explicit):
    if not explicit:
        for rel in (".claude/skills", ".cursor/skills"):
            (consumer / rel).unlink()
            (consumer / rel).mkdir()
    module = imported(producers["source"][0])
    original = module.subprocess.run
    calls = []
    def timeout(command, *args, **kwargs):
        if command[:2] == [b"git", probe]:
            calls.append(command)
            assert kwargs["timeout"] == 15
            assert kwargs["cwd"] == consumer and kwargs["capture_output"] is True
            assert "GIT_DIR" not in kwargs["env"]
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original(command, *args, **kwargs)
    monkeypatch.setattr(module.subprocess, "run", timeout)
    before = snapshot(consumer)
    flags = ["--layout", LAYOUT_NAME] if explicit else []
    assert module.main(["--repo-root", str(consumer), *flags]) == 2
    assert len(calls) == 1
    assert "AGENT-VALIDATION-INSTALL-BLOCKED" in capsys.readouterr().err
    assert snapshot(consumer) == before


@pytest.mark.parametrize("case", ["source", "installed-uncommitted", "installed-committed", "layout"])
@pytest.mark.parametrize("mode", [0o755, 0o655])
@pytest.mark.parametrize("flags", [(), ("--check",), ("--dry-run",)])
def test_owner_execute_mode_contract(producers, consumer, case, mode, flags):
    source, top = producers["source"]
    unavailable = None
    if case.startswith("installed"):
        result = invoke(source, consumer)
        assert result.returncode == 0, result.stderr
        if case == "installed-committed":
            commit(consumer, "installed bundle")
        unavailable = top.with_name(top.name + "-mode-unavailable")
        top.rename(unavailable)
        source = consumer
        changed = consumer / "scripts/agent-check.py"
    elif case == "layout":
        changed = consumer / LAYOUT_NAME
        changed.chmod(0o755)
        commit(consumer, "executable owner layout")
    else:
        changed = source / "scripts/agent-check.py"
    original_mode = changed.stat().st_mode & 0o777
    changed.chmod(mode)
    before = snapshot(consumer)
    source_before = snapshot(unavailable if unavailable else top)
    try:
        result = invoke(source, consumer, *flags)
        expected = 2 if mode == 0o655 else int(flags == ("--check",) and not case.startswith("installed"))
        assert result.returncode == expected, result.stderr
        if mode == 0o655 or flags:
            assert snapshot(consumer) == before
        assert snapshot(unavailable if unavailable else top) == source_before
    finally:
        changed.chmod(original_mode)
        if unavailable:
            unavailable.rename(top)


@pytest.mark.parametrize("kind", ["plugin", "registry"])
def test_packaged_source_owner_execute_refusal(producers, consumer, kind):
    source, top = producers[kind]
    path = source / "scripts/agent-check.py"
    original_mode = path.stat().st_mode & 0o777
    path.chmod(0o655)
    before, source_before = snapshot(consumer), snapshot(top)
    try:
        result = invoke(source, consumer)
        assert result.returncode == 2, result.stderr
        assert snapshot(consumer) == before
        assert snapshot(top) == source_before
    finally:
        path.chmod(original_mode)


@pytest.mark.parametrize("changed", ["source-mode", "source-index", "installed-mode", "installed-index", "layout-mode"])
def test_mode_or_index_change_after_planning_is_prewrite(producers, consumer, monkeypatch, changed):
    source, top = producers["source"]
    if changed.startswith("installed"):
        assert invoke(source, consumer).returncode == 0
        commit(consumer, "installed bundle")
    if changed == "layout-mode":
        (consumer / LAYOUT_NAME).chmod(0o755)
        commit(consumer, "executable owner layout")
    module = imported(source)
    original = module.build_plan
    observed = {}
    index_before = (top / ".git/index").read_bytes()
    path = (source / "scripts/agent-check.py" if changed.startswith("source") else
            consumer / (LAYOUT_NAME if changed == "layout-mode" else "scripts/agent-check.py"))
    original_mode = path.stat().st_mode & 0o777
    def altered(args):
        plan = original(args)
        if changed.endswith("index"):
            git(top if changed.startswith("source") else consumer,
                "update-index", "--chmod=-x", "scripts/agent-check.py")
        else:
            path.chmod(0o655)
        observed["target"] = snapshot(consumer)
        observed["source"] = snapshot(top)
        return plan
    monkeypatch.setattr(module, "build_plan", altered)
    try:
        assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME]) == 2
        assert snapshot(consumer) == observed["target"]
        assert snapshot(top) == observed["source"]
    finally:
        path.chmod(original_mode)
        (top / ".git/index").write_bytes(index_before)


def consumer_skill_contract(path):
    """Generic consumer discovery contract, independent of Pack's registry lint."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---\n", 2)
    metadata = yaml.safe_load(frontmatter)
    for field in ("name", "description", "author", "version", "license"):
        assert isinstance(metadata.get(field), str) and metadata[field].strip(), field
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
    assert metadata["name"] == path.parent.name
    assert re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", metadata["version"])
    assert re.search(r"^# \S.*$", body, re.MULTILINE)
    return metadata, body


@pytest.mark.parametrize("kind", ["source", "plugin", "registry"])
def test_consumer_skill_metadata_and_installed_origin(producers, consumer, kind):
    """The authored/rendered/installed skill satisfies discovery without byte edits."""
    source, top = producers[kind]
    logical = ".claude/skills/agent-validation/SKILL.md"
    skill = source / (logical if kind == "source" else logical.removeprefix(".claude/"))
    metadata, body = consumer_skill_contract(skill)
    assert metadata["author"] == "AI Starter Pack contributors"
    assert metadata["license"] == "Apache-2.0"
    assert body.lstrip().startswith("# Agent Validation\n")
    # Metadata and discovery heading must not change the existing instructions.
    expected = (ROOT / ".claude/skills/agent-validation/SKILL.md").read_text()
    assert skill.read_text() == expected
    before = snapshot(consumer)
    assert invoke(source, consumer, "--dry-run").returncode == 0
    assert snapshot(consumer) == before
    applied = invoke(source, consumer)
    assert applied.returncode == 0, applied.stderr
    installed = consumer / ".github/skills/agent-validation/SKILL.md"
    assert consumer_skill_contract(installed) == (metadata, body)
    assert installed.read_bytes() == skill.read_bytes()
    receipt = json.loads((consumer / RECEIPT).read_text())
    assert set(receipt["files"]) == set(NAMES)
    assert receipt["files"][logical] == hashlib.sha256(installed.read_bytes()).hexdigest()
    after = snapshot(consumer)
    unavailable = top.with_name(top.name + "-unavailable")
    top.rename(unavailable)
    try:
        for flag in ("--check", "--dry-run"):
            checked = invoke(consumer, consumer, flag)
            assert checked.returncode == 0, checked.stderr
            assert snapshot(consumer) == after
    finally:
        unavailable.rename(top)


@pytest.mark.parametrize("bad", ["name", "description", "author", "version", "license", "h1", "directory", "semver"])
def test_consumer_skill_contract_rejects_missing_requirements(tmp_path, bad):
    """Warnings about optional examples/evals do not waive discovery requirements."""
    source = (ROOT / ".claude/skills/agent-validation/SKILL.md").read_text()
    _, frontmatter, body = source.split("---\n", 2)
    metadata = yaml.safe_load(frontmatter)
    if bad in metadata:
        del metadata[bad]
    elif bad == "h1":
        body = body.replace("# Agent Validation\n", "")
    elif bad == "directory":
        metadata["name"] = "another-skill"
    elif bad == "semver":
        metadata["version"] = "1.0"
    path = tmp_path / "agent-validation/SKILL.md"
    path.parent.mkdir()
    path.write_text("---\n" + yaml.safe_dump(metadata) + "---\n" + body)
    with pytest.raises(AssertionError):
        consumer_skill_contract(path)


def test_plan_link_replacement_refused_when_snapshot_metadata_repeats(tmp_path, monkeypatch):
    """Inode pinning cannot rely on timestamp resolution or a reused snapshot."""
    module = imported(ROOT)
    root = tmp_path / "link-plan"
    root.mkdir()
    (root / "target").mkdir()
    alias = root / "alias"
    alias.symlink_to("target", target_is_directory=True)
    original_open, original_state = module.os.open, module.state
    opened = []

    def observe_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == alias:
            opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(module.os, "open", observe_open)
    plan = module.OutputPlan(root)
    expected = plan.watch(alias)
    alias.unlink()
    alias.symlink_to("target", target_is_directory=True)
    monkeypatch.setattr(module, "state", lambda path: expected if path == alias else original_state(path))
    try:
        with pytest.raises(ValueError, match="link identity changed"):
            plan.revalidate()
    finally:
        close = getattr(plan, "close", None)
        if close is not None:
            close()
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("replacement", ["link", "regular"])
def test_failed_cleanup_preserves_another_writers_temp_entry(
        replacement_kernel, tmp_path, monkeypatch, capsys, replacement):
    """Failure cleanup owns the original descriptor, never a replacement path."""
    module = replacement_kernel
    root = tmp_path / "changed-temp"
    root.mkdir()
    path = root / "owner.txt"
    path.write_bytes(b"Owner bytes.\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Outside bytes.\n")
    outside.chmod(0o644)
    primary = OSError(errno.ENOSPC, "synthetic replacement failure")
    names = []

    def replace_after_other_writer(src, dst, **kwargs):
        descriptor = kwargs["src_dir_fd"]
        module.os.unlink(src, dir_fd=descriptor)
        if replacement == "link":
            module.os.symlink(str(outside), src, dir_fd=descriptor)
        else:
            handle = module.os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
                                    dir_fd=descriptor)
            os.fchmod(handle, 0o644)
            with os.fdopen(handle, "wb") as stream:
                stream.write(b"Other writer bytes.\n")
        names.append(root / src)
        raise primary

    monkeypatch.setattr(module.os, "replace", replace_after_other_writer)
    plan = module.OutputPlan(root)
    plan.add(path, b"Replacement.\n", "fixture")
    with pytest.raises(OSError) as caught:
        plan.apply([path])
    assert caught.value is primary
    assert path.read_bytes() == b"Owner bytes.\n"
    assert outside.read_bytes() == b"Outside bytes.\n"
    assert outside.stat().st_mode & 0o777 == 0o644
    assert len(names) == 1
    if replacement == "link":
        assert names[0].is_symlink() and os.readlink(names[0]) == str(outside)
    else:
        assert names[0].read_bytes() == b"Other writer bytes.\n"
        assert names[0].stat().st_mode & 0o777 == 0o644
    assert "temporary path no longer names the owned file" in capsys.readouterr().err


def test_completed_rename_error_keeps_published_output_mode(
        replacement_kernel, tmp_path, monkeypatch, capsys):
    """An ambiguous completed rename is not an unpublished private orphan."""
    module = replacement_kernel
    root = tmp_path / "ambiguous-rename"
    root.mkdir()
    path = root / "owner.txt"
    path.write_bytes(b"Owner bytes.\n")
    path.chmod(0o644)
    plan = module.OutputPlan(root)
    plan.add(path, b"Replacement.\n", "fixture")
    original = module.os.replace
    primary = OSError(errno.EIO, "synthetic error after completed rename")

    def completed_then_error(*args, **kwargs):
        original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(module.os, "replace", completed_then_error)
    with pytest.raises(OSError) as caught:
        plan.apply([path])
    assert caught.value is primary
    assert path.read_bytes() == b"Replacement.\n"
    assert path.stat().st_mode & 0o777 == 0o644
    assert not list(root.glob(".agent-validation-*"))
    assert plan.completed == []
    report = capsys.readouterr().err.split("AGENT-VALIDATION-RECOVERY: ", 1)[1]
    assert json.loads(report)["reason"] == str(primary)


@pytest.mark.parametrize("replacement", ["link", "regular"])
def test_failed_cleanup_preserves_replacement_after_final_name_snapshot(
        replacement_kernel, tmp_path, monkeypatch, capsys, replacement):
    """A checked basename never authorizes a later unlink of another inode."""
    module = replacement_kernel
    root = tmp_path / "cleanup-last-stat"
    root.mkdir()
    path = root / "owner.txt"
    path.write_bytes(b"Owner bytes.\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Outside bytes.\n")
    outside.chmod(0o644)
    primary = OSError(errno.ENOSPC, "synthetic replacement failure")
    original_stat, original_unlink, original_open = module.os.stat, module.os.unlink, module.os.open
    checked = []

    def replace_after_last_snapshot(name, *args, **kwargs):
        observed = original_stat(name, *args, **kwargs)
        if isinstance(name, str) and name.startswith(".agent-validation-") and "dir_fd" in kwargs:
            checked.append(name)
            if len(checked) == 2:
                parent = kwargs["dir_fd"]
                original_unlink(name, dir_fd=parent)
                if replacement == "link":
                    module.os.symlink(str(outside), name, dir_fd=parent)
                else:
                    handle = original_open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                           0o644, dir_fd=parent)
                    os.fchmod(handle, 0o644)
                    with os.fdopen(handle, "wb") as stream:
                        stream.write(b"Other writer bytes.\n")
        return observed

    monkeypatch.setattr(module.os, "stat", replace_after_last_snapshot)
    monkeypatch.setattr(module.os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(primary))
    plan = module.OutputPlan(root)
    plan.add(path, b"Replacement.\n", "fixture")
    with pytest.raises(OSError) as caught:
        plan.apply([path])
    assert caught.value is primary
    assert len(checked) == 2
    other = root / checked[-1]
    if replacement == "link":
        assert other.is_symlink() and os.readlink(other) == str(outside)
    else:
        assert other.read_bytes() == b"Other writer bytes.\n"
        assert other.stat().st_mode & 0o777 == 0o644
    assert outside.read_bytes() == b"Outside bytes.\n"
    assert outside.stat().st_mode & 0o777 == 0o644
    assert path.read_bytes() == b"Owner bytes.\n"
    assert plan.completed == []
    error = capsys.readouterr().err
    record = json.loads(error.split("AGENT-VALIDATION-TEMPORARY-RECOVERY: ", 1)[1].splitlines()[0])
    assert record["held_mode"] == "0600"
    assert record["name_observation"] == "owned_at_check"
    assert record["name"] == checked[-1]


def test_moved_temporary_is_not_claimed_private(
        replacement_kernel, tmp_path, monkeypatch, capsys):
    """A missing name can be an ambiguous completed move, so do not chmod it."""
    module = replacement_kernel
    root = tmp_path / "cleanup-moved"
    root.mkdir()
    path, moved = root / "owner.txt", root / "moved.txt"
    path.write_bytes(b"Owner bytes.\n")
    path.chmod(0o644)
    primary = OSError(errno.EIO, "synthetic error after another move")
    original = module.os.replace

    def moved_then_error(src, dst, **kwargs):
        original(src, "moved.txt", **kwargs)
        raise primary

    monkeypatch.setattr(module.os, "replace", moved_then_error)
    plan = module.OutputPlan(root)
    plan.add(path, b"Replacement.\n", "fixture")
    with pytest.raises(OSError) as caught:
        plan.apply([path])
    assert caught.value is primary
    assert path.read_bytes() == b"Owner bytes.\n"
    assert moved.read_bytes() == b"Replacement.\n"
    assert moved.stat().st_mode & 0o777 == 0o644
    assert not list(root.glob(".agent-validation-*"))
    assert "AGENT-VALIDATION-TEMPORARY-RECOVERY:" not in capsys.readouterr().err


def test_failed_privacy_change_reports_actual_held_mode(
        replacement_kernel, tmp_path, monkeypatch, capsys):
    """A denied chmod preserves the primary failure without claiming privacy."""
    module = replacement_kernel
    root = tmp_path / "cleanup-privacy-denied"
    root.mkdir()
    path = root / "owner.txt"
    path.write_bytes(b"Owner bytes.\n")
    path.chmod(0o644)
    primary = OSError(errno.ENOSPC, "synthetic replacement failure")
    original_fchmod = module.os.fchmod

    def deny_privacy(handle, mode):
        if mode == 0o600:
            raise PermissionError(errno.EACCES, "synthetic privacy denial")
        return original_fchmod(handle, mode)

    monkeypatch.setattr(module.os, "fchmod", deny_privacy)
    monkeypatch.setattr(module.os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(primary))
    plan = module.OutputPlan(root)
    plan.add(path, b"Replacement.\n", "fixture")
    with pytest.raises(OSError) as caught:
        plan.apply([path])
    assert caught.value is primary
    leftovers = list(root.glob(".agent-validation-*"))
    assert len(leftovers) == 1
    assert leftovers[0].stat().st_mode & 0o777 == 0o644
    error = capsys.readouterr().err
    assert "synthetic privacy denial" in error
    record = json.loads(error.split("AGENT-VALIDATION-TEMPORARY-RECOVERY: ", 1)[1].splitlines()[0])
    assert record["held_mode"] == "0644"
    assert record["private"] is False


@pytest.mark.parametrize("kind", ["ancestor", "leaf"])
def test_missing_link_handle_flags_preserve_legacy_recipe_cli(producers, consumer, tmp_path, kind):
    for rel in (".claude/skills", ".cursor/skills"):
        (consumer / rel).unlink()
        (consumer / rel).mkdir()
    recipes = tmp_path / "portable-recipes"
    recipes.mkdir()
    raw = b'{"schema_version":1,"base":"origin/main","checks":[]}\n'
    (recipes / "checks.json").write_bytes(raw)
    alias = tmp_path / "portable-recipe-alias"
    alias.symlink_to(recipes if kind == "ancestor" else recipes / "checks.json")
    supplied = alias / "checks.json" if kind == "ancestor" else alias
    source = producers["source"][0]
    wrapper = ("import os,runpy,sys\n"
               "for name in ('O_PATH','O_NOFOLLOW','O_SYMLINK'):\n"
               "    if hasattr(os,name): delattr(os,name)\n"
               "sys.argv=sys.argv[1:]\nrunpy.run_path(sys.argv[0],run_name='__main__')\n")
    environment = {**ENV, **{k: os.environ[k] for k in (
        "HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS") if k in os.environ}}
    def run(*flags):
        return subprocess.run([sys.executable, "-B", "-c", wrapper,
            str(source / "scripts/install-agent-validation.py"), "--repo-root", str(consumer),
            "--recipes", str(supplied), *flags], env=environment, cwd=tmp_path,
            capture_output=True, text=True, timeout=30)
    before = snapshot(consumer)
    dry, check = run("--dry-run"), run("--check")
    assert dry.returncode == 0, dry.stderr
    assert check.returncode == 1, check.stderr
    assert snapshot(consumer) == before
    applied = run()
    assert applied.returncode == 0, applied.stderr
    assert (consumer / "config/agent-checks.json").read_bytes() == raw
    after = snapshot(consumer)
    for flags in [("--check",), ("--dry-run",), ()]:
        result = run(*flags)
        assert result.returncode == 0, result.stderr
        assert snapshot(consumer) == after
    assert alias.is_symlink()


@pytest.mark.parametrize("changed", ["alias", "canonical"])
def test_portable_link_recipe_inputs_still_revalidate(producers, consumer, tmp_path, monkeypatch, changed):
    for name in ("O_PATH", "O_NOFOLLOW", "O_SYMLINK"):
        monkeypatch.delattr(os, name, raising=False)
    for rel in (".claude/skills", ".cursor/skills"):
        (consumer / rel).unlink()
        (consumer / rel).mkdir()
    recipes = tmp_path / "portable-recipes"
    recipes.mkdir()
    recipe = recipes / "checks.json"
    recipe.write_bytes((consumer / "config/agent-checks.json").read_bytes())
    alias = tmp_path / "portable-recipe-alias"
    alias.symlink_to(recipes, target_is_directory=True)
    module = imported(producers["source"][0])
    assert module.DIRFD_ATOMIC is False
    original = module.build_plan
    changed_inputs = []
    def altered(args):
        plan = original(args)
        if changed == "alias":
            alternate = tmp_path / "alternate-recipes"
            alternate.mkdir()
            (alternate / "checks.json").write_bytes(recipe.read_bytes())
            alias.unlink()
            alias.symlink_to(alternate, target_is_directory=True)
        else:
            recipe.write_bytes(recipe.read_bytes() + b"\n")
        changed_inputs.append(changed)
        return plan
    monkeypatch.setattr(module, "build_plan", altered)
    before = snapshot(consumer)
    assert module.main(["--repo-root", str(consumer), "--recipes", str(alias / "checks.json")]) == 2
    assert changed_inputs == [changed]
    assert snapshot(consumer) == before


@pytest.mark.parametrize("flags", [(), ("--check",), ("--dry-run",)])
def test_missing_link_handle_flags_do_not_qualify_explicit_layout(producers, consumer, monkeypatch, flags):
    for name in ("O_PATH", "O_NOFOLLOW", "O_SYMLINK"):
        monkeypatch.delattr(os, name, raising=False)
    module = imported(producers["source"][0])
    assert module.DIRFD_ATOMIC is False
    before = snapshot(consumer)
    assert module.main(["--repo-root", str(consumer), "--layout", LAYOUT_NAME, *flags]) == 2
    assert snapshot(consumer) == before


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_link_fileio_failure_closes_descriptor_without_masking_primary(tmp_path, monkeypatch, cleanup_error):
    module = imported(ROOT)
    root = tmp_path / "constructor-failure"
    root.mkdir()
    (root / "target").write_text("synthetic")
    alias = root / "alias"
    alias.symlink_to("target")
    opened = []
    original_open, original_close = os.open, os.close
    primary = ValueError("synthetic FileIO initialization failure")
    def opening(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if Path(path) == alias:
            opened.append(fd)
        return fd
    def closing(fd):
        original_close(fd)
        if cleanup_error and fd in opened:
            raise OSError(errno.EIO, "synthetic close reporting failure")
    def construct(*args, **kwargs):
        raise primary
    monkeypatch.setattr(module.os, "open", opening)
    monkeypatch.setattr(module.os, "close", closing)
    monkeypatch.setattr(module.io, "FileIO", construct)
    plan = module.OutputPlan(root)
    try:
        with pytest.raises(ValueError) as error:
            plan.watch(alias)
        assert error.value is primary
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        assert plan._link_handles == {}
        if cleanup_error:
            assert isinstance(error.value.__context__, OSError)
            assert error.value.__context__.errno == errno.EIO
    finally:
        plan.close()
        for fd in opened:
            try:
                os.fstat(fd)
            except OSError:
                continue
            original_close(fd)


@pytest.mark.skipif(not hasattr(os, "O_PATH") or not hasattr(os, "O_NOFOLLOW"),
                    reason="actual O_PATH directory constructor behavior requires these host flags")
def test_link_replaced_with_directory_closes_actual_constructor_failure(tmp_path, monkeypatch):
    module = imported(ROOT)
    root = tmp_path / "directory-race"
    root.mkdir()
    (root / "target").mkdir()
    alias = root / "alias"
    alias.symlink_to("target", target_is_directory=True)
    original_open = os.open
    opened = []
    def opening(path, flags, *args, **kwargs):
        if Path(path) == alias:
            alias.rename(root / "retained-alias")
            alias.mkdir()
        fd = original_open(path, flags, *args, **kwargs)
        if Path(path) == alias:
            opened.append(fd)
        return fd
    monkeypatch.setattr(module.os, "open", opening)
    plan = module.OutputPlan(root)
    try:
        with pytest.raises(IsADirectoryError):
            plan.watch(alias)
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        assert plan._link_handles == {}
    finally:
        plan.close()
        for fd in opened:
            try:
                os.fstat(fd)
            except OSError:
                continue
            os.close(fd)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"),
                    reason="requires actual no-follow directory descriptor support")
def test_explicit_layout_never_downgrades_with_directory_descriptors_only(tmp_path, monkeypatch):
    for name in ("O_PATH", "O_SYMLINK"):
        monkeypatch.delattr(os, name, raising=False)
    module = imported(ROOT)
    assert module.DIRFD_ATOMIC is True
    root = tmp_path / "explicit-no-link-handle"
    root.mkdir()
    (root / "target").mkdir()
    alias = root / "alias"
    alias.symlink_to("target", target_is_directory=True)
    plan = module.OutputPlan(root, layout=True)
    try:
        with pytest.raises(ValueError, match="symlink"):
            plan.watch(alias)
        assert plan._link_handles == {}
    finally:
        plan.close()
