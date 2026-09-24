"""Unit tests for the capability registry machinery — pack-internal, excluded
from rendered consumers (resolver/lint semantics, scope fail-closed rule)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RESOLVE = REPO / "scripts" / "ai-resolve.py"
LINT = REPO / "scripts" / "registry-lint.py"


def make_registry(root: Path, plugins: dict[str, list[tuple[str, str]]]) -> Path:
    """Build a minimal registry: plugins.json + scope dirs + plugin trees."""
    reg = root / "registry"
    reg.mkdir(parents=True)
    index = {"schema_version": 1, "scopes": {}, "plugins": []}
    for scope_path, files in plugins.items():
        scope, name = scope_path.split("/", 1)
        pdir = reg / scope / name
        (pdir / ".claude-plugin").mkdir(parents=True)
        (pdir / ".devin-plugin").mkdir(parents=True)
        (pdir / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": name, "version": "1.0.0", "description": "test"}))
        (pdir / ".devin-plugin" / "plugin.json").write_text(json.dumps(
            {"name": name, "version": "1.0.0"}))
        index["plugins"].append({
            "scope": scope, "name": name,
            "path": f"registry/{scope}/{name}",
        })
        for fname, content in files:
            fp = pdir / fname
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
    (reg / "plugins.json").write_text(json.dumps(index))
    return reg.parent  # checkout root containing registry/


def write_manifest(repo: Path, universe: str, requires: list[dict]) -> Path:
    import yaml
    m = repo / "ai-manifest.yaml"
    m.write_text(yaml.dump({
        "version": 1,
        "universe": universe,
        "requires": requires,
    }))
    return m


def run_resolver(manifest: Path, registry_root: Path, repo_root: Path, *flags: str):
    return subprocess.run(
        [sys.executable, str(RESOLVE), "--manifest", str(manifest),
         "--registry", str(registry_root), "--repo-root", str(repo_root), *flags],
        capture_output=True, text=True, timeout=120)


def test_platform_require_materialises(tmp_path):
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nbody")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "platform/framework@1.0.0" in r.stdout

    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stderr + r.stdout
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    assert skill.is_file()
    assert "name: demo" in skill.read_text()
    assert (consumer / ".ai" / "capability-lock.json").is_file()


def test_cross_universe_fails_closed(tmp_path):
    reg_root = make_registry(tmp_path / "src", {
        "manolii/secret-thing": [("skills/x/SKILL.md",
                                  "---\nname: x\ndescription: d\n---\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "cpdcheck",
                       [{"plugin": "manolii/secret-thing", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "not reachable" in r.stdout
    assert not (consumer / ".claude").exists()  # nothing applied


def test_no_clobber_hand_edits(tmp_path):
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nnew")],
    })
    consumer = tmp_path / "consumer"
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("hand edit — not registry content")
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "refusing to clobber" in r.stdout
    assert skill.read_text() == "hand edit — not registry content"


def test_version_caret_range(tmp_path):
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "^9.0"}])
    r = run_resolver(m, reg_root, consumer)
    assert r.returncode == 1
    assert "not satisfied" in r.stdout


def test_check_detects_drift(tmp_path):
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1 and "DRIFT" in r.stdout
    run_resolver(m, reg_root, consumer, "--apply")
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 0 and "matches" in r.stdout


def test_tracked_hand_edit_refuses(tmp_path):
    """A hand edit to a lockfile-tracked file is a conflict, not an update."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    skill.write_text("hand edit after install")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "modified since install" in r.stdout
    assert skill.read_text() == "hand edit after install"


def test_lockfile_paths_are_repo_relative(tmp_path):
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    lock = json.loads((consumer / ".ai" / "capability-lock.json").read_text())
    assert all(not Path(p).is_absolute() for p in lock["files"])
    assert ".claude/skills/demo/SKILL.md" in lock["files"]



def test_nested_script_dependency_not_materialised(tmp_path):
    """A skill invoking a NESTED bundled helper (`python3 scripts/audit/tool.py`)
    is as unrunnable in resolver mode as a flat one — the dep scan must see
    through subdirectories."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/auditor/SKILL.md",
             "Run `python3 scripts/audit/tool.py --strict`"),
            ("scripts/audit/tool.py", "# nested bundled helper"),
            ("skills/plain/SKILL.md", "self-contained"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    skills = consumer / ".claude" / "skills"
    assert not (skills / "auditor" / "SKILL.md").exists()
    assert (skills / "plain" / "SKILL.md").is_file()
def test_apply_prune_removes_orphans(tmp_path):
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1"),
                               ("skills/extra/SKILL.md",
                                "---\nname: extra\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    # requirement shrinks: drop the plugin, keep nothing
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 0
    assert not (consumer / ".claude" / "skills" / "demo").exists()
    # --prune without --apply is an argparse error
    assert run_resolver(m, reg_root, consumer, "--prune").returncode == 2


def test_output_path_collision_conflicts(tmp_path):
    """Two plugins shipping different content to the same .claude/ path must
    fail closed — never let resolution order pick a winner."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/a": [("skills/demo/SKILL.md",
                        "---\nname: demo\ndescription: from-a\n---\n")],
        "platform/b": [("skills/demo/SKILL.md",
                        "---\nname: demo\ndescription: from-b\n---\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii", [
        {"plugin": "platform/a", "ref": "1.0.0"},
        {"plugin": "platform/b", "ref": "1.0.0"},
    ])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "output-path collision" in r.stdout
    assert not (consumer / ".claude").exists()


def test_prefix_collision_file_vs_descendant_conflicts(tmp_path):
    """One plugin shipping .claude/x while another ships .claude/x/y must
    fail closed — the exact-key check misses it and --apply would write
    the file then crash on mkdir() for the descendant, unlocked."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/a": [("skills/demo", "file body\n")],
        "platform/b": [("skills/demo/deep.md", "nested\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    for order in (
        [{"plugin": "platform/a", "ref": "1.0.0"},
         {"plugin": "platform/b", "ref": "1.0.0"}],
        [{"plugin": "platform/b", "ref": "1.0.0"},
         {"plugin": "platform/a", "ref": "1.0.0"}],
    ):
        m = write_manifest(consumer, "manolii", order)
        r = run_resolver(m, reg_root, consumer, "--apply")
        assert r.returncode == 1
        assert "output-path collision" in r.stdout
        assert not (consumer / ".claude").exists()


def test_identical_collision_dedupes(tmp_path):
    """Identical content from two plugins is a dedup skip, not a conflict."""
    body = "---\nname: demo\ndescription: d\n---\nsame bytes\n"
    reg_root = make_registry(tmp_path / "src", {
        "platform/a": [("skills/demo/SKILL.md", body)],
        "platform/b": [("skills/demo/SKILL.md", body)],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii", [
        {"plugin": "platform/a", "ref": "1.0.0"},
        {"plugin": "platform/b", "ref": "1.0.0"},
    ])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert "already provided by" in r.stdout
    assert (consumer / ".claude" / "skills" / "demo" / "SKILL.md").read_text() == body


def test_prune_refuses_hand_edited_orphan(tmp_path):
    """--prune must not unlink a file that was hand-edited after install."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    skill.write_text("hand edit after install")
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "modified since install" in r.stdout
    assert skill.read_text() == "hand edit after install"


def test_orphan_stays_tracked_without_prune(tmp_path):
    """--apply without --prune keeps the orphan AND its lockfile entry, so a
    later --prune still verifies it against the install digest."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0
    assert skill.is_file()
    lock = json.loads((consumer / ".ai" / "capability-lock.json").read_text())
    assert ".claude/skills/demo/SKILL.md" in lock["files"]
    # a later --prune still removes it (digest verified against lock)
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 0
    assert not skill.exists()
    lock = json.loads((consumer / ".ai" / "capability-lock.json").read_text())
    assert ".claude/skills/demo/SKILL.md" not in lock["files"]


def test_modified_orphan_kept_without_prune(tmp_path):
    """--apply without --prune promises to keep orphans — a hand-edited orphan
    must not abort unrelated updates or lose its lockfile tracking."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    skill.write_text("hand edit after install")
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert skill.read_text() == "hand edit after install"
    lock = json.loads((consumer / ".ai" / "capability-lock.json").read_text())
    assert ".claude/skills/demo/SKILL.md" in lock["files"]


def test_lockfile_path_escape_conflicts(tmp_path):
    """A lockfile entry that escapes the repo/.claude roots (absolute path or
    .. traversal) must never steer a prune unlink — fail closed."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not delete")
    settings = consumer / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"keep": "me"}')
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    # poison the lockfile: entries escaping repo_root and resolver-owned roots
    lock_file = consumer / ".ai" / "capability-lock.json"
    lock = json.loads(lock_file.read_text())
    lock["files"]["../victim.txt"] = "0" * 64
    lock["files"][str(victim)] = "0" * 64
    lock["files"]["etc/passwd"] = "0" * 64
    lock["files"][".claude/settings.json"] = hashlib.sha256(
        settings.read_bytes()).hexdigest()
    lock_file.write_text(json.dumps(lock))
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "resolver-owned" in r.stdout
    assert victim.read_text() == "do not delete"
    # .claude/settings.json is not resolver-owned — matching digest or not,
    # prune must never unlink it
    assert settings.read_text() == '{"keep": "me"}'


def test_symlinked_destination_conflicts(tmp_path):
    """A .claude/skills symlink pointing outside the repo must refuse
    materialisation — mkdir/copy2 would otherwise write outside repo_root."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (consumer / ".claude").mkdir()
    (consumer / ".claude" / "skills").symlink_to(outside)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "contains a symlink" in r.stdout
    assert not any(outside.iterdir())


def test_symlink_within_owned_roots_conflicts(tmp_path):
    """A destination symlink must be refused even when its target is inside
    another resolver-owned subtree — copy2 would silently overwrite the
    target capability."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x"),
                               ("agents/real.md", "real")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    real = consumer / ".claude" / "agents" / "real.md"
    x = consumer / ".claude" / "skills" / "demo" / "x.md"
    x.unlink()
    x.symlink_to("../../agents/real.md")
    # craft a second plugin revision so x.md has new content (drift update)
    (reg_root / "registry" / "platform" / "framework" / "skills" / "demo"
     / "x.md").write_text("x v2")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "contains a symlink" in r.stdout
    assert real.read_text() == "real"


def test_plugin_root_commands_not_materialised(tmp_path):
    """Files referencing ${CLAUDE_PLUGIN_ROOT} cannot run in resolver mode
    (there is no plugin root) — they are advisory, never written."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("commands/doctor.md",
             "run ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py"),
            ("commands/standalone.md", "a self-contained command"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert "plugin root or scripts/" in r.stdout
    cmds = consumer / ".claude" / "commands"
    assert not (cmds / "doctor.md").exists()
    assert (cmds / "standalone.md").is_file()
    lock = json.loads((consumer / ".ai" / "capability-lock.json").read_text())
    assert ".claude/commands/doctor.md" not in lock["files"]


def test_prune_conflicts_symlink_entry_preserves_target(tmp_path):
    """An orphan lock entry replaced by a symlink is a type change — prune
    conflicts instead of unlinking it; the link AND its target survive."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/real.md", "required content")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    target = consumer / ".claude" / "skills" / "demo" / "real.md"
    link = consumer / ".claude" / "skills" / "demo" / "stale-link.md"
    link.symlink_to(target.name)  # relative link -> same dir
    # poison the lock: orphan entry for the symlink, digest = target's bytes
    lock_file = consumer / ".ai" / "capability-lock.json"
    lock = json.loads(lock_file.read_text())
    lock["files"][".claude/skills/demo/stale-link.md"] = hashlib.sha256(
        target.read_bytes()).hexdigest()
    lock["provenance"][".claude/skills/demo/stale-link.md"] = (
        "platform/framework")
    lock_file.write_text(json.dumps(lock))
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "symlink" in r.stdout
    assert link.is_symlink()
    assert target.read_text() == "required content"  # target survived


def test_symlinked_lockfile_destination_conflicts(tmp_path):
    """A symlinked .ai dir makes the lock write follow the link out of the
    repo — --apply must refuse before touching it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    (consumer / ".ai").symlink_to(outside)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "lockfile destination" in r.stdout
    assert not any(outside.iterdir())


def test_orphan_behind_symlinked_dir_conflicts(tmp_path):
    """A lockfile orphan whose PARENT chain contains a symlink: unlink()
    would traverse the link and delete another capability's file."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("agents/real.md", "real")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    real = consumer / ".claude" / "agents" / "real.md"
    (consumer / ".claude" / "skills").mkdir()
    (consumer / ".claude" / "skills" / "alias").symlink_to(
        "../../agents", target_is_directory=True)
    lock_file = consumer / ".ai" / "capability-lock.json"
    lock = json.loads(lock_file.read_text())
    lock["files"][".claude/skills/alias/real.md"] = hashlib.sha256(
        real.read_bytes()).hexdigest()
    lock_file.write_text(json.dumps(lock))
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "symlinked directory" in r.stdout
    assert real.read_text() == "real"


def test_tag_pin_verified_against_checkout(tmp_path):
    """tag:/sha: refs must match the registry checkout's actual HEAD — a
    checkout at the wrong commit cannot silently satisfy a pin."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    # pin a tag that does not name HEAD → conflict
    sp.run(["git", "commit", "-qm", "second", "--allow-empty"],
           cwd=reg_root, env=env, check=True)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "not at the pinned ref" in r.stdout
    # a non-git registry source cannot satisfy a pin at all
    bare = make_registry(tmp_path / "bare", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    r = run_resolver(m, bare, consumer, "--apply")
    assert r.returncode == 1
    assert "needs a verifiable git checkout" in r.stdout


def test_dirty_worktree_rejects_pin(tmp_path):
    """A tag pin matching HEAD must still fail when the plugin subtree has
    uncommitted changes — else dirty bytes ship under the pinned ref."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    # dirty the plugin subtree — same HEAD, uncommitted bytes
    (reg_root / "registry" / "platform" / "framework" / "skills" / "demo"
     / "x.md").write_text("dirty")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "clean worktree" in r.stdout


def test_malformed_lock_conflicts(tmp_path):
    """A corrupt capability-lock.json must not masquerade as an empty
    ownership map — --apply would overwrite it and lose installed files."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    ai = consumer / ".ai"
    ai.mkdir()
    (ai / "capability-lock.json").write_text('{"files": {<<<truncated')
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "malformed" in r.stdout


def test_malformed_ref_conflicts(tmp_path):
    """^1.typo must be a conflict, not a silently broadened 1.x range."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "^1.typo"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "malformed ref" in r.stdout


def test_dirty_index_rejects_pin(tmp_path):
    """An uncommitted plugins.json edit is itself a resolution input — a pin
    must not copy content the pinned index didn't describe."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    (reg_root / "registry" / "plugins.json").write_text(
        '{"plugins": []}')  # uncommitted index change
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "clean worktree" in r.stdout


def test_index_path_scope_mismatch_conflicts(tmp_path):
    """An index entry whose path doesn't match its scope/name aliases
    another plugin — refuse it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    # point platform/framework at a different path that exists
    idx = reg_root / "registry" / "plugins.json"
    idx.write_text(json.dumps({"plugins": [
        {"scope": "platform", "name": "framework",
         "path": "registry/platform/other"}]}))
    (reg_root / "registry" / "platform" / "other").mkdir()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "does not match" in r.stdout


def test_symlinked_component_file_conflicts(tmp_path):
    """skills/leak/SKILL.md -> /etc/passwd must not copy the link target
    into the consumer."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/ok/SKILL.md", "x")],
    })
    leak = reg_root / "registry" / "platform" / "framework" / "skills" / "leak"
    leak.mkdir()
    (leak / "SKILL.md").symlink_to("/etc/passwd")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "symlink" in r.stdout


def test_illustrative_script_mentions_materialise(tmp_path):
    """Prose that merely mentions scripts/x.sh (sample findings, example
    output) is not a dependency — the file must still materialise."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/demo/SKILL.md",
             'Report example: {"file": "scripts/sync.py", "issue": "leak"}'),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert (consumer / ".claude" / "skills" / "demo" / "SKILL.md").is_file()


def test_index_fails_on_symlinked_component_file(tmp_path):
    """INDEX flags a symlinked file inside a real plugin dir."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    pdir = reg / "platform" / "framework"
    pdir.mkdir(parents=True)
    for sub in (".claude-plugin", ".devin-plugin"):
        (pdir / sub).mkdir()
        (pdir / sub / "plugin.json").write_text(
            '{"name": "framework", "version": "1.0.0", "description": "x"}')
    (reg / "plugins.json").write_text(json.dumps({"plugins": [
        {"scope": "platform", "name": "framework",
         "path": "registry/platform/framework"}]}))
    sdir = pdir / "skills" / "leak"
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").symlink_to("/etc/passwd")
    mod.REGISTRY = reg
    mod.results = []
    mod.check_index()
    fails = [f for f in mod.results
             if f.check == "INDEX" and f.status == "FAIL"]
    assert any("symlinked file" in f.detail for f in fails)


def test_script_dependent_skills_not_materialised(tmp_path):
    """A skill that invokes a script the plugin BUNDLES cannot run in
    resolver mode — advisory-skip it, never write it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/analytics/SKILL.md",
             "Run `python3 scripts/session-analytics.py --days 7`"),
            ("scripts/session-analytics.py", "# bundled helper"),
            ("skills/plain/SKILL.md", "self-contained"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    skills = consumer / ".claude" / "skills"
    assert not (skills / "analytics" / "SKILL.md").exists()
    assert (skills / "plain" / "SKILL.md").is_file()


def test_consumer_repo_script_reference_materialises(tmp_path):
    """A skill whose `python3 scripts/x.py` refers to a script the plugin
    does NOT ship AND that declares consumer_scripts: [...] is a
    consumer-repository command — it must materialise, not be skipped."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/migration-drift/SKILL.md",
             "---\nname: migration-drift\n"
             "consumer_scripts: [scripts/check-migration-drift-mgmt.py]\n---\n"
             "Fetch it first, then `python3 scripts/check-migration-drift-mgmt.py`"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert (consumer / ".claude" / "skills" / "migration-drift"
            / "SKILL.md").is_file()


def test_undeclared_script_reference_skips(tmp_path):
    """A `python3 scripts/x.py` invocation for a script the plugin does not
    bundle and with NO consumer_scripts declaration is an unverifiable dep —
    the file is skipped, not shipped broken."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/analytics/SKILL.md",
             "Run `python3 scripts/session-analytics.py --days 7`"),
            ("skills/plain/SKILL.md", "self-contained"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    skills = consumer / ".claude" / "skills"
    assert not (skills / "analytics" / "SKILL.md").exists()
    assert (skills / "plain" / "SKILL.md").is_file()


def test_script_prose_mention_does_not_gate(tmp_path):
    """A bare `scripts/x.py` mention in prose is NOT a dependency — only
    executable-invocation shapes gate materialise."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/prose/SKILL.md",
             "---\nname: prose\nconsumer_scripts: [scripts/setup.py]\n---\n"
             "Run `python3 scripts/setup.py` first.\n"
             "The scripts/reference.py file documents the format."),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert (consumer / ".claude" / "skills" / "prose" / "SKILL.md").is_file()


def test_consumer_scripts_mismatch_skips(tmp_path):
    """A consumer_scripts declaration must cover EVERY unbundled invocation —
    declaring scripts/setup.py cannot exempt invoking scripts/missing.py."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/mismatch/SKILL.md",
             "---\nname: mismatch\nconsumer_scripts: [scripts/setup.py]\n---\n"
             "Then run `python3 scripts/missing.py`"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert not (consumer / ".claude" / "skills" / "mismatch"
                / "SKILL.md").exists()


def test_index_fails_on_symlinked_plugin_dir(tmp_path):
    """A symlinked plugin dir aliases another scope's tree — INDEX must
    refuse to follow it."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    pdir = reg / "platform" / "framework"
    pdir.mkdir(parents=True)
    for sub in (".claude-plugin", ".devin-plugin"):
        (pdir / sub).mkdir()
        (pdir / sub / "plugin.json").write_text(
            '{"name": "framework", "version": "1.0.0", "description": "x"}')
    (reg / "plugins.json").write_text(json.dumps({"plugins": [
        {"scope": "platform", "name": "framework",
         "path": "registry/platform/framework"}]}))
    (reg / "platform" / "alias").symlink_to("framework")
    mod.REGISTRY = reg
    mod.results = []
    mod.check_index()
    fails = [f for f in mod.results
             if f.check == "INDEX" and f.status == "FAIL"]
    assert any("symlink" in f.detail for f in fails)


def test_manifest_version_must_be_semver(tmp_path):
    """version: null must not pass the manifest gate — the resolver calls
    string methods on it."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    pdir = reg / "platform" / "framework"
    (pdir / ".claude-plugin").mkdir(parents=True)
    (pdir / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "framework", "version": null, "description": "x"}')
    (pdir / ".devin-plugin").mkdir()
    (pdir / ".devin-plugin" / "plugin.json").write_text('{"name": "framework"}')
    mod.REGISTRY = reg
    mod.results = []
    mod.check_manifests()
    fails = [f for f in mod.results
             if f.check == "MANIFEST" and f.status == "FAIL"]
    assert any("version" in f.detail for f in fails)


def test_sha_pin_requires_hex(tmp_path):
    """sha:HEAD must not pass — rev-parse accepts arbitrary expressions but
    a pin must be a literal commit id."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "sha:HEAD"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "hexadecimal" in r.stdout


def test_tag_resolves_under_refs_tags(tmp_path):
    """tag:main must not satisfy against a branch — tags resolve strictly
    under refs/tags/."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-qb", "main"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:main"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "does not resolve" in r.stdout


def test_surfaces_devin_only_writes_nothing(tmp_path):
    """A manifest selecting surfaces:[devin] materialises no .claude/
    output and reports the unsupported surface as advisory."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = consumer / "ai-manifest.yaml"
    m.write_text(
        "version: 1\nuniverse: manolii\n"
        "surfaces: [devin]\n"
        "requires:\n  - plugin: platform/framework\n    ref: 1.0.0\n")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert not (consumer / ".claude" / "skills" / "demo" / "x.md").exists()
    assert "devin" in r.stdout


def test_xscope_scans_manifests(tmp_path):
    """A plugin.json referencing ../manolii/ fails XSCOPE — the metadata
    exemption covers the org-name scan only."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    pdir = reg / "platform" / "framework"
    pdir.mkdir(parents=True)
    (pdir / ".claude-plugin").mkdir()
    (pdir / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "framework", "version": "1.0.0", "description": "x",\n'
        ' "wiring": "../manolii/secret"}')
    mod.REGISTRY = reg
    mod.results = []
    mod.check_xscope()
    fails = [f for f in mod.results
             if f.check == "XSCOPE" and f.status == "FAIL"]
    assert any("manolii" in f.detail for f in fails)


def test_backticked_script_mention_materialises(tmp_path):
    """A backticked `scripts/x.py` mention is prose, not an invocation —
    the file still materialises."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("agents/executor.md",
             "Tier classification runs via `scripts/suggester.py`."),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert (consumer / ".claude" / "agents" / "executor.md").is_file()


def test_requires_scripts_frontmatter_skips(tmp_path):
    """requires_scripts: [...] in frontmatter is an explicit dependency —
    the file is advisory-skipped in resolver mode."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("commands/fan-out.md",
             "---\nname: fan-out\nrequires_scripts: [sprint_status.py]\n---\n"
             "Decomposes into parallel tasks."),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert not (consumer / ".claude" / "commands" / "fan-out.md").exists()
    assert "scripts/" in r.stdout


def test_lock_parent_not_dir_conflicts(tmp_path):
    """.ai as a plain file must be a plan-time conflict — otherwise --apply
    copies files then fails the lock write, leaving them unowned."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    (consumer / ".ai").write_text("not a dir")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "not a directory" in r.stdout
    assert not (consumer / ".claude" / "skills" / "demo" / "x.md").exists()


def test_git_status_failure_conflicts(tmp_path):
    """A nonzero git status (corrupt/unreadable index) must not read as a
    clean worktree — fail closed."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    (reg_root / ".git" / "index").write_text("garbage")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "cannot verify worktree cleanliness" in r.stdout


def test_caret_zero_major_bounds(tmp_path):
    """^0.1 accepts 0.1.x but not 0.2.0 — pre-1.0 caret ranges bound at the
    first nonzero component."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    # plugin manifests at version 0.2.0
    (reg_root / "registry" / "platform" / "framework" / ".claude-plugin"
     / "plugin.json").write_text(
         '{"name": "framework", "version": "0.2.0", "description": "x"}')
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "^0.1"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "not satisfied" in r.stdout


def seed_catalog(reg: Path, patterns: list[str] | None = None) -> None:
    p = reg / "platform" / "framework" / "data"
    p.mkdir(parents=True, exist_ok=True)
    (p / "token-shapes.json").write_text(json.dumps({
        "patterns": [{"name": "t", "regex": rx} for rx in (patterns or [])]}))


def test_secrets_covers_catalog_patterns(tmp_path):
    """The SECRETS scan derives patterns from the shipped token-shapes.json —
    types absent from the hardcoded base (sk-ant-*) must still fail."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    mod.REGISTRY = reg
    mod.SECRETS_ALLOWLIST_PATH = reg / "secrets-allowlist.txt"
    mod.results = []
    seed_catalog(reg, [r"sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{80,}"])
    (reg / "platform").mkdir(parents=True, exist_ok=True)
    (reg / "platform" / "cfg").write_text(
        "key = sk-ant-api03-" + "a" * 90 + "\n")
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("cfg" in f.detail for f in fails)


def test_secrets_scans_detector_corpus(tmp_path):
    """token-shapes.json itself is scanned — a real credential planted inside
    the detector corpus cannot hide behind the file-level exemption."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    p = reg / "platform" / "framework" / "data"
    p.mkdir(parents=True)
    (p / "token-shapes.json").write_text(
        '{"patterns": [], "comment": "ghp_' + "b" * 40 + '"}\n')
    mod.REGISTRY = reg
    mod.SECRETS_ALLOWLIST_PATH = reg / "secrets-allowlist.txt"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("token-shapes.json" in f.detail for f in fails)


def test_secrets_scans_utf16_credentials(tmp_path):
    """A credential stored as UTF-16LE interleaves NULs between characters —
    the normalized scan must still see it."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    mod.REGISTRY = reg
    mod.SECRETS_ALLOWLIST_PATH = reg / "secrets-allowlist.txt"
    mod.results = []
    seed_catalog(reg)
    (reg / "platform" / "key.txt").write_bytes(
        ("ghp_" + "c" * 40).encode("utf-16-le"))
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("key.txt" in f.detail for f in fails)


def test_secrets_fails_when_catalog_missing(tmp_path):
    """An unreadable credential catalog must fail closed — silently degrading
    to the base list would leave catalog-only shapes uncovered."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    reg.mkdir(parents=True)
    mod.REGISTRY = reg
    mod.SECRETS_ALLOWLIST_PATH = reg / "secrets-allowlist.txt"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("catalog" in f.detail for f in fails)


def test_secrets_scans_binary_like_files(tmp_path):
    """A NUL byte must not exempt a file from the secrets scan — the gate
    scans every file under registry/ regardless of content shape."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    mod.REGISTRY = reg
    mod.SECRETS_ALLOWLIST_PATH = reg / "secrets-allowlist.txt"
    mod.results = []
    seed_catalog(reg)
    (reg / "platform" / "weird").write_bytes(
        b"\x00binary-ish prefix\nkey = ghp_abcdefghij0123456789\n")
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("weird" in f.detail for f in fails)


def test_org_leak_scans_scope_root_files(tmp_path):
    """Only scope.yaml is exempt at a scope root — a stray
    registry/platform/leak.txt cannot bypass the privacy gate."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    mod.REGISTRY = reg
    mod.ALLOWLIST_PATH = reg / "leak-allowlist.txt"
    mod.results = []
    (reg / "platform").mkdir(parents=True)
    (reg / "platform" / "scope.yaml").write_text("scope: platform\n")
    (reg / "platform" / "leak.txt").write_text("belongs to buro entity\n")
    mod.check_org_leak()
    fails = [f for f in mod.results
             if f.check == "ORG-LEAK" and f.status == "FAIL"]
    assert any("leak.txt" in f.detail for f in fails)


def test_org_leak_scans_nested_readme(tmp_path):
    """Nested plugin READMEs are distributed content — org identifiers in
    them hit the scan (basename exemption would bypass the ratchet)."""
    mod = load_lint_module()
    src = tmp_path / "src"
    reg_root = make_registry(src, {
        "platform/framework": [("README.md", "this plugin mentions impaktful\n")],
    })
    reg = reg_root / "registry"
    mod.REGISTRY = reg
    mod.ALLOWLIST_PATH = reg / "leak-allowlist.txt"
    mod.results = []
    mod.check_org_leak()
    per_line = [f for f in mod.results
                if f.check == "ORG-LEAK" and f.status == "FAIL"
                and "README.md" in f.detail]
    assert per_line, [f.detail for f in mod.results]


def test_unindexed_dir_fails_index(tmp_path):
    """A scope child dir that is neither indexed nor carries a manifest must
    still fail INDEX — treating it as a non-plugin left a hole where INDEX
    and MANIFEST both passed for an unresolvable plugin."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    (reg / "platform" / "ghost").mkdir(parents=True)  # no manifest, not indexed
    (reg / "plugins.json").write_text(json.dumps({"plugins": []}))
    mod.REGISTRY = reg
    mod.ALLOWLIST_PATH = reg / "leak-allowlist.txt"
    mod.results = []
    mod.check_index()
    fails = [f for f in mod.results
             if f.check == "INDEX" and f.status == "FAIL"]
    assert fails and "ghost" in fails[0].detail


def test_indexed_plugin_missing_manifest_fails(tmp_path):
    """An indexed plugin dir without .claude-plugin/plugin.json must FAIL —
    the resolver would silently treat it as version 0.0.0."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    pdir = reg / "platform" / "ghost"
    pdir.mkdir(parents=True)  # no .claude-plugin/plugin.json
    (reg / "plugins.json").write_text(json.dumps({
        "plugins": [{"scope": "platform", "name": "ghost",
                     "path": "registry/platform/ghost"}]}))
    mod.REGISTRY = reg
    mod.ALLOWLIST_PATH = reg / "leak-allowlist.txt"
    mod.results = []
    mod.check_manifests()
    fails = [f for f in mod.results
             if f.check == "MANIFEST" and f.status == "FAIL"]
    assert fails and "ghost" in fails[0].detail


