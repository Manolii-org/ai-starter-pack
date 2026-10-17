"""Exercise native installation, preservation, failure recovery and loading limits."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from lib import native_guidance as native


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    source = tmp_path / "source"
    plugin = source / "registry/platform/framework"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"framework","version":"1.14.0"}')
    (source / "registry/plugins.json").write_text(json.dumps({"plugins": [{
        "scope": "platform", "name": "framework", "path": "registry/platform/framework"}]}))
    for name in native.NAMES:
        for leaf in ("SKILL.md", "evals/cases.json"):
            dst = plugin / f"skills/{name}/{leaf}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((REPO / f".claude/skills/{name}/{leaf}").read_bytes())
    root = tmp_path / "consumer"
    root.mkdir()
    (root / "ai-manifest.yaml").write_text(
        'version: 1\nuniverse: example\nrequires:\n  - plugin: platform/framework\n    ref: "^1.14"\n'
        'surfaces: [claude-code, codex, cursor, devin]\n')
    return root, source


def run(consumer, *flags):
    root, source = consumer
    return subprocess.run([sys.executable, str(REPO / "scripts/ai-resolve.py"),
                           "--repo-root", str(root), "--registry", str(source),
                           "--native-guidance", *flags],
                          capture_output=True, text=True, timeout=30)


def files(root):
    # The existing resolver mutex is operational state, not an installed file.
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name != "capability-apply.lock"}


def test_preview_install_repeat_and_check_preserve_local_guidance(consumer):
    root, _ = consumer
    local = b"# Product rules\r\n\r\nPreserve domain/security/release instructions.\r\n"
    (root / "AGENTS.md").write_bytes(local)
    (root / "CLAUDE.md").write_bytes(b"# Claude local\nNo production release.\n")
    before = files(root)
    assert run(consumer).returncode == 0
    assert files(root) == before
    result = run(consumer, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "AGENTS.md").read_bytes().endswith(local)
    assert (root / "CLAUDE.md").read_bytes().splitlines()[1] == b"@.claude/skills/adaptable-development/SKILL.md"
    assert (root / ".cursor/rules/adaptable-development.mdc").read_text().startswith("---\n")
    assert "alwaysApply: true" in (root / ".cursor/rules/adaptable-development.mdc").read_text()
    installed = files(root)
    assert run(consumer, "--apply").returncode == 0
    assert files(root) == installed
    assert run(consumer, "--check").returncode == 0
    assert (root / "AGENTS.md").read_bytes().count(native.BEGIN) == 1


def test_update_preserves_new_local_text(consumer):
    root, source = consumer
    assert run(consumer, "--apply").returncode == 0
    with (root / "AGENTS.md").open("ab") as stream:
        stream.write(b"\nNew local rule survives.\n")
    # A reviewed update changes only the managed source, never local tails.
    skill = source / "registry/platform/framework/skills/adaptable-development/SKILL.md"
    skill.write_text(skill.read_text().replace("version: 1.0.0", "version: 1.0.1")
                     .replace("Build the current approved feature", "Implement the current approved feature"))
    assert run(consumer, "--check").returncode == 1
    assert run(consumer, "--apply").returncode == 0
    assert (root / "AGENTS.md").read_bytes().endswith(b"New local rule survives.\n")
    assert json.loads((root / native.LOCK).read_text())["policy_version"] == "1.0.1"


@pytest.mark.parametrize("path", ["AGENTS.md", ".claude/skills/design-feature-adoption/SKILL.md"])
def test_local_owned_edits_fail_without_partial_rewrite(consumer, path):
    root, _ = consumer
    assert run(consumer, "--apply").returncode == 0
    p = root / path
    p.write_bytes(p.read_bytes().replace(b"approved", b"LOCAL", 1))
    before = files(root)
    assert run(consumer, "--apply").returncode == 1
    assert files(root) == before


def test_curated_procedure_conflict_is_atomic(consumer):
    root, _ = consumer
    p = root / ".claude/skills/compatibility-improvement/SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text("Curated local procedure")
    before = files(root)
    assert run(consumer, "--apply").returncode == 1
    assert files(root) == before


@pytest.mark.parametrize("ancestor", [False, True])
def test_symlink_destination_or_parent_is_refused(consumer, tmp_path, ancestor):
    root, _ = consumer
    outside = tmp_path / "outside"
    outside.mkdir()
    if ancestor:
        (root / ".claude").symlink_to(outside, target_is_directory=True)
    else:
        (outside / "local.md").write_text("Outside content")
        (root / "AGENTS.md").symlink_to(outside / "local.md")
    before = files(outside)
    assert run(consumer, "--apply").returncode == 1
    assert files(outside) == before
    assert not (root / native.LOCK).exists()


def test_root_override_and_scoped_claude_import(consumer):
    root, _ = consumer
    (root / "AGENTS.override.md").write_text("# Local override\nRetain this.\n")
    (root / ".claude").mkdir()
    (root / ".claude/CLAUDE.md").write_text("# Scoped Claude\n")
    assert run(consumer, "--apply").returncode == 0
    assert (root / "AGENTS.override.md").read_text().endswith("Retain this.\n")
    assert b"@../.claude/skills/adaptable-development/SKILL.md" in (root / ".claude/CLAUDE.md").read_bytes()
    assert not (root / "CLAUDE.md").exists()


def test_explicit_repo_local_skill_alias_is_preserved(consumer):
    root, _ = consumer
    (root / ".github/skills").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".claude/skills").symlink_to("../.github/skills")
    manifest = root / "ai-manifest.yaml"
    manifest.write_text(manifest.read_text() + 'feature_flags:\n  native_guidance_skill_root: .github/skills\n')
    assert run(consumer, "--apply").returncode == 0
    assert (root / ".claude/skills").is_symlink()
    assert (root / ".github/skills/design-feature-adoption/SKILL.md").is_file()
    assert native.live_import((root / "CLAUDE.md").read_bytes(), b"@.github/skills/adaptable-development/SKILL.md")
    assert run(consumer, "--check").returncode == 0


def test_custom_skill_root_cannot_retarget_arbitrary_path(consumer):
    root, _ = consumer
    manifest = root / "ai-manifest.yaml"
    manifest.write_text(manifest.read_text() + 'feature_flags:\n  native_guidance_skill_root: .github/skills\n')
    assert run(consumer, "--apply").returncode == 1
    assert not (root / "AGENTS.md").exists()


@pytest.mark.parametrize("example", [
    "`@AGENTS.md`\n```md\n@AGENTS.md\n```\n",
    "<!-- example\n@AGENTS.md\n-->\n",
    "Notes <!-- example\n@AGENTS.md\n-->\n",
    "```md\n```still-an-example\n@AGENTS.md\n```\n",
    "    @AGENTS.md\n",
])
def test_fake_import_does_not_satisfy_loading(consumer, example):
    root, _ = consumer
    (root / "CLAUDE.md").write_text("# Notes\n" + example)
    assert run(consumer, "--apply").returncode == 0
    data = (root / "CLAUDE.md").read_bytes()
    assert native.live_import(data, b"@.claude/skills/adaptable-development/SKILL.md")
    assert data.startswith(native.BEGIN + b"\n@.claude/skills/adaptable-development/SKILL.md\n")


@pytest.mark.parametrize("target", ["AGENTS.md", ".claude/skills/adaptable-development/SKILL.md"])
def test_existing_live_import_is_preserved(consumer, target):
    root, _ = consumer
    local = f"# Local\n@{target}\n# Other instructions\n".encode()
    (root / "CLAUDE.md").write_bytes(local)
    assert run(consumer, "--apply").returncode == 0
    assert (root / "CLAUDE.md").read_bytes() == local


def test_policy_only_import_migrates_owned_block_without_loading_curated_tail(consumer):
    root, _ = consumer
    (root / "AGENTS.md").write_text("# Codex-only catalogue\n" + "Unrelated rule.\n" * 500)
    assert run(consumer, "--apply").returncode == 0
    claude = root / "CLAUDE.md"
    data = claude.read_bytes().replace(b"@.claude/skills/adaptable-development/SKILL.md", b"@AGENTS.md")
    claude.write_bytes(data + b"\n# Curated Claude rules\n")
    lock = root / native.LOCK
    recorded = json.loads(lock.read_text())
    recorded["files"]["CLAUDE.md"]["sha256"] = native.digest(native.block(data))
    lock.write_text(json.dumps(recorded))
    assert run(consumer, "--apply").returncode == 0
    updated = claude.read_bytes()
    assert native.live_import(updated, b"@.claude/skills/adaptable-development/SKILL.md")
    assert not native.live_import(updated, b"@AGENTS.md")
    assert updated.endswith(b"# Curated Claude rules\n")
    assert b"Unrelated rule" not in (root / ".claude/skills/adaptable-development/SKILL.md").read_bytes()
    assert run(consumer, "--check").returncode == 0


def test_nested_instruction_chain_limit_blocks_before_writes(consumer):
    root, _ = consumer
    (root / "AGENTS.md").write_bytes(b"r" * 20000)
    (root / "src").mkdir()
    (root / "src/AGENTS.override.md").write_bytes(b"s" * 15000)
    before = files(root)
    result = run(consumer, "--apply")
    assert result.returncode == 1
    assert "limit 32768" in result.stdout
    assert files(root) == before


def test_configured_fallback_and_global_size_are_counted(consumer, monkeypatch, tmp_path):
    root, _ = consumer
    home = tmp_path / "custom-codex"
    home.mkdir()
    (home / "AGENTS.override.md").write_bytes(b"g" * 1000)
    (home / "config.toml").write_text('project_doc_max_bytes = 1500\nproject_doc_fallback_filenames = ["TEAM.md"]\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    result = run(consumer, "--apply")
    assert result.returncode == 1
    assert "limit 1500" in result.stdout
    # The helper records paths/sizes, never global instruction content.
    assert "g" * 20 not in result.stdout


@pytest.mark.parametrize("surfaces", ["[codex-web]", "[codex, codex]", "[cursor, 2]"])
def test_unsupported_manifest_values_are_rejected(consumer, surfaces):
    root, _ = consumer
    p = root / "ai-manifest.yaml"
    p.write_text(p.read_text().replace("[claude-code, codex, cursor, devin]", surfaces))
    assert run(consumer, "--apply").returncode == 2
    assert not (root / "AGENTS.md").exists()


def test_mixed_advisory_surface_installs_supported_subset(consumer):
    root, _ = consumer
    manifest = root / "ai-manifest.yaml"
    manifest.write_text(manifest.read_text().replace(
        "[claude-code, codex, cursor, devin]", "[claude-code, openhands]"))
    result = run(consumer, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "surface 'openhands' is advisory-only" in result.stdout
    assert (root / "CLAUDE.md").is_file()
    assert not (root / ".cursor/rules/adaptable-development.mdc").exists()
    assert json.loads((root / native.LOCK).read_text())["surfaces"] == ["claude-code"]
    assert run(consumer, "--check").returncode == 0


def test_advisory_only_selection_fails_without_writes(consumer):
    root, _ = consumer
    manifest = root / "ai-manifest.yaml"
    manifest.write_text(manifest.read_text().replace(
        "[claude-code, codex, cursor, devin]", "[openhands]"))
    before = files(root)
    result = run(consumer, "--apply")
    assert result.returncode == 1
    assert "surface 'openhands' is advisory-only" in result.stdout
    assert files(root) == before


def test_pin_cannot_hide_modified_policy(consumer):
    root, source = consumer
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "commit", "-qm", "Fixture"], check=True)
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    manifest = root / "ai-manifest.yaml"
    manifest.write_text(manifest.read_text().replace("^1.14", "sha:" + sha))
    assert run(consumer, "--apply").returncode == 0
    rel = "registry/platform/framework/skills/design-feature-adoption/SKILL.md"
    subprocess.run(["git", "-C", str(source), "update-index", "--skip-worktree", rel], check=True)
    (source / rel).write_text((source / rel).read_text() + "\nHidden edit\n")
    before = files(root)
    result = run(consumer, "--apply")
    assert result.returncode == 1
    assert "pinned object" in result.stdout
    assert files(root) == before


def test_io_failure_rolls_back_local_files(consumer, monkeypatch):
    root, source = consumer
    local = b"# Existing product rules\n"
    (root / "AGENTS.md").write_bytes(local)
    spec = importlib.util.spec_from_file_location("native_test_resolver", REPO / "scripts/ai-resolve.py")
    resolver = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = resolver
    spec.loader.exec_module(resolver)
    original, failed = resolver.atomic_replace, False

    def fail_once(dst, *args, **kwargs):
        nonlocal failed
        if dst.name == "adaptable-development.mdc" and not failed:
            failed = True
            raise OSError("simulated write failure")
        return original(dst, *args, **kwargs)

    monkeypatch.setattr(resolver, "atomic_replace", fail_once)
    args = type("Args", (), {"apply": True, "check": False})()
    manifest = resolver.load_manifest(root / "ai-manifest.yaml")
    registry = resolver.find_registry_root(source)
    before = files(root)
    assert native.resolve(args, root, resolver, manifest, registry, resolver.load_plugins_index(registry)) == 1
    assert files(root) == before


def test_generated_carrier_matches_authored_policy():
    # The repository's own installation is a reproducible generated carrier.
    data = (REPO / "AGENTS.md").read_bytes()
    policy = native.body((REPO / ".claude/skills/adaptable-development/SKILL.md").read_bytes())
    assert policy in data[:16384]
    assert 250 <= len(policy.split()) <= 350


def test_bounded_mirror_generation_preserves_unrelated_content(consumer):
    _, source = consumer
    unrelated = source / "registry/platform/framework/skills/local/SKILL.md"
    unrelated.parent.mkdir()
    unrelated.write_text("Mirror-owned existing content")
    changed = source / "registry/platform/framework/skills/design-feature-adoption/SKILL.md"
    changed.write_text("Old policy")
    command = [sys.executable, str(REPO / "scripts/build-registry.py"), "--policy-only",
               "--registry-root", str(source / "registry")]
    preview = subprocess.run(command + ["--check"], capture_output=True)
    assert preview.returncode == 1
    assert changed.read_text() == "Old policy"
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert unrelated.read_text() == "Mirror-owned existing content"
    assert subprocess.run(command + ["--check"], capture_output=True).returncode == 0


def load_script(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_native_pin_stays_at_verified_commit_after_checkout(consumer, monkeypatch):
    root, source = consumer
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    def commit():
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                        "commit", "-qm", "Fixture"], check=True)
        return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    original = commit()
    p = source / "registry/platform/framework/skills/adaptable-development/SKILL.md"
    p.write_text(p.read_text().replace("Build the current", "Implement the current"))
    other = commit()
    subprocess.run(["git", "-C", str(source), "checkout", "-q", original], check=True)
    manifest = root / "ai-manifest.yaml"
    manifest.write_text(manifest.read_text().replace("^1.14", "sha:" + original))
    resolver = load_script("native_checkout_resolver", "scripts/ai-resolve.py")
    validate = resolver.plan_requirement
    def switch_after_validation(*args, **kwargs):
        validate(*args, **kwargs)
        subprocess.run(["git", "-C", str(source), "checkout", "-q", other], check=True)
    monkeypatch.setattr(resolver, "plan_requirement", switch_after_validation)
    args = type("Args", (), {"apply": True, "check": False})()
    registry = source / "registry"
    result = native.resolve(args, root, resolver, resolver.load_manifest(manifest), registry,
                            resolver.load_plugins_index(registry))
    assert result == 1
    assert not (root / "AGENTS.md").exists()


def test_native_source_ancestor_swap_is_refused(consumer, monkeypatch, tmp_path):
    root, source = consumer
    registry = source / "registry"
    platform = registry / "platform"
    outside = tmp_path / "outside-platform"
    shutil.copytree(platform, outside)
    resolver = load_script("native_ancestor_resolver", "scripts/ai-resolve.py")
    validate = resolver.plan_requirement
    def swap_after_validation(*args, **kwargs):
        validate(*args, **kwargs)
        platform.rename(registry / "original-platform")
        platform.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(resolver, "plan_requirement", swap_after_validation)
    args = type("Args", (), {"apply": True, "check": False})()
    result = native.resolve(args, root, resolver, resolver.load_manifest(root / "ai-manifest.yaml"),
                            registry, resolver.load_plugins_index(registry))
    assert result == 1
    assert not (root / "AGENTS.md").exists()


def test_mirror_late_conflict_does_not_update_earlier_file(consumer, tmp_path):
    _, source = consumer
    earlier = source / "registry/platform/framework/skills/adaptable-development/SKILL.md"
    earlier.write_text("Previous policy")
    late = source / "registry/platform/framework/skills/compatibility-improvement/evals/cases.json"
    late.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("Preserve outside data")
    late.symlink_to(outside)
    result = subprocess.run([sys.executable, str(REPO / "scripts/build-registry.py"), "--policy-only",
                             "--registry-root", str(source / "registry")], capture_output=True, text=True)
    assert result.returncode != 0
    assert earlier.read_text() == "Previous policy"
    assert outside.read_text() == "Preserve outside data"


def test_mirror_write_failure_restores_prior_assets(consumer, monkeypatch):
    _, source = consumer
    registry = source / "registry"
    earlier = registry / "platform/framework/skills/adaptable-development/SKILL.md"
    later = registry / "platform/framework/skills/design-feature-adoption/SKILL.md"
    earlier.write_text("Previous policy")
    later.write_text("Previous procedure")
    before = files(registry)
    builder = load_script("native_mirror_builder", "scripts/build-registry.py")
    resolver = builder._policy_resolver()
    atomic, failed = resolver.atomic_replace, False
    def fail_once(dst, *args, **kwargs):
        nonlocal failed
        if dst == later and not failed:
            failed = True
            raise OSError("simulated mirror write failure")
        return atomic(dst, *args, **kwargs)
    monkeypatch.setattr(resolver, "atomic_replace", fail_once)
    with pytest.raises(OSError, match="simulated mirror"):
        builder.refresh_policy(registry)
    assert files(registry) == before


@pytest.mark.parametrize("conflict", ["created", "edited", "mode"])
def test_late_destination_change_preserves_work_and_does_not_publish_lock(tmp_path, monkeypatch, conflict):
    root = tmp_path
    first, later = root / "first.md", root / "later.md"
    first.write_bytes(b"Original first")
    if conflict != "created":
        later.write_bytes(b"Original later")
        later.chmod(0o644)
    resolver = load_script("native_late_edit_resolver", "scripts/ai-resolve.py")
    outputs = {"first.md": b"Updated first", "later.md": b"Updated later", "lock.json": b"Owned"}
    snapshots = {rel: native.file_snapshot(root, rel, resolver) for rel in outputs}
    atomic = resolver.atomic_replace

    def edit_while_preparing(dst, fill, **kwargs):
        def prepare(stream):
            fill(stream)
            if dst == later:
                if conflict == "mode":
                    later.chmod(0o600)
                else:
                    later.write_bytes(b"Concurrent local edit")
        return atomic(dst, prepare, **kwargs)

    monkeypatch.setattr(resolver, "atomic_replace", edit_while_preparing)
    with resolver._repo_apply_mutex(root):
        with pytest.raises(ValueError, match="later.md: changed before replacement"):
            native.apply_outputs(root, outputs, snapshots, resolver)
    assert first.read_bytes() == b"Original first"
    assert later.read_bytes() == (b"Original later" if conflict == "mode" else b"Concurrent local edit")
    if conflict == "mode":
        assert later.stat().st_mode & 0o777 == 0o600
    assert not (root / "lock.json").exists()
    assert not list(root.glob(".*.tmp"))


def test_rollback_revalidates_after_preparing_recovery(tmp_path, monkeypatch):
    root = tmp_path
    first, later = root / "first.md", root / "later.md"
    first.write_bytes(b"Original first")
    later.write_bytes(b"Original later")
    resolver = load_script("native_rollback_edit_resolver", "scripts/ai-resolve.py")
    outputs = {"first.md": b"Updated first", "later.md": b"Updated later", "lock.json": b"Owned"}
    snapshots = {rel: native.file_snapshot(root, rel, resolver) for rel in outputs}
    atomic = resolver.atomic_replace

    def fail_then_edit_recovery(dst, fill, **kwargs):
        if dst == later:
            raise OSError("simulated late failure")
        def prepare(stream):
            fill(stream)
            if first.read_bytes() == b"Updated first":
                first.write_bytes(b"Newer local edit")
        return atomic(dst, prepare, **kwargs)

    monkeypatch.setattr(resolver, "atomic_replace", fail_then_edit_recovery)
    with resolver._repo_apply_mutex(root):
        with pytest.raises(ValueError, match="during rollback; retained for manual recovery"):
            native.apply_outputs(root, outputs, snapshots, resolver)
    assert first.read_bytes() == b"Newer local edit"
    assert later.read_bytes() == b"Original later"
    assert not (root / "lock.json").exists()
    assert not list(root.glob(".*.tmp"))


@pytest.mark.parametrize("descriptor_relative", [False, True])
def test_atomic_replace_guard_runs_after_preparation_without_publishing(tmp_path, descriptor_relative):
    resolver = load_script("native_guard_order_resolver", "scripts/ai-resolve.py")
    target = tmp_path / "target.md"
    target.write_bytes(b"Original")
    filled = []
    def fill(stream):
        stream.write(b"Replacement")
        filled.append(True)
    def reject():
        assert filled == [True]
        assert target.read_bytes() == b"Original"
        raise ValueError("guard rejected replacement")
    dfd = resolver.secure_dir_fd(tmp_path, ".") if descriptor_relative else None
    try:
        with pytest.raises(ValueError, match="guard rejected replacement"):
            resolver.atomic_replace(target, fill, mode=0o644, dfd=dfd, before_replace=reject)
    finally:
        if dfd is not None:
            os.close(dfd)
    assert target.read_bytes() == b"Original"
    assert not list(tmp_path.glob(".*.tmp"))
