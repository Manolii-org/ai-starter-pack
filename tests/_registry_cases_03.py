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

def seed_catalog(reg: Path, patterns: list[str] | None = None) -> None:
    p = reg / "platform" / "framework" / "data"
    p.mkdir(parents=True, exist_ok=True)
    (p / "token-shapes.json").write_text(json.dumps({
        "patterns": [{"name": "t", "regex": rx} for rx in (patterns or [])]}))

def load_lint_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("registry_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass field resolution needs the module registered
    spec.loader.exec_module(mod)
    return mod

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
                MIRROR_TRUST_DIRS=os.pathsep.join(filter(None,
                    (str(bin_dir), os.environ.get("MIRROR_TRUST_DIRS", "")))))

def _load_bootstrap():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bootstrap_mirror", "scripts/bootstrap-mirror.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def load_resolve_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ai_resolve", RESOLVE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ai_resolve"] = mod  # dataclass annotation lookup needs this
    spec.loader.exec_module(mod)
    return mod


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