def load_lint_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("registry_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass field resolution needs the module registered
    spec.loader.exec_module(mod)
    return mod


def test_secrets_scans_metadata_files(tmp_path):
    """SECRETS must cover files exempt from ORG-LEAK (scope.yaml, manifests):
    a credential in registry metadata is still a leak."""
    mod = load_lint_module()
    reg = tmp_path / "registry"
    mod.REGISTRY = reg
    mod.SECRETS_ALLOWLIST_PATH = reg / "secrets-allowlist.txt"
    mod.results = []
    seed_catalog(reg)
    (reg / "platform" / "scope.yaml").write_text(
        "scope: platform\napi_key: ghp_abcdefghij0123456789\n")
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("scope.yaml" in f.detail for f in fails)


def test_org_leak_ratchet_is_per_line(tmp_path):
    """A grandfathered line suppresses only itself — a new identifier on
    another line of the same file still fails."""
    mod = load_lint_module()
    src = tmp_path / "src"
    reg_root = make_registry(src, {
        "platform/framework": [("skills/demo/SKILL.md",
                                "line one mentions manolii here\n"
                                "line two now mentions impaktful\n")],
    })
    reg = reg_root / "registry"
    mod.REGISTRY = reg
    mod.ALLOWLIST_PATH = reg / "leak-allowlist.txt"
    mod.results = []
    rel = "platform/framework/skills/demo/SKILL.md"
    mod.ALLOWLIST_PATH.write_text(
        mod.line_key(rel, "line one mentions manolii here") + "\n")
    mod.check_org_leak()
    per_line = [f for f in mod.results
                if f.check == "ORG-LEAK" and f.status == "FAIL"
                and f"{rel}:" in f.detail]
    assert len(per_line) == 1, [f.detail for f in mod.results]
    assert ":2" in per_line[0].detail and "impaktful" in per_line[0].detail


def test_repo_manifest_schema_valid():
    """The shipped ai-manifest.yaml validates against the schema."""
    import yaml
    try:
        import jsonschema
    except ImportError:
        pytest.skip("jsonschema not installed")
    schema = json.loads((REPO / "schemas" / "ai-manifest.schema.json").read_text())
    doc = yaml.safe_load((REPO / "ai-manifest.yaml").read_text())
    jsonschema.validate(doc, schema)


def test_registry_lint_passes_on_repo():
    """The real registry in this repo passes its own gate."""
    r = subprocess.run([sys.executable, str(LINT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_symlinked_scope_root_fails(tmp_path):
    """A scope root that is a symlink passes is_dir() but rglob() won't
    descend into it — MANIFEST/ORG-LEAK/SECRETS/XSCOPE all skip its bytes.
    INDEX must refuse."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    reg = reg_root / "registry"
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "hidden.md").write_text("manolii secrets")
    (reg / "manolii").symlink_to(target)
    mod = load_lint_module()
    mod.REGISTRY = reg
    mod.results = []
    mod.check_index()
    fails = [f for f in mod.results if f.check == "INDEX" and f.status == "FAIL"]
    assert any("scope root is a symlink" in f.detail for f in fails)


def test_hardlinked_destination_update_isolated(tmp_path):
    """A destination hard-linked to an external file shares an inode —
    --apply must replace, not overwrite-in-place, or the external peer
    gets modified by a registry update."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    skill_dst = consumer / ".claude" / "skills" / "demo" / "x.md"
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout                 # resolver-installed
    external = consumer / "external.md"
    os.link(skill_dst, external)                      # shared inode
    (reg_root / "registry" / "platform" / "framework"
     / "skills" / "demo" / "x.md").write_text("v2")     # registry drifts
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert skill_dst.read_text() == "v2"
    assert external.read_text() == "v1"                # peer untouched


def test_directory_destination_conflicts(tmp_path):
    """A directory at a component destination must be a plan-time
    conflict — not an IsADirectoryError crash."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    (consumer / ".claude" / "skills" / "demo" / "x.md").mkdir(parents=True)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "non-regular file" in r.stdout
    assert "IsADirectoryError" not in r.stderr


def test_apply_ignores_preplanted_temp_link(tmp_path):
    """A consumer could pre-create a predictable sibling temp path as a
    link — the resolver's exclusively-created temp must never follow it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    consumer = tmp_path / "consumer"
    skills_dir = consumer / ".claude" / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    external = consumer / "external.md"
    external.write_text("external")
    (skills_dir / ".x.md.ai-resolve-tmp").symlink_to(external)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert external.read_text() == "external"
    assert (skills_dir / "x.md").read_text() == "v1"


def test_hardlinked_lock_isolated(tmp_path):
    """The capability lock, hard-linked to an external file, must not let
    an --apply write reach the peer through the shared inode."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    lock = consumer / ".ai" / "capability-lock.json"
    external = consumer / "external-lock.json"
    os.link(lock, external)
    external_bytes = external.read_bytes()
    (reg_root / "registry" / "platform" / "framework"
     / "skills" / "demo" / "x.md").write_text("v2")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert external.read_bytes() == external_bytes


def test_check_detects_stale_lock_metadata(tmp_path):
    """--check compares the full expected lock doc — a lock whose universe
    or version field drifts (same files) is stale, not OK."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    lock_path = consumer / ".ai" / "capability-lock.json"
    doc = json.loads(lock_path.read_text())
    doc["universe"] = "buro"
    lock_path.write_text(json.dumps(doc, indent=2) + "\n")
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "lock is stale" in r.stdout


def test_tag_revision_operator_rejected(tmp_path):
    """tag:v2~1 resolves to the tagged commit's PARENT via gitrev syntax —
    tag: must accept only valid tag names (check-ref-format), not
    arbitrary revision expressions."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0~1"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "valid git tag name" in r.stdout


def test_ignored_component_under_pin_conflicts(tmp_path):
    """An IGNORED untracked component (e.g. *.pyc) is invisible to
    git status --untracked-files=all — but must never materialise under a
    pin, since the lock would claim bytes the pinned revision lacks."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / ".gitignore").write_text("*.pyc\n")
    (reg_root / "registry" / "platform" / "framework" / "skills" / "demo"
     / "debug.pyc").write_bytes(b"\x00pyc-bytes")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "not tracked at the pinned revision" in r.stdout


def test_org_leak_snake_case_identifier_fails(tmp_path):
    """`\\b` treats `_` as a word char — manolii_infrastructure_dependencies
    would pass a \\b-boundary scan. ORG-LEAK must match org names inside
    snake_case identifiers."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md",
                                "uses manolii_infrastructure_dependencies\n")],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_org_leak()
    fails = [f for f in mod.results
             if f.check == "ORG-LEAK" and f.status == "FAIL"]
    assert fails, "snake_case org identifier not flagged"


def test_xscope_registry_root_form_fails(tmp_path):
    """A manifest 'path' uses the repo-root form registry/<scope>/ — the
    XSCOPE scan must flag it, not only ../<scope>/."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md",
                                "see registry/manolii/private\n")],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_xscope()
    fails = [f for f in mod.results
             if f.check == "XSCOPE" and f.status == "FAIL"]
    assert any("manolii" in f.detail for f in fails)


def test_xscope_json_escaped_path_fails(tmp_path):
    """{"path": "registry\\/manolii\\/private"} — JSON '/' escapes parse to the
    forbidden cross-scope path; the raw-text match must normalize them."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "skills/demo/x.md",
            '{"ref": "registry\\/manolii\\/private"}')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_xscope()
    fails = [f for f in mod.results
             if f.check == "XSCOPE" and f.status == "FAIL"]
    assert any("manolii" in f.detail for f in fails)


def test_xscope_unicode_escaped_json_path_fails(tmp_path):
    """"registry/\\u006danolii/x" decodes to registry/manolii/x — the scan must
    see JSON-decoded string values, not only the raw text."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "data/x.json",
            '{"p": "registry/\\u006danolii/x"}')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_xscope()
    fails = [f for f in mod.results
             if f.check == "XSCOPE" and f.status == "FAIL"]
    assert any("manolii" in f.detail for f in fails)


def test_secrets_unicode_escaped_token_fails(tmp_path):
    """ghp_\\u0041... decodes to ghp_A... — a credential hidden behind
    \\uXXXX escapes must still trip the SECRETS scan."""
    esc = "\\u0041" * 20
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "data/cfg.json",
            '{"k": "ghp_' + esc + '"}')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("cfg.json" in f.detail for f in fails), \
        "unicode-escaped credential not flagged"


def test_secrets_yaml_unicode_escape_fails(tmp_path):
    """PyYAML decodes \\uXXXX inside quoted scalars — a YAML credential must
    trip the SECRETS scan just like a JSON one."""
    esc = "\\u0041" * 20
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "data/cfg.yaml",
            'token: "ghp_' + esc + '"')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("cfg.yaml" in f.detail for f in fails), \
        "yaml unicode-escaped credential not flagged"


def test_secrets_toml_unicode_escape_fails(tmp_path):
    """TOML basic strings decode \\uXXXX too — token = "ghp_\u0041..." must
    trip the SECRETS scan."""
    esc = "\\u0041" * 20
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "data/cfg.toml",
            'token = "ghp_' + esc + '"')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("cfg.toml" in f.detail for f in fails), \
        "toml unicode-escaped credential not flagged"


def test_secrets_source_hex_escape_fails(tmp_path):
    """"ghp_\\x41..." in a Python source file decodes to a credential at
    runtime — the scan must see source-language escape forms too."""
    esc = "\\x41" * 20
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "scripts/helper.py",
            'token = "ghp_' + esc + '"')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("helper.py" in f.detail for f in fails), \
        "source-language escaped credential not flagged"


def test_secrets_ecmascript_codepoint_escape_fails(tmp_path):
    """"ghp_\\u{41}..." in a JS/TS source file is an ECMAScript code-point
    escape that decodes to a credential — must trip SECRETS."""
    esc = "\\u{41}" * 20
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "scripts/helper.ts",
            'const token = "ghp_' + esc + '";')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("helper.ts" in f.detail for f in fails), \
        "ECMAScript code-point escaped credential not flagged"


def test_secrets_python_octal_escape_fails(tmp_path):
    """"ghp_\\101..." in a Python source file decodes to a credential at
    runtime — octal escapes must trip SECRETS like hex/unicode forms."""
    esc = "\\101" + "A" * 19
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "scripts/helper.py",
            'token = "ghp_' + esc + '"')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("helper.py" in f.detail for f in fails), \
        "octal-escaped credential not flagged"


def test_secrets_python_named_escape_fails(tmp_path):
    """"ghp_\\N{LATIN CAPITAL LETTER A}..." in a Python file decodes to a
    credential at runtime — named Unicode escapes must trip SECRETS."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "scripts/helper.py",
            'token = "ghp_\\N{LATIN CAPITAL LETTER A}AAAAAAAAAAAAAAAAAAA"')],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("helper.py" in f.detail for f in fails), \
        "named-escaped credential not flagged"


def test_xscope_dot_segment_path_fails(tmp_path):
    """.././manolii/ and registry/x/../buro/ resolve into another scope —
    XSCOPE must match normalized paths, not only literal spellings."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "skills/demo/x.md",
            "load .././manolii/private.md\n"
            "also registry/x/../buro/secret.md\n")],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_xscope()
    fails = [f for f in mod.results
             if f.check == "XSCOPE" and f.status == "FAIL"]
    assert len(fails) >= 2


def test_secrets_utf16_json_escaped_credential_fails(tmp_path):
    """A UTF-16 JSON file carrying 'ghp_\\u0041...' must trip SECRETS —
    the structured decode must run on the detected encoding, not on the
    garbled utf-8 read (which fails json.loads and skips decoded values)."""
    doc = '{"k": "ghp_\\u0041AAAAAAAAAAAAAAAAAAA"}'
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [(
            "data/encoded.json", doc)],
    })
    f = reg_root / "registry/platform/framework/data/encoded.json"
    f.write_bytes(b"\xff\xfe" + doc.encode("utf-16-le"))
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("encoded.json" in f.detail for f in fails), \
        "UTF-16 JSON escaped credential not flagged"


def test_stale_org_allowlist_entry_fails(tmp_path):
    """A grandfathered line that no longer matches is a REUSABLE exemption —
    reintroducing the identical leaked line would silently pass. Stale
    entries must FAIL, not warn."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "clean")],
    })
    reg = reg_root / "registry"
    (reg / "leak-allowlist.txt").write_text(
        "platform/framework/skills/demo/x.md#deadbeef\n")
    mod = load_lint_module()
    mod.REGISTRY = reg
    mod.results = []
    mod.check_org_leak()
    fails = [f for f in mod.results
             if f.check == "ORG-LEAK" and f.status == "FAIL"]
    assert any("deadbeef" in f.detail or "stale" in f.detail
               for f in fails), "stale allowlist entry did not FAIL"


def test_public_boundary_universe_content_fails(tmp_path):
    """registry/<universe>/ content beyond scope.yaml is banned in the
    public repo — universe plugins live in private mirrors."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
        "manolii/secret-thing": [("skills/x/SKILL.md",
                                  "---\nname: x\ndescription: y\n---\n")],
    })
    (reg_root / "registry/manolii/scope.yaml").write_text("scope: manolii\n")
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_public_boundary()
    fails = [f for f in mod.results if f.status == "FAIL"]
    assert any("manolii/" in f.detail for f in fails)


def test_public_boundary_scope_yaml_only_passes(tmp_path):
    """The universe scope.yaml CONTRACT is public-safe scaffold — it must
    not trip the boundary."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / "registry/manolii").mkdir(exist_ok=True)
    (reg_root / "registry/manolii/scope.yaml").write_text("scope: manolii\n")
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_public_boundary()
    assert not [f for f in mod.results if f.status == "FAIL"]


def test_public_boundary_private_mirror_marker_waives(tmp_path):
    """registry/.private-mirror marks a private mirror — the boundary is
    waived there, but ONLY when the origin remote verifies this isn't
    the canonical public repo (a bare committable marker cannot waive
    the guard)."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
        "manolii/secret-thing": [("skills/x/SKILL.md",
                                  "---\nname: x\ndescription: y\n---\n")],
    })
    (reg_root / "registry/.private-mirror").write_text("")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=reg_root, env=env, check=True)
    (reg_root / "registry/private-mirrors.txt").write_text(
        hashlib.sha256(b"buro-built/buro-registry").hexdigest() + "\n")
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.REPO = reg_root
    mod.PRIVATE_MIRRORS_PATH = reg_root / "registry/private-mirrors.txt"
    mod._repo_visibility = lambda slug: "private"  # stub the live gh api call
    mod.results = []
    mod.check_public_boundary()
    assert not [f for f in mod.results if f.status == "FAIL"]


def test_public_boundary_marker_undeclared_remote_fails(tmp_path):
    """A parseable but undeclared origin (e.g. a public fork) cannot
    waive the boundary — the slug must appear in private-mirrors.txt."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / "registry/.private-mirror").write_text("")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/someuser/ai-starter-pack.git"],
           cwd=reg_root, env=env, check=True)
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.REPO = reg_root
    mod.PRIVATE_MIRRORS_PATH = reg_root / "registry/private-mirrors.txt"
    mod.results = []
    mod.check_public_boundary()
    fails = [f for f in mod.results if f.status == "FAIL"]
    assert any("not a declared private mirror" in f.detail for f in fails)


def test_public_boundary_marker_in_canonical_repo_fails(tmp_path):
    """A committed .private-mirror in the canonical public repo is a
    self-granted waiver — it must FAIL, not silently pass."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / "registry/.private-mirror").write_text("")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Manolii-org/ai-starter-pack.git"],
           cwd=reg_root, env=env, check=True)
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.REPO = reg_root
    mod.results = []
    mod.check_public_boundary()
    fails = [f for f in mod.results if f.status == "FAIL"]
    assert any("canonical" in f.detail or "private-mirror" in f.detail
               for f in fails)


def test_public_boundary_marker_without_remote_fails(tmp_path):
    """A marker in a checkout whose origin cannot be verified is
    unverifiable — fail closed rather than waive."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / "registry/.private-mirror").write_text("")
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.REPO = reg_root
    mod.results = []
    mod.check_public_boundary()
    fails = [f for f in mod.results if f.status == "FAIL"]
    assert any("cannot be verified" in f.detail or "unverifiable"
               in f.detail for f in fails)


def test_pack_surface_lowercase_slug_fails(tmp_path):
    """Owner/repo casing doesn't matter to git or DNS — a lowercase
    manolii-org/<repo> slug must trip the gate."""
    repo = tmp_path / "pack"
    repo.mkdir()
    (repo / "registry").mkdir()
    (repo / "x.md").write_text("clone github.com/manolii-org/secret-repo")
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod.results = []
    mod.check_pack_surface()
    fails = [f for f in mod.results
             if f.check == "PACK-SURFACE" and f.status == "FAIL"]
    assert any("x.md" in f.detail for f in fails)


def test_pack_surface_prefixed_canonical_slug_fails(tmp_path):
    """`Manolii-org/ai-starter-pack-private` is a DIFFERENT repo — the
    canonical-name exception must end at a true slug delimiter, not at
    `\\b` (which fires before '-' and '.')."""
    repo = tmp_path / "pack"
    repo.mkdir()
    (repo / "registry").mkdir()
    (repo / "x.md").write_text(
        "see Manolii-org/ai-starter-pack-private and "
        "Manolii-org/ai-starter-pack.private")
    (repo / "ok.md").write_text(
        "clone github.com/Manolii-org/ai-starter-pack.git and "
        "gh:Manolii-org/ai-starter-pack")
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod.results = []
    mod.check_pack_surface()
    fails = [f for f in mod.results
             if f.check == "PACK-SURFACE" and f.status == "FAIL"]
    assert any("x.md" in f.detail for f in fails), \
        "prefixed private slug not flagged"
    assert not any("ok.md" in f.detail for f in fails), \
        "canonical slug (incl .git suffix) wrongly flagged"


def test_secrets_stale_allowlist_entry_fails(tmp_path):
    """An unused secrets-ratchet entry is a reusable credential
    exemption — reintroducing the identical line would silently pass.
    Stale entries must FAIL, matching the ORG-LEAK/PACK-SURFACE
    contract."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "clean")],
    })
    reg = reg_root / "registry"
    (reg / "secrets-allowlist.txt").write_text(
        "platform/framework/skills/demo/x.md#deadbeef\n")
    mod = load_lint_module()
    mod.REGISTRY = reg
    mod.results = []
    mod.check_secrets()
    fails = [f for f in mod.results
             if f.check == "SECRETS" and f.status == "FAIL"]
    assert any("deadbeef" in f.detail or "stale" in f.detail
               for f in fails), "stale secrets allowlist entry did not FAIL"



