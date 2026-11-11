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


_CASE_FILES = ('_registry_cases_01.py', '_registry_cases_02.py', '_registry_cases_03.py', '_registry_cases_04.py', '_registry_cases_05.py', '_registry_cases_06.py', '_registry_cases_07.py', '_registry_cases_08.py', '_registry_cases_09.py')


# Keep this explicit entrypoint and every collected test name stable. Complete
# function bodies, imports and helper context live in bounded source modules so
# the existing per-rule/file scanner ceiling can qualify the whole suite.
def _load_registry_cases():
    from types import ModuleType

    for filename in _CASE_FILES:
        path = Path(__file__).with_name(filename)
        module = ModuleType(path.stem)
        module.__file__ = str(path)
        # Compile current trusted test source, independent of pytest import mode
        # and without a cached bytecode or search-path dependency.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        for name, value in vars(module).items():
            if name.startswith("test_") and callable(value):
                if name in globals():
                    raise RuntimeError(f"duplicate registry test: {name}")
                globals()[name] = value


_load_registry_cases()


def test_bootstrap_fixture_isolates_host_git_wrapper(tmp_path, monkeypatch):
    """Positive fixtures use system Git; an untrusted PATH shadow stays denied."""
    # The stable entrypoint re-exports functions with their original globals.
    fixture = test_bootstrap_mirror_seed.__globals__["_bootstrap_env"]
    load_bootstrap = test_bootstrap_mirror_seed.__globals__["_load_bootstrap"]
    wrapper = tmp_path / "host-wrapper"
    wrapper.mkdir()
    marker = tmp_path / "wrapper-executed"
    shadow = wrapper / "git"
    shadow.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nexit 1\n")
    shadow.chmod(0o755)
    monkeypatch.setenv("PATH", str(wrapper) + os.pathsep + os.environ["PATH"])
    env = fixture(tmp_path)
    system_git = shutil.which("git", path=os.defpath)
    assert system_git, "the positive fixture requires an installed system Git"
    assert shutil.which("git", path=env["PATH"]) == system_git
    bootstrap = load_bootstrap()
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("MIRROR_TRUST_DIRS", env["MIRROR_TRUST_DIRS"])
    assert bootstrap._trusted_prog("git") == os.path.realpath(system_git)
    monkeypatch.setenv("PATH", str(wrapper) + os.pathsep + env["PATH"])
    assert bootstrap._trusted_prog("git") == ""
    assert not marker.exists()


@pytest.mark.parametrize("case", [
    "test_lock_records_full_installed_mode",
    "test_legacy_exec_record_mode_drift_conflicts",
])
def test_mode_fixtures_preserve_assertions_under_restrictive_umask(tmp_path, case):
    """The original mode and drift assertions hold even when file creation is 0600."""
    previous = os.umask(0o077)
    try:
        globals()[case](tmp_path)
    finally:
        os.umask(previous)
