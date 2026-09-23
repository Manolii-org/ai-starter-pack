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
