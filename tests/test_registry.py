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
    (consumer / ".claude" / "skills" / "demo").mkdir(parents=True)
    skill_dst = consumer / ".claude" / "skills" / "demo" / "x.md"
    skill_dst.write_text("v1")                       # identical to registry
    external = consumer / "external.md"
    os.link(skill_dst, external)                      # shared inode
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout                 # adopted into lock
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


def test_public_boundary_private_mirror_marker_waives(tmp_path, monkeypatch):
    """registry/.private-mirror marks a private mirror — the boundary is
    waived there, but ONLY when (a) the origin remote verifies this isn't
    the canonical public repo, (b) the slug is declared, and (c)
    MIRROR_VISIBILITY is asserted from outside the checkout (a public
    fork can carry both marker and digest)."""
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
    # Without the external assertion the waiver must still refuse.
    mod.results = []
    monkeypatch.delenv("MIRROR_VISIBILITY", raising=False)
    mod.check_public_boundary()
    assert any("MIRROR_VISIBILITY" in f.detail
               for f in mod.results if f.status == "FAIL")
    mod.results = []
    monkeypatch.setenv("MIRROR_VISIBILITY", "private")
    mod.check_public_boundary()
    assert not [f for f in mod.results if f.status == "FAIL"]
    # `internal` is NOT an acceptable assertion — on GitHub Enterprise it
    # grants every enterprise member (incl. other orgs) read access.
    mod.results = []
    monkeypatch.setenv("MIRROR_VISIBILITY", "internal")
    mod.check_public_boundary()
    assert any("MIRROR_VISIBILITY" in f.detail
               for f in mod.results if f.status == "FAIL")


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


def test_pack_surface_mirror_mode_exempts_own_org_only(tmp_path,
                                                      monkeypatch):
    """In a verified private mirror the OWNING org's slug is legitimate
    (a buro mirror names Buro-Built/* on purpose), while other orgs'
    slugs and infra ids still FAIL — the cross-org boundary holds.
    Mirror mode also requires the external MIRROR_VISIBILITY assertion
    (committed files alone cannot prove the repo is private)."""
    import subprocess as sp
    monkeypatch.setenv("MIRROR_VISIBILITY", "private")
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
    (the visibility check fails closed when gh can't answer), and PATH
    including it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho private\n")
    gh.chmod(0o755)
    # Isolate the harness machine's GLOBAL git config — e.g. a Devin box
    # rewrites every github.com url through its auth proxy via
    # url.insteadOf, which would silently steer push-target checks.
    empty_cfg = tmp_path / "gitconfig.empty"
    empty_cfg.write_text("")
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                GIT_CONFIG_GLOBAL=str(empty_cfg))


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
    # and must carry the API-side privacy assertion that feeds
    # MIRROR_VISIBILITY into the lint env.
    wf = (root / ".github/workflows/registry-lint.yml").read_text()
    assert "registry-lint.py" in wf
    assert "Assert mirror privacy" in wf
    assert "MIRROR_VISIBILITY" in wf
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
        def __init__(self, out, rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, ""

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
    # git invokes 'ssh -G <user>@<host>' for an scp-style URL.
    assert calls == [["/usr/bin/ssh", "-G", "git@github.com"]]

    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "github.com:Buro-Built/buro-registry.git") is None
    assert calls[-1] == ["/usr/bin/ssh", "-G", "github.com"]

    # The -G target carries the URL's port and percent-DECODED user so
    # `Match user`/`Match port` evaluate like the real push.
    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "ssh://redir%65ct@github.com:2222/Buro-Built/buro-registry.git"
    ) is None
    assert calls[-1] == ["/usr/bin/ssh", "-G", "-p", "2222",
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
    doc["files"][".claude/skills/demo/x.md"] = "0" * 64
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