def test_pack_surface_mirror_mode_exempts_own_org_only(tmp_path):
    """In a verified private mirror the OWNING org's slug is legitimate
    (a buro mirror names Buro-Built/* on purpose), while other orgs'
    slugs and infra ids still FAIL — the cross-org boundary holds.
    Mirror mode requires the live `gh api` visibility assertion
    (committed files alone cannot prove the repo is private) — stubbed
    here."""
    import subprocess as sp
    repo = tmp_path / "mirror"
    (repo / "registry").mkdir(parents=True)
    (repo / "x.md").write_text(
        "see Buro-Built/buro-core\n"
        "and Impaktful-Platform/impaktful_3.0\n"
        "and db.abc123.supabase.co\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=repo, env=env, check=True)
    # ls-files enumerates the INDEX — stage the file so the tracked-file
    # scan sees it (no commit needed).
    sp.run(["git", "add", "x.md"], cwd=repo, env=env, check=True)
    (repo / "registry/.private-mirror").write_text("")
    (repo / "registry/private-mirrors.txt").write_text(
        hashlib.sha256(b"buro-built/buro-registry").hexdigest() + "\n")
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PRIVATE_MIRRORS_PATH = repo / "registry/private-mirrors.txt"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod._repo_visibility = lambda slug: "private"
    mod.results = []
    mod.check_pack_surface()
    flagged = {f.detail.split(" — ", 1)[0]
               for f in mod.results
               if f.check == "PACK-SURFACE" and f.status == "FAIL"}
    assert "x.md:2" in flagged, "cross-org slug not flagged"
    assert "x.md:3" in flagged, "infra id not flagged"
    assert "x.md:1" not in flagged, "own-org slug wrongly flagged"

def _bootstrap_env(tmp_path):
    """env for bootstrap subprocess calls: a `gh` stub reporting 'private'
    (the visibility check fails closed when gh can't answer), PATH
    including it, and MIRROR_TRUST_DIRS declaring the stub — a binary
    outside the system dirs is untrusted unless the operator declares
    the root."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho \"$@\" > \"" + str(tmp_path / "gh_args")
                  + "\"\necho private\n")
    gh.chmod(0o755)
    # Isolate the harness machine's GLOBAL git config — e.g. a Devin box
    # rewrites every github.com url through its auth proxy via
    # url.insteadOf, which would silently steer push-target checks.
    empty_cfg = tmp_path / "gitconfig.empty"
    empty_cfg.write_text("")
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                GIT_CONFIG_GLOBAL=str(empty_cfg),
                MIRROR_TRUST_DIRS=str(bin_dir))

def test_bootstrap_mirror_seed(tmp_path):
    """bootstrap-mirror.py seeds a complete lint-clean mirror scaffold:
    marker, universe scope, vendored platform, merged index, digests-only
    trust file, minimal generated workflow, and freshly-regenerated
    ratchet allowlists (mirror mode never freezes own-org hits)."""
    import subprocess as sp
    import yaml
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "buro-registry"
    root.mkdir()
    # Mirrors are clones — a non-git root must fail (staging the scaffold
    # is required for the lint to enumerate registry/** hits), and --slug
    # must equal the checkout's origin.
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    # The visibility query must be pinned to github.com — GH_HOST
    # could otherwise point gh at an Enterprise instance where a
    # private same-slug repo passes while the bound repo is public.
    assert "--hostname" in (tmp_path / "gh_args").read_text()
    assert "github.com" in (tmp_path / "gh_args").read_text()
    assert (root / "registry/.private-mirror").is_file()
    assert (root / "registry/buro/scope.yaml").is_file()
    assert (root / "registry/platform").is_dir()
    assert (root / "registry/plugins.json").is_file()
    assert (root / "scripts/registry-lint.py").is_file()
    assert hashlib.sha256(b"buro-built/buro-registry").hexdigest() \
        in (root / "registry/private-mirrors.txt").read_text()
    doc = yaml.safe_load((root / "registry/buro/scope.yaml").read_text())
    assert doc["scope"] == "buro" and doc["visibility"] == "buro"
    assert doc["parent_scope"] == "platform"
    # Ratchets seeded for THIS mirror's content — a vendored platform tree
    # carries canonical's grandfathered hits; an empty allowlist would
    # fail the first lint.
    assert (root / "registry/leak-allowlist.txt").is_file()
    assert (root / "registry/pack-surface-allowlist.txt").is_file()
    # Secrets ratchet is vendored — its token-shape catalogue ships
    # unchanged inside registry/platform/**.
    assert (root / "registry/secrets-allowlist.txt").is_file()
    # Generated workflow must not invoke files the scaffold never seeds,
    # and must authenticate the lint's own `gh api` visibility check.
    wf = (root / ".github/workflows/registry-lint.yml").read_text()
    assert "registry-lint.py" in wf
    assert "GH_TOKEN" in wf
    assert "build-registry.py" not in wf
    assert "pytest" not in wf

def test_bootstrap_mirror_fails_closed(tmp_path):
    """Seeding refuses (rc 2, nothing written) when repo visibility cannot
    be verified or is public — and refuses at the staging step when --root
    is not a git checkout (an empty rglob-fallback allowlist would leave
    the first pushed CI run red)."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent

    def run(root, env, slug="buro-built/buro-registry", universe="buro"):
        return sp.run(
            [sys.executable, "scripts/bootstrap-mirror.py", "--root",
             str(root), "--universe", universe, "--slug", slug],
            cwd=pack, capture_output=True, text=True, env=env)

    # gh absent from PATH (git still resolvable) → visibility
    # unverifiable → refuse before writes.
    import shutil as _shutil
    root = tmp_path / "nogh"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    os.symlink(_shutil.which("git"), empty_bin / "git")
    r = run(root, dict(os.environ, PATH=str(empty_bin)))
    assert r.returncode == 2, r.stderr
    assert not (root / "registry").exists()

    # A PATH-shadowing git (a real file, not a symlink into the
    # system dirs) cannot attest to itself — refuse before writes
    # even though the wrapper delegates honestly.
    eb = tmp_path / "evilbin"
    eb.mkdir()
    (eb / "git").write_text("#!/bin/sh\nexec /usr/bin/git \"$@\"\n")
    os.chmod(eb / "git", 0o755)
    ebroot = tmp_path / "ebroot"
    ebroot.mkdir()
    sp.run(["git", "init", "-q"], cwd=ebroot, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ebroot, capture_output=True)
    r = run(ebroot,
            dict(os.environ,
                 PATH=f"{eb}{os.pathsep}{os.environ['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert "trusted" in r.stderr or "origin" in r.stderr
    assert not (ebroot / "registry").exists()

    # --slug naming ANOTHER accessible repo → the digest would not bind
    # to this checkout's origin → refuse.
    mism = tmp_path / "mism"
    mism.mkdir()
    sp.run(["git", "init", "-q"], cwd=mism, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Impaktful-Platform/impaktful-registry.git"],
           cwd=mism, capture_output=True)
    r = run(mism, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (mism / "registry").exists()

    # --universe not matching the origin's owning org → refuse (the lint
    # exempts patterns by owner; a buro scope under impaktful-platform
    # would seed the wrong universe AND exempt the wrong org).
    r = run(mism, _bootstrap_env(tmp_path),
            slug="impaktful-platform/impaktful-registry")
    assert r.returncode == 2, r.stderr
    assert "requires the mirror to live under" in r.stderr
    assert not (mism / "registry").exists()

    # gh reports public → refuse.
    pub_bin = tmp_path / "pubbin"
    pub_bin.mkdir()
    gh = pub_bin / "gh"
    gh.write_text("#!/bin/sh\necho public\n")
    gh.chmod(0o755)
    r = run(root, dict(os.environ,
                       PATH=f"{pub_bin}:{os.environ['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert not (root / "registry").exists()

    # A pushurl redirecting `git push` to a different repo than the
    # verified fetch url → refuse (seeded content would land elsewhere).
    pu = tmp_path / "pushurl"
    pu.mkdir()
    sp.run(["git", "init", "-q"], cwd=pu, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pu, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other-Org/public-repo.git"], cwd=pu,
           capture_output=True)
    r = run(pu, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pu / "registry").exists()

    # A pushInsteadOf rewrite doing the same redirect → refuse.
    pi = tmp_path / "pushinsteadof"
    pi.mkdir()
    sp.run(["git", "init", "-q"], cwd=pi, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pi, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.pushInsteadOf",
            "https://github.com/"], cwd=pi, capture_output=True)
    r = run(pi, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pi / "registry").exists()

    # insteadOf rewrites pushes too when no pushInsteadOf rule exists —
    # a github→gitlab insteadOf must refuse even though the raw origin
    # url is GitHub.
    io = tmp_path / "insteadof"
    io.mkdir()
    sp.run(["git", "init", "-q"], cwd=io, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=io, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=io, capture_output=True)
    r = run(io, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (io / "registry").exists()

    # insteadOf still applies to an EXPLICIT pushurl (only pushInsteadOf
    # is ignored for those) — pushurl pinned to the verified slug but an
    # insteadOf redirecting github.com elsewhere must refuse.
    pio = tmp_path / "pushurl-insteadof"
    pio.mkdir()
    sp.run(["git", "init", "-q"], cwd=pio, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pio, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=pio,
           capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=pio, capture_output=True)
    r = run(pio, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pio / "registry").exists()

    # remote.pushDefault pointing at a second remote whose slug differs —
    # a plain `git push` would select that remote over origin → refuse.
    pd = tmp_path / "pushdefault"
    pd.mkdir()
    sp.run(["git", "init", "-q"], cwd=pd, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pd, capture_output=True)
    sp.run(["git", "remote", "add", "evil",
            "https://github.com/Other-Org/public-repo.git"],
           cwd=pd, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault", "evil"], cwd=pd,
           capture_output=True)
    r = run(pd, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pd / "registry").exists()

    # branch.<name>.pushRemote has the highest precedence — same
    # redirect via the current branch's pushRemote → refuse.
    pr = tmp_path / "pushremote"
    pr.mkdir()
    sp.run(["git", "init", "-q"], cwd=pr, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pr, capture_output=True)
    sp.run(["git", "remote", "add", "evil",
            "https://github.com/Other-Org/public-repo.git"],
           cwd=pr, capture_output=True)
    cur = sp.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=pr,
                 capture_output=True, text=True).stdout.strip()
    sp.run(["git", "config", f"branch.{cur}.pushRemote", "evil"], cwd=pr,
           capture_output=True)
    r = run(pr, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pr / "registry").exists()

    # A push destination may be a literal URL, not a remote name —
    # a URL-valued remote.pushDefault pointing elsewhere → refuse.
    pv = tmp_path / "pushdefault-url"
    pv.mkdir()
    sp.run(["git", "init", "-q"], cwd=pv, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pv, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Other-Org/public-repo.git"], cwd=pv,
           capture_output=True)
    r = run(pv, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pv / "registry").exists()

    # When NO pushInsteadOf rule matches the URL destination, git falls
    # back to insteadOf rules — a URL pushDefault at the verified slug
    # with an unrelated pushInsteadOf rule AND a github→gitlab
    # insteadOf still redirects the push → refuse.
    pf = tmp_path / "pushdefault-url-fallback"
    pf.mkdir()
    sp.run(["git", "init", "-q"], cwd=pf, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pf, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=pf,
           capture_output=True)
    sp.run(["git", "config",
            "url.ssh://git@bitbucket.org/.pushInsteadOf",
            "ssh://git@bitbucket.org/"], cwd=pf, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=pf, capture_output=True)
    r = run(pf, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pf / "registry").exists()

    # remote.<name>.vcs delegates pushes to a git-remote-<vcs> helper
    # that can forward the pack anywhere — the configured URL is not
    # evidence of the real destination → refuse.
    vc = tmp_path / "vcs-helper"
    vc.mkdir()
    sp.run(["git", "init", "-q"], cwd=vc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vc, capture_output=True)
    sp.run(["git", "config", "remote.origin.vcs",
            "helper--token=SUPERSECRET"], cwd=vc,
           capture_output=True)
    r = run(vc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "vcs" in r.stderr
    # The helper name itself may carry a credential — never echoed.
    assert "SUPERSECRET" not in r.stderr
    assert "helper--token" not in r.stderr
    assert not (vc / "registry").exists()

    # And a credential-bearing remote NAME is not echoed either.
    vn = tmp_path / "vcs-name"
    vn.mkdir()
    sp.run(["git", "init", "-q"], cwd=vn, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vn, capture_output=True)
    sp.run(["git", "remote", "add", "SUPERSECRETTOKEN",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vn, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault", "SUPERSECRETTOKEN"],
           cwd=vn, capture_output=True)
    sp.run(["git", "config", "remote.SUPERSECRETTOKEN.vcs", "evil"],
           cwd=vn, capture_output=True)
    r = run(vn, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "vcs" in r.stderr
    assert "SUPERSECRETTOKEN" not in r.stderr
    assert not (vn / "registry").exists()

    # GIT_EXEC_PATH swaps which git-remote-<scheme> helper the push
    # execs — a verified https URL is no evidence of the transport.
    gx = tmp_path / "execpath"
    gx.mkdir()
    sp.run(["git", "init", "-q"], cwd=gx, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=gx, capture_output=True)
    r = run(gx, dict(_bootstrap_env(tmp_path),
                     GIT_EXEC_PATH="/tmp/evil-exec"))
    assert r.returncode == 2, r.stderr
    assert "GIT_EXEC_PATH" in r.stderr
    assert not (gx / "registry").exists()

    # An ssh/scp URL parses to the verified slug, but core.sshCommand
    # replaces the transport entirely — the push can land anywhere →
    # refuse.
    sc = tmp_path / "sshcommand"
    sc.mkdir()
    sp.run(["git", "init", "-q"], cwd=sc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=sc, capture_output=True)
    sp.run(["git", "config", "core.sshCommand", "evil-ssh"], cwd=sc,
           capture_output=True)
    r = run(sc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "sshCommand" in r.stderr
    assert not (sc / "registry").exists()

    # GIT_SSH_COMMAND is the environment-level equivalent of
    # core.sshCommand → refuse.
    se = tmp_path / "sshenv"
    se.mkdir()
    sp.run(["git", "init", "-q"], cwd=se, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=se, capture_output=True)
    env = _bootstrap_env(tmp_path)
    env["GIT_SSH_COMMAND"] = "evil-ssh"
    r = run(se, env)
    assert r.returncode == 2, r.stderr
    assert "GIT_SSH_COMMAND" in r.stderr
    assert not (se / "registry").exists()

    # A mixed-case scp host ('GitHub.com') still binds the verified slug
    # via the case-insensitive _slug_of — the ssh transport checks must
    # run on that same normalised view or GIT_SSH_COMMAND slips past.
    mc = tmp_path / "mixedcase-scp"
    mc.mkdir()
    sp.run(["git", "init", "-q"], cwd=mc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=mc, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "git@GitHub.com:Buro-Built/buro-registry.git"], cwd=mc,
           capture_output=True)
    r = run(mc, env)
    assert r.returncode == 2, r.stderr
    assert "GIT_SSH_COMMAND" in r.stderr
    assert not (mc / "registry").exists()

    # A rejected credential-bearing push URL must not print the
    # credential to stderr (it lands in transcripts/CI logs).
    cr = tmp_path / "creds"
    cr.mkdir()
    sp.run(["git", "init", "-q"], cwd=cr, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cr, capture_output=True)
    sp.run(["git", "remote", "set-url", "--push", "origin",
            "https://x-access-token:SECRETTOKEN@github.com/"
            "Other-Org/public-repo.git"],
           cwd=cr, capture_output=True)
    r = run(cr, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETTOKEN" not in r.stderr
    assert not (cr / "registry").exists()

    # Same for a credential-bearing literal URL in remote.pushDefault —
    # the configured destination itself must be redacted.
    cl = tmp_path / "credlit"
    cl.mkdir()
    sp.run(["git", "init", "-q"], cwd=cl, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cl, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://x-access-token:SECRETTOKEN@github.com/"
            "Other-Org/public-repo.git"],
           cwd=cl, capture_output=True)
    r = run(cl, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETTOKEN" not in r.stderr
    assert not (cl / "registry").exists()

    # An 'ssh' resolved from PATH to a non-system location is a
    # wrapper — it would attest to its own '-G' output while forwarding
    # the push anywhere, so refuse without asking it.
    sh = tmp_path / "sshuntrusted"
    sh.mkdir()
    stub = tmp_path / "sshstub"
    stub.mkdir()
    s = stub / "ssh"
    s.write_text("#!/bin/sh\necho 'hostname github.com'\n")
    s.chmod(0o755)
    sp.run(["git", "init", "-q"], cwd=sh, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=sh, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = run(sh, dict(env, PATH=f"{stub}:{env['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert "ssh transport cannot be verified" in r.stderr
    assert not (sh / "registry").exists()

    # git:// is plaintext transport (no encryption, no server
    # authentication) — refused outright regardless of gitProxy config.
    gp = tmp_path / "gitproto"
    gp.mkdir()
    sp.run(["git", "init", "-q"], cwd=gp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git://github.com/Buro-Built/buro-registry.git"],
           cwd=gp, capture_output=True)
    r = run(gp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "plaintext git" in r.stderr
    assert not (gp / "registry").exists()

    # An opaque helper destination can carry credentials in its
    # arguments — they must not reach stderr.
    ex = tmp_path / "extcreds"
    ex.mkdir()
    sp.run(["git", "init", "-q"], cwd=ex, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ex, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "ext::helper --token=SUPERSECRET %S"], cwd=ex,
           capture_output=True)
    r = run(ex, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRET" not in r.stderr
    assert not (ex / "registry").exists()

    # The opaque helper address can also carry a credential with no
    # space at all — the whole '<transport>::<address>' is withheld.
    e2 = tmp_path / "extcreds-nospace"
    e2.mkdir()
    sp.run(["git", "init", "-q"], cwd=e2, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=e2, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "ext::helper--token=SUPERSECRET"], cwd=e2,
           capture_output=True)
    r = run(e2, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRET" not in r.stderr
    assert "helper" not in r.stderr
    assert not (e2 / "registry").exists()

    # An ordinary URL path can carry a credential too — the diagnostic
    # must withhold the path, keeping only scheme+host.
    cp = tmp_path / "credpath"
    cp.mkdir()
    sp.run(["git", "init", "-q"], cwd=cp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cp, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://example.test/git/SECRETPATH/repo.git"], cwd=cp,
           capture_output=True)
    r = run(cp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETPATH" not in r.stderr
    assert not (cp / "registry").exists()

    # Userinfo may contain MORE than one '@' — git preserves the whole
    # value, so masking through only the first delimiter would leak the
    # remainder to stderr.
    mu = tmp_path / "multiat"
    mu.mkdir()
    sp.run(["git", "init", "-q"], cwd=mu, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=mu, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://foo@SUPERSECRETTOKEN@github.com/Other/repo.git"],
           cwd=mu, capture_output=True)
    r = run(mu, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRETTOKEN" not in r.stderr
    assert not (mu / "registry").exists()

    # A local filesystem path is a valid git push destination — any of
    # its components may be sensitive, so none reach stderr.
    lp = tmp_path / "localpath"
    lp.mkdir()
    sp.run(["git", "init", "-q"], cwd=lp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=lp, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "/tmp/SECRETDIR/repo.git"], cwd=lp,
           capture_output=True)
    r = run(lp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETDIR" not in r.stderr
    assert "<opaque destination>" in r.stderr
    assert not (lp / "registry").exists()

    # 'file://' is the URL form of a local path — opaque too.
    lf = tmp_path / "localfile"
    lf.mkdir()
    sp.run(["git", "init", "-q"], cwd=lf, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=lf, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "file:///tmp/SECRETFILE/repo.git"], cwd=lf,
           capture_output=True)
    r = run(lf, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETFILE" not in r.stderr
    assert "file:***" in r.stderr
    assert not (lf / "registry").exists()

    # Even on github.com a path exceeding owner/repo(.git) may carry a
    # credential — it is withheld wholesale, not exempted by the host.
    gp = tmp_path / "ghpath"
    gp.mkdir()
    sp.run(["git", "init", "-q"], cwd=gp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=gp, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other-Org/public-repo/SECRETX"], cwd=gp,
           capture_output=True)
    r = run(gp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETX" not in r.stderr
    assert "github.com/***" in r.stderr
    assert not (gp / "registry").exists()

    # Plain-HTTP is refused outright — it is plaintext transport and
    # its effective proxy chain cannot be trusted for a private push.
    ht = tmp_path / "httppush"
    ht.mkdir()
    sp.run(["git", "init", "-q"], cwd=ht, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "http://github.com/Buro-Built/buro-registry.git"],
           cwd=ht, capture_output=True)
    r = run(ht, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "plaintext http" in r.stderr
    assert not (ht / "registry").exists()

    # https with TLS verification disabled is MITM-able — refuse
    # (global http.sslVerify=false, a URL-scoped override, and the
    # GIT_SSL_NO_VERIFY environment variable all count).
    for i, cfg in enumerate((["http.sslVerify", "false"],
                             ["http.https://github.com/.sslVerify",
                              "false"],
                             # An explicitly-EMPTY value canonicalises
                             # to false — its blank --get-urlmatch
                             # record must not read as 'enabled'.
                             ["http.sslVerify", ""])):
        sv = tmp_path / f"ssloff{i}"
        sv.mkdir()
        sp.run(["git", "init", "-q"], cwd=sv, capture_output=True)
        sp.run(["git", "remote", "add", "origin",
                "https://github.com/Buro-Built/buro-registry.git"],
               cwd=sv, capture_output=True)
        sp.run(["git", "config", *cfg], cwd=sv, capture_output=True)
        r = run(sv, _bootstrap_env(tmp_path))
        assert r.returncode == 2, r.stderr
        assert "tls verification" in r.stderr
        assert not (sv / "registry").exists()

    sv = tmp_path / "sslenv"
    sv.mkdir()
    sp.run(["git", "init", "-q"], cwd=sv, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=sv, capture_output=True)
    # GIT_SSL_NO_VERIFY is defined by presence — '=0' still disables.
    r = run(sv, dict(_bootstrap_env(tmp_path), GIT_SSL_NO_VERIFY="0"))
    assert r.returncode == 2, r.stderr
    assert "tls verification" in r.stderr
    assert not (sv / "registry").exists()

    # A custom CA trust store keeps verification ON while trusting a
    # root the attacker may control — refuse env and config forms.
    ca = tmp_path / "sslcaenv"
    ca.mkdir()
    sp.run(["git", "init", "-q"], cwd=ca, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ca, capture_output=True)
    r = run(ca, dict(_bootstrap_env(tmp_path),
                     GIT_SSL_CAINFO="/tmp/evil-ca.pem"))
    assert r.returncode == 2, r.stderr
    assert "custom CA" in r.stderr
    assert not (ca / "registry").exists()

    cb = tmp_path / "sslcacfg"
    cb.mkdir()
    sp.run(["git", "init", "-q"], cwd=cb, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cb, capture_output=True)
    sp.run(["git", "config", "http.sslCAInfo", "/tmp/evil-ca.pem"],
           cwd=cb, capture_output=True)
    r = run(cb, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "custom CA" in r.stderr
    assert not (cb / "registry").exists()

    # The same URL-scoped key repeated in a later scope: git resolves
    # equal-specificity entries by order — the later value wins.
    genv = _bootstrap_env(tmp_path)
    eq = tmp_path / "ssleq"
    eq.mkdir()
    sp.run(["git", "init", "-q"], cwd=eq, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=eq, capture_output=True)
    sp.run(["git", "config", "--global",
            "http.https://github.com/.sslVerify", "true"],
           cwd=eq, capture_output=True, env=genv)
    sp.run(["git", "config", "http.https://github.com/.sslVerify",
            "false"], cwd=eq, capture_output=True)
    r = run(eq, genv)
    assert r.returncode == 2, r.stderr
    assert "tls verification" in r.stderr
    assert not (eq / "registry").exists()

    # An insteadOf <base> containing spaces still applies — dropping
    # the rule via whitespace parsing would pass the pre-rewrite
    # github.com URL while git pushes to the rewritten helper.
    es = tmp_path / "extspace"
    es.mkdir()
    sp.run(["git", "init", "-q"], cwd=es, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=es, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=es, capture_output=True)
    sp.run(["git", "config",
            'url."ext::echo something ".insteadOf',
            "https://github.com/"], cwd=es, capture_output=True)
    r = run(es, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (es / "registry").exists()

    # scp-style 'user@host:path' userinfo is credential-bearing too —
    # it has no '://' for the userinfo rule to catch.
    cs = tmp_path / "credscp"
    cs.mkdir()
    sp.run(["git", "init", "-q"], cwd=cs, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cs, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "SECRETSCP@github.com:Other/public.git"], cwd=cs,
           capture_output=True)
    r = run(cs, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETSCP" not in r.stderr
    assert not (cs / "registry").exists()

    # The auth-proxy exemption is for https proxies only — a non-https
    # prefix (e.g. an 'ext::… ' helper transport) must not exempt the
    # rewritten destination.
    ep = tmp_path / "extproxy"
    ep.mkdir()
    sp.run(["git", "init", "-q"], cwd=ep, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ep, capture_output=True)
    sp.run(["git", "config",
            'url."ext::helper ".insteadOf', "https://github.com/"],
           cwd=ep, capture_output=True)
    r = run(ep, dict(_bootstrap_env(tmp_path),
                     MIRROR_GITHUB_PROXY_PREFIX="ext::helper "))
    assert r.returncode == 2, r.stderr
    assert not (ep / "registry").exists()

    # Credentials in the URL query or fragment must not reach stderr.
    cq = tmp_path / "credquery"
    cq.mkdir()
    sp.run(["git", "init", "-q"], cwd=cq, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cq, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other/public.git?access_token="
            "SECRETQUERY#frag=SECRETQUERY"], cwd=cq,
           capture_output=True)
    r = run(cq, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETQUERY" not in r.stderr
    assert not (cq / "registry").exists()

    # A non-GitHub origin that parses to a valid-looking slug — gh would
    # verify an UNRELATED github.com repo of the same name → refuse.
    gl = tmp_path / "gitlab"
    gl.mkdir()
    sp.run(["git", "init", "-q"], cwd=gl, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://gitlab.com/Buro-Built/buro-registry.git"],
           cwd=gl, capture_output=True)
    r = run(gl, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (gl / "registry").exists()

    # gh stub OK but --root is not a git checkout → refused (no origin).
    plain = tmp_path / "plainroot"
    plain.mkdir()
    r = run(plain, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr

    # A credential.helper configured INSIDE the clone (local scope or
    # a file it includes) executes during the https push — the clone
    # is untrusted input; refuse it.
    ch = tmp_path / "credhelper"
    ch.mkdir()
    sp.run(["git", "init", "-q"], cwd=ch, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ch, capture_output=True)
    sp.run(["git", "config", "credential.helper", "!leak"],
           cwd=ch, capture_output=True)
    r = run(ch, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "credential.helper" in r.stderr
    assert not (ch / "registry").exists()

    # The same helper in the operator's GLOBAL config is the env trust
    # channel (like MIRROR_TRUST_DIRS), not untrusted input → allowed.
    chg = tmp_path / "credhelper-global"
    chg.mkdir()
    gcfg = tmp_path / "gitconfig.helper"
    gcfg.write_text("[credential]\n\thelper = !leak\n")
    sp.run(["git", "init", "-q"], cwd=chg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=chg, capture_output=True)
    r = run(chg, dict(_bootstrap_env(tmp_path),
                      GIT_CONFIG_GLOBAL=str(gcfg)))
    assert r.returncode == 0, r.stderr

    # A LOCAL include.path pointing outside the checkout keeps scope
    # 'local' — the helper is still clone-derived untrusted input.
    inc = tmp_path / "incl"
    inc.mkdir()
    outside = tmp_path / "outside-helper.cfg"
    outside.write_text("[credential]\n\thelper = !leak\n")
    sp.run(["git", "init", "-q"], cwd=inc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=inc, capture_output=True)
    sp.run(["git", "config", "include.path", str(outside)],
           cwd=inc, capture_output=True)
    r = run(inc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "credential.helper" in r.stderr
    assert not (inc / "registry").exists()

    # core.askPass answers the push's authentication prompt — a
    # clone-local value is the same untrusted-input class.
    ap = tmp_path / "askpass"
    ap.mkdir()
    sp.run(["git", "init", "-q"], cwd=ap, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ap, capture_output=True)
    sp.run(["git", "config", "core.askPass", "/tmp/upload-creds"],
           cwd=ap, capture_output=True)
    r = run(ap, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "askPass" in r.stderr
    assert not (ap / "registry").exists()

    # commit.gpgSign + a LOCAL gpg.program execs the program during
    # 'git commit' — clone-local signing config is untrusted input.
    sg = tmp_path / "signer"
    sg.mkdir()
    sp.run(["git", "init", "-q"], cwd=sg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=sg, capture_output=True)
    sp.run(["git", "config", "commit.gpgsign", "true"],
           cwd=sg, capture_output=True)
    sp.run(["git", "config", "gpg.program", "/tmp/sign"],
           cwd=sg, capture_output=True)
    r = run(sg, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "signing" in r.stderr
    assert not (sg / "registry").exists()

    # Attributes binding against a path the bootstrap CREATES
    # (README.md / schemas/**) must refuse even though the path does
    # not exist yet — the seeded file enters the filter during
    # 'git add'.
    fg = tmp_path / "filtgen"
    fg.mkdir()
    sp.run(["git", "init", "-q"], cwd=fg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fg, capture_output=True)
    sp.run(["git", "config", "filter.leak.clean", "cat > /tmp/out2"],
           cwd=fg, capture_output=True)
    (fg / ".gitattributes").write_text(
        "README.md filter=leak\nschemas/** filter=leak\n")
    r = run(fg, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "filter" in r.stderr
    assert not (fg / "registry").exists()


def test_bootstrap_mirror_push_target_pass(tmp_path):
    """Push-target validation must not over-refuse: (a) an explicit
    pushurl pinned to the verified slug stays valid even when a
    pushInsteadOf rule exists — git ignores pushInsteadOf for remotes
    with an explicit pushurl; (b) an env-supplied auth-proxy prefix
    whose insteadOf rewrites the effective push URL still lands on
    the verified github.com slug."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    env = _bootstrap_env(tmp_path)

    def run(root, e=None):
        return sp.run(
            [sys.executable, "scripts/bootstrap-mirror.py", "--root",
             str(root), "--universe", "buro",
             "--slug", "buro-built/buro-registry"],
            cwd=pack, capture_output=True, text=True, env=e or env)

    a = tmp_path / "pushurl-ok"
    a.mkdir()
    sp.run(["git", "init", "-q"], cwd=a, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=a, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=a,
           capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.pushInsteadOf",
            "https://github.com/"], cwd=a, capture_output=True)
    r = run(a)
    assert r.returncode == 0, r.stderr

    # Hostname case is immaterial to DNS/git — 'GITHUB.COM' binds the
    # same verified slug.
    uc = tmp_path / "uppercase-host"
    uc.mkdir()
    sp.run(["git", "init", "-q"], cwd=uc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://GITHUB.COM/Buro-Built/buro-registry.git"],
           cwd=uc, capture_output=True)
    r = run(uc)
    assert r.returncode == 0, r.stderr
    assert (uc / "registry/.private-mirror").is_file()

    b = tmp_path / "proxy-ok"
    b.mkdir()
    sp.run(["git", "init", "-q"], cwd=b, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=b, capture_output=True)
    prefix = "https://auth-proxy.example.test/github.com/"
    sp.run(["git", "config",
            f"url.{prefix}.insteadOf",
            "https://github.com/"], cwd=b, capture_output=True)
    r = run(b, dict(env, MIRROR_GITHUB_PROXY_PREFIX=prefix))
    assert r.returncode == 0, r.stderr

    # remote.pushDefault selecting a DIFFERENT remote is fine when that
    # remote resolves to the same verified slug — the push still lands
    # on the private repo.
    c = tmp_path / "pushdefault-same-slug"
    c.mkdir()
    sp.run(["git", "init", "-q"], cwd=c, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=c, capture_output=True)
    sp.run(["git", "remote", "add", "mirror",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=c, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault", "mirror"], cwd=c,
           capture_output=True)
    r = run(c)
    assert r.returncode == 0, r.stderr

    # An ssh/scp destination passes when the system ssh's effective
    # config verifies a direct, authenticated connection to github.com
    # (the real 'ssh -G' on a default-configured host does).
    sh = tmp_path / "ssh-ok"
    sh.mkdir()
    sp.run(["git", "init", "-q"], cwd=sh, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=sh, capture_output=True)
    r = run(sh)
    assert r.returncode == 0, r.stderr

    # Userless scp-style 'github.com:slug' is valid git ssh syntax —
    # it must be accepted like the user@ form, verified via 'ssh -G'.
    us = tmp_path / "userlessscp"
    us.mkdir()
    sp.run(["git", "init", "-q"], cwd=us, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "github.com:Buro-Built/buro-registry.git"],
           cwd=us, capture_output=True)
    r = run(us)
    assert r.returncode == 0, r.stderr

    # A URL-valued branch.<name>.pushRemote at the verified slug — git
    # accepts a URL there just as it accepts a remote name.
    d = tmp_path / "pushremote-url-ok"
    d.mkdir()
    sp.run(["git", "init", "-q"], cwd=d, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=d, capture_output=True)
    cur = sp.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=d,
                 capture_output=True, text=True).stdout.strip()
    sp.run(["git", "config", f"branch.{cur}.pushRemote",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=d,
           capture_output=True)
    r = run(d)
    assert r.returncode == 0, r.stderr

    # A pre-push hook runs arbitrary code during the push — it can read
    # the staged universe files and exfiltrate them even when every
    # transport check passes. An executable hook in .git/hooks fails
    # closed, and core.hooksPath's directory is checked too.
    hk = tmp_path / "prepushhook"
    hk.mkdir()
    sp.run(["git", "init", "-q"], cwd=hk, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=hk, capture_output=True)
    hook = hk / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)
    r = run(hk)
    assert r.returncode == 2, r.stderr
    assert "pre-push" in r.stderr
    assert not (hk / "registry").exists()

    hd = tmp_path / "hooksdir"
    hd.mkdir()
    hp2 = tmp_path / "prepushpath"
    hp2.mkdir()
    sp.run(["git", "init", "-q"], cwd=hp2, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=hp2, capture_output=True)
    (hd / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(hd / "pre-push", 0o755)
    sp.run(["git", "config", "core.hooksPath", str(hd)],
           cwd=hp2, capture_output=True)
    r = run(hp2)
    assert r.returncode == 2, r.stderr
    assert "pre-push" in r.stderr
    assert not (hp2 / "registry").exists()

    # Commit-side hooks run during the recommended `git commit` too —
    # every hook the add/commit/push sequence invokes is checked.
    hk2 = tmp_path / "precommithook"
    hk2.mkdir()
    sp.run(["git", "init", "-q"], cwd=hk2, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=hk2, capture_output=True)
    hook = hk2 / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)
    r = run(hk2)
    assert r.returncode == 2, r.stderr
    assert "pre-commit" in r.stderr
    assert not (hk2 / "registry").exists()

    # Index/reference hooks run during `git add`/`git commit` too —
    # post-index-change fires inside the bootstrap's own staging.
    rt = tmp_path / "reftranhook"
    rt.mkdir()
    sp.run(["git", "init", "-q"], cwd=rt, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=rt, capture_output=True)
    hook = rt / ".git" / "hooks" / "post-index-change"
    hook.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)
    r = run(rt)
    assert r.returncode == 2, r.stderr
    assert "post-index-change" in r.stderr
    assert not (rt / "registry").exists()

    # core.fsmonitor=<path> executes an external command during index
    # operations — a non-boolean value must refuse.
    fm = tmp_path / "fsm"
    fm.mkdir()
    sp.run(["git", "init", "-q"], cwd=fm, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fm, capture_output=True)
    sp.run(["git", "config", "core.fsmonitor", "/tmp/leak"],
           cwd=fm, capture_output=True)
    r = run(fm)
    assert r.returncode == 2, r.stderr
    assert "fsmonitor" in r.stderr
    assert not (fm / "registry").exists()

    # A BOOLEAN core.fsmonitor is only safe on git >=2.35.1 — on 2.35.0
    # git still treats the value as a hook pathname, so 'true' execs a
    # PATH 'true' wrapper during 'git add'. The stub answers
    # '--version' with 2.35.0 and delegates everything else to the real
    # git; MIRROR_TRUST_DIRS makes the stub a trusted binary.
    fm0 = tmp_path / "fsm0"
    fm0.mkdir()
    sp.run(["git", "init", "-q"], cwd=fm0, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fm0, capture_output=True)
    sp.run(["git", "config", "core.fsmonitor", "true"],
           cwd=fm0, capture_output=True)
    real_git = os.path.realpath(shutil.which("git") or "/usr/bin/git")
    gitstub = Path(env["MIRROR_TRUST_DIRS"]) / "git"
    gitstub.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\n"
        "  echo \"git version 2.35.0\"\n  exit 0\nfi\n"
        f"exec {real_git} \"$@\"\n")
    gitstub.chmod(0o755)
    r = run(fm0)
    assert r.returncode == 2, r.stderr
    assert "fsmonitor" in r.stderr
    assert not (fm0 / "registry").exists()
    gitstub.unlink()

    # filter.<name>.clean/.process runs the configured program on
    # staged file contents during 'git add' — refuse when configured.
    fl = tmp_path / "fil"
    fl.mkdir()
    sp.run(["git", "init", "-q"], cwd=fl, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fl, capture_output=True)
    sp.run(["git", "config", "filter.leak.clean", "cat > /tmp/out"],
           cwd=fl, capture_output=True)
    # A configured filter is inert until attributes bind it — a
    # system git-lfs config must NOT refuse, a bound one must. The
    # binding can name paths that don't exist yet (registry/**)
    # and still receive every seeded file during 'git add'.
    (fl / ".gitattributes").write_text("registry/** filter=leak\n")
    r = run(fl)
    assert r.returncode == 2, r.stderr
    assert "filter" in r.stderr
    assert not (fl / "registry").exists()


def _load_bootstrap():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bootstrap_mirror", "scripts/bootstrap-mirror.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bootstrap_ssh_effective_config(monkeypatch, tmp_path):
    """_ssh_host_unchanged must refuse any effective-config evidence of
    redirection or disabled server authentication, and must not trust a
    PATH-shadowing ssh executable to attest to itself."""
    import subprocess as _sp
    import types
    mod = _load_bootstrap()
    calls = []

    class R:
        def __init__(self, out, rc=0, err=""):
            self.stdout, self.returncode, self.stderr = out, rc, err

    def patch(lines, which="/usr/bin/ssh"):
        calls.clear()

        def run(argv, **_kw):
            calls.append(argv)
            return R("".join(f"{k} {v}\n" for k, v in lines))

        monkeypatch.setattr(
            mod, "subprocess",
            types.SimpleNamespace(
                run=run, TimeoutExpired=_sp.TimeoutExpired))
        monkeypatch.setattr(
            mod, "shutil", types.SimpleNamespace(which=lambda _n: which))

    CLEAN = [("hostname", "github.com"),
             ("stricthostkeychecking", "ask"),
             ("userknownhostsfile", "~/.ssh/known_hosts")]
    URL = "git@github.com:Buro-Built/buro-registry.git"

    patch(CLEAN)
    assert mod._ssh_host_unchanged(URL) is None
    # git invokes 'ssh -G <user>@<host>' for an scp-style URL; '-v'
    # traces the config sources ssh actually read.
    assert calls == [["/usr/bin/ssh", "-G", "-v", "git@github.com"]]

    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "github.com:Buro-Built/buro-registry.git") is None
    assert calls[-1] == ["/usr/bin/ssh", "-G", "-v", "github.com"]

    # The -G target carries the URL's port and the RAW user — git hands
    # ssh userinfo byte-for-byte (no percent-decoding), so 'git%40x' is
    # a different user than 'git@x' to a Match user block. Encoded or
    # control-char users cannot be attested → refused.
    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "ssh://redir%65ct@github.com:2222/Buro-Built/buro-registry.git"
    ) is not None
    assert "cannot be attested" in mod._ssh_host_unchanged(
        "ssh://redir%65ct@github.com:2222/Buro-Built/buro-registry.git")
    # A plain user with a port probes exactly what git would invoke.
    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "ssh://redirect@github.com:2222/Buro-Built/buro-registry.git"
    ) is None
    assert calls[-1] == ["/usr/bin/ssh", "-G", "-v", "-p", "2222",
                         "redirect@github.com"]

    patch([(k, "evil.example.test" if k == "hostname" else v)
           for k, v in CLEAN])
    assert "redirects" in mod._ssh_host_unchanged(URL)

    patch(CLEAN + [("proxycommand", "ssh -W %h:%p bastion")])
    assert "ProxyCommand" in mod._ssh_host_unchanged(URL)

    patch(CLEAN + [("proxyjump", "bastion")])
    assert "ProxyCommand" in mod._ssh_host_unchanged(URL)

    for shkc in ("no", "off", "false", "0", "accept-new"):
        patch(CLEAN + [("stricthostkeychecking", shkc)])
        assert "host key" in mod._ssh_host_unchanged(URL), shkc
    # HostKeyAlias swaps the name used for host-key lookup while
    # hostname still reports github.com — refused unless unset or the
    # identity alias.
    patch(CLEAN + [("hostkeyalias", "attacker.example")])
    assert "HostKeyAlias" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("hostkeyalias", "github.com")])
    assert mod._ssh_host_unchanged(URL) is None
    # A KnownHostsCommand supplies host keys beyond the files —
    # refused unless unset/'none'.
    patch(CLEAN + [("knownhostscommand", "emit-attacker-key")])
    assert "KnownHostsCommand" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("knownhostscommand", "none")])
    assert mod._ssh_host_unchanged(URL) is None

    patch(CLEAN + [("userknownhostsfile", "/dev/null"),
                   ("globalknownhostsfile", "/dev/null")])
    assert "known-hosts" in mod._ssh_host_unchanged(URL)

    # A custom known-hosts path can point at an attacker-seeded file —
    # only ~/.ssh and /etc/ssh locations are trusted.
    patch(CLEAN + [("userknownhostsfile", "/tmp/attacker_hosts")])
    assert "untrusted" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("userknownhostsfile", "/etc/ssh/ssh_known_hosts")])
    assert mod._ssh_host_unchanged(URL) is None

    # An approved LOCATION is not proof of KEY identity: ssh accepts
    # any matching entry, so every github.com entry under ~/.ssh must
    # fingerprint to GitHub's published host keys — content is checked,
    # not just the directory the file lives in.
    import base64
    import hmac
    home = tmp_path / "h"
    kh_file = home / ".ssh" / "attacker_hosts"
    kh_file.parent.mkdir(parents=True)
    monkeypatch.setattr(mod, "pwd", types.SimpleNamespace(
        getpwuid=lambda _uid: types.SimpleNamespace(
            pw_dir=str(home), pw_name="u")))
    bad = base64.b64encode(b"forged-attacker-key-blob").decode()
    kh_file.write_text(f"github.com ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "published" in mod._ssh_host_unchanged(URL)

    # The pinned set holds GitHub's real published fingerprints.
    assert "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" \
        in mod._GITHUB_HOST_KEY_SHA256
    assert "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s" \
        in mod._GITHUB_HOST_KEY_SHA256
    assert "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM" \
        in mod._GITHUB_HOST_KEY_SHA256

    # A github.com entry whose blob fingerprints to the pinned set
    # anchors the host — patch the pinned set to a test blob's digest.
    blob = b"fake-github-key-blob"
    fp = "SHA256:" + base64.b64encode(
        hashlib.sha256(blob).digest()).decode().rstrip("=")
    monkeypatch.setattr(mod, "_GITHUB_HOST_KEY_SHA256", {fp})
    good = base64.b64encode(blob).decode()
    kh_file.write_text(f"github.com ssh-rsa {good}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert mod._ssh_host_unchanged(URL) is None

    # A certificate-authority entry covering github.com lets a CA sign
    # any host key — refused even beside a pinned key.
    kh_file.write_text(f"github.com ssh-rsa {good}\n"
                       f"@cert-authority github.com ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "certificate-authority" in mod._ssh_host_unchanged(URL)

    # Hashed known-hosts entries (HashKnownHosts) resolve via HMAC-SHA1
    # — a forged hashed github.com entry refuses the same way.
    salt = b"somesalt"
    tok = "|1|" + base64.b64encode(salt).decode() + "|" + \
        base64.b64encode(hmac.new(
            salt, b"github.com", hashlib.sha1).digest()).decode()
    kh_file.write_text(
        f"{tok} ssh-rsa {base64.b64encode(b'attacker-hashed').decode()}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "published" in mod._ssh_host_unchanged(URL)

    # No github.com entry anywhere → interactive TOFU stands (shkc=ask
    # already passed) — not refused.
    kh_file.write_text(f"other.example ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert mod._ssh_host_unchanged(URL) is None

    # Non-default effective port: 'Host github.com / Port 443' makes
    # ssh look up '[github.com]:443' — a forged key under that hashed
    # token must fail too (the EFFECTIVE port, not the URL's).
    salt = b"portsalt"
    tok443 = "|1|" + base64.b64encode(salt).decode() + "|" + \
        base64.b64encode(hmac.new(
            salt, b"[github.com]:443", hashlib.sha1).digest()).decode()
    kh_file.write_text(
        f"{tok443} ssh-rsa {base64.b64encode(b'forged-443').decode()}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("port", "443")])
    assert "published" in mod._ssh_host_unchanged(URL)

    # A padded known-hosts file beyond the verification cap is refused
    # outright — skipping it would still let ssh read a forged entry.
    kh_file.write_text(f"github.com ssh-rsa {bad}\n" + "#" * (8 << 20))
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "size" in mod._ssh_host_unchanged(URL)

    # A quoted path containing spaces is emitted WITHOUT its quoting —
    # 'one two' parses as two absent fragments while ssh loads the
    # real spaced file. A spaced join naming an existing file is
    # ambiguous → refuse.
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh" / "one two").write_text(f"github.com ssh-rsa {bad}\n")
    patch([(k, f"{home}/.ssh/one two"
           if k == "userknownhostsfile" else v) for k, v in CLEAN])
    assert "ambiguous" in mod._ssh_host_unchanged(URL)

    # PermitLocalCommand + LocalCommand executes a command locally
    # after connecting — an exfil path invisible to the host checks.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("permitlocalcommand", "yes"),
                                 ("localcommand", "/bin/evil %h")])
    assert "local command" in mod._ssh_host_unchanged(URL)

    # 'Match exec' in ANY config source ssh read is state-dependent
    # local execution — a condition false now can be true at push
    # time, so -G's snapshot cannot attest it. The -v debug trace
    # names the sources; 'Match host exec.example.com' is a hostname
    # pattern and must NOT trip the scan.
    cfg = tmp_path / "ssh_match_exec"
    cfg.write_text('Match exec "test -f /tmp/marker"\n'
                   '  HostName attacker.example\n'
                   'Match host exec.example.com\n')

    def run_v(argv, **_kw):
        return R("hostname github.com\nstricthostkeychecking ask\n"
                 "userknownhostsfile ~/.ssh/known_hosts\n",
                 err=(f"debug1: Reading configuration data {cfg}\n"))
    monkeypatch.setattr(
        mod, "subprocess",
        types.SimpleNamespace(run=run_v,
                              TimeoutExpired=_sp.TimeoutExpired))
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(which=lambda _n: "/usr/bin/ssh"))
    assert "Match exec" in mod._ssh_host_unchanged(URL)
    hostpat = tmp_path / "ssh_host_pattern"
    hostpat.write_text('Match host exec.example.com\n'
                       '  HostName attacker.example\n')
    assert mod._match_exec_in([str(hostpat)]) is False
    # OpenSSH accepts quoted criteria — Match "exec" must refuse too.
    qcfg = tmp_path / "ssh_quoted_exec"
    qcfg.write_text('Match "exec" "test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(qcfg)]) is True
    # Optional '=' keyword separators: 'Match=exec cmd' and
    # 'Match exec="cmd"' are the same criterion to ssh.
    eqcfg = tmp_path / "ssh_eq_exec"
    eqcfg.write_text('Match=exec "test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(eqcfg)]) is True
    eqcfg2 = tmp_path / "ssh_eq_exec2"
    eqcfg2.write_text('Match exec="test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(eqcfg2)]) is True
    # A criterion name in ARGUMENT position is a value, not a
    # condition — 'Match host exec' targets a host literally named
    # exec and must not trip.
    argpos = tmp_path / "ssh_arg_pos"
    argpos.write_text("Match host exec\n  HostName attacker.example\n")
    assert mod._match_exec_in([str(argpos)]) is False
    # Flag criteria take no argument — 'final' must not swallow a
    # following 'exec' criterion as its value.
    flag = tmp_path / "ssh_flag_exec"
    flag.write_text('Match final exec "test -e /tmp/x"\n')
    assert mod._match_exec_in([str(flag)]) is True
    # Negated criteria: 'Match !exec "cmd"' is still state-dependent
    # execution — the command's result can differ between check and
    # push, so it refuses; a negated VALUE ('Match host
    # !exec.example.com') is a pattern and stays allowed.
    neg = tmp_path / "ssh_neg_exec"
    neg.write_text('Match !exec "test ! -e /tmp/marker"\n')
    assert mod._match_exec_in([str(neg)]) is True
    negval = tmp_path / "ssh_neg_val"
    negval.write_text('Match host !exec.example.com\n')
    assert mod._match_exec_in([str(negval)]) is False
    # GlobalKnownHostsFile 'none' disables the global file — the
    # remaining user file is still validated, not refused as a path.
    kh_file.write_text(f"github.com ssh-rsa {good}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("globalknownhostsfile", "none")])
    assert mod._ssh_host_unchanged(URL) is None

    # Provider libraries dlopen during authentication — non-default
    # PKCS11Provider/SecurityKeyProvider refuse, defaults pass.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("pkcs11provider", "/tmp/libp11.so")])
    assert "PKCS" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("securitykeyprovider", "/tmp/sk.so")])
    assert "security-key" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("securitykeyprovider", "internal")])
    assert mod._ssh_host_unchanged(URL) is None

    # ControlMaster attaches the push to an existing session — the
    # peer may not be github.com though -G reads clean. Refuse any
    # enabled form; 'no' (the default) passes.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN]
          + [("controlmaster", "auto"), ("controlpath", "/tmp/cm-%r@%h:%p")])
    assert "multiplex" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("controlmaster", "no")])
    assert mod._ssh_host_unchanged(URL) is None
    # Reaching the source cap must fail closed — a partial scan can
    # leave a 'Match exec' in an unscanned Include'd file.
    benign = tmp_path / "ssh_benign"
    benign.write_text("Host *\n")
    assert mod._match_exec_in([str(benign)] * 64) is False
    assert mod._match_exec_in([str(benign)] * 65) is True

    # A PATH-resolved binary outside the system dirs earns no trust —
    # a wrapper can attest to itself.
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(
            which=lambda _n: str(tmp_path / "nogit")))
    assert mod._trusted_prog("git") == ""
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(which=lambda _n: "/usr/bin/ssh"))
    assert mod._trusted_prog("ssh").startswith("/usr/")

    # Windows: the trust root is OS-derived (_windows_dir → kernel32),
    # never the caller-controlled SystemRoot env. When the OS cannot
    # answer there is NO trusted directory — a system-looking ssh
    # still refuses, and on POSIX _windows_dir reports nothing.
    assert mod._windows_dir() == ""
    monkeypatch.setattr(mod, "_windows_dir", lambda: "")
    monkeypatch.setattr(mod.os, "name", "nt")
    patch(CLEAN)
    assert "cannot be verified" in mod._ssh_host_unchanged(URL)
    monkeypatch.setattr(mod.os, "name", "posix")

    # A PATH-resolved ssh outside the system dirs is never even asked.
    patch(CLEAN, which="/tmp/evil/ssh")
    assert "cannot be verified" in mod._ssh_host_unchanged(URL)
    assert calls == []


def test_bootstrap_mirror_dirty_clone_no_ratchet(tmp_path):
    """A first seed into a clone that ALREADY has tracked files gets merge
    semantics too — hits in pre-existing files are the org's own content
    and must surface as lint FAILs, never be silently grandfathered into
    the ratchet."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "buro-registry"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    # A tracked pre-existing file carrying another org's slug.
    (root / "notes.md").write_text("see Impaktful-Platform/impaktful_3.0\n")
    sp.run(["git", "add", "notes.md"], cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    al = (root / "registry/pack-surface-allowlist.txt").read_text()
    entries = {ln.split("#", 1)[0] for ln in al.splitlines()
               if ln.strip() and not ln.startswith("#")}
    assert "notes.md" not in entries, \
        "pre-existing own-content hit was ratcheted on a dirty first seed"
    assert any(e.startswith("registry/platform/") for e in entries), \
        "vendored platform hits must still be seeded"

def test_bootstrap_mirror_refresh_aborts_on_bad_index(tmp_path):
    """An established mirror whose plugins.json is momentarily malformed
    (e.g. mid conflict-resolution) must NOT be overwritten with the
    canonical platform-only index — abort and keep the file."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "buro-registry"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    idx = root / "registry/plugins.json"
    idx.write_text("{malformed\n")
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry",
         "--refresh-platform"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 2, r.stderr
    assert idx.read_text() == "{malformed\n", "index clobbered on abort"

def test_bootstrap_mirror_refresh_preserves_index(tmp_path):
    """--refresh-platform overwrites the vendored platform tree but MERGES
    plugins.json — the mirror's own universe plugin entries survive."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "impaktful-registry"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Impaktful-Platform/impaktful-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "impaktful", "--slug",
         "impaktful-platform/impaktful-registry", "--refresh-platform"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    idx = json.loads((root / "registry/plugins.json").read_text())
    idx["plugins"].append({
        "scope": "impaktful", "name": "dqms",
        "path": "registry/impaktful/dqms",
        "version_source": ".claude-plugin/plugin.json",
        "description": "org-private plugin"})
    (root / "registry/plugins.json").write_text(json.dumps(idx))
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "impaktful", "--slug",
         "impaktful-platform/impaktful-registry", "--refresh-platform"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    names = {(p["scope"], p["name"])
             for p in json.loads(
                 (root / "registry/plugins.json").read_text())["plugins"]}
    assert ("impaktful", "dqms") in names, "mirror plugin dropped on refresh"
    assert ("platform", "framework") in names

def test_bootstrap_mirror_no_refresh_keeps_platform_index(tmp_path):
    """An established-mirror rerun WITHOUT --refresh-platform keeps the
    EXISTING platform index entries — importing a newer canonical's
    platform list while the old vendored tree stays put would desync
    index↔tree (indexed-but-missing dirs trip the INDEX lint check)."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "buro-registry"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr

    # The mirror's platform tree predates a canonical platform addition:
    # locally drop one platform entry (tree+index stay consistent).
    idx_path = root / "registry/plugins.json"
    idx = json.loads(idx_path.read_text())
    plat = [p for p in idx["plugins"] if p.get("scope") == "platform"]
    assert len(plat) >= 1
    dropped = (plat[0]["scope"], plat[0]["name"])
    idx["plugins"].remove(plat[0])
    idx_path.write_text(json.dumps(idx))

    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    names = {(p["scope"], p["name"])
             for p in json.loads(idx_path.read_text())["plugins"]}
    assert dropped not in names, \
        "no-refresh rerun re-imported a platform entry the tree lacks"

def test_bootstrap_mirror_refresh_preserves_owned_state(tmp_path):
    """On an established mirror, refresh must not (a) clobber the org's own
    scope.yaml edits, or (b) ratchet NEW violations in mirror-owned paths —
    only vendored-path hits may enter the regenerated allowlists, while
    pre-existing mirror-owned entries that are still live are preserved."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "cpdcheck-registry"
    root.mkdir()
    # The PACK-SURFACE scan only sees registry/** in a real git checkout —
    # its non-git rglob fallback skip-lists the dir. Mirrors are clones, so
    # init the root before seeding (regen also stages via git add).
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/CPDcheck/cpdcheck-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "cpdcheck", "--slug",
         "cpdcheck/cpdcheck-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr

    scope_file = root / "registry/cpdcheck/scope.yaml"
    scope_file.write_text(scope_file.read_text()
                          + "\n# org-specific note\n")

    # A pre-existing live ratchet entry for mirror-owned content.
    keep_line = "see Manolii-org/master for context"
    keep_key = "registry/cpdcheck/keep.md#" + \
        hashlib.sha256(keep_line.lower().encode()).hexdigest()[:8]
    (root / "registry/cpdcheck/keep.md").write_text(keep_line + "\n")
    al = root / "registry/pack-surface-allowlist.txt"
    al.write_text(al.read_text() + keep_key + "\n")

    # A NEW mirror-owned violation that was never ratcheted.
    (root / "registry/cpdcheck/new.md").write_text(
        "also see Manolii-org/master\n")

    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "cpdcheck", "--slug",
         "cpdcheck/cpdcheck-registry", "--refresh-platform"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr

    assert "org-specific note" in scope_file.read_text()

    entries = {ln.split("#", 1)[0] for ln in al.read_text().splitlines()
               if ln.strip() and not ln.startswith("#")}
    assert "registry/cpdcheck/keep.md" in entries, \
        "live mirror-owned ratchet entry dropped on refresh"
    assert "registry/cpdcheck/new.md" not in entries, \
        "refresh ratcheted a new mirror-owned violation"
def test_pack_surface_scans_tracked_skip_dir(tmp_path):
    """A slug committed under a skip-listed dir is still published —
    the scan enumerates tracked files, not directory names."""
    import subprocess as sp
    repo = tmp_path / "pack"
    repo.mkdir()
    (repo / "registry").mkdir()
    brand = repo / ".brand"
    brand.mkdir()
    (brand / "branded.yml").write_text("upstream: Buro-Built/bcp-core")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod.results = []
    mod.check_pack_surface()
    fails = [f for f in mod.results
             if f.check == "PACK-SURFACE" and f.status == "FAIL"]
    assert any(".brand" in f.detail for f in fails)


def test_pack_surface_private_slug_fails(tmp_path):
    """A private-repo slug outside registry/ is recon surface in a public
    repo — PACK-SURFACE fails on it."""
    repo = tmp_path / "pack"
    repo.mkdir()
    (repo / "registry").mkdir()
    (repo / "docs").mkdir()
    (repo / "docs/x.md").write_text("see Buro-Built/internal-repo here")
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod.results = []
    mod.check_pack_surface()
    fails = [f for f in mod.results
             if f.check == "PACK-SURFACE" and f.status == "FAIL"]
    assert any("docs/x.md" in f.detail for f in fails)


def test_pack_surface_self_repo_slug_ok(tmp_path):
    """References to ai-starter-pack itself are legitimate — the repo may
    name its own slug."""
    repo = tmp_path / "pack"
    repo.mkdir()
    (repo / "registry").mkdir()
    (repo / "README.md").write_text(
        "install: copier copy gh:Manolii-org/ai-starter-pack .")
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod.results = []
    mod.check_pack_surface()
    assert not [f for f in mod.results if f.status == "FAIL"]


def test_missing_plugin_manifest_conflicts(tmp_path):
    """A plugin dir without .claude-plugin/plugin.json must conflict — a
    silent 0.0.0 default would let `ref: "0"` satisfy it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / "registry/platform/framework/.claude-plugin/plugin.json"
     ).unlink()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "non-semver" in r.stdout or "missing" in r.stdout


def test_non_semver_manifest_version_conflicts(tmp_path):
    """'1.bad.2' must not satisfy ref '1.2' — version is a resolution input,
    non-semver values conflict instead of permissive parsing."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / "registry/platform/framework/.claude-plugin/plugin.json"
     ).write_text(json.dumps(
         {"name": "framework", "version": "1.bad.2", "description": "t"}))
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.2"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "non-semver" in r.stdout


def test_exact_ref_abbreviated_matches_zero_padded_version(tmp_path):
    """ref '1.0' must satisfy registry version '1.0.0' — the grammar accepts
    x[.y[.z]] so exact compares normalize to three components."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert (consumer / ".claude" / "skills" / "demo" / "x.md").is_file()


def test_wrongly_typed_lock_conflicts(tmp_path):
    """{"files": null} is valid JSON but not a lock — it must report a
    repairable conflict, not crash locked_digests with TypeError."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    (consumer / ".ai").mkdir(parents=True)
    (consumer / ".ai" / "capability-lock.json").write_text(
        '{"files": null}')
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "malformed" in r.stdout
    assert "Traceback" not in r.stderr


def test_nested_resolved_files_null_conflicts(tmp_path):
    """{"resolved":[{"files":null}]} — the entry is a dict but its nested
    files value is None; locked_digests must not TypeError."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    (consumer / ".ai").mkdir(parents=True)
    (consumer / ".ai" / "capability-lock.json").write_text(
        '{"resolved": [{"plugin": "platform/framework", "files": null}]}')
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "malformed" in r.stdout
    assert "Traceback" not in r.stderr


def test_skip_worktree_file_conflicts_under_pin(tmp_path):
    """git update-index --skip-worktree hides modified worktree bytes from
    status AND ls-files — only a comparison against the pinned git object
    catches the divergence."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    rel = "registry/platform/framework/skills/demo/x.md"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    (reg_root / rel).write_text("tampered")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "differs from the pinned git object" in r.stdout


def test_skip_worktree_manifest_conflicts_under_pin(tmp_path):
    """A skip-worktree .claude-plugin/plugin.json keeps status clean and every
    component file can match HEAD while the lock records a MODIFIED version —
    the manifest itself must be compared against the pinned object."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    rel = "registry/platform/framework/.claude-plugin/plugin.json"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    (reg_root / rel).write_text(
        '{"name": "framework", "version": "9.9.9", "description": "x"}')
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "differs from or is absent at the pinned" in r.stdout


def test_skip_worktree_deleted_file_conflicts_under_pin(tmp_path):
    """A tracked component DELETED under skip-worktree leaves git status
    clean and vanishes from worktree enumeration — the per-file git show
    would never run, materialising an incomplete plugin under the pin.
    The resolver must compare the pinned TREE's file list instead."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/demo/x.md", "v1"),
            ("skills/demo/y.md", "v1"),
        ],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    rel = "registry/platform/framework/skills/demo/y.md"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    (reg_root / rel).unlink()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "absent from the worktree" in r.stdout


def test_pinned_script_dep_ignores_untracked_plant(tmp_path):
    """Under a pin, bundled-script existence comes from the pinned git tree:
    an ignored worktree plant of scripts/setup.py must not flip the dep
    decision for a skill that declares it as a consumer script."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/demo/SKILL.md",
             "---\nname: demo\nconsumer_scripts: [scripts/setup.py]\n---\n"
             "Run `python3 scripts/setup.py`"),
        ],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    # Commit a .gitignore covering the plugin's scripts dir BEFORE the tag so
    # the plant below is ignored and the worktree stays clean under the pin.
    (reg_root / ".gitignore").write_text(
        "registry/platform/framework/scripts/\n")
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    # Ignored plant — invisible to porcelain, but is_file() would find it.
    plant = reg_root / "registry/platform/framework/scripts/setup.py"
    plant.parent.mkdir(parents=True)
    plant.write_text("print('planted')")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert (consumer / ".claude" / "skills" / "demo" / "SKILL.md").is_file()


def test_pinned_bundled_script_deleted_from_worktree_still_blocks(tmp_path):
    """A bundled scripts/dep tracked at the pin but hidden via skip-worktree
    must still count as bundled — the dep check reads the pinned tree, not
    the worktree."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/demo/SKILL.md",
             "---\nname: demo\n---\nRun `python3 scripts/setup.py`"),
            ("scripts/setup.py", "print('x')"),
        ],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    rel = "registry/platform/framework/scripts/setup.py"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    (reg_root / rel).unlink()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    # The skill still sees the pinned tree's bundled dep → skipped, not shipped.
    assert not (consumer / ".claude" / "skills" / "demo" / "SKILL.md").exists()


def test_component_ancestor_not_dir_conflicts(tmp_path):
    """.claude/agents as a plain FILE (not a symlink) must be a plan-time
    conflict — otherwise --apply copies earlier files then crashes at
    mkdir, leaving them materialised without a lock."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/demo/SKILL.md", "---\nname: demo\ndescription: d\n---\nv1"),
            ("agents/x.md", "agent"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / ".claude").mkdir()
    (consumer / ".claude" / "agents").write_text("a file, not a dir")
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "is not a directory" in r.stdout
    assert not (consumer / ".claude" / "skills" / "demo" / "SKILL.md").exists()


def test_check_detects_missing_lock(tmp_path):
    """Files matching the registry but no capability-lock.json → DRIFT,
    not OK — ownership was never established."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    run_resolver(m, reg_root, consumer, "--apply")
    (consumer / ".ai" / "capability-lock.json").unlink()
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "lock missing" in r.stdout


def test_check_detects_stale_lock(tmp_path):
    """A lock whose files map differs from what resolution produces →
    DRIFT even though every file on disk matches."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    run_resolver(m, reg_root, consumer, "--apply")
    lock_p = consumer / ".ai" / "capability-lock.json"
    doc = json.loads(lock_p.read_text())
    doc["resolved"][0]["resolved_version"] = "9.9.9"
    lock_p.write_text(json.dumps(doc))
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "lock is stale" in r.stdout


def test_undeclared_scope_dir_fails(tmp_path):
    """A registry-root dir outside ALL_SCOPES is invisible to the scope
    loop — its files escape ORG-LEAK and XSCOPE entirely. INDEX must fail."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    acme = reg_root / "registry" / "acme" / "private"
    acme.mkdir(parents=True)
    (acme / "s.md").write_text("manolii internal")
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_index()
    fails = [f for f in mod.results if f.check == "INDEX" and f.status == "FAIL"]
    assert any("undeclared scope" in f.detail for f in fails)


def test_xscope_scans_all_extensions(tmp_path):
    """Cross-scope ../<scope>/ refs in non-md/py/sh/json files (hooks,
    scripts, extensionless assets) must fail — no extension allowlist."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("hooks/run", "#!/bin/sh\nexec ../manolii/private/hook.sh"),
            ("data/config.yaml", "ref: ../manolii/x"),
        ],
    })
    mod = load_lint_module()
    mod.REGISTRY = reg_root / "registry"
    mod.results = []
    mod.check_xscope()
    fails = [f for f in mod.results if f.check == "XSCOPE" and f.status == "FAIL"]
    assert any("manolii" in f.detail for f in fails)
    assert len(fails) >= 2


def test_skip_worktree_modified_plugins_json_conflicts_under_pin(tmp_path):
    """plugins.json edited under skip-worktree leaves git status clean —
    but the catalog is a resolution input: a modified index could add a
    plugin the pin never published. The resolver must byte-compare the
    worktree catalog against `git show HEAD:./plugins.json`."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    rel = "registry/plugins.json"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    idx = json.loads((reg_root / rel).read_text())
    idx["plugins"].append({"scope": "platform", "name": "planted",
                           "path": "registry/platform/planted"})
    (reg_root / rel).write_text(json.dumps(idx))
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "plugins.json differs" in r.stdout


def test_exec_bit_drift_is_repaired(tmp_path):
    """Exec-bit drift on a locked file is drift, not 'identical' — the skip
    check compares mode, else a registry +x change never lands."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh",
                                "#!/bin/sh\ntrue\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    assert not dst.stat().st_mode & 0o111
    src = (reg_root / "registry" / "platform" / "framework" / "skills"
           / "demo" / "run.sh")
    src.chmod(0o755)
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    assert dst.stat().st_mode & 0o111


def test_prune_refuses_nonregular_orphan(tmp_path):
    """A lockfile entry whose on-disk path is a directory (or other
    non-regular file) conflicts under --prune — never silently skipped."""
    reg_root = make_registry(tmp_path / "src", {})
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii", [])
    (consumer / ".claude" / "skills" / "junk").mkdir(parents=True)
    ai = consumer / ".ai"
    ai.mkdir()
    (ai / "capability-lock.json").write_text(json.dumps({
        "version": 1, "universe": "manolii", "resolved": [],
        "files": {".claude/skills/junk": "0" * 64},
        "provenance": {".claude/skills/junk": "platform/framework"},
    }))
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "non-regular" in r.stdout
    assert (consumer / ".claude" / "skills" / "junk").is_dir()


def test_prune_releases_unattributed_orphan(tmp_path):
    """A lockfile 'files' claim with no install provenance (forged, hand-
    written, or adopted-on-match) must never unlink a path the resolver did
    not install — it is released from tracking, never pruned."""
    reg_root = make_registry(tmp_path / "src", {})
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii", [])
    victim = consumer / ".claude" / "skills" / "demo" / "keep.md"
    victim.parent.mkdir(parents=True)
    victim.write_text("hand-maintained")
    ai = consumer / ".ai"
    ai.mkdir()
    (ai / "capability-lock.json").write_text(json.dumps({
        "version": 1, "universe": "manolii", "resolved": [],
        "files": {".claude/skills/demo/keep.md":
                  hashlib.sha256(b"hand-maintained").hexdigest()},
    }))
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 0
    assert "not resolver-installed" in r.stdout
    assert victim.read_text() == "hand-maintained"
    lock = json.loads((ai / "capability-lock.json").read_text())
    assert ".claude/skills/demo/keep.md" not in lock["files"]


def test_prune_keeps_adopted_prematch(tmp_path):
    """A consumer file that merely matches registry content is adopted for
    drift-watching only — never provenanced, so --prune releases tracking
    instead of deleting the consumer's own file."""
    body = "---\nname: demo\ndescription: d\n---\nshared bytes\n"
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md", body)],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    own = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text(body)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    lock = json.loads((consumer / ".ai" / "capability-lock.json").read_text())
    assert ".claude/skills/demo/SKILL.md" in lock["files"]
    assert ".claude/skills/demo/SKILL.md" not in lock.get("provenance", {})
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 0, r.stdout
    assert own.read_text() == body
    lock = json.loads((consumer / ".ai" / "capability-lock.json").read_text())
    assert ".claude/skills/demo/SKILL.md" not in lock["files"]


def test_check_passes_with_kept_orphan(tmp_path):
    """Kept orphans are legitimate state — --check must not flag them as
    drift once the lock records them."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nbody")],
        "platform/other": [("skills/extra/SKILL.md",
                            "---\nname: extra\ndescription: d\n---\nbody")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"},
                        {"plugin": "platform/other", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    # Drop 'other' — its file becomes a kept orphan, still lock-tracked.
    write_manifest(consumer, "manolii",
                   [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 0, r.stdout
    assert "OK" in r.stdout


def test_manifest_requires_entry_types_validated(tmp_path):
    """A non-mapping or unquoted-numeric requires entry must FAIL up front —
    `ref: 1.10` parses as the float 1.1, silently resolving a wrong pin."""
    reg_root = make_registry(tmp_path / "src", {})
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = consumer / "ai-manifest.yaml"
    m.write_text("version: 1\nuniverse: manolii\nrequires:\n"
                 "  - plugin: platform/framework\n    ref: 1.10\n")
    r = run_resolver(m, reg_root, consumer)
    assert r.returncode == 2
    assert "requires[0]" in r.stderr


def load_resolve_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ai_resolve", RESOLVE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ai_resolve"] = mod  # dataclass annotation lookup needs this
    spec.loader.exec_module(mod)
    return mod


def test_mini_yaml_manifest_grammar():
    """The stdlib fallback must cover the manifest/frontmatter grammar and
    reject richer YAML explicitly — fail closed, never guess."""
    mod = load_resolve_module()
    doc = mod._mini_yaml(
        "# c\nversion: 1\nuniverse: manolii\nrequires:\n"
        "  - plugin: platform/framework\n    ref: \"^1.14\"\n"
        "  - plugin: platform/other\n    ref: \"1.0\"\n"
        "surfaces: [claude-code]\nfeature_flags: {}\n")
    assert doc == {
        "version": 1, "universe": "manolii",
        "requires": [{"plugin": "platform/framework", "ref": "^1.14"},
                     {"plugin": "platform/other", "ref": "1.0"}],
        "surfaces": ["claude-code"], "feature_flags": {},
    }
    # Plain scalars fold across deeper-indented continuation lines.
    doc2 = mod._mini_yaml("description: first\n  second line\nother:\n"
                          "  bare folded\nname: x\n")
    assert doc2 == {"description": "first second line",
                    "other": "bare folded", "name": "x"}
    for bad in ("a: &anchor", "a:\n\tb: 1",
                "a: \"unterminated", "x: [a,"):
        with pytest.raises(ValueError):
            mod._mini_yaml(bad)


def test_mini_yaml_indentationless_sequence():
    """`key:` followed by `-` items at the SAME indent is valid YAML —
    yaml.dump emits it for requires lists. Sibling keys must not be
    consumed as seq items."""
    mod = load_resolve_module()
    doc = mod._mini_yaml(
        "version: 1\nuniverse: manolii\nrequires:\n"
        "- plugin: platform/framework\n  ref: \"^1.14\"\n"
        "- plugin: platform/other\n  ref: \"1.0\"\n"
        "surfaces: [claude-code]\n")
    assert doc == {
        "version": 1, "universe": "manolii",
        "requires": [{"plugin": "platform/framework", "ref": "^1.14"},
                     {"plugin": "platform/other", "ref": "1.0"}],
        "surfaces": ["claude-code"],
    }
    try:
        import yaml as pyyaml
    except ImportError:
        pyyaml = None
    if pyyaml is not None:
        assert pyyaml.safe_load(
            "requires:\n- plugin: a\n  ref: '1.0'\nother: 2\n") == \
            mod._mini_yaml("requires:\n- plugin: a\n  ref: '1.0'\nother: 2\n")


def test_mini_yaml_rejects_duplicate_keys():
    """Duplicate keys silently keep the last value in YAML — the resolver
    must reject them outright instead of resolving an ambiguous manifest."""
    mod = load_resolve_module()
    for bad in ("a: 1\na: 2\n",
                "x:\n  k: 1\n  k: 2\n",
                "requires:\n- plugin: a\n  ref: '1.0'\n  ref: '2.0'\n"):
        with pytest.raises(ValueError):
            mod._mini_yaml(bad)


def test_check_flags_modified_or_missing_kept_orphan(tmp_path):
    """A kept orphan is still lockfile-tracked: editing or deleting it is
    drift, not a pass — --check verifies on-disk bytes vs the recorded
    digest."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nbody")],
        "platform/other": [("skills/extra/SKILL.md",
                            "---\nname: extra\ndescription: d\n---\nbody")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"},
                        {"plugin": "platform/other", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    write_manifest(consumer, "manolii",
                   [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    orphan = consumer / ".claude" / "skills" / "extra" / "SKILL.md"
    assert run_resolver(m, reg_root, consumer, "--check").returncode == 0
    orphan.write_text("hand edit — no longer the installed bytes")
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "modified" in r.stdout
    orphan.write_text("---\nname: extra\ndescription: d\n---\nbody")
    assert run_resolver(m, reg_root, consumer, "--check").returncode == 0
    orphan.unlink()
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "missing" in r.stdout


def test_apply_conflicts_on_local_exec_bit_drift(tmp_path):
    """A local chmod on a managed file is indistinguishable from a registry
    mode change — fail closed as a conflict instead of silently repairing."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    os.chmod(dst, (dst.stat().st_mode & ~0o111) if dst.stat().st_mode & 0o111
             else dst.stat().st_mode | 0o111)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "exec mode differs" in r.stdout


def test_pinned_exec_mode_conflicts(tmp_path):
    """A skip-worktree exec-bit flip passes the blob comparison — the
    resolver must compare the pinned tree's mode, not just its bytes."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    rel = "registry/platform/framework/skills/demo/x.md"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    f = reg_root / rel
    os.chmod(f, f.stat().st_mode | 0o111)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "exec bit differs from the pinned git tree" in r.stdout


def test_partial_exec_mask_chmod_conflicts(tmp_path):
    """0755 -> 0744 differs from the installed-mode record — the lock
    records the full installed mask (copystat ignores umask, so the
    installed bits are deterministic), and a chmod inside the exec bits
    is a local edit that must not be silently overwritten."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    src = (reg_root / "registry" / "platform" / "framework" / "skills"
           / "demo" / "run.sh")
    src.chmod(0o755)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    dst.chmod(0o744)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "exec mode differs" in r.stdout


def test_lock_records_full_installed_mode(tmp_path):
    """The lock's exec map stores the full installed mask — copystat
    applies the source's exact bits, so the record can detect a chmod
    inside the exec bits (0755 -> 0744) that a bool could not."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n"),
                               ("skills/demo/SKILL.md", "---\nname: demo\n"
                                "description: d\n---\nbody")],
    })
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "demo" / "run.sh").chmod(0o750)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    lock = json.loads(
        (consumer / ".ai" / "capability-lock.json").read_text())
    # New records are EXEC_TAG-tagged full masks — the tag keeps a real
    # installed mode of 0 or 0o111 distinct from a legacy any-exec record.
    assert lock["exec"][".claude/skills/demo/run.sh"] == 0o10000 | 0o750
    assert lock["exec"][".claude/skills/demo/SKILL.md"] == 0o10000 | 0o644


def test_legacy_bool_exec_record_conflicts_on_drift(tmp_path):
    """A bool record only expresses the any-exec state — drift that flips
    that state conflicts; a consistent state upgrades to an int record."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "demo" / "run.sh").chmod(0o755)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    dst.parent.mkdir(parents=True)
    dst.write_text("echo hi\n")
    dst.chmod(0o644)
    ai = consumer / ".ai"
    ai.mkdir()
    rel = ".claude/skills/demo/run.sh"
    (ai / "capability-lock.json").write_text(json.dumps({
        "version": 1, "universe": "manolii",
        "resolved": [{"plugin": "platform/framework", "scope": "platform",
                      "ref": "1.0.0", "resolved_version": "1.0.0",
                      "source": "platform/framework", "sha256": None}],
        "files": {rel: hashlib.sha256(b"echo hi\n").hexdigest()},
        "provenance": {rel: "platform/framework"},
        "exec": {rel: True},
    }))
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "exec mode differs" in r.stdout
    dst.chmod(0o755)
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    lock = json.loads((ai / "capability-lock.json").read_text())
    assert lock["exec"][rel] == 0o10000 | 0o755


def test_legacy_bool_record_allows_registry_mode_repair(tmp_path):
    """A bool 'false' record plus a non-exec destination proves the
    installed mode — a registry +x change is an attributable repair, not
    a conflict."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "demo" / "run.sh").chmod(0o755)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    dst.parent.mkdir(parents=True)
    dst.write_text("echo hi\n")
    dst.chmod(0o644)
    ai = consumer / ".ai"
    ai.mkdir()
    rel = ".claude/skills/demo/run.sh"
    (ai / "capability-lock.json").write_text(json.dumps({
        "version": 1, "universe": "manolii",
        "resolved": [{"plugin": "platform/framework", "scope": "platform",
                      "ref": "1.0.0", "resolved_version": "1.0.0",
                      "source": "platform/framework", "sha256": None}],
        "files": {rel: hashlib.sha256(b"echo hi\n").hexdigest()},
        "provenance": {rel: "platform/framework"},
        "exec": {rel: False},
    }))
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert dst.stat().st_mode & 0o111
    lock = json.loads((ai / "capability-lock.json").read_text())
    assert lock["exec"][rel] == 0o10000 | 0o755


def test_coincident_local_and_registry_chmod_conflicts(tmp_path):
    """Consumer chmod + the SAME registry chmod must still conflict — the
    dst matching the source does not prove the local change didn't happen;
    only the installed-mode record can tell."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    dst.chmod(0o755)
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "demo" / "run.sh").chmod(0o755)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "exec mode differs" in r.stdout


def test_permission_only_registry_change_repairs(tmp_path):
    """A registry chmod with identical bytes (0644 -> 0444) is mode drift,
    not 'identical' — rewriting keeps the installed file in step with the
    tagged full-mask record instead of stranding the consumer's copy."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md", "---\nname: demo\n"
                                "description: d\n---\nbody")],
    })
    src = (reg_root / "registry" / "platform" / "framework" / "skills"
           / "demo" / "SKILL.md")
    src.chmod(0o644)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    src.chmod(0o444)
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    assert dst.stat().st_mode & 0o777 == 0o444
    lock = json.loads(
        (consumer / ".ai" / "capability-lock.json").read_text())
    rel = ".claude/skills/demo/SKILL.md"
    assert lock["exec"][rel] == 0o10000 | 0o444
    # Steady state — the repaired mode satisfies the record on re-resolve.
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    assert run_resolver(m, reg_root, consumer, "--check").returncode == 0


def test_adopted_identical_records_dst_mode(tmp_path):
    """An untracked file byte-identical to the registry source is adopted
    for drift-watching — the lock must record the mode the file actually
    carries, not the source's, or the next resolve sees phantom drift."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md", "---\nname: demo\n"
                                "description: d\n---\nbody")],
    })
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "demo" / "SKILL.md").chmod(0o600)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    dst = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    dst.parent.mkdir(parents=True)
    dst.write_text("---\nname: demo\ndescription: d\n---\nbody")
    dst.chmod(0o644)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    lock = json.loads(
        (consumer / ".ai" / "capability-lock.json").read_text())
    rel = ".claude/skills/demo/SKILL.md"
    assert lock["exec"][rel] == 0o10000 | 0o644
    # Re-resolve is a clean identical-skip — no phantom mode conflict.
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    assert run_resolver(m, reg_root, consumer, "--check").returncode == 0


def test_exec_matches_tagged_vs_legacy_records():
    """Tagged records compare the full mask — a genuine installed mode of
    0 or 0o111 must not be mistaken for a legacy any-exec record."""
    m = load_resolve_module()._exec_matches
    assert m(0o10000 | 0o111, 0o111) is True
    assert m(0o10000 | 0o111, 0o755) is False
    assert m(0o10000 | 0, 0o400) is False
    assert m(0o10000 | 0o644, 0o644) is True
    # Legacy encodings keep any-exec semantics.
    assert m(0o111, 0o755) is True
    assert m(0o111, 0o644) is False
    assert m(True, 0o755) is True
    assert m(False, 0o600) is True


def test_manifest_version_must_be_int(tmp_path):
    """`version: true` and `version: 1.0` satisfy `!= 1` in Python — the
    schema version is a literal int or the manifest is malformed."""
    for bad in ("true", "1.0"):
        consumer = tmp_path / f"consumer-{bad}"
        consumer.mkdir()
        m = consumer / "ai-manifest.yaml"
        m.write_text(f"version: {bad}\nuniverse: manolii\nrequires: []\n")
        r = run_resolver(m, tmp_path / "src", consumer)
        assert r.returncode == 2
        assert "unsupported manifest version" in r.stderr


def test_mini_yaml_flow_trailing_comma():
    """A trailing comma inside a flow list/map is legal YAML — the empty
    text after it is not an item."""
    mod = load_resolve_module()
    data = mod._mini_yaml(
        "requires: [{plugin: platform/framework, ref: \"1.0.0\",}, "
        "]\nsurfaces: [claude-code,]\n")
    assert data["surfaces"] == ["claude-code"]
    assert data["requires"] == [
        {"plugin": "platform/framework", "ref": "1.0.0"}]


def test_mini_yaml_plain_scalar_containing_bracket():
    """'description: Use [ to open' is a plain scalar with '[' text — not
    a flow collection; folding/depth must not fire."""
    mod = load_resolve_module()
    data = mod._mini_yaml("a:\n  description: Use [ to open\n")
    assert data["a"]["description"] == "Use [ to open"


def test_pinned_ref_nested_checkout_conflicts(tmp_path):
    """rev-parse inside a registry nested beneath an unrelated repository
    resolves against the PARENT's refs — a pin would verify a tree it does
    not describe, so refuse unless the registry IS the checkout root or
    its registry/ dir."""
    import subprocess as sp
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    outer = tmp_path / "outer"
    outer.mkdir()
    sp.run(["git", "init", "-q"], cwd=outer, env=env, check=True)
    (outer / "README").write_text("x")
    sp.run(["git", "add", "-A"], cwd=outer, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=outer, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=outer, env=env, check=True)
    # Registry nested at outer/sub/registry — not the checkout root.
    reg_root = make_registry(outer / "sub", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework",
                         "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "checkout root" in r.stdout
    # A registry at the checkout's registry/ dir still verifies.
    good_root = make_registry(outer / "ok", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    sp.run(["git", "init", "-q"], cwd=good_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=good_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=good_root, env=env,
           check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=good_root, env=env, check=True)
    r2 = run_resolver(m, good_root, consumer, "--apply")
    assert r2.returncode == 0, r2.stdout


def test_mode_divergent_collision_conflicts(tmp_path):
    """Identical bytes + different any-exec state is a collision, not a
    dedup — the lock records one plugin's mode while the file carries the
    other's, and --check passes an inconsistent state."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/shared/tool.sh", "echo x\n")],
        "platform/other": [("skills/shared/tool.sh", "echo x\n")],
    })
    (reg_root / "registry" / "platform" / "other" / "skills"
     / "shared" / "tool.sh").chmod(0o755)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"},
                        {"plugin": "platform/other", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "output-path collision" in r.stdout


def test_partial_mask_collision_conflicts(tmp_path):
    """0755 vs 0750 is an rw-bit divergence — identical bytes still collide.
    Apply installs the source mask verbatim, so deduping would make the
    installed file depend on provider order."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/shared/tool.sh", "echo x\n")],
        "platform/other": [("skills/shared/tool.sh", "echo x\n")],
    })
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "shared" / "tool.sh").chmod(0o755)
    (reg_root / "registry" / "platform" / "other" / "skills"
     / "shared" / "tool.sh").chmod(0o750)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"},
                        {"plugin": "platform/other", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "output-path collision" in r.stdout


def test_adopted_mode_difference_stays_adopted(tmp_path):
    """An adopted file keeps its own mode forever: identical bytes but
    different rw bits than the source must NOT schedule a rewrite on the
    next run — the consumer owns that file's permissions."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/shared/tool.sh", "echo x\n")],
    })
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "shared" / "tool.sh").chmod(0o600)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    adopted = consumer / ".claude" / "skills" / "shared" / "tool.sh"
    adopted.parent.mkdir(parents=True)
    adopted.write_text("echo x\n")
    adopted.chmod(0o644)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    assert (adopted.stat().st_mode & 0o777) == 0o644
    # Second run: --check must see 'identical', not a planned rewrite.
    assert run_resolver(m, reg_root, consumer, "--check").returncode == 0
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0
    assert (adopted.stat().st_mode & 0o777) == 0o644
    lock = json.loads((consumer / ".ai" / "capability-lock.json").read_text())
    prov = lock.get("provenance", {})
    assert ".claude/skills/shared/tool.sh" not in prov


def test_flow_scalar_hyphen_apostrophe(tmp_path):
    """'editor-'s' inside a flow list is ONE plain scalar — '-' mid-token
    must not open a quote region and break the flow item parse."""
    mod = load_resolve_module()
    got = mod._mini_yaml("tags: [editor-'s, other]\n")
    assert got["tags"] == ["editor-'s", "other"]


def test_flow_map_quoted_keys(tmp_path):
    """Quoted keys in a flow map decode like any other scalar — PyYAML
    accepts {\"plugin\": x} and the fallback must too."""
    mod = load_resolve_module()
    got = mod._mini_yaml(
        'requires: [{"plugin": platform/framework, "ref": "1.0.0"}]\n')
    assert got["requires"] == [
        {"plugin": "platform/framework", "ref": "1.0.0"}]


def test_versioned_python_invocation_is_script_dep(tmp_path):
    """python3.11 scripts/x.py is the same bundled-script dependency as
    python3 — the interpreter version suffix must not hide it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/analytics/SKILL.md",
             "Run `python3.11 scripts/session-analytics.py --days 7`"),
            ("scripts/session-analytics.py", "# bundled helper"),
            ("skills/plain/SKILL.md", "self-contained"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    skills = consumer / ".claude" / "skills"
    assert not (skills / "analytics" / "SKILL.md").exists()
    assert (skills / "plain" / "SKILL.md").is_file()


def test_adopted_shared_file_no_false_collision(tmp_path):
    """Two plugins providing identical bytes+mode must BOTH adopt an
    existing consumer file even when the consumer's own mask differs —
    the collision check compares provider source modes, never the
    adopted file's recorded mode."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/shared/tool.sh", "echo x\n")],
        "platform/other": [("skills/shared/tool.sh", "echo x\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    adopted = consumer / ".claude" / "skills" / "shared" / "tool.sh"
    adopted.parent.mkdir(parents=True)
    adopted.write_text("echo x\n")
    adopted.chmod(0o600)
    for scope in ("framework", "other"):
        (reg_root / "registry" / "platform" / scope / "skills"
         / "shared" / "tool.sh").chmod(0o644)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"},
                        {"plugin": "platform/other", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert "collision" not in r.stdout
    assert (adopted.stat().st_mode & 0o777) == 0o600


def test_adopted_file_survives_registry_drift(tmp_path):
    """An adopted file is consumer-owned: the registry changing its
    content later must CONFLICT, never rewrite the file nor claim
    provenance over it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/shared/tool.sh", "echo x\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    adopted = consumer / ".claude" / "skills" / "shared" / "tool.sh"
    adopted.parent.mkdir(parents=True)
    adopted.write_text("echo x\n")
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    # Registry content drifts — the consumer file must not be clobbered.
    src = (reg_root / "registry" / "platform" / "framework" / "skills"
           / "shared" / "tool.sh")
    src.write_text("echo REGISTRY-V2\n")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode != 0 or "adopted" in r.stdout
    assert adopted.read_text() == "echo x\n"


def test_inline_comment_after_parent_key(tmp_path):
    """`requires: # comment` is a parent key with no scalar value — the
    strip must not feed an empty token to scalar and kill the parse."""
    mod = load_resolve_module()
    got = mod._mini_yaml(
        "version: 1\nuniverse: manolii\nrequires: # plugins\n"
        "  - plugin: platform/framework\n    ref: \"1.0.0\"\n")
    assert got["requires"] == [
        {"plugin": "platform/framework", "ref": "1.0.0"}]
    got2 = mod._mini_yaml(
        "requires:\n  - plugin: x # note\n    ref: \"1\"\n")
    assert got2["requires"] == [{"plugin": "x", "ref": "1"}]


def test_block_map_quoted_keys(tmp_path):
    """Quoted keys in block mappings decode like bare keys — PyYAML
    accepted `"version": 1` and `- "plugin": x`, so the stdlib parser
    must too."""
    mod = load_resolve_module()
    got = mod._mini_yaml(
        "\"version\": 1\n'universe': manolii\nrequires:\n"
        "  - \"plugin\": platform/framework\n    'ref': \"1.0.0\"\n")
    assert got == {
        "version": 1, "universe": "manolii",
        "requires": [{"plugin": "platform/framework", "ref": "1.0.0"}]}


def test_root_flow_map_frontmatter(tmp_path):
    """A whole-document flow map is valid frontmatter — `{description: x}`
    must parse through scalar rather than die in the block parser."""
    mod = load_resolve_module()
    got = mod._mini_yaml("{description: sample, consumer_scripts: [s.sh]}")
    assert got == {"description": "sample", "consumer_scripts": ["s.sh"]}
    got2 = mod._mini_yaml("[a, b]")
    assert got2 == ["a", "b"]


def test_quoted_key_with_colon_folds_flow_value(tmp_path):
    """A `:` inside a quoted key is not the map separator — a folded flow
    value beneath such a key must still collect its continuation lines."""
    mod = load_resolve_module()
    got = mod._mini_yaml(
        "\"description: usage\": [one,\n  two]\nnext: 1\n")
    assert got == {"description: usage": ["one", "two"], "next": 1}


def test_apostrophe_in_seq_key_folds_flow_value(tmp_path):
    """A mid-scalar apostrophe is not a quote opener — `- author's:[a,`
    must still find its map colon and fold the flow continuation. (Also
    asserts PyYAML's own semantics: `key:[` is a scalar, `key: [` a map.)"""
    mod = load_resolve_module()
    got = mod._mini_yaml("items:\n  - author's:[a,\n    b]\n")
    assert got == {"items": ["author's:[a, b]"]}
    got = mod._mini_yaml("items:\n  - author's: [a,\n    b]\n")
    assert got == {"items": [{"author's": ["a", "b"]}]}


def test_sourced_and_bun_script_invocations_are_deps(tmp_path):
    """`source scripts/x.sh`, `bun scripts/x.ts`, `exec scripts/x.sh` are
    bundled-script invocations — script_dep_block must gate them like
    interpreter calls."""
    mod = load_resolve_module()
    plug = tmp_path / "reg" / "registry" / "platform" / "p"
    (plug / "scripts").mkdir(parents=True)
    (plug / "scripts" / "setup.sh").write_text("x")
    for invocation, expect in (
            (b"source scripts/setup.sh", True),
            (b". scripts/setup.sh", True),    # POSIX `.` builtin sources too
            (b"resource scripts/setup.sh", False),  # `source` inside a word
            (b"bun scripts/setup.sh", True),
            (b"exec scripts/setup.sh", True),
            (b"bash -c scripts/setup.sh", True)):
        assert mod.script_dep_block(plug, invocation) is expect, invocation


def test_lock_dest_unwritable_fails_before_writes(tmp_path):
    """An unwritable .ai/ must refuse --apply BEFORE any component write —
    otherwise files land with no ownership record."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nbody")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    ai_dir = consumer / ".ai"
    ai_dir.mkdir()
    ai_dir.chmod(0o555)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode != 0
    assert not (consumer / ".claude" / "skills" / "demo" / "SKILL.md"
                ).exists()


def test_prune_refuses_chmodded_orphan(tmp_path):
    """An orphan whose exec mode drifted is a possible local chmod — --prune
    must refuse to unlink it, same as a byte-edited orphan."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nbody")],
        "platform/other": [("skills/extra/run.sh", "echo hi\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"},
                        {"plugin": "platform/other", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    write_manifest(consumer, "manolii",
                   [{"plugin": "platform/framework", "ref": "1.0.0"}])
    orphan = consumer / ".claude" / "skills" / "extra" / "run.sh"
    orphan.chmod(orphan.stat().st_mode | 0o111)
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "installed-mode record" in r.stdout
    assert orphan.is_file()


def test_check_flags_orphan_exec_drift(tmp_path):
    """A chmodded kept orphan is drift: --check compares the on-disk exec
    mask against the recorded one, not just bytes."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nbody")],
        "platform/other": [("skills/extra/run.sh", "echo hi\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"},
                        {"plugin": "platform/other", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    write_manifest(consumer, "manolii",
                   [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    orphan = consumer / ".claude" / "skills" / "extra" / "run.sh"
    assert run_resolver(m, reg_root, consumer, "--check").returncode == 0
    orphan.chmod(orphan.stat().st_mode | 0o111)
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "exec mode changed" in r.stdout


def test_legacy_lock_provenance_backfill(tmp_path):
    """Locks written by the shipped pre-provenance resolver keep ownership
    only in the top-level 'files' map (resolved[] was serialised without
    'files'). Backfilled 'unknown' entries stay tracked but can never be
    pruned — the record cannot distinguish an install from an adopted
    consumer file, and deletion is irreversible."""
    reg_root = make_registry(tmp_path / "src", {})
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    victim = consumer / ".claude" / "skills" / "demo" / "gone.md"
    victim.parent.mkdir(parents=True)
    victim.write_text("installed by old resolver")
    ai = consumer / ".ai"
    ai.mkdir()
    (ai / "capability-lock.json").write_text(json.dumps({
        "version": 1, "universe": "manolii",
        "resolved": [{"plugin": "platform/framework", "scope": "platform",
                      "ref": "1.0.0", "resolved_version": "1.0.0",
                      "source": "platform/framework", "sha256": None}],
        "files": {".claude/skills/demo/gone.md":
                  hashlib.sha256(b"installed by old resolver").hexdigest()},
    }))
    m = write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "unverifiable provenance" in r.stdout
    assert victim.is_file()
    # Without --prune the entry stays tracked under 'unknown' provenance.
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    lock = json.loads((ai / "capability-lock.json").read_text())
    assert lock["provenance"][".claude/skills/demo/gone.md"] == "unknown"
    assert victim.is_file()


def test_unknown_provenance_drift_conflicts(tmp_path):
    """A legacy lock cannot distinguish a resolver-installed file from an
    adopted consumer file ('unknown' provenance). Registry drift must NOT
    rewrite it and stamp fresh provenance — a later --prune would then
    delete a file that may be consumer-owned. Conflict instead."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\n"
                                "new body")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    dst = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    dst.parent.mkdir(parents=True)
    dst.write_text("---\nname: demo\ndescription: d\n---\nold body")
    ai = consumer / ".ai"
    ai.mkdir()
    rel = ".claude/skills/demo/SKILL.md"
    (ai / "capability-lock.json").write_text(json.dumps({
        "version": 1, "universe": "manolii",
        "resolved": [{"plugin": "platform/framework", "scope": "platform",
                      "ref": "1.0.0", "resolved_version": "1.0.0",
                      "source": "platform/framework", "sha256": None}],
        "files": {rel: hashlib.sha256(
            b"---\nname: demo\ndescription: d\n---\nold body").hexdigest()},
    }))
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "not provably resolver-installed" in r.stdout
    assert dst.read_text() == "---\nname: demo\ndescription: d\n---\nold body"
    # And it can never acquire provenance that makes it prune-eligible.
    lock = json.loads((ai / "capability-lock.json").read_text())
    assert lock.get("provenance", {}).get(rel) != "platform/framework"


def test_deleted_unknown_orphan_clears_lock(tmp_path):
    """A deleted 'unknown'-provenance orphan has nothing to unlink — --prune
    may clear its lock entry; only an EXISTING unknown path is refused."""
    reg_root = make_registry(tmp_path / "src", {})
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    ai = consumer / ".ai"
    ai.mkdir()
    (ai / "capability-lock.json").write_text(json.dumps({
        "version": 1, "universe": "manolii",
        "resolved": [{"plugin": "platform/framework", "scope": "platform",
                      "ref": "1.0.0", "resolved_version": "1.0.0",
                      "source": "platform/framework", "sha256": None}],
        "files": {".claude/skills/demo/gone.md":
                  hashlib.sha256(b"installed by old resolver").hexdigest()},
    }))
    m = write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 0, r.stdout
    lock = json.loads((ai / "capability-lock.json").read_text())
    assert ".claude/skills/demo/gone.md" not in lock["files"]


def test_symlinked_orphan_is_drift(tmp_path):
    """A kept orphan swapped for a symlink — even to identical bytes — is a
    type change: --check must flag it as drift."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    write_manifest(consumer, "manolii", [])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    orphan = consumer / ".claude" / "skills" / "demo" / "run.sh"
    twin = consumer / "twin.sh"
    twin.write_text("echo hi\n")
    orphan.unlink()
    orphan.symlink_to(twin)
    assert run_resolver(m, reg_root, consumer, "--check").returncode == 1


def test_prune_refuses_symlinked_orphan(tmp_path):
    """An orphan swapped for a symlink — even to identical bytes — is a
    type change (the digest check follows links): --prune must conflict,
    not unlink the consumer's link."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    write_manifest(consumer, "manolii", [])
    orphan = consumer / ".claude" / "skills" / "demo" / "run.sh"
    twin = consumer / "twin.sh"
    twin.write_text("echo hi\n")
    orphan.unlink()
    orphan.symlink_to(twin)
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "symlink" in r.stdout
    assert orphan.is_symlink()
    assert twin.read_text() == "echo hi\n"


def test_matching_local_edit_conflicts_on_ownership(tmp_path):
    """Consumer edits a resolver-installed file to bytes that coincide
    with the new registry content — the edit was never resolver-written,
    so recording the new digest would let a later --prune delete it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nA")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    edited = "---\nname: demo\ndescription: d\n---\nB"
    (consumer / ".claude" / "skills" / "demo" / "SKILL.md"
     ).write_text(edited)
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "demo" / "SKILL.md").write_text(edited)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "modified since install" in r.stdout


def test_legacy_resolved_files_backfill_is_unknown(tmp_path):
    """A legacy lock's resolved[].files records digests for written AND
    adopted-on-match files alike — backfilled provenance must be
    'unknown', so --prune refuses rather than deleting a possibly
    consumer-owned file."""
    reg_root = make_registry(tmp_path / "src", {})
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    victim = consumer / ".claude" / "skills" / "demo" / "keep.md"
    victim.parent.mkdir(parents=True)
    victim.write_text("hand-maintained")
    ai = consumer / ".ai"
    ai.mkdir()
    rel = ".claude/skills/demo/keep.md"
    (ai / "capability-lock.json").write_text(json.dumps({
        "version": 1, "universe": "manolii",
        "resolved": [{"plugin": "platform/framework", "scope": "platform",
                      "ref": "1.0.0", "resolved_version": "1.0.0",
                      "source": "platform/framework", "sha256": None,
                      "files": {rel: hashlib.sha256(
                          b"hand-maintained").hexdigest()}}],
        "files": {rel: hashlib.sha256(b"hand-maintained").hexdigest()},
    }))
    m = write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "unverifiable provenance" in r.stdout
    assert victim.is_file()


def test_mini_yaml_flow_list_quoted_commas():
    """A comma inside a quoted flow item belongs to the item, not the list."""
    mod = load_resolve_module()
    assert mod._mini_yaml('tags: ["a,b", c]') == {"tags": ["a,b", "c"]}
    assert mod._mini_yaml("tags: ['x,y', 'z']") == {"tags": ["x,y", "z"]}
    assert mod._mini_yaml('tags: [["a,b"], 2]') == {"tags": [["a,b"], 2]}
    # Apostrophes are legal inside PLAIN flow scalars — only a quote at an
    # item's start opens a quoted region.
    assert mod._mini_yaml("surfaces: [editor's-tool, claude-code]") == {
        "surfaces": ["editor's-tool", "claude-code"]}


def test_mini_yaml_nested_quoted_brackets():
    """A quote at a NESTED item boundary opens a quoted region too — a `]`
    inside it must not corrupt the outer bracket depth."""
    mod = load_resolve_module()
    assert mod._mini_yaml('x: [["a]b", c], d]') == {
        "x": [["a]b", "c"], "d"]}
    assert mod._mini_yaml("x: [['a]b', c], d]") == {
        "x": [["a]b", "c"], "d"]}
    assert mod._mini_yaml('x: [["a", "b]c"], d]') == {
        "x": [["a", "b]c"], "d"]}


def test_mini_yaml_flow_maps():
    """`{k: v}` flow maps parse — requires entries and feature_flags may
    use them; quoted values keep embedded commas."""
    mod = load_resolve_module()
    assert mod._mini_yaml(
        'requires: [{plugin: platform/framework, ref: "^1.0"}]') == {
            "requires": [{"plugin": "platform/framework", "ref": "^1.0"}]}
    assert mod._mini_yaml(
        "feature_flags: {kl_integration: true, x: 2}") == {
            "feature_flags": {"kl_integration": True, "x": 2}}
    assert mod._mini_yaml('x: [{a: "1,2", b: [y, {z: w}]}]') == {
        "x": [{"a": "1,2", "b": ["y", {"z": "w"}]}]}
    try:
        mod._mini_yaml("x: {a b}")
        raise AssertionError("keyless flow-map item must raise")
    except ValueError:
        pass


def test_mini_yaml_multiline_flow_list():
    """A flow collection folded across deeper lines joins with one space
    — each continuation line still strips its own comment."""
    mod = load_resolve_module()
    assert mod._mini_yaml("tags: [a,\n  b,\n  c]") == {
        "tags": ["a", "b", "c"]}
    assert mod._mini_yaml('tags: [\n  "a#b", # inline\n  c]') == {
        "tags": ["a#b", "c"]}
    try:
        mod._mini_yaml("x: [a,")
        raise AssertionError("unterminated flow must raise")
    except ValueError:
        pass


def test_mini_yaml_decodes_quoted_escapes():
    """Double-quoted scalars decode YAML escapes; single-quoted decode ''."""
    mod = load_resolve_module()
    assert mod._mini_yaml('ref: "^\\u0031.0.0"') == {"ref": "^1.0.0"}
    assert mod._mini_yaml('plugin: "platform\\u002fframework"') == {
        "plugin": "platform/framework"}
    assert mod._mini_yaml("d: 'it''s'") == {"d": "it's"}
    assert mod._mini_yaml('d: "Don\\u0027t run" # note') == {
        "d": "Don't run"}
    try:
        mod._mini_yaml('d: "bad\\q"')
        raise AssertionError("unknown escape must raise")
    except ValueError:
        pass


def test_apply_strips_privileged_mode_bits(tmp_path):
    """A setuid/setgid registry source must never propagate its privileged
    bits — copystat copies st_mode wholesale, which under a root-run
    --apply would publish setuid on the output."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "demo" / "run.sh").chmod(0o4755)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    assert dst.stat().st_mode & 0o7000 == 0
    assert dst.stat().st_mode & 0o111


def test_unparseable_frontmatter_treated_as_deps():
    """Frontmatter that will not parse must fail closed — treated as
    deps-declared (skip), never as clean."""
    mod = load_resolve_module()
    src = b"---\nname: [unclosed\n---\nbody"
    assert mod.declares_script_deps(src) is True
    assert mod.declared_consumer_scripts(src) == set()


def test_mini_yaml_single_document_markers():
    """`---`/`...` document markers are accepted for a single document;
    a second `---` (multi-doc) and content after `...` still fail closed."""
    mod = load_resolve_module()
    doc = mod._mini_yaml(
        "---\nversion: 1\nuniverse: buro\nrequires: []\n...\n")
    assert doc == {"version": 1, "universe": "buro", "requires": []}
    with pytest.raises(ValueError):
        mod._mini_yaml("---\na: 1\n---\nb: 2\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("a: 1\n...\nb: 2\n")


def test_block_scalar_hash_lines_preserved(tmp_path):
    """Literal `#` lines inside a `|`/`>` block scalar are content, not
    comments — `requires_scripts: |` holding only `# scripts/x.sh` must
    still gate materialisation."""
    mod = load_resolve_module()
    fm = ("---\nname: gated\nrequires_scripts: |\n"
          "  # scripts/setup.sh\n---\nbody").encode()
    assert mod.declares_script_deps(fm) is True
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/gated/SKILL.md", fm.decode())],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert not (consumer / ".claude" / "skills" / "gated" / "SKILL.md"
                ).exists()


def test_fifo_orphan_is_drift_not_hang(tmp_path):
    """A kept orphan replaced by a fifo must be reported as drift —
    sha256() opening a fifo would block on a writer forever."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh", "echo hi\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    write_manifest(consumer, "manolii", [])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    orphan = consumer / ".claude" / "skills" / "demo" / "run.sh"
    orphan.unlink()
    os.mkfifo(orphan)
    r = run_resolver(m, reg_root, consumer, "--check")  # timeout kills a hang
    assert r.returncode == 1
    assert "non-regular" in r.stdout


def test_apply_preflights_every_destination(tmp_path):
    """A later unwritable target must fail --apply BEFORE the first write —
    otherwise earlier files land without an ownership record and the retry
    adopts them as local files."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nbody"),
                               ("agents/runner.md", "agent body\n")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    agents_dir = consumer / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    agents_dir.chmod(0o555)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode != 0
    assert not (consumer / ".claude" / "skills" / "demo" / "SKILL.md"
                ).exists()


def test_mini_yaml_seq_block_scalars():
    """`- |` and `- key: |` block scalars in sequences parse like PyYAML —
    content dedents by its first line's indent, `>` folds, `x-`/`x+` chomp.
    The seq item's logical indent is indent+2, so a sibling key inside the
    item map is never swallowed as block content."""
    mod = load_resolve_module()
    doc = mod._mini_yaml(
        "items:\n  - |\n    line one\n    line two\n  - plain\n")
    assert doc == {"items": ["line one\nline two\n", "plain"]}
    doc = mod._mini_yaml(
        "entries:\n  - description: |\n      literal\n      # not a comment\n"
        "    tag: x\n  - description: >-\n      folded\n      lines\n")
    assert doc == {"entries": [
        {"description": "literal\n# not a comment\n", "tag": "x"},
        {"description": "folded lines"},
    ]}
    doc = mod._mini_yaml("ref: |-\n  ^1.14\n")
    assert doc == {"ref": "^1.14"}
    doc = mod._mini_yaml("ref: |\n  ^1.14\n")
    assert doc == {"ref": "^1.14\n"}
    doc = mod._mini_yaml("ref: >\n  ^1.14\n")
    assert doc == {"ref": "^1.14\n"}
    doc = mod._mini_yaml("v: |\n\n  a\n\n  b\n")
    assert doc == {"v": "\na\n\nb\n"}
    try:
        import yaml as pyyaml
    except ImportError:
        pyyaml = None
    if pyyaml is not None:
        for y in ("items:\n  - |\n    one\n    two\n  - plain\n",
                  "ref: |\n  ^1.14\n",
                  "ref: >-\n  a\n  b\n",
                  "v: |\n\n  a\n\n  b\n",
                  "entries:\n  - description: |\n      lit\n    tag: x\n"):
            assert pyyaml.safe_load(y) == mod._mini_yaml(y), y


def test_mini_yaml_double_doc_marker_rejected():
    """`---\\n---` is two documents — an empty first document still counts,
    so a second marker must fail closed."""
    mod = load_resolve_module()
    with pytest.raises(ValueError):
        mod._mini_yaml("---\n---\nversion: 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("---\n# comment\n---\nversion: 1\n")


def test_dot_source_never_crosses_lines(tmp_path):
    """The POSIX `.` builtin needs same-line whitespace — a full stop at
    the end of a prose line must not 'source' the NEXT line's scripts/
    path and gate a runnable file."""
    fm = ("---\nname: demo\ndescription: d\n---\n"
          "Sentence ends here.\nscripts/setup.sh is prose, not a dep.\n")
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md", fm)],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert (consumer / ".claude" / "skills" / "demo" / "SKILL.md").is_file()
    # A real `. scripts/x.sh` invocation still gates materialisation.
    fm2 = ("---\nname: gated\ndescription: d\n---\n"
           ". scripts/setup.sh\n")
    reg_root = make_registry(tmp_path / "src2", {
        "platform/framework": [("skills/gated/SKILL.md", fm2)],
    })
    consumer2 = tmp_path / "consumer2"
    consumer2.mkdir()
    m = write_manifest(consumer2, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer2, "--apply")
    assert r.returncode == 0, r.stdout
    assert not (consumer2 / ".claude" / "skills" / "gated" / "SKILL.md"
                ).exists()


def test_manifest_block_scalar_ref_resolves(tmp_path):
    """`ref: |-` is valid YAML — its value dedents and clips to the bare
    version constraint instead of reaching plan_requirement with the
    content indent or a trailing newline."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = consumer / "ai-manifest.yaml"
    m.write_text(
        "version: 1\nuniverse: manolii\nrequires:\n"
        "  - plugin: platform/framework\n    ref: |-\n      ^1.0\n")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (consumer / ".claude" / "skills" / "demo" / "SKILL.md").is_file()


def test_atomic_replace_never_restasts_source(tmp_path):
    """--apply uses the plan-time (atime, mtime) snapshot — a registry
    source deleted between plan and apply must not strand the write."""
    mod = load_resolve_module()
    src = tmp_path / "src.md"
    src.write_text("payload")
    st = src.stat()
    src.unlink()
    dst = tmp_path / "out.md"
    mod.atomic_replace(dst, lambda f: f.write(b"payload"),
                       times=(st.st_atime_ns, st.st_mtime_ns), mode=0o644)
    assert dst.read_bytes() == b"payload"
    assert dst.stat().st_mode & 0o777 == 0o644
    assert dst.stat().st_mtime_ns == st.st_mtime_ns


def test_prune_probe_skips_missing_orphan_parent(tmp_path):
    """An already-deleted orphan's parent needs no write probe — a
    read-only parent must not block pruning the other entries."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1"),
                               ("skills/extra/SKILL.md",
                                "---\nname: extra\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    orphan = consumer / ".claude" / "skills" / "extra" / "SKILL.md"
    orphan.unlink()
    (consumer / ".claude" / "skills" / "extra").chmod(0o555)
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 0, r.stdout + r.stderr
    lock = json.loads(
        (consumer / ".ai" / "capability-lock.json").read_text())
    assert ".claude/skills/extra/SKILL.md" not in lock["files"]


def test_prune_requires_full_mode_record(tmp_path):
    """A legacy or missing exec record cannot distinguish a consumer chmod
    of the non-exec bits — --prune conflicts instead of unlinking."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    rel = ".claude/skills/demo/SKILL.md"
    orphan = consumer / rel
    lock_file = consumer / ".ai" / "capability-lock.json"
    lock = json.loads(lock_file.read_text())
    lock["exec"][rel] = False  # legacy any-exec record — no full mask
    lock_file.write_text(json.dumps(lock))
    orphan.chmod(0o600)  # consumer chmod the digest cannot see
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert "installed-mode record" in r.stdout
    assert orphan.is_file()
    # An untouched file is equally unverifiable under a legacy record.
    orphan.chmod(0o644)
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 1
    assert orphan.is_file()


def test_mini_yaml_block_scalar_keep_and_fold_parity():
    """Keep-chomp preserves terminators, literal trailing spaces survive,
    and `>` folding keeps breaks around more-indented lines — asserted
    against PyYAML when importable."""
    mod = load_resolve_module()
    cases = [
        "plugin: |+\n      platform/framework\nref: x\n",
        "v: |-\n      a  \n",
        "v: |\n      a  \n",
        "x: >\n  a\n    b\n  c\n",
        "x: >\n  a\n\n  b\n",
        "x: >\n  a\n\n\n  b\n",
        "x: >+\n  a\n\n  b\n\n",
        "x: |+\n  a\n\n  b\n\n",
        "x: |+\n  a\n\n",
        "v: >-\n  a\n  b\n",
        # All-blank blocks: clip yields '' (trailing blanks are chomped),
        # keep yields exactly the blank lines' breaks.
        "x: |\n  \n", "x: |+\n  \n", "x: |\n  \n  \n", "x: >\n  \n",
        "x: >+\n  \n",
        # Blanks are content even when the block ends at an unindented
        # line — `x: |+\n\ny: 1` keeps '\n' for x.
        "x: |\n\ny: 1\n", "x: |+\n\ny: 1\n", "x: |+\n\n  a\n",
        "x: |+\n  \n  a\n", "x: |+\n  a\n\ny: 1\n", "x: |\n  a\n\ny: 1\n",
        # Unterminated EOF: `|+`/`>+` must not invent a final break, and
        # clip keeps a break only when the last non-blank line carried one.
        "x: |+\n  a", "x: >+\n  a", "x: |\n  a", "x: |\n  a\n",
        "x: |+\n  a\n  ", "x: |\n  a\n  ", "x: >-\n  a\n\n  ",
        "x: |\n", "x: >-\n",
        # Leading blanks + interior blank runs under `>`.
        "x: >-\n  \n  p\n", "x: >\n  \n  a\n\n  b\n",
        "x: >\n  a\n\n    m\n\n  b\n", "x: >+\n  a\n\n\n", "x: >+\n  a\n\n\n\n",
        "x: >-\n  a\n\n",
        # Explicit indentation indicators, both modifier orders, and on
        # sequence items — `|0` is invalid YAML (digit range is 1-9).
        "x: |2-\n      1.0.0\ny: 2\n", "x: |2\n  ab\n", "x: |-2\n  ab\n",
        "x: |4\n    ab\n", "x: |9+\n          ab\n",
        "- |2\n    a\n- b\n", "- key: |2\n      a\n- b\n", "- |2\n   a\n- b\n",
        # YAML 1.1 booleans/nulls in any case; y/n stay strings.
        "v: yes", "v: no", "v: On", "v: OFF", "v: y", "v: n",
        "v: Null", "v: NULL", "v: ~", "v: None",
        # Flow maps: quoted keys may abut their ':'; `{key:}` is null.
        'x: {"key":v}', "x: {k: v}", "x: {key:}", "x: {key: }",
    ]
    try:
        import yaml as pyyaml
    except ImportError:
        pyyaml = None
    for y in cases:
        got = mod._mini_yaml(y)
        if pyyaml is not None:
            assert pyyaml.safe_load(y) == got, y
    # Hard assertions independent of PyYAML availability.
    assert mod._mini_yaml("v: |-\n      a  \n") == {"v": "a  "}
    assert mod._mini_yaml("v: |\n      a  \n") == {"v": "a  \n"}
    assert mod._mini_yaml("x: >\n  a\n    b\n  c\n") == {"x": "a\n  b\nc\n"}
    assert mod._mini_yaml("plugin: |+\n      a\n") == {"plugin": "a\n"}
    assert mod._mini_yaml("x: |\n  \n") == {"x": ""}
    assert mod._mini_yaml("x: |+\n  \n") == {"x": "\n"}
    assert mod._mini_yaml("x: |+\n  a") == {"x": "a"}
    assert mod._mini_yaml("x: |\n  a") == {"x": "a"}
    assert mod._mini_yaml("x: |\n  a\n  ") == {"x": "a\n"}
    assert mod._mini_yaml("x: |+\n\ny: 1\n") == {"x": "\n", "y": 1}
    assert mod._mini_yaml("x: |2-\n      1.0.0\ny: 2\n") == {
        "x": "    1.0.0", "y": 2}
    assert mod._mini_yaml("v: off") == {"v": False}
    assert mod._mini_yaml('x: {"key":v}') == {"x": {"key": "v"}}
    assert mod._mini_yaml("x: {key:}") == {"x": {"key": None}}
    # `|0` is not a legal indicator (digits are 1-9) — the value is not a
    # block scalar at all, so it must raise rather than silently parse.
    try:
        mod._mini_yaml("x: |0\n  ab\n")
        raise AssertionError("|0 must fail closed")
    except ValueError:
        pass


def test_mini_yaml_flow_map_plain_key_requires_space():
    """In a flow map a PLAIN key's ':' separates only when followed by
    whitespace or the item's end — `{key:v}` is the scalar key 'key:v' in
    real YAML. A malformed requirement must not resolve: raising is the
    fail-closed behaviour here."""
    mod = load_resolve_module()
    for bad in ("x: {key:v}", "x: {a:b,c: d}", "x: {key :v}",
                'requires: [{plugin:platform/framework, ref:"1.0.0"}]'):
        try:
            mod._mini_yaml(bad)
            raise AssertionError(f"{bad!r} must not parse as a mapping")
        except ValueError:
            pass


def test_script_ref_ignores_hyphenated_prose():
    """`open-source scripts/x.sh` is prose, not a source invocation — the
    word boundary must exclude a hyphen prefix."""
    mod = load_resolve_module()
    assert not mod.SCRIPT_REF.search(b"an open-source scripts/setup.sh")
    assert not mod.SCRIPT_REF.search(b"re-exec scripts/setup.sh")
    assert mod.SCRIPT_REF.search(b"source scripts/setup.sh")
    assert mod.SCRIPT_REF.search(b". scripts/setup.sh")
    assert mod.SCRIPT_REF.search(b"bash scripts/setup.sh")


def test_keep_chomped_plugin_name_fails_validation():
    """`plugin: |+` keeps the trailing newline YAML keeps — the plugin
    name is then (correctly) invalid, not silently stripped."""
    mod = load_resolve_module()
    doc = mod._mini_yaml("plugin: |+\n      platform/framework\nref: x\n")
    assert doc["plugin"] == "platform/framework\n"
    assert not mod.REQUIRES_RE.match(doc["plugin"])


def test_prune_cleanup_survives_readonly_parent(tmp_path):
    """A missing orphan's empty dir under a read-only parent must not
    strand the lock update — directory cleanup is best-effort."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/old/SKILL.md",
                                "---\nname: old\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    orphan = consumer / ".claude" / "skills" / "old" / "SKILL.md"
    orphan.unlink()  # manually deleted — missing orphan, empty dir stays
    (consumer / ".claude" / "skills").chmod(0o555)
    write_manifest(consumer, "manolii", [])
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cleanup skipped" in r.stdout
    lock = json.loads(
        (consumer / ".ai" / "capability-lock.json").read_text())
    assert ".claude/skills/old/SKILL.md" not in lock["files"]


def test_mini_yaml_tab_rejected():
    """Tabs can never start a token — inside or outside a block scalar —
    matching PyYAML's ScannerError."""
    mod = load_resolve_module()
    with pytest.raises(ValueError):
        mod._mini_yaml("x: |\n  a\n\tb\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x:\n\ta\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("a:\tb\n")
    # a tab DEEPER than the content indent is literal content, not a token
    assert mod._mini_yaml("x: |\n  a\n  \tb\n") == {"x": "a\n\tb\n"}


def test_mini_yaml_block_whitespace_lines():
    """Whitespace-only lines inside a block scalar dedent relative to the
    content indent — a blank when shallower, literal spaces when deeper
    (kept even trailing). A leading ws-line deeper than the first content
    line is a PyYAML error, not auto-detected indentation."""
    mod = load_resolve_module()
    assert mod._mini_yaml("x: |\n  a\n   \n  b\n") == {"x": "a\n \nb\n"}
    # trailing ws deeper than the dedent is literal content — kept
    assert mod._mini_yaml("x: |\n  a\n   \n") == {"x": "a\n \n"}
    # a leading ws line deeper than the first content line → error
    with pytest.raises(ValueError):
        mod._mini_yaml("x: |\n    \n  a\n")
    # all-pending whitespace lines (never reaching content) → ''
    assert mod._mini_yaml("x: |\n    \n") == {"x": ""}
    assert mod._mini_yaml("x: |\n \n") == {"x": ""}
    assert mod._mini_yaml("x: |\n  \n   \n") == {"x": ""}
    # explicit indent: a ws line deeper than it keeps the excess spaces
    assert mod._mini_yaml("x: |2\n    \n") == {"x": "  \n"}
    assert mod._mini_yaml("x: |-\n  \n   \n") == {"x": ""}


def test_mini_yaml_doc_markers_col0_only():
    """`---`/`...` are document markers at column 0 only — an indented
    `---` inside a scalar continuation is plain text."""
    mod = load_resolve_module()
    assert mod._mini_yaml("---\nx: a\n...") == {"x": "a"}
    assert mod._mini_yaml("x:\n  ---\n  a\n") == {"x": "--- a"}
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n---\ny: 2\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x: |\n  a\n---\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n...\n---\ny: 2\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("---\n# c\n\n---\nx: 1\n")


def test_mini_yaml_quoted_multiline_folds():
    """An open quoted scalar folds continuation lines — content joins on
    spaces, blank lines become literal newlines, structure inside the
    quote is text."""
    mod = load_resolve_module()
    assert mod._mini_yaml('x: "a\n  b"\n') == {"x": "a b"}
    assert mod._mini_yaml("x: 'a\n  b'\n") == {"x": "a b"}
    assert mod._mini_yaml('x: "a\n\n  b"\n') == {"x": "a\nb"}
    assert mod._mini_yaml("x: 'a\n\n  b'\n") == {"x": "a\nb"}
    # blank line before the continuation → the fold keeps its newline
    assert mod._mini_yaml('x: "a"\n') == {"x": "a"}
    doc = mod._mini_yaml('x: "a\n   \n  b"\n')
    assert doc == {"x": "a\nb"}
    # a continuation line's leading whitespace (spaces or tabs) folds
    # into the single joining space — never content
    assert mod._mini_yaml('x: "a\n   b"\n') == {"x": "a b"}
    assert mod._mini_yaml('x: "a\n  \tb"\n') == {"x": "a b"}
    # key-shaped and dash lines inside the quote are text
    assert mod._mini_yaml('x: "a\n  y: 1"\n') == {"x": "a y: 1"}
    assert mod._mini_yaml("x: 'a\n  - s'\n") == {"x": "a - s"}
    # an unterminated quote is a ScannerError in PyYAML — we raise too
    with pytest.raises(ValueError):
        mod._mini_yaml('x: "a\ny: 2\n')
    with pytest.raises(ValueError):
        mod._mini_yaml('x: "a\n  y: 1\n')


def test_mini_yaml_plain_scalar_continuations():
    """Plain scalars fold deeper-indented continuations: content on
    spaces, blank lines to newlines, even lines shaped like seq items
    or document markers. A key-shaped line is 'mapping values are not
    allowed here'; a comment ends the scalar entirely."""
    mod = load_resolve_module()
    assert mod._mini_yaml("x: a\n  b\n") == {"x": "a b"}
    assert mod._mini_yaml("x: a\n\n  b\n") == {"x": "a\nb"}
    assert mod._mini_yaml("x: a\n  - s\n") == {"x": "a - s"}
    assert mod._mini_yaml("x: a\n  ---\n") == {"x": "a ---"}
    assert mod._mini_yaml("x: a\n  |\n") == {"x": "a |"}
    assert mod._mini_yaml("x: a\n   \n  b\n") == {"x": "a\nb"}
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n  \tb\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x:\n  a\n  y: 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x:\n  a\n y: 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n y: 1\n")
    # an outdented key ends the continuation — new sibling, not an error
    assert mod._mini_yaml("x: a\n  b\ny: 1\n") == {"x": "a b", "y": 1}
    # a comment terminates the scalar — a following indented line is an
    # error at any depth
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n # c\n  b\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n  # c\n  b\n")


def test_mini_yaml_empty_value_dispatch():
    """`key:` with nothing after it dispatches on the next real line:
    deeper scalar → scalar, same-indent dash → nested sequence, deeper
    key → nested map, a lone `|`/`>` → block scalar, else None."""
    mod = load_resolve_module()
    assert mod._mini_yaml("x:\n  a\n") == {"x": "a"}
    assert mod._mini_yaml("x:\n b: 2\n") == {"x": {"b": 2}}
    assert mod._mini_yaml("x:\n- s\n") == {"x": ["s"]}
    assert mod._mini_yaml("x:\n - s\n") == {"x": ["s"]}
    assert mod._mini_yaml("x:\nb: 2\n") == {"x": None, "b": 2}
    assert mod._mini_yaml("x:\n") == {"x": None}
    # lone indicator lines discovered AFTER the key
    assert mod._mini_yaml("x:\n  |\n    a\n") == {"x": "a\n"}
    assert mod._mini_yaml("x:\n  >\n    a\n") == {"x": "a\n"}
    assert mod._mini_yaml("x:\n\n  |\n    a\n") == {"x": "a\n"}
    assert mod._mini_yaml("x:\n  # c\n  |\n    a\n") == {"x": "a\n"}
    assert mod._mini_yaml("x:\n  |\n") == {"x": ""}
    # doc-root lone indicator is a scalar document
    assert mod._mini_yaml("|\n  a\n") == "a\n"
    # blank/comment lines between `x:` and a deeper scalar don't detach
    # it — the scalar still attaches as the value
    assert mod._mini_yaml("x:\n\n  a\n") == {"x": "a"}
    assert mod._mini_yaml("x:\n # c\n  a\n") == {"x": "a"}


def test_mini_yaml_comment_ends_block():
    """A `#` line deeper than the key ends the block when shallower than
    established content indent (or, pre-content, shallower than a deeper
    whitespace line). At/above content indent it is literal."""
    mod = load_resolve_module()
    assert mod._mini_yaml("x: |\n   a\n  # c\n") == {"x": "a\n"}
    assert mod._mini_yaml("x: |\n   \n  # c\n") == {"x": ""}
    assert mod._mini_yaml("x: |\n    w\n  # c\n") == {"x": "w\n"}
    assert mod._mini_yaml("- |\n   a\n  # c\n") == ["a\n"]
    # comment at/above content indent is literal
    assert mod._mini_yaml("x: |\n  a\n  # c\n  b\n") == {"x": "a\n# c\nb\n"}
    assert mod._mini_yaml("x: |\n # c\n") == {"x": "# c\n"}
    assert mod._mini_yaml("x: |\n # c\n  a\n") == {"x": "# c\n a\n"}
    # an orphan line after the ended block still errors
    with pytest.raises(ValueError):
        mod._mini_yaml("x: |\n   \n  # c\n  a\n")


def test_mini_yaml_dash_key_colon_dispatch():
    """`- k:` (empty value inside a seq item) resolves like PyYAML:
    a key AT the key column is a SIBLING of the item map, a deeper key
    or deeper scalar is the VALUE, a dash attaches at >= key column,
    and a marker in between detaches the next line."""
    mod = load_resolve_module()
    assert mod._mini_yaml("- k:\n  b: 2\n") == [{"k": None, "b": 2}]
    assert mod._mini_yaml("- k:\n    b: 2\n") == [{"k": {"b": 2}}]
    assert mod._mini_yaml("- k:\n   b: 2\n") == [{"k": {"b": 2}}]
    with pytest.raises(ValueError):
        mod._mini_yaml("- k:\n - s\n")
    assert mod._mini_yaml("- k:\n   \n  b: 2\n") == [{"k": None, "b": 2}]
    assert mod._mini_yaml("- k:\n\n  - x\n") == [{"k": ["x"]}]
    assert mod._mini_yaml("- k:\n  - x\n") == [{"k": ["x"]}]
    assert mod._mini_yaml("- k:\n  |\n    v\n") == [{"k": "v\n"}]
    assert mod._mini_yaml("- k:\n\n   a\n") == [{"k": "a"}]
    with pytest.raises(ValueError):
        mod._mini_yaml("- k:\n\n  a\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("- k:\n - s\n")


def test_mini_yaml_dash_scalar_and_seq_folds():
    """`- scalar` and deeper plain items fold continuations at their own
    thresholds; a sibling dash inside a deeper seq's fold region is text."""
    mod = load_resolve_module()
    assert mod._mini_yaml("- foo\n  b\n") == ["foo b"]
    # a dash line inside the scalar's fold region is text, not an item
    assert mod._mini_yaml("- foo\n - s\n") == ["foo - s"]
    assert mod._mini_yaml("- foo\n- s\n") == ["foo", "s"]
    assert mod._mini_yaml("- k: v\n   c\n") == [{"k": "v c"}]
    assert mod._mini_yaml("-\n  v\n c\n") == ["v c"]
    # a shallower dash after a nested seq is a PyYAML error — orphan
    with pytest.raises(ValueError):
        mod._mini_yaml("-\n  - a\n - s\n")
    assert mod._mini_yaml("x:\n  - foo\n    - bar\n") == {"x": ["foo - bar"]}
    # a blank line inside the fold keeps a real newline
    assert mod._mini_yaml("- foo\n   \n  b\n") == ["foo\nb"]
    with pytest.raises(ValueError):
        mod._mini_yaml("- desc: x\n    y: 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("- a: 1\n      b: 2\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("-\n  a\n    y: 1\n")


def test_mini_yaml_lone_indicator_after_dash():
    """`- |` values are every line deeper than the dash; the first
    content line's indent may be deeper than the indicator's own."""
    mod = load_resolve_module()
    # deeper content than the first line's indent → PyYAML error
    with pytest.raises(ValueError):
        mod._mini_yaml("-\n  |\n   a\n  b\n")
    # a leading ws line deeper than the first content line → error
    with pytest.raises(ValueError):
        mod._mini_yaml("- |\n   \n  a\n")
    assert mod._mini_yaml("- |\n  a\n   b\n") == ["a\n b\n"]
    # - | indented still ends cleanly at outdent
    assert mod._mini_yaml("- |\n  a\n- s\n") == ["a\n", "s"]
    # content below a bare dash's `|` dedents to its own first line
    assert mod._mini_yaml("-\n  |\n a\n") == ["a\n"]


def test_script_ref_separator_and_extensionless():
    """Invocation prefixes allow ANY amount of whitespace before
    scripts/ (`uv  run`, `sh\\t`), and extension-less names count too —
    but prose `top/scripts/x` still doesn't invoke."""
    mod = load_resolve_module()
    assert mod.SCRIPT_REF.search(b"uv  run scripts/setup.py")
    assert mod.SCRIPT_REF.search(b"sh\tscripts/setup.sh")
    assert mod.SCRIPT_REF.search(b"env bash scripts/setup.sh")
    assert mod.SCRIPT_REF.search(b"pipenv run scripts/setup.py")
    assert mod.SCRIPT_REF.search(b"./scripts/setup.sh")
    assert mod.SCRIPT_REF.search(b"source ./scripts/setup.sh")
    assert not mod.SCRIPT_REF.search(b"top/scripts/setup.sh")
    # extension-less scripts/ names are invocable too
    body = (b"---\nconsumer_scripts: [helper]\n---\n"
            b"source scripts/helper\n")
    assert mod.SCRIPT_REF.search(body)
    n = mod.SCRIPT_NAME.search(mod.SCRIPT_REF.search(body).group(0))
    assert n.group(1) == b"helper"


def test_script_dep_block_dot_slash_declared(tmp_path):
    """`consumer_scripts: [./scripts/x]` declares `x` — a bundled script
    stays a dep, an undeclared unbundled name still gates."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    # unbundled + declared via ./scripts/ → not a dep
    body = (b"---\nconsumer_scripts: [./scripts/helper.sh]\n---\n"
            b"bash scripts/helper.sh\n")
    assert not mod.script_dep_block(pdir, body)
    # same name declared WITHOUT the ./ prefix also counts
    body2 = (b"---\nconsumer_scripts: [helper.sh]\n---\n"
             b"bash scripts/helper.sh\n")
    assert not mod.script_dep_block(pdir, body2)
    # unbundled + NOT declared → dep
    assert mod.script_dep_block(pdir, b"bash scripts/other.sh\n")
    # bundled → dep regardless of declaration
    (pdir / "scripts" / "helper.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, body)


def test_secure_dir_fd_refuses_symlink_ancestors(tmp_path):
    """secure_dir_fd must raise when ANY path component is a symlink —
    a swapped-in link cannot redirect the write outside the tree."""
    mod = load_resolve_module()
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    fd = mod.secure_dir_fd(root, "a/b")
    os.close(fd)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "a" / "b").rmdir()
    (root / "a" / "b").symlink_to(outside)
    with pytest.raises(OSError):
        mod.secure_dir_fd(root, "a/b")
    (root / "a" / "b").unlink()
    (root / "a" / "b").mkdir()
    fd = mod.secure_dir_fd(root, "a/b")
    # writes through the fd land in the real dir even if a later level
    # is swapped — verified via atomic_replace's dfd path
    mod.atomic_replace(
        root / "a" / "b" / "out.md", lambda f: f.write(b"payload"),
        mode=0o600, dfd=fd)
    os.close(fd)
    assert (root / "a" / "b" / "out.md").read_bytes() == b"payload"
    assert not (root / "a" / "b" / "out.md").is_symlink()


def test_dirfd_apply_writes_through_planted_parent_symlink(tmp_path):
    """An --apply that prefetched a dir_fd must write beneath the real
    directory even if the parent path is swapped for a symlink between
    planning and apply."""
    mod = load_resolve_module()
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    assert dst.is_file() and not dst.is_symlink()


def test_mini_yaml_dquote_backslash_escape_fold():
    """In a double-quoted scalar, an ODD trailing '\\' run escapes the line
    break itself — join with NO separator. An even run is an escaped
    backslash + an ordinary space fold. Single-quoted '\\' is literal."""
    mod = load_resolve_module()
    assert mod._mini_yaml('x: "a\\\n  b"\n') == {"x": "ab"}
    assert mod._mini_yaml('x: "platform/\\\n  framework"\n') == \
        {"x": "platform/framework"}
    assert mod._mini_yaml('x: "a\\\n\n  b"\n') == {"x": "a\nb"}
    assert mod._mini_yaml('x: "a \\\n  b"\n') == {"x": "a b"}
    assert mod._mini_yaml('x: "a\\\\\n  b"\n') == {"x": "a\\ b"}
    assert mod._mini_yaml("x: 'a\\\n  b'\n") == {"x": "a\\ b"}


def test_mini_yaml_commented_key_lone_block_indicator():
    """`key: # note` strips to `key: ` — a lone `|`/`>` on the next line
    still opens a block scalar."""
    mod = load_resolve_module()
    assert mod._mini_yaml("ref: # pin\n  |\n    1.2\n") == {"ref": "1.2\n"}
    assert mod._mini_yaml("- key: # c\n   |\n    a\n") == [{"key": "a\n"}]
    assert mod._mini_yaml("- # c\n  |\n   a\n") == ["a\n"]


def test_mini_yaml_quoted_seq_item_with_colon():
    """A quoted scalar containing ': ' inside a seq item is a string, not
    a key — the plain-key alternative must not start with a quote char."""
    mod = load_resolve_module()
    assert mod._mini_yaml("- 'setup: done'\n- other\n") == \
        ["setup: done", "other"]
    assert mod._mini_yaml("examples:\n  - 'setup: done'\n") == \
        {"examples": ["setup: done"]}
    # a genuinely quoted KEY still parses
    assert mod._mini_yaml("- 'key': v\n") == [{"key": "v"}]


def test_mini_yaml_inline_doc_start_node():
    """`--- <node>` — the root node may share the marker line."""
    mod = load_resolve_module()
    assert mod._mini_yaml("--- {a: 1}\n") == {"a": 1}
    assert mod._mini_yaml("--- # c\n{a: 1}\n") == {"a": 1}
    mod._mini_yaml("--- \n")  # bare marker with empty doc tolerated
    with pytest.raises(ValueError):
        mod._mini_yaml("--- - 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("--- {a: 1}\n--- {b: 2}\n")


def test_script_dep_block_python_dash_m(tmp_path):
    """`python -m scripts.check` invokes the same file — a bundled module
    is an unsatisfiable dep; a consumer-declared one materialises."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/plugm": [
            ("commands/run.md",
             "run `python3 -m scripts.check` to verify"),
            ("scripts/check.py", "print(1)"),
        ],
        "platform/plugm2": [
            ("commands/run2.md",
             "run `python3 -m scripts.check` to verify"),
        ],
        "platform/plugm3": [
            ("commands/run3.md",
             "---\nconsumer_scripts: [scripts/check.py]\n---\n"
             "run `python3 -m scripts.check` to verify"),
        ],
        "platform/plugm4": [
            ("commands/run4.md",
             "run `python3 -m scripts.pkg.check` to verify"),
            ("scripts/pkg/check/__init__.py", ""),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(
        consumer, "manolii",
        [{"plugin": "platform/plugm", "ref": "1.0.0"},
         {"plugin": "platform/plugm2", "ref": "1.0.0"},
         {"plugin": "platform/plugm3", "ref": "1.0.0"},
         {"plugin": "platform/plugm4", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    cmds = consumer / ".claude" / "commands"
    installed = {p.name for p in cmds.glob("*.md")}
    assert "run3.md" in installed
    assert not (installed & {"run.md", "run2.md", "run4.md"})


def test_script_dep_block_mixed_invocation(tmp_path):
    """`python -m scripts.check scripts/extra.py` has TWO deps — the
    bundled module still blocks even when the later slash-path is
    declared."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    # the module dep is unbundled but undeclared -> blocks
    body = (b"---\nconsumer_scripts: [scripts/extra.py]\n---\n"
            b"python -m scripts.check scripts/extra.py\n")
    assert mod.script_dep_block(pdir, body)
    # declaring the module too -> allowed
    body2 = (b"---\nconsumer_scripts: [scripts/check.py,"
             b" scripts/extra.py]\n---\n"
             b"python -m scripts.check scripts/extra.py\n")
    assert not mod.script_dep_block(pdir, body2)
    # bundle the MODULE -> blocked even though extra.py is declared
    (pdir / "scripts" / "check.py").write_bytes(b"x")
    assert mod.script_dep_block(pdir, body2)


def test_script_dep_block_dash_m_hyphenated_module(tmp_path):
    """`python -m scripts-tools` is a DIFFERENT module argument, not bare
    `-m scripts` — it must not pin a dep on scripts/__main__.py
    (Codex/Devin on bcp-core#1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert not mod.script_dep_block(pdir, b"python -m scripts-tools\n")
    # bare `python -m scripts` still gates on scripts/__main__.py
    assert mod.script_dep_block(pdir, b"python -m scripts\n")
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "__main__.py").write_bytes(b"x")
    # bundled dep -> blocks; hyphenated still free
    assert not mod.script_dep_block(pdir, b"python -m scripts-tools\n")


def test_script_dep_exec_capable_program_heads(tmp_path):
    """sed's `e` command and awk's system()/cmd|getline execute text inside
    their program arguments — grouped with pure-output heads, `sed '1e
    bash scripts/x.sh'` read as inert (Codex + Devin on vendored
    review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"sed '1e bash scripts/x.sh' f\n")
    assert mod.script_dep_block(
        pdir, b"awk 'BEGIN{system(\"bash scripts/x.sh\")}' f\n")
    assert mod.script_dep_block(
        pdir, b"sed -n -e 'p' -e '1e bash scripts/x.sh' f\n")
    # inert heads stay literal
    assert not mod.script_dep_block(pdir, b'echo "bash scripts/x.sh"\n')
    assert not mod.script_dep_block(pdir, b"grep -l bash scripts/x.sh\n")


def test_script_dep_command_substitution_executes(tmp_path):
    """`$(...)` runs BEFORE the outer command head — `echo "$(bash x)"`
    executes x even though echo only prints. Single-quoted or escaped
    `$(` stays literal (Codex on vendored review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"echo $(bash scripts/x.sh)\n")
    assert mod.script_dep_block(pdir, b'echo "$(bash scripts/x.sh)"\n')
    assert mod.script_dep_block(pdir,
                                b'echo "$(x"$(bash scripts/x.sh)")"\n')
    assert not mod.script_dep_block(
        pdir, b"echo '$(bash scripts/x.sh)'\n")
    assert not mod.script_dep_block(
        pdir, b'echo "\\$(bash scripts/x.sh)"\n')


def test_script_dep_quoted_separator_is_literal(tmp_path):
    """A quoted `;`/`|`/`&` is not a command boundary — `echo "note; bash
    x"` stays an echo. A backward _command_start scan cannot tell an
    opening from a closing quote and split mid-string, turning the head
    into `bash` (Codex on vendored review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert not mod.script_dep_block(
        pdir, b'echo "note; bash scripts/x.sh"\n')
    assert not mod.script_dep_block(
        pdir, b'echo "a|b; bash scripts/x.sh"\n')
    # real separators still split
    assert mod.script_dep_block(pdir, b"echo ok; bash scripts/x.sh\n")


def test_script_dep_python_module_after_options(tmp_path):
    """`python -u -m scripts.check` and `python -X dev -m scripts.check`
    run the module — only `python -m` matched before (Devin on
    impaktful_3.0#1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"python -u -m scripts.check\n")
    assert mod.script_dep_block(pdir, b"python -B -m scripts.check\n")
    assert mod.script_dep_block(
        pdir, b"python -X dev -m scripts.check\n")
    assert mod.script_dep_block(
        pdir, b'python -X "dev mode" -m scripts.check\n')
    assert mod.script_dep_block(
        pdir, b"python3 -B -u -m scripts.check\n")
    # `-m` after a program operand is argv, not a module flag
    assert not mod.script_dep_block(
        pdir, b"python foo.py -m scripts.check\n")


def test_script_dep_word_concatenation(tmp_path):
    """A scripts/ word concatenated with a quoted suffix is a DIFFERENT
    argument — `scripts'-tools'` unquotes to `scripts-tools`, never the
    module `scripts` (Codex on vendored review). Quoted whole paths
    still invoke."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"bash 'scripts/x.sh'\n")
    assert mod.script_dep_block(pdir, b'bash "scripts/x.sh"\n')
    assert not mod.script_dep_block(pdir, b"python -m scripts'-tools'\n")
    assert not mod.script_dep_block(pdir, b"bash scripts/x.sh' more'\n")
    # Any dotted extension is invocable — bash runs `x.sh.bak` if it is
    # bundled (the extension allowlist was dropped on Codex's #123
    # finding), so the invocation is a dep like any other script call.
    assert mod.script_dep_block(pdir, b"bash scripts/x.sh.bak\n")


def test_yaml_load_strips_bom(tmp_path):
    """A UTF-8 BOM is a signature, not content — PyYAML skips it; leaving
    it would corrupt the first key for the mini parser (Codex on
    vendored review)."""
    mod = load_resolve_module()
    doc = mod._yaml_load("\ufeffversion: 1\nuniverse: manolii\n")
    assert doc == {"version": 1, "universe": "manolii"}


def test_prune_restores_when_lock_write_fails(tmp_path):
    """A failed apply must restore a staged prune by RENAME — the old
    byte-rewrite rollback needed free space and could clobber a path
    recreated after staging (Codex + Devin on vendored review)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")]})
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    orphan = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    assert orphan.is_file()
    write_manifest(consumer, "manolii", [])
    # Break only the lock write: .ai becomes non-writable.
    (consumer / ".ai").chmod(0o555)
    try:
        r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    finally:
        (consumer / ".ai").chmod(0o755)
    assert r.returncode == 2
    assert orphan.is_file()          # renamed back, not byte-restored
    assert not list(orphan.parent.glob(".ai-prune-*"))


def test_legacy_null_digest_lock_fails_closed(tmp_path):
    """A v1 lock entry with no digest cannot attribute byte/mode drift —
    planning fails closed (conflict) rather than scheduling a repair it
    cannot verify. The apply-time snapshot ALSO compares against the
    plan's recorded digest, so a write that does get scheduled for a
    null-digest entry can't be blind-allowed either (Devin on
    vendored-resolver review)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh",
                                "#!/bin/sh\ntrue\n")]})
    reg_sh = (reg_root / "registry" / "platform" / "framework"
              / "skills" / "demo" / "run.sh")
    reg_sh.chmod(0o755)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    assert dst.stat().st_mode & 0o777 == 0o755
    # Simulate a v1 lock: digest-less entry.
    lockf = consumer / ".ai" / "capability-lock.json"
    lock = json.loads(lockf.read_text())
    rel = ".claude/skills/demo/run.sh"
    assert rel in lock["files"]
    lock["files"][rel] = None
    lockf.write_text(json.dumps(lock))
    # Mode drift with no digest to attribute it -> conflict, not repair.
    dst.chmod(0o644)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "refusing to clobber" in r.stdout
    assert dst.stat().st_mode & 0o777 == 0o644


def test_script_dep_block_literal_text_not_invocation(tmp_path):
    """An interpreter+path inside text that only PRINTS (echo/printf/cat,
    quoted or not) or inside a `#` comment is documentation, not an
    invocation — it must not suppress the capability (Devin on
    impaktful_3.0#1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert not mod.script_dep_block(
        pdir, b'echo "bash scripts/demo.sh"\n')
    assert not mod.script_dep_block(
        pdir, b"echo bash scripts/demo.sh\n")
    assert not mod.script_dep_block(
        pdir, b'printf "%s\\n" "python scripts/x.py"\n')
    assert not mod.script_dep_block(
        pdir, b'# run bash scripts/demo.sh first\n')
    assert not mod.script_dep_block(
        pdir, b'grep -l bash scripts/x.sh\n')
    # Executing contexts still gate: -c strings and eval DO run.
    assert mod.script_dep_block(
        pdir, b"sh -c 'bash scripts/demo.sh'\n")
    assert mod.script_dep_block(
        pdir, b'eval "bash scripts/demo.sh"\n')
    assert mod.script_dep_block(
        pdir, b'bash scripts/demo.sh  # note\n')


def test_stale_skip_recheck(tmp_path):
    """A skipped (identical) destination whose bytes/mode drifted between
    planning and the lock write must be flagged — its digest would
    otherwise enter the lock stale (Devin on impaktful_3.0#1953)."""
    import hashlib as _hl
    mod = load_resolve_module()
    root = tmp_path
    dst = root / ".claude" / "agents" / "a.md"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"same")
    plan = mod.Plan(
        skips=[(dst, "identical")],
        resolved=[{"files": {".claude/agents/a.md":
                             _hl.sha256(b"same").hexdigest()},
                   "exec": {".claude/agents/a.md": 0o644}}])
    ok_snap = lambda p: (b"same", 0o644, (1, 2))
    assert mod._stale_skip(root, plan, ok_snap) is None
    # changed bytes -> stale
    bad_snap = lambda p: (b"changed", 0o644, (1, 2))
    assert mod._stale_skip(
        root, plan, bad_snap) == ".claude/agents/a.md"
    # vanished -> stale
    gone_snap = lambda p: None
    assert mod._stale_skip(
        root, plan, gone_snap) == ".claude/agents/a.md"
    # mode drift -> stale
    mode_snap = lambda p: (b"same", 0o755, (1, 2))
    assert mod._stale_skip(
        root, plan, mode_snap) == ".claude/agents/a.md"
    # and the reverse: bundled path + declared module still blocks
    pdir2 = tmp_path / "plug2"
    (pdir2 / "scripts").mkdir(parents=True)
    (pdir2 / "scripts" / "extra.py").write_bytes(b"x")
    body3 = (b"---\nconsumer_scripts: [scripts/check.py]\n---\n"
             b"python -m scripts.check scripts/extra.py\n")
    assert mod.script_dep_block(pdir2, body3)


def test_script_dep_block_module_main_py(tmp_path):
    """`python -m scripts.pkg` runs pkg/__main__.py (or pkg.py) — an
    __init__.py declaration alone does NOT satisfy the invocation."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    body_init = (b"---\nconsumer_scripts: [scripts/pkg/__init__.py]\n---\n"
                 b"python -m scripts.pkg\n")
    assert mod.script_dep_block(pdir, body_init)
    body_main = (b"---\nconsumer_scripts: [scripts/pkg/__main__.py]\n---\n"
                 b"python -m scripts.pkg\n")
    assert not mod.script_dep_block(pdir, body_main)
    body_mod = (b"---\nconsumer_scripts: [scripts/pkg.py]\n---\n"
                b"python -m scripts.pkg\n")
    assert not mod.script_dep_block(pdir, body_mod)
    # a bundled __init__.py is NOT an entry point — a consumer-declared
    # __main__.py still satisfies `python -m pkg` (Codex on #1344)
    (pdir / "scripts" / "pkg").mkdir()
    (pdir / "scripts" / "pkg" / "__init__.py").write_bytes(b"")
    assert not mod.script_dep_block(pdir, body_main)
    # but a bundled __main__.py is a real bundled dep — still blocks
    (pdir / "scripts" / "pkg" / "__main__.py").write_bytes(b"")
    assert mod.script_dep_block(pdir, body_main)


def test_mini_yaml_doc_start_mapping_rejected():
    """`--- key: v` — a block mapping entry may not share the marker
    line; PyYAML raises 'mapping values are not allowed here'."""
    mod = load_resolve_module()
    for bad in ("--- version: 1\nuniverse: manolii\n",
                "--- x:\n", "--- 'k': v\n", "--- -\n",
                "--- 'k: v'\n", "--- a\n"):
        try:
            mod._mini_yaml(bad)
            raise AssertionError(f"{bad!r} must raise")
        except ValueError:
            pass
    # legal remainders keep working: a flow collection or block-scalar
    # indicator is a COMPLETE node; bare scalars stay unsupported
    # (fail-closed — the supported subset never had a scalar root)
    assert mod._mini_yaml("--- {a: 1}\n") == {"a": 1}
    assert mod._mini_yaml("--- [a]\n") == ["a"]
    assert mod._mini_yaml("--- |\n  x\n") == "x\n"


def test_mini_yaml_plain_key_charset():
    """Plain keys may contain '/', '+', '=' etc. — in flow maps and in
    block mappings alike (PyYAML accepts them)."""
    mod = load_resolve_module()
    assert mod._mini_yaml(
        "feature_flags: {rollout/phase: true, a+b: 2}") == {
            "feature_flags": {"rollout/phase": True, "a+b": 2}}
    assert mod._mini_yaml("rollout/phase: true\n") == {
        "rollout/phase": True}
    assert mod._mini_yaml("x: {a:b: v}") == {"x": {"a:b": "v"}}


def test_apply_rollback_on_lock_failure(tmp_path):
    """If the lock write fails after outputs were materialised, the
    resolver must restore the prior state — files written this run are
    removed and pre-existing tracked files get their old bytes back.
    An unrolled-back output would be adopted WITHOUT provenance next
    run and conflict later on files the resolver itself wrote."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    assert skill.read_text().endswith("v1")
    # registry drifts to v2; break the lock path so apply fails at the
    # lock write after rewriting SKILL.md
    (reg_root / "registry" / "platform" / "framework" / "skills" / "demo"
     / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\n---\nv2")
    ai_dir = consumer / ".ai"
    os.chmod(ai_dir, 0o555)
    try:
        r = run_resolver(m, reg_root, consumer, "--apply")
    finally:
        os.chmod(ai_dir, 0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    # rolled back: v1 content restored, not half-applied v2
    assert skill.read_text().endswith("v1"), skill.read_text()


def test_apply_rollback_removes_new_outputs(tmp_path):
    """A FIRST apply that fails at the lock write must leave no
    materialised outputs behind."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1"),
                               ("commands/run.md", "run")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    ai_dir = consumer / ".ai"
    ai_dir.mkdir()
    os.chmod(ai_dir, 0o555)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    try:
        r = run_resolver(m, reg_root, consumer, "--apply")
    finally:
        os.chmod(ai_dir, 0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (consumer / ".claude" / "skills" / "demo"
                / "SKILL.md").exists()
    assert not (consumer / ".claude" / "commands" / "run.md").exists()


def test_apply_rollback_restores_file_timestamps(tmp_path):
    """Rollback must put back the prior file's mtimes, not a fresh
    `now` — a resolver-owned file restored byte-identically should be
    indistinguishable from the pre-failure state (Codex on #123)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    times = (1_600_000_000_000_000_000, 1_500_000_000_000_000_000)
    os.utime(skill, ns=times)
    # registry drift triggers a replace; lock write fails -> rollback
    reg_file = (reg_root / "registry" / "platform" / "framework"
                / "skills" / "demo" / "SKILL.md")
    reg_file.write_text("---\nname: demo\ndescription: d\n---\nv2")
    os.chmod(consumer / ".ai", 0o555)
    try:
        r = run_resolver(m, reg_root, consumer, "--apply")
    finally:
        os.chmod(consumer / ".ai", 0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    assert skill.read_bytes() == b"---\nname: demo\ndescription: d\n---\nv1"
    # mtime survives the round-trip. atime is NOT asserted — the resolver
    # reads the file during planning, which refreshes it before the
    # snapshot is taken, so the recorded atime is legitimately ~now.
    assert skill.stat().st_mtime_ns == times[1]


def test_apply_restores_deleted_installed_file(tmp_path):
    """An installed file the consumer deleted is a planned RESTORE, not
    a 'vanished destination' — apply must put it back, not refuse
    forever (Devin on #123)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    skill.unlink()
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert skill.read_bytes() == b"---\nname: demo\ndescription: d\n---\nv1"


def test_mode_drift_does_not_hide_local_edit(tmp_path):
    """Install A@0644, hand-edit the consumer copy to B@0644, then move the
    registry to B@0600 — the local edit must still CONFLICT, not be
    rewritten + re-provenanced as resolver-owned. Mode drift
    (mode_consistent False) must not bypass the modified-since-install
    guard."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    os.chmod(skill, 0o644)
    skill.write_text("---\nname: demo\ndescription: d\n---\nv2")
    # registry carries the SAME new bytes but at a different mode
    reg_file = (reg_root / "registry" / "platform" / "framework"
                / "skills" / "demo" / "SKILL.md")
    reg_file.write_text("---\nname: demo\ndescription: d\n---\nv2")
    os.chmod(reg_file, 0o600)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "modified since install" in r.stdout
    # the lock must NOT claim ownership of the local edit
    lock = json.loads((consumer / ".ai" / "capability-lock.json")
                      .read_text())
    assert lock["files"][".claude/skills/demo/SKILL.md"] != \
        hashlib.sha256(skill.read_bytes()).hexdigest()


def test_mini_yaml_next_line_collections():
    """`key:` empty followed by a deeper flow collection or quoted
    scalar — the node IS the value (Devin Review): `requires:` and
    `surfaces:` written this way must resolve, not come back as a
    string."""
    mod = load_resolve_module()
    doc = ('version: 1\nuniverse: manolii\nrequires:\n'
           '  [{plugin: platform/framework, ref: "1.0.0"}]\n')
    assert mod._mini_yaml(doc) == {
        "version": 1, "universe": "manolii",
        "requires": [{"plugin": "platform/framework", "ref": "1.0.0"}]}
    assert mod._mini_yaml('surfaces:\n  [claude-code]\n') == {
        "surfaces": ["claude-code"]}
    assert mod._mini_yaml('ref:\n  "1.0.0"\n') == {"ref": "1.0.0"}
    assert mod._mini_yaml('ref:\n  \'v\'\n') == {"ref": "v"}
    assert mod._mini_yaml('x:\n  {a: 1}\n') == {"x": {"a": 1}}
    # a deeper stray line after the collection still rejects
    try:
        mod._mini_yaml('x:\n  [a]\n  junk\n')
        raise AssertionError("nested structure must raise")
    except ValueError:
        pass
    # plain folded scalars still fold
    assert mod._mini_yaml('x:\n  a\n  b\n') == {"x": "a b"}
    assert mod._mini_yaml('x:\n  a\n  b\n') == {"x": "a b"}


def test_script_dep_block_second_arg(tmp_path):
    """`bash scripts/first.sh scripts/second.sh` — the regex match ends at
    first.sh but second.sh is just as much a dependency: bundled it must
    block, undeclared it must block (Devin Review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    body = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
            b"bash scripts/first.sh scripts/second.sh\n")
    # second.sh bundled -> block even though first.sh is declared
    (pdir / "scripts" / "second.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, body)
    # second.sh unbundled + undeclared -> block
    (pdir / "scripts" / "second.sh").unlink()
    assert mod.script_dep_block(pdir, body)
    # both declared -> allowed
    body2 = (b"---\nconsumer_scripts: [scripts/first.sh,"
             b" scripts/second.sh]\n---\n"
             b"bash scripts/first.sh scripts/second.sh\n")
    assert not mod.script_dep_block(pdir, body2)
    # a scripts/ path inside a trailing shell comment is NOT a dep
    body3 = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
             b"bash scripts/first.sh # see scripts/notes.sh\n")
    assert not mod.script_dep_block(pdir, body3)


def test_prune_dir_cleanup_after_lock_write(tmp_path):
    """--prune removes a dir's last file; if the lock write then fails,
    rollback must restore the file — so empty-dir cleanup may only run
    AFTER the lock commit (Devin Review + Codex on #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    # drop the requirement so --prune removes the file AND its now-empty
    # dir; then break the lock write
    m.write_text("version: 1\nuniverse: manolii\nrequires: []\n")
    ai_dir = consumer / ".ai"
    os.chmod(ai_dir, 0o555)
    try:
        r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    finally:
        os.chmod(ai_dir, 0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    # rollback restored the file — its parent dir survived cleanup
    assert skill.read_text().endswith("v1")


def test_pinned_mode_normalised_to_tree(tmp_path):
    """Under a tag:/sha: pin a worktree chmod on the r/w bits (0644 ->
    0600) hides from status AND the blob compare — the install + lock
    record must carry the pinned TREE mode, not the worktree's."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    os.chmod(reg_root / "registry" / "platform" / "framework" / "skills"
             / "demo" / "x.md", 0o600)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "x.md"
    assert dst.stat().st_mode & 0o777 == 0o644
    lock = json.loads((consumer / ".ai" / "capability-lock.json")
                      .read_text())
    assert lock["exec"][".claude/skills/demo/x.md"] & 0o777 == 0o644


def test_mini_yaml_quoted_fold_trailing_comment():
    """A comment on the line that CLOSES a folded quoted scalar is a
    comment, not scalar text — `plugin: "platform/\n framework" # c` is
    the value 'platform/ framework' (Devin Review on #1344)."""
    mod = load_resolve_module()
    doc = 'plugin: "platform/\n    framework" # plugin name\n'
    assert mod._mini_yaml(doc) == {"plugin": "platform/ framework"}
    # a '#' inside the folded quote is literal
    doc2 = 'plugin: "a\n    b#c" x\n'
    try:
        mod._mini_yaml(doc2)
        raise AssertionError("trailing token must raise")
    except ValueError:
        pass
    assert mod._mini_yaml('plugin: "a\n    b#c" # tail\n') == {
        "plugin": "a b#c"}


def test_script_dep_block_quoted_hash(tmp_path):
    """`"a#b"`/`a#b` mid-word is NOT a shell comment — later script args
    still reach the gate (Devin Review); a real ` # comment` still ends
    the scan."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    body = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
            b'bash scripts/first.sh "a#b" scripts/second.sh\n')
    (pdir / "scripts" / "second.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, body)
    (pdir / "scripts" / "second.sh").unlink()
    assert mod.script_dep_block(pdir, body)
    # mid-word unquoted hash is not a comment either
    body2 = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
             b"bash scripts/first.sh a#b scripts/second.sh\n")
    assert mod.script_dep_block(pdir, body2)
    # a real comment still bounds the window
    body3 = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
             b"bash scripts/first.sh # scripts/notes.sh\n")
    assert not mod.script_dep_block(pdir, body3)


def test_dq_escape_space(tmp_path):
    """YAML's standard `\\ ` escape decodes to a space (PyYAML accepts it —
    Codex)."""
    import yaml
    mod = load_resolve_module()
    for s, want in [
            ('a: "Escaped\\ space"', 'Escaped space'),
            ('a: "x\\ty"', 'x\ty'),
            ('a: "\\u0041"', 'A')]:
        doc = f"{s}\n"
        assert mod._mini_yaml(doc)['a'] == want == yaml.safe_load(doc)['a']


def test_prune_type_change_fails_closed(tmp_path):
    """A prune candidate swapped for a symlink/dir after planning fails the
    apply (rollback) instead of being silently skipped while its lock entry
    drops (Codex on #1953)."""
    import os, subprocess, json
    repo = tmp_path / "repo"
    (repo / ".claude" / "skills" / "old-skill").mkdir(parents=True)
    stale = repo / ".claude" / "skills" / "old-skill" / "SKILL.md"
    stale.write_text("stale")
    reg = tmp_path / "reg"
    (reg / "registry").mkdir(parents=True)
    lockdir = repo / ".ai"
    lockdir.mkdir()
    (lockdir / "capability-lock.json").write_text(json.dumps({
        "files": {".claude/skills/old-skill/SKILL.md":
                  {"scope": "platform", "plugin": "old-skill",
                   "sha256": "0" * 64, "exec": 0}},
        "resolved": {"platform/old-skill": {"version": "0.0.0"}}}))
    # swap the tracked file for a symlink before --apply --prune
    stale.unlink()
    stale.symlink_to(repo / ".ai" / "capability-lock.json")
    manifest = repo / "ai-manifest.yaml"
    manifest.write_text("version: 1\nuniverse: platform\nrequires: {}\n")
    out = run_resolver(manifest, reg, repo, "--apply", "--prune")
    assert out.returncode != 0
    # the symlink itself must not have been deleted or followed
    assert stale.is_symlink()
    # lock must not have been rewritten (drop would leave it untracked)
    assert json.loads((lockdir / "capability-lock.json"
                       ).read_text())["files"]


def test_mini_yaml_next_line_scalar_typed(tmp_path):
    """`key:` + next-line scalar node — the value is typed (`1` -> int,
    `on` -> bool, `{k: v}` -> map), matching PyYAML (Devin on #123)."""
    mod = load_resolve_module()
    doc = mod._mini_yaml("version:\n  1\nuniverse:\n  manolii\n"
                         "requires: []\nfeature_flags:\n  {a: 1}\n")
    assert doc == {"version": 1, "universe": "manolii",
                   "requires": [], "feature_flags": {"a": 1}}
    # A bare `-` + deeper flow map is a mapping item, not folded text.
    doc2 = mod._mini_yaml(
        "requires:\n  -\n    {plugin: platform/framework, ref: '1.0.0'}\n")
    assert doc2 == {"requires": [{"plugin": "platform/framework",
                                  "ref": "1.0.0"}]}
    # And a bare `-` + deeper plain/quoted scalar stays typed too.
    doc3 = mod._mini_yaml("items:\n  -\n    5\n  -\n    'six'\n")
    assert doc3 == {"items": [5, "six"]}


def test_line_continuation_script_dep(tmp_path):
    """`python3 \\` + `scripts/x.py` on the next line is ONE command — the
    dependency must be detected (Codex on #1953)."""
    reg = make_registry(tmp_path / "reg", {
        "platform/plugin": [(
            "commands/do.md",
            "run:\n```sh\npython3 \\\n    scripts/setup.py\n```\n")],
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = write_manifest(repo, "manolii",
                              [{"plugin": "platform/plugin", "ref": "1.0"}])
    out = run_resolver(manifest, reg, repo, "--apply")
    assert out.returncode == 0, out.stderr
    assert "not materialised" in out.stdout
    assert not (repo / ".claude" / "commands" / "do.md").exists()


def test_prune_missing_parent_dir_noop(tmp_path):
    """An orphan whose parent dir was deleted — under a read-only ancestor —
    is a no-op lock clear, never a mkdir failure (Devin on #123)."""
    import os, json
    repo = tmp_path / "repo"
    skills = repo / ".claude" / "skills"
    skills.mkdir(parents=True)
    reg = tmp_path / "reg"
    (reg / "registry").mkdir(parents=True)
    (reg / "registry" / "plugins.json").write_text(json.dumps(
        {"schema_version": 1, "scopes": {}, "plugins": []}))
    lockdir = repo / ".ai"
    lockdir.mkdir()
    (lockdir / "capability-lock.json").write_text(json.dumps({
        "files": {".claude/skills/old/SKILL.md": "0" * 64},
        "resolved": [{"plugin": "platform/old", "version": "0.0.0"}]}))
    manifest = repo / "ai-manifest.yaml"
    manifest.write_text("version: 1\nuniverse: platform\nrequires: []\n")
    os.chmod(skills, 0o555)  # recreation of old/ would fail here
    try:
        out = run_resolver(manifest, reg, repo, "--apply", "--prune")
    finally:
        os.chmod(skills, 0o755)
    assert out.returncode == 0, out.stderr
    assert not (skills / "old").exists()  # never resurrected
    assert json.loads((lockdir / "capability-lock.json"
                       ).read_text())["files"] == {}


def test_join_continuations_parity_and_quotes():
    """`\\<newline>` joins only where the shell keeps tokens contiguous —
    odd runs outside quotes or in dq join; even runs and everything in
    single quotes keep the backslash+newline literal (a literal backslash-newline
    inside sq is NOT a join — joining it forged invocations like
    `printf 'bash \\` + nl + `scripts/x.sh'`)."""
    mod = load_resolve_module()
    j, BS, NL = mod._join_continuations, b"\\", b"\n"
    assert j(b"python3 " + BS + NL + b"echo") == b"python3 echo"
    # even run: the last backslash is itself escaped — separate commands
    assert j(b"python3 " + BS * 2 + NL + b"echo") == (
        b"python3 " + BS * 2 + NL + b"echo")
    assert j(b"x " + BS * 3 + NL + b"y") == b"x " + BS * 2 + b"y"
    assert j(b'x "a' + BS + NL + b'b" y') == b'x "ab" y'
    # single quotes: every backslash is literal — no join
    assert j(b"x 'a" + BS + NL + b"b' y") == b"x 'a" + BS + NL + b"b' y"
    assert j(b"x " + BS + b"\r\n" + b"y") == b"x y"
    assert j(b"x " + BS + b"y") == b"x " + BS + b"y"


def test_mini_yaml_embedded_colon_plain_key():
    """`rollout:phase: true` keys on `rollout:phase` — a `:` inside a plain
    key is legal when not followed by whitespace (PyYAML-verified; Codex
    on #123)."""
    mod = load_resolve_module()
    try:
        import yaml
    except ImportError:
        yaml = None
    for doc, want in [
        ('feature_flags:\n  rollout:phase: true\n',
         {'feature_flags': {'rollout:phase': True}}),
        ('f:\n  a:b: [1, 2]\n', {'f': {'a:b': [1, 2]}}),
        ('k:v: |\n  txt\n', {'k:v': 'txt\n'}),
        ('k:v: 1\n', {'k:v': 1}),
        ('x: a:b\n', {'x': 'a:b'}),
    ]:
        assert mod._mini_yaml(doc) == want, doc
        if yaml is not None:
            assert mod._mini_yaml(doc) == yaml.safe_load(doc), doc


def test_script_dep_even_run_keeps_commands_separate(tmp_path):
    """`python3 \\\\` + `echo scripts/x.sh` are TWO commands — fusing them
    made x.sh look like a python3 dep (Codex on #1953 / Devin on #6)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    body = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
            b"python3 " + b"\\" * 2 + b"\necho scripts/x.sh\n")
    # x.sh is an echo arg, not a python dep — no block either way
    assert not mod.script_dep_block(pdir, body)
    # Sanity: a genuinely undeclared python dep still blocks
    body2 = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
             b"python3 " + b"\\" + b"\nscripts/x.sh\n")
    assert mod.script_dep_block(pdir, body2)


def test_script_dep_redirect_target_not_a_dep(tmp_path):
    """`cmd > scripts/out` creates the file — an output-redirect target is
    not a dependency the command reads (Devin on #1344). `<` still is."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    declared = b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
    for op in (b">", b">>", b"2>"):
        body = (declared + b"bash scripts/first.sh "
                + op + b" scripts/generated.sh\n")
        assert not mod.script_dep_block(pdir, body), op
    # input redirect still counts — that file must exist
    body = declared + b"bash scripts/first.sh < scripts/input.sh\n"
    assert mod.script_dep_block(pdir, body)


def test_script_dep_module_init_only_package(tmp_path):
    """`python -m scripts.pkg` runs pkg.py or pkg/__main__.py — a bundled
    __init__.py alone is no entry point, so a consumer-declared
    __main__.py satisfies the dep (Codex on #1344)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts" / "pkg").mkdir(parents=True)
    (pdir / "scripts" / "pkg" / "__init__.py").write_bytes(b"x = 1\n")
    body = (b"---\nconsumer_scripts: [scripts/pkg/__main__.py]\n---\n"
            b"python3 -m scripts.pkg\n")
    assert not mod.script_dep_block(pdir, body)
    # undeclared __main__.py still blocks
    body2 = (b"---\nconsumer_scripts: [scripts/other.py]\n---\n"
             b"python3 -m scripts.pkg\n")
    assert mod.script_dep_block(pdir, body2)


def test_legacy_exec_record_mode_drift_conflicts(tmp_path):
    """An untagged (legacy any-exec) exec record can't attribute r/w-bit
    drift — dst mode differing from src mode in non-exec bits must
    conflict, not pass as 'identical' or silently rewrite (Devin on
    #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    rel = ".claude/skills/demo/SKILL.md"
    lock_file = consumer / ".ai" / "capability-lock.json"
    lock = json.loads(lock_file.read_text())
    lock["exec"][rel] = False  # legacy any-exec record — no full mask
    lock_file.write_text(json.dumps(lock))
    (consumer / rel).chmod(0o600)  # r/w-bit drift a legacy record can't name
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "legacy exec record" in r.stdout
    # Same drift provable under a tagged record (exec class unchanged):
    # still refuses to clobber a local chmod — different message.
    lock["exec"][rel] = 0o10000 | 0o644
    lock_file.write_text(json.dumps(lock))
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "installed-mode record" in r.stdout


def test_prune_staging_name_collision_aborts(tmp_path):
    """A leftover `.ai-prune-<name>` sibling means the compare-and-unlink
    staging slot is taken — apply must abort, not clobber it (Codex on
    #1953)."""
    repo = tmp_path / "repo"
    skills = repo / ".claude" / "skills"
    (skills / "old").mkdir(parents=True)
    stale = skills / "old" / "SKILL.md"
    stale.write_text("stale")
    (skills / "old" / ".ai-prune-SKILL.md").write_text("leftover")
    reg = tmp_path / "reg"
    (reg / "registry").mkdir(parents=True)
    (reg / "registry" / "plugins.json").write_text(json.dumps(
        {"schema_version": 1, "scopes": {}, "plugins": []}))
    lockdir = repo / ".ai"
    lockdir.mkdir()
    lockdir.joinpath("capability-lock.json").write_text(json.dumps({
        "files": {".claude/skills/old/SKILL.md":
                  {"scope": "platform", "plugin": "old",
                   "sha256": "0" * 64, "exec": 0}},
        "resolved": [{"plugin": "platform/old", "version": "0.0.0"}]}))
    manifest = repo / "ai-manifest.yaml"
    manifest.write_text("version: 1\nuniverse: platform\nrequires: []\n")
    # digest mismatch alone would already abort — make it match first
    import hashlib
    dig = hashlib.sha256(b"stale").hexdigest()
    lock = json.loads(lockdir.joinpath("capability-lock.json").read_text())
    lock["files"][".claude/skills/old/SKILL.md"]["sha256"] = dig
    lockdir.joinpath("capability-lock.json").write_text(json.dumps(lock))
    out = run_resolver(manifest, reg, repo, "--apply", "--prune")
    assert out.returncode != 0
    assert stale.read_text() == "stale"  # restored, never unlinked
    assert (skills / "old" / ".ai-prune-SKILL.md").read_text() == "leftover"


def test_mini_yaml_signed_and_dot_scalars():
    """PyYAML numeric grammar: `+1` -> 1, `+1.5` -> 1.5, `.5`/`1.` are
    floats — but bare exponents (`-1e3`) stay strings (Codex on #123)."""
    mod = load_resolve_module()
    try:
        import yaml
    except ImportError:
        yaml = None
    for doc, want in [
        ('x: +1\n', {'x': 1}),
        ('x: +1.5\n', {'x': 1.5}),
        ('x: .5\n', {'x': 0.5}),
        ('x: 1.\n', {'x': 1.0}),
        ('x: -1e3\n', {'x': '-1e3'}),
        ('x: +1e3\n', {'x': '+1e3'}),
        # Non-decimal spellings Codex flagged on #123 — hex, binary,
        # legacy octal, underscore runs, signed exponents, sexagesimal,
        # .inf/.nan. `0o`/`0X`/`0B`/unsigned-exponent/`.Nan`/`-.nan` are
        # NOT PyYAML numbers and stay strings.
        ('x: 0x1\n', {'x': 1}),
        ('x: 0x10\n', {'x': 16}),
        ('x: +0x1\n', {'x': 1}),
        ('x: -0x10\n', {'x': -16}),
        ('x: 0b101\n', {'x': 5}),
        ('x: 012\n', {'x': 10}),
        ('x: -012\n', {'x': -10}),
        ('x: 00\n', {'x': 0}),
        ('x: 1_000\n', {'x': 1000}),
        ('x: 1_2_3\n', {'x': 123}),
        ('x: 1_0.5\n', {'x': 10.5}),
        ('x: 0.5_0\n', {'x': 0.5}),
        ('x: 1.0E+3\n', {'x': 1000.0}),
        ('x: -1.5e-2\n', {'x': -0.015}),
        ('x: 1e+3\n', {'x': '1e+3'}),  # no dot in mantissa → string
        ('x: 1:30\n', {'x': 90}),
        ('x: -1:2:3\n', {'x': -3723}),
        ('x: 08\n', {'x': '08'}),
        ('x: 09\n', {'x': '09'}),
        ('x: 0o7\n', {'x': '0o7'}),
        ('x: 0X1\n', {'x': '0X1'}),
        ('x: 0B1\n', {'x': '0B1'}),
        ('x: 1.0e3\n', {'x': '1.0e3'}),
        ('x: +1.e5\n', {'x': '+1.e5'}),
        ('x: .inf\n', {'x': float('inf')}),
        ('x: -.INF\n', {'x': -float('inf')}),
        ('x: -.nan\n', {'x': '-.nan'}),
        ('x: .Nan\n', {'x': '.Nan'}),
    ]:
        assert mod._mini_yaml(doc) == want, doc
        if yaml is not None:
            assert mod._mini_yaml(doc) == yaml.safe_load(doc), doc
    assert math.isnan(mod._mini_yaml('x: .NaN\n')['x'])
    if yaml is not None:
        assert math.isnan(yaml.safe_load('x: .NaN\n')['x'])


def test_mini_yaml_bool_case_is_pyyaml_exact():
    """PyYAML's bool/null regexes take only lower/Title/UPPER spellings —
    `tRuE`, `yEs`, `oFf`, `nUll` are STRINGS (Devin on #123)."""
    mod = load_resolve_module()
    for doc, want in [
        ('x: tRuE\n', {'x': 'tRuE'}),
        ('x: yEs\n', {'x': 'yEs'}),
        ('x: oFf\n', {'x': 'oFf'}),
        ('x: nUll\n', {'x': 'nUll'}),
        ('x: YeS\n', {'x': 'YeS'}),
        ('x: true\n', {'x': True}),
        ('x: True\n', {'x': True}),
        ('x: TRUE\n', {'x': True}),
        ('x: off\n', {'x': False}),
        ('x: Off\n', {'x': False}),
        ('x: OFF\n', {'x': False}),
        ('x: Null\n', {'x': None}),
        ('x: NULL\n', {'x': None}),
        ('x: ~\n', {'x': None}),
    ]:
        got = mod._mini_yaml(doc)
        assert got == want and type(got['x']) is type(want['x']), doc


def test_mini_yaml_sexagesimal_float_scale():
    """Sexagesimal floats scale every field but the LAST by 60^k:
    `1:20.5` = 80.5, `1:02:03.5` = 3723.5 (PyYAML; Codex on #1370)."""
    mod = load_resolve_module()
    for doc, want in [
        ('x: 1:20.5\n', 80.5),
        ('x: 1:02:03.5\n', 3723.5),
        ('x: -1:20.5\n', -80.5),
    ]:
        got = mod._mini_yaml(doc)['x']
        assert got == pytest.approx(want), doc
        try:
            import yaml as pyyaml
        except ImportError:
            pyyaml = None
        if pyyaml is not None:
            assert got == pyyaml.safe_load(doc)['x'], doc


def test_mini_yaml_rejects_embedded_mapping_value():
    """`description: foo: bar` is a ScannerError in PyYAML — a `:` + space
    inside a plain scalar is a nested mapping value, never text. The mini
    parser must fail closed, not ship 'foo: bar' as a valid value
    (Devin on #123)."""
    mod = load_resolve_module()
    for bad in ["description: foo: bar", "x: a: b", "x: foo:",
                "x: [a: b]", "x:\n  description: foo: bar"]:
        with pytest.raises(ValueError):
            mod._mini_yaml(bad + "\n")


def test_mini_yaml_block_scalar_tab_indentation():
    """A tab in block-scalar INDENTATION is a PyYAML ScannerError; a tab
    at/past the content column is literal content — including a tab-led
    FIRST content line (cursor[bot] on #1344, corrected against PyYAML)."""
    mod = load_resolve_module()
    try:
        import yaml
    except ImportError:
        yaml = None
    for doc, want in [
        ('x: |-\n  \ta\n', {'x': '\ta'}),
        ('x: |-\n  a\n  \tb\n', {'x': 'a\n\tb'}),
    ]:
        assert mod._mini_yaml(doc) == want, doc
        if yaml is not None:
            assert mod._mini_yaml(doc) == yaml.safe_load(doc), doc
    for doc in ('x: |-\n  a\n \tb\n', 'x: |-\n  a\n\tb\n'):
        with pytest.raises(ValueError):
            mod._mini_yaml(doc)
        if yaml is not None:
            with pytest.raises(Exception):
                yaml.safe_load(doc)


def test_script_dep_redirect_targets_and_bare_module(tmp_path):
    """Redirect targets that create/fill a file or take a word are not
    dependencies; input redirects (`<`, `<>`) still are — and a bare
    `python -m scripts` probes scripts/__main__.py."""
    mod = load_resolve_module()
    dep = lambda b: mod.script_dep_block(tmp_path, b.encode())
    # output / fd / heredoc / here-string targets — created, not read
    for body in [
        "python run.py > scripts/out.py",
        "python run.py >> scripts/out.py",
        "python run.py 2> scripts/err.txt",
        "python run.py &> scripts/log.txt",
        "python run.py >| scripts/x.py",
        "python run.py >& scripts/x.py",
        "python run.py << scripts/delim.py",
        "python run.py <<- scripts/delim.py",
        "python run.py <<< scripts/word.py",
        "python run.py <& scripts/fd.py",
    ]:
        assert not dep(body), body
    # input redirects READ the file — real deps
    for body in [
        "python run.py < scripts/in.py",
        "python run.py <> scripts/rw.py",
    ]:
        assert dep(body), body
    # a redirect target then a real dep in the next pipeline stage
    assert dep("cmd > scripts/x.py | bash scripts/y.sh")
    # bare package invocation needs __main__.py: unbundled + undeclared
    # blocks, bundled blocks (scripts/ is never materialised), declared
    # consumer-side materialises
    assert dep("python -m scripts")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "__main__.py").write_text("x=1")
    assert dep("python -m scripts")
    shutil.rmtree(tmp_path / "scripts")
    body = (b"---\nconsumer_scripts: [scripts/__main__.py]\n---\n"
            b"python -m scripts\n")
    assert not mod.script_dep_block(tmp_path, body)


def test_script_dep_extensionless_option_values(tmp_path):
    """An extensionless scripts/ path after a long option or `=` is an
    argument value (`--directory scripts/site`), not an invocation;
    after a bare `--` or a short flag it still gates (Codex on #1344)."""
    mod = load_resolve_module()
    dep = lambda b: mod.script_dep_block(tmp_path, b.encode())
    assert not dep("python -m http.server --directory scripts/site")
    assert not dep("python -m http.server --directory=scripts/site")
    assert not dep("python -m http.server -d scripts/site")
    assert not dep("python -m http.server -dscripts/site")
    assert not dep("python run.py --input=scripts/fixtures")
    # still gated: positional after `--`, short flag, plain arg
    assert dep("bash -- scripts/run")
    assert dep("bash -x scripts/run")
    assert dep("bash opts scripts/run")
    # An env-prefix `VAR=scripts/x` is a mention, not an invocation — the
    # interpreter's args carry no scripts/ path; a real dep declares
    # requires_scripts/consumer_scripts in frontmatter.
    assert not dep("DATA=scripts/fixtures bash run.sh")
    # extended names count even after a long option — fail-closed
    assert dep("python run.py --out scripts/out.py")
    assert dep("python run.py --input scripts/data.py")


def test_script_dep_sq_continuation_is_not_a_join(tmp_path):
    """`printf 'bash \\<nl>scripts/x.sh'` prints text — a literal
    backslash-newline inside single quotes is NOT a line continuation,
    so it must not register a dependency (cursor[bot] on #1344)."""
    mod = load_resolve_module()
    assert not mod.script_dep_block(
        tmp_path, b"printf 'bash \\\nscripts/missing.sh'\n")


def test_apply_aborts_on_symlink_destination(tmp_path):
    """A dst that is a link/non-regular at apply time must abort, not be
    journaled 'absent' and written over (Codex on #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    dst = consumer / ".claude" / "skills" / "a" / "SKILL.md"
    dst.parent.mkdir(parents=True)
    dst.symlink_to("/nonexistent-target")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode != 0  # plan conflict or apply abort — fail-closed
    assert dst.is_symlink()  # untouched — no clobber, no unlink


def test_apply_rollback_only_unlinks_its_own_inode(tmp_path, monkeypatch):
    """Rollback must delete the inode the write created — a path swapped
    to a different file before rollback is left alone (Codex on #1953)."""
    import errno
    mod = load_resolve_module()
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx"),
                       ("skills/b/SKILL.md",
                        "---\nname: b\ndescription: d\n---\ny")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    monkeypatch.setattr(sys, "argv", [
        str(RESOLVE), "--manifest", str(m), "--registry", str(reg_root),
        "--repo-root", str(consumer), "--apply"])
    real = mod.atomic_replace
    swap_src = tmp_path / "swapped"
    swap_src.write_text("SWAPPED")
    state = {"first": None}

    def fake(dst, fill, times=None, mode=None, dfd=None):
        if state["first"] is None:
            state["first"] = dst
            return real(dst, fill, times=times, mode=mode, dfd=dfd)
        # Concurrent edit simulation: the first written path is now a
        # DIFFERENT inode, then this write fails and unwinds.
        os.replace(swap_src, state["first"])
        raise OSError(errno.ENOMEM, "simulated write failure")

    monkeypatch.setattr(mod, "atomic_replace", fake)
    assert mod.main() == 2
    assert state["first"] is not None
    # Rollback left the swapped file alone — it is not ours to delete.
    assert state["first"].read_text() == "SWAPPED"


def test_prune_restores_after_staged_read_failure(tmp_path, monkeypatch):
    """A failed read of the staged `.ai-prune-*` name must put the file
    back, not strand it under the staging name (Devin on #6/#1344)."""
    import errno
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stderr
    f = consumer / ".claude" / "skills" / "a" / "SKILL.md"
    assert f.is_file()
    # Drop the requirement → f becomes a prune candidate.
    m.write_text("version: 1\nuniverse: buro\nrequires: []\n")
    mod = load_resolve_module()
    monkeypatch.setattr(sys, "argv", [
        str(RESOLVE), "--manifest", str(m), "--registry", str(reg_root),
        "--repo-root", str(consumer), "--apply", "--prune"])
    real_open = os.open

    def fake_open(path, flags, *a, **kw):
        if str(path).startswith(".ai-prune-"):
            raise OSError(errno.EACCES, "denied")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(mod.os, "open", fake_open)
    assert mod.main() == 2
    assert f.read_text() == "---\nname: a\ndescription: d\n---\nx"
    assert not list(f.parent.glob(".ai-prune-*"))


def test_script_dep_inner_substitution_head(tmp_path):
    """The head INSIDE `$(...)` decides, not the outer command —
    `$(echo bash x)` only prints, `$(bash x)` runs (Devin on
    #123/#6/#1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    # inner non-executing heads -> literal, not a dep
    assert not mod.script_dep_block(
        pdir, b'x="$(echo bash scripts/x.sh)"\n')
    assert not mod.script_dep_block(
        pdir, b"x=$(printf 'bash scripts/x.sh')\n")
    # inner EXECUTING head still gates
    assert mod.script_dep_block(
        pdir, b'x="$(bash scripts/x.sh)"\n')
    # nesting descends to the innermost command
    assert mod.script_dep_block(
        pdir, b'x="$(echo $(bash scripts/x.sh))"\n')
    assert not mod.script_dep_block(
        pdir, b'x="$(bash $(echo scripts/x.sh))"\n'
        .replace(b"scripts/x.sh", b'"$(echo ok)"'))


def test_script_dep_pipe_to_executor(tmp_path):
    """A non-executing head piped into an interpreter is not inert —
    `printf 'bash x' | sh` runs the text (Devin on #1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | sh\n")
    assert mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' |& sh\n")
    # harmless downstreams stay literal
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | wc -l\n")
    # `||` is a fallback, not a pipe to exec
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' || true\n")


def test_script_dep_heredoc_body(tmp_path):
    """Heredoc bodies are stdin data unless the head (or a pipe
    consumer) executes them (Devin on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    inert = b"cat <<EOF\nbash scripts/x.sh\nEOF\n"
    assert not mod.script_dep_block(pdir, inert)
    assert not mod.script_dep_block(
        pdir, b"cat <<- EOF\n\tbash scripts/x.sh\n\tEOF\n")
    # exec head / pipe-to-exec still gates
    assert mod.script_dep_block(
        pdir, b"sh <<EOF\nbash scripts/x.sh\nEOF\n")
    assert mod.script_dep_block(
        pdir, b"cat <<EOF | sh\nbash scripts/x.sh\nEOF\n")
    # quoted delimiter disables expansion -> stays literal
    assert not mod.script_dep_block(
        pdir, b"cat <<'EOF'\n$(bash scripts/x.sh)\nEOF\n")
    # unquoted body still expands $( ) — the substitution's own head runs
    assert mod.script_dep_block(
        pdir, b"cat <<EOF\n$(bash scripts/x.sh)\nEOF\n")
    # ...but non-expanding text in that same body stays inert
    assert not mod.script_dep_block(
        pdir, b"cat <<EOF\n$(date)\nbash scripts/x.sh\nEOF\n")


def test_script_dep_word_member_forms(tmp_path):
    """Glued short-option operands, $PWD-prefixed paths and input
    redirects still invoke the script (Codex + Devin on #1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"node -rscripts/preload.js\n")
    assert mod.script_dep_block(pdir, b"python $PWD/scripts/tool.py\n")
    assert mod.script_dep_block(pdir, b"python ${PWD}/scripts/tool.py\n")
    assert mod.script_dep_block(pdir, b"python <scripts/tool.py\n")
    # the -d directory-operand suppression still holds
    assert not mod.script_dep_block(pdir, b"node -dscripts/site\n")
    # bare-prefix concatenation still rejected
    assert not mod.script_dep_block(pdir, b"bash xscripts/tool.py\n")


def test_mini_yaml_inline_comment_ends_scalar(tmp_path):
    """`k: v # c` followed by a deeper line is a PyYAML error — the
    comment ends the scalar; folding a continuation across it must
    fail closed (Devin on #123)."""
    mod = load_resolve_module()
    try:
        mod._mini_yaml("key: safe # note\n  continuation\n")
        raise AssertionError("must raise")
    except ValueError:
        pass
    # normal inline comments still parse fine
    assert mod._mini_yaml("k: v # note\n") == {"k": "v"}
    assert mod._mini_yaml("k: v # note\nother: x\n") == {
        "k": "v", "other": "x"}
    # a deeper line after a NON-commented scalar still folds
    assert mod._mini_yaml("k: v\n  more\n") == {"k": "v more"}


def test_prune_staged_unlink_uses_dir_fd(tmp_path, monkeypatch):
    """Staged prune files are deleted through the verified dir FD while
    it is still open — a parent swapped to a symlink after staging
    cannot redirect the delete (Codex on #1953)."""
    mod = load_resolve_module()
    if not getattr(mod, "_HAS_DIRFD", False):
        pytest.skip("dirfd platform only")
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stderr
    m.write_text("version: 1\nuniverse: buro\nrequires: []\n")
    monkeypatch.setattr(sys, "argv", [
        str(RESOLVE), "--manifest", str(m), "--registry", str(reg_root),
        "--repo-root", str(consumer), "--apply", "--prune"])
    real_unlink = os.unlink
    seen = []

    def rec(path, *a, **kw):
        seen.append(kw.get("dir_fd"))
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(mod.os, "unlink", rec)
    assert mod.main() == 0
    assert any(fd is not None for fd in seen)


def test_registry_dotdot_index_path_fails_closed(tmp_path):
    """An index `path` escaping via `..` — even to a REAL sibling dir —
    is outside the canonical registry tree and conflicts (Devin SEC on
    #1953): resolved containment, not lexical string shape, is the
    boundary."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx")],
    })
    # redirect the index entry through `..` into a real sibling tree
    evil = reg_root / "evil" / "p"
    (evil / "skills" / "a").mkdir(parents=True)
    (evil / "skills" / "a" / "SKILL.md").write_text(
        "---\nname: a\ndescription: d\n---\nx")
    idx = reg_root / "registry" / "plugins.json"
    doc = json.loads(idx.read_text())
    doc["plugins"][0]["path"] = "registry/../evil/p"
    idx.write_text(json.dumps(doc))
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode != 0
    assert "outside the registry" in r.stdout


def test_script_dep_pipe_through_wrapper(tmp_path):
    """`printf 'bash x' | env bash` runs the emitted script — the head
    after the pipe is `env`, which execs bash with that stdin (Codex +
    Devin on vendored review). Same for command/sudo/nohup/exec."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for wrapped in (b"env bash", b"env -i FOO=1 bash", b"command bash",
                    b"sudo bash", b"nohup sh", b"exec sh", b"time sh"):
        assert mod.script_dep_block(
            pdir, b"printf 'bash scripts/x.sh' | " + wrapped + b"\n"), wrapped
    # a genuine non-interpreter consumer still does not execute
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | env cat\n")
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | env\n")
    # wrapper options that take an operand: the operand must be skipped,
    # not mistaken for the pipe's effective head (Devin Review on #1370)
    for wrapped in (b"sudo -u root bash", b"stdbuf -o L bash",
                    b"env -C /tmp bash", b"env -u FOO bash",
                    b"env --unset=FOO bash", b"exec -a sh bash",
                    b"time -o t.txt bash", b"sudo -uroot bash",
                    b"FOO=1 bash"):
        assert mod.script_dep_block(
            pdir, b"printf 'bash scripts/x.sh' | " + wrapped + b"\n"), wrapped
    # `command -v`/`-V` describe a command — nothing executes (same review)
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | command -v bash\n")
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | command -V sh\n")


def test_script_dep_pipe_multi_hop_and_sink(tmp_path):
    """The pipeline walk continues past forwarding heads —
    `| tee /dev/stderr | sh` executes (Devin on #123) — and stops at a
    sink: an interpreter taking its program from argv (`sh -c`,
    `python f.py`) ignores the pipe's contents (Devin on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for pipe in (b"cat scripts/x.sh | tee /dev/stderr | sh",
                 b"cat scripts/x.sh | grep -v '^#' | bash",
                 b"cat scripts/x.sh | sort | uniq | python",
                 b"cat scripts/x.sh | tee f | command bash"):
        assert mod.script_dep_block(pdir, pipe + b"\n"), pipe
    for pipe in (b"cat scripts/x.sh | sh -c 'true'",
                 b"cat scripts/x.sh | bash -c 'echo hi'",
                 b"cat scripts/x.py | python -c 'print(1)'",
                 b"cat scripts/x.sh | perl -e '1'",
                 b"cat scripts/x.py | python other.py",
                 b"cat scripts/x.sh | tee f | command -v sh",
                 b"cat scripts/x.sh | wc -l"):
        assert not mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # stdin-reading forms still execute the pipe's contents
    assert mod.script_dep_block(pdir, b"cat scripts/x.py | python -u\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.py | python -X dev\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | sh -s\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | bash -O extglob\n")


def test_script_dep_output_exec_substitution(tmp_path):
    """`bash -c "$(cat scripts/x.sh)"` and `bash <(cat scripts/x.sh)`
    execute the FILE's contents — the substitution's output is code, so
    the reader head inside supplies a dependency (Codex on #123)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for line in (b'bash -c "$(cat scripts/x.sh)"',
                 b"bash <(cat scripts/x.sh)",
                 b"source <(cat scripts/x.sh)",
                 b'. <(cat scripts/x.sh)',
                 b'eval "$(cat scripts/x.sh)"',
                 b'python -c "$(cat scripts/x.py)"',
                 b'node -e "$(cat scripts/x.sh)"',
                 b'eval `cat scripts/x.sh`'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # the substitution's output is NOT code under a reader/echo head
    for line in (b'echo "$(cat scripts/x.sh)"',
                 b'cat "$(cat scripts/x.sh)"',
                 b'cat <(cat scripts/x.sh)',
                 b'bash "$(cat scripts/x.sh)"'):  # operand is a path, not code
        assert not mod.script_dep_block(pdir, line + b"\n"), line


def test_script_dep_process_substitution(tmp_path):
    """`cat <(bash scripts/x.sh)` runs the script before cat sees the
    /dev/fd path — process substitution is an executing context, not an
    outer-cat literal (Codex on vendored review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, b"cat <(bash scripts/x.sh)\n")
    # the inner head decides: `<(echo bash x)` only prints the text
    assert not mod.script_dep_block(
        pdir, b"cat <(echo bash scripts/x.sh)\n")
    assert mod.script_dep_block(pdir, b"diff <(bash scripts/x.sh) f\n")
    # output process substitution runs the consumer too
    assert mod.script_dep_block(
        pdir, b"bash scripts/x.sh | tee >(cat)\n")
    # quoted/escaped openers stay literal text — `cat "<(x)"` passes the
    # literal string to cat (Devin Review: process substitution does NOT
    # expand inside double quotes; `$(` does)
    assert not mod.script_dep_block(
        pdir, b"echo '<(bash scripts/x.sh)'\n")
    assert not mod.script_dep_block(
        pdir, b'cat "<(bash scripts/x.sh)"\n')
    # `$(` inside double quotes DOES expand — still a dep
    assert mod.script_dep_block(
        pdir, b'x="$(bash scripts/x.sh)"\n')
    # a plain input redirect is still a read, not execution
    assert not mod.script_dep_block(
        pdir, b"cat < other-file\n")


def test_script_dep_cat_piped_to_interpreter(tmp_path):
    """`cat scripts/x.sh | sh` executes the script's contents — the file
    must register as a dep, including through a wrapped consumer
    (`| env sh`) (Devin Review on cpdcheck #6)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | sh\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | env sh\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh |& bash\n")
    # bare `cat` only reads the file — no dep
    assert not mod.script_dep_block(pdir, b"cat scripts/x.sh\n")
    # piping to a non-interpreter reads, doesn't run
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | grep foo\n")


def test_script_dep_dash_ksh_and_variable_interpreters(tmp_path):
    """`dash`/`ksh`/`ash scripts/x.sh` run like bash (Devin Review on
    #1953); `$PYTHON`/`${PYTHON} scripts/x.py` invoke through a variable
    the resolver can't see (Codex on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for head in (b"dash", b"ksh", b"ash"):
        assert mod.script_dep_block(
            pdir, head + b" scripts/x.sh\n"), head
    assert mod.script_dep_block(pdir, b"$PYTHON scripts/x.py\n")
    assert mod.script_dep_block(pdir, b"${PYTHON} scripts/x.py\n")
    assert mod.script_dep_block(pdir, b"${NODE} scripts/x.js\n")
    # double-quoted var head still expands and runs (Devin on #1953)
    assert mod.script_dep_block(pdir, b'"$PYTHON" scripts/x.py\n')
    # options bind operands — `$PYTHON -X dev scripts/x.py` runs x.py
    # (Devin on cpdcheck #6); a bare non-dash operand is the script
    assert mod.script_dep_block(
        pdir, b"$PYTHON -X dev scripts/x.py\n")
    assert mod.script_dep_block(
        pdir, b"$PYTHON -u -X dev scripts/x.py\n")
    # loose prose must not become an invocation
    assert not mod.script_dep_block(
        pdir, b"set $FOO to the scripts/x.py path\n")
    assert not mod.script_dep_block(
        pdir, b"$FOO and scripts/x.py are mentioned\n")


def test_script_dep_runner_forms(tmp_path):
    """Runner heads (`poetry run`, `pdm run`, `hatch run`) execute the
    script argument exactly like `uv run`/`pipenv run` (Codex on #1953).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for head in (b"uv run", b"pipenv run", b"poetry run", b"pdm run",
                 b"hatch run"):
        assert mod.script_dep_block(
            pdir, head + b" scripts/x.py\n"), head


def test_mini_yaml_block_scalar_inline_comment():
    """`ref: |- # comment` is legal YAML — the comment trails the block
    indicator, and deeper lines are still the scalar's content (Codex on
    vendored-resolver review)."""
    mod = load_resolve_module()
    y = ("entry:\n"
         "  ref: |-  # pinned\n"
         "    scripts/x.sh\n")
    assert mod._mini_yaml(y) == {"entry": {"ref": "scripts/x.sh"}}
    # folding variant + a `#`-carrying content line
    y = ("entry:\n"
         "  ref: >-  # pinned\n"
         "    scripts/x.sh\n")
    assert mod._mini_yaml(y) == {"entry": {"ref": "scripts/x.sh"}}


def test_mini_yaml_plain_key_edge_chars():
    """`rollout#phase`, `-x`, and `?x` are legal plain mapping keys —
    `#` mid-token is content (a comment needs a preceding space), and a
    leading `-`/`?` is an indicator only before whitespace (Codex on
    #123)."""
    mod = load_resolve_module()
    assert mod._mini_yaml("feature_flags:\n  rollout#phase: true\n") == {
        "feature_flags": {"rollout#phase": True}}
    assert mod._mini_yaml("-x: v\n") == {"-x": "v"}
    assert mod._mini_yaml("?x: v\n") == {"?x": "v"}
    # a real comment still truncates at ` #`
    assert mod._mini_yaml("k: v # note\n") == {"k": "v"}


def test_prune_cleanup_revalidates_ancestor_chain(tmp_path):
    """`.claude` swapped for a symlink after the lock commit must stop
    the empty-dir sweep — `islink` on the leaf alone misses the
    ancestor-level swap and rmdir would act outside repo_root (Codex on
    #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    external = tmp_path / "external"
    (external / "commands").mkdir(parents=True)
    m.write_text("version: 1\nuniverse: manolii\nrequires: []\n")
    claude = consumer / ".claude"
    real = consumer / ".claude-real"
    claude.rename(real)
    claude.symlink_to(external)
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    # the external commands dir must survive the cleanup sweep
    assert (external / "commands").is_dir()


def test_script_dep_stream_replacing_pipe_heads(tmp_path):
    """A pipe head whose output REPLACES the stream ends the chain:
    `cat x | wc -l | sh` feeds sh a line count, not the script (Devin on
    #123); `python -m` takes its program from argv so the pipe is data
    (Devin BUG_0003 on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for pipe in (b"cat scripts/x.sh | wc -l | sh",
                 b"cat scripts/x.sh | sha256sum | bash",
                 b"cat scripts/x.sh | echo done | sh",
                 b"cat scripts/x.py | python -m json.tool",
                 b"cat scripts/x.py | python -m json.tool | sh"):
        assert not mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # transformers forward content — still execute downstream
    for pipe in (b"cat scripts/x.sh | grep p | sh",
                 b"cat scripts/x.sh | sed s/a/b/ | sh",
                 b"cat scripts/x.sh | tr a b | bash"):
        assert mod.script_dep_block(pdir, pipe + b"\n"), pipe


def test_script_dep_env_split_string(tmp_path):
    """`env -S`/`--split-string` re-parses its operand into a command —
    `cat x | env -S 'bash -s'` executes the pipe (Codex on #1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for pipe in (b"cat scripts/x.sh | env -S 'bash -s'",
                 b"cat scripts/x.sh | env --split-string 'sh'",
                 b"cat scripts/x.sh | env -S'sh'",
                 b"cat scripts/x.sh | env --split-string='sh -s'",
                 b"cat scripts/x.sh | env A=1 -S 'bash'"):
        assert mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # an operand whose inner command is a sink still ends the chain
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | env -S 'wc -l' | sh\n")
    # env without -S runs the named command on the pipe, unchanged
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | env sh\n")


def test_script_dep_reader_heads_and_generic_suffix(tmp_path):
    """grep/head/tail only read, so a bare use stays inert — but under an
    executing context (`eval "$(grep p x)"`, `head x | sh`) the reader
    supplies code (Codex on #1370). Any dotted extension counts as a
    script — `bash scripts/setup.bash` is an invocation (Codex on #123).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "setup.bash").write_bytes(b"x")
    for line in (b'eval "$(grep p scripts/x.sh)"',
                 b'eval "$(head -5 scripts/x.sh)"',
                 b"grep p scripts/x.sh | sh",
                 b"head -5 scripts/x.sh | bash",
                 b"tail scripts/x.sh | python",
                 b"bash scripts/setup.bash",
                 b"./scripts/setup.bash"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # bare reader invocations still read — no dep
    for line in (b"grep p scripts/x.sh",
                 b"head -5 scripts/x.sh",
                 b"tail scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line


def test_script_dep_substitution_output_piped(tmp_path):
    """`echo "$(cat scripts/x.sh)" | sh` executes the substitution's
    output — the file inside is a dep even though echo is a sink (Devin
    on cpdcheck #6)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.sh)" | sh',
                 b'echo `cat scripts/x.sh` | bash',
                 b'printf "%s" "$(cat scripts/x.sh)" | python'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # no pipe — the output is just printed
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)"\n')
    # pipe to a non-executor still inert
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" | wc -l\n')


def test_script_dep_single_quoted_backticks_literal(tmp_path):
    """Backticks inside single quotes are literal text — `'run `x`'` is
    an argument, not a substitution (Devin BUG_0002 on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b"echo 'run `sh scripts/x.sh` today'",
                 b"printf 'use `bash scripts/x.sh` here'",
                 b"echo 'esc \\`sh scripts/x.sh\\`'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # inside double quotes a backtick still substitutes
    assert mod.script_dep_block(
        pdir, b'echo "run `sh scripts/x.sh` today"\n')
    # a bare backtick substitution still counts
    assert mod.script_dep_block(pdir, b"eval `cat scripts/x.sh`\n")


def test_script_dep_heredoc_multiple_ops_one_line(tmp_path):
    """`cat <<A; sh <<B` queues two heredocs on one line — B's body must
    start after A's delimiter, not after the opening line, or A's body
    is wrongly attributed to B's command (Devin on #1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    # an invocation in A's body must NOT be attributed to sh's heredoc —
    # cat only reads it, so nothing executes
    body = b"cat <<A; sh <<B\nbash scripts/x.sh\nA\ntext\nB\n"
    assert not mod.script_dep_block(pdir, body)
    # the same invocation in B's own body IS attributed to the exec head
    body = b"cat <<A; sh <<B\ntext\nA\nbash scripts/x.sh\nB\n"
    assert mod.script_dep_block(pdir, body)


def test_read_source_refuses_links_and_nonregular(tmp_path):
    """`_read_source` opens with O_NOFOLLOW and revalidates a regular
    file — a source swapped for a symlink after the resolve-time check
    cannot ship the link target's bytes, and a fifo cannot block the
    read (Codex P1 on #1953)."""
    import os
    mod = load_resolve_module()
    target = tmp_path / "target"
    target.write_bytes(b"real")
    assert mod._read_source(target) == b"real"
    link = tmp_path / "link"
    link.symlink_to(target)
    assert mod._read_source(link) is None
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert mod._read_source(fifo) is None
    assert mod._read_source(tmp_path / "missing") is None


def test_apply_writes_mutex_lockfile(tmp_path):
    """--apply holds an exclusive flock on .ai/capability-apply.lock for
    the plan→apply window so two resolvers can't lose each other's
    installs (Devin BUG_0001 on #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    assert (consumer / ".ai" / "capability-apply.lock").exists()
