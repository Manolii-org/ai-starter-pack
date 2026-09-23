"""Unit tests for the capability registry machinery — pack-internal, excluded
from rendered consumers (resolver/lint semantics, scope fail-closed rule)."""
from __future__ import annotations

import hashlib
import json
import os
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
        capture_output=True, text=True)


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


def test_prune_removes_symlink_entry_not_target(tmp_path):
    """An orphan lock entry that is a symlink to a required file: prune must
    unlink the LINK, never its target."""
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
    lock_file.write_text(json.dumps(lock))
    r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    assert r.returncode == 0, r.stdout
    assert target.read_text() == "required content"  # target survived
    assert not link.exists() and not link.is_symlink()  # link removed


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
    """A skill that calls a sibling script it doesn't ship cannot run in
    resolver mode — advisory-skip it, never write it."""
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


def test_backticked_script_ref_skipped(tmp_path):
    """`scripts/x.py` in backticks is an invocation-shaped dependency —
    the file is advisory-skipped, not materialised."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("commands/run-status.md",
             "Step: run `scripts/sprint_status.py` to regenerate status.md"),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert not (consumer / ".claude" / "commands" / "run-status.md").exists()
    assert "scripts/" in r.stdout


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
