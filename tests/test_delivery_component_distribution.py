"""Public component bytes and synthetic opaque mirrors; no live provider proof."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEGACY_SHA256 = "aa0afc19ff2581a70e7aceb99ee0e5ca16f4dfd6dccbea2bbcd320308fdea6c4"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


C = load(ROOT / "scripts/lib/delivery_component.py", "distribution_component")
B = load(ROOT / "scripts/bootstrap-mirror.py", "distribution_bootstrap")


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          timeout=30, check=check, env={**os.environ,
                          "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "synthetic public inputs")


def component_roots():
    return (ROOT, ROOT / "plugin/manolii-framework", ROOT / "registry/platform/framework")


@pytest.mark.parametrize("root", component_roots(), ids=("source", "plugin", "registry"))
def test_generated_component_has_exact_canonical_bytes_modes_and_pinned_digest(root):
    expected = C.capture(ROOT)
    assert C.verify(root, expected_digest=expected["input_digest"])["input_files"] == 8
    for name in C.FILES:
        assert C.read_file(root, name) == C.read_file(ROOT, name)
    assert C.read_file(root, C.INPUTS) == C.read_file(ROOT, C.INPUTS)
    assert C.read_file(root, C.VERIFIER) == C.read_file(ROOT, C.VERIFIER)
    assert json.loads((root / C.INPUTS).read_text()) == expected


@pytest.mark.parametrize("root", component_roots(), ids=("source", "plugin", "registry"))
def test_actual_distributed_modules_import_and_preserve_v1_v2_and_audit_non_authority(root):
    fixtures = ROOT / "scripts/tests/fixtures"
    program = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import release_manifest as record
import release_state as state
import github_approval as audit
for version in (1,2):
 value=json.loads((Path(sys.argv[2])/('release-manifest.v'+str(version)+'.json')).read_text())
 assert record.validate(value)==value
assert audit.classify_history([{'state':'approved','release_id':'a'*64}])['execution_authorized'] is False
assert audit.classify_history([])['status']=='unsupported'
assert hasattr(record,'validate_native_binding') and hasattr(state.GitHubStateStore,'compare_and_swap')
"""
    result = subprocess.run([sys.executable, "-B", "-c", program, str(root / C.ACTION), str(fixtures)],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_published_c435_reader_still_accepts_unchanged_v1_v2(tmp_path):
    raw = (ROOT / "scripts/tests/fixtures/release-manifest.c435.py").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == LEGACY_SHA256
    action = tmp_path / C.ACTION
    action.mkdir(parents=True)
    (tmp_path / "schemas").mkdir()
    for version in (1, 2):
        name = f"release-manifest.v{version}.schema.json"
        (tmp_path / "schemas" / name).write_bytes((ROOT / "schemas" / name).read_bytes())
    legacy = ModuleType("published_c435")
    legacy.__file__ = str(action / "reader.py")
    exec(compile(raw, legacy.__file__, "exec"), legacy.__dict__)
    for version in (1, 2):
        value = json.loads((ROOT / f"scripts/tests/fixtures/release-manifest.v{version}.json").read_text())
        assert legacy.validate(value) == value


@pytest.mark.parametrize("change", ["bytes", "mode", "missing", "record", "linked"])
def test_changed_or_missing_payload_and_stale_input_record_never_verify(tmp_path, change):
    root = tmp_path / "component"
    root.mkdir()
    for name in (*C.FILES, C.INPUTS):
        dst = root / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dst)
    expected = C.capture(ROOT)["input_digest"]
    path = root / C.FILES[0]
    if change == "bytes":
        path.write_bytes(path.read_bytes() + b"\nchanged\n")
    elif change == "mode":
        path.chmod(0o755)
    elif change == "missing":
        path.unlink()
    elif change == "record":
        (root / C.INPUTS).write_text('{"schema":"ambiguous"}')
    else:
        original = root / "other"
        path.rename(original)
        path.symlink_to(original)
    with pytest.raises((C.ComponentError, OSError)):
        C.verify(root, expected_digest=expected)


def test_independent_expected_digest_cannot_be_replaced_by_candidate_metadata():
    with pytest.raises(C.ComponentError):
        C.verify(ROOT, expected_digest="0" * 64)


def public_pack(tmp_path):
    root = tmp_path / "public-pack"
    root.mkdir()
    for name in ("scripts/bootstrap-mirror.py", C.VERIFIER, "scripts/ai-resolve.py"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    for name in (*C.FILES, C.INPUTS, C.VERIFIER):
        target = root / "registry/platform/framework" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "registry/platform/framework" / name, target)
    opaque = root / "registry/private-fixture/opaque.bin"
    opaque.parent.mkdir()
    opaque.write_bytes(b"\x00OPAQUE SYNTHETIC SOURCE UNIVERSE\xff")
    git(root, "init", "-q")
    commit(root)
    opaque.chmod(0)
    return root


@pytest.mark.parametrize("changed", ["working", "staged", "linked", "head"])
def test_real_git_source_binding_rejects_changed_public_inputs(tmp_path, changed):
    root = public_pack(tmp_path)
    first = C.pack_source(root, component_only=True)
    name = "registry/platform/framework/" + C.FILES[0]
    path = root / name
    if changed == "head":
        (root / "new-public-note").write_text("new revision")
        git(root, "add", "new-public-note")
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "later public work")
        assert C.pack_source(root, component_only=True)["pack_sha"] != first["pack_sha"]
        return
    raw = path.read_bytes()
    if changed == "linked":
        path.unlink()
        path.symlink_to(root / "registry/private-fixture/opaque.bin")
    else:
        path.write_bytes(raw + b"\nchanged\n")
        if changed == "staged":
            git(root, "add", name)
            path.write_bytes(raw)
    with pytest.raises((C.ComponentError, OSError)):
        C.pack_source(root, component_only=True)


def mirror(tmp_path, universe):
    root = tmp_path / "mirror"
    root.mkdir()
    git(root, "init", "-q")
    slug = B.UNIVERSE_OWNERS[universe] + "/fixture-registry"
    git(root, "remote", "add", "origin", "https://github.com/" + slug + ".git")
    (root / "registry").mkdir()
    (root / "registry/.private-mirror").write_bytes(b"")
    (root / "registry/private-mirrors.txt").write_text(hashlib.sha256(slug.encode()).hexdigest() + "\n")
    index = root / "registry/plugins.json"
    index.write_bytes(b'{ "plugins": [], "opaque-owner-metadata": {"retain": [1,2,3]} }\n')
    scope = root / "registry" / universe
    scope.mkdir()
    (scope / "scope.yaml").write_bytes(b"opaque synthetic owner scope; do not parse\x00")
    opaque = scope / "opaque.bin"
    opaque.write_bytes(b"\x00OPAQUE SYNTHETIC PRIVATE PLUGIN\xff")
    wf = root / ".github/workflows/owner.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("unchanged opaque workflow fixture\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "synthetic private mirror metadata")
    opaque.chmod(0)
    return root, slug


def run_component_main(monkeypatch, public, root, slug, universe, component=C):
    source = component.pack_source(public, component_only=True)
    monkeypatch.setattr(B, "PACK", public)
    monkeypatch.setattr(B, "check_visibility", lambda _: "private")  # synthetic only; no native privacy proof
    monkeypatch.setattr(B, "_component_source", lambda _: (component, source, shutil.which("git")))
    for name in ("seed_scope", "merge_index", "regen_allowlists"):
        monkeypatch.setattr(B, name, lambda *a, **k: pytest.fail("opaque owner surface accessed"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--root", str(root), "--slug", slug,
                                     "--universe", universe, "--refresh-delivery-component"])
    return B.main(), source


@pytest.mark.parametrize("universe", B.UNIVERSES)
def test_component_only_refresh_preserves_all_opaque_universe_index_and_workflows(tmp_path, monkeypatch, universe):
    public = public_pack(tmp_path)
    root, slug = mirror(tmp_path, universe)
    opaque = root / "registry" / universe / "opaque.bin"
    before = opaque.stat()
    index = (root / "registry/plugins.json").read_bytes()
    scope = (root / "registry" / universe / "scope.yaml").read_bytes()
    workflow = (root / ".github/workflows/owner.yml").read_bytes()
    original = C.read_file
    allowed = {"registry/.private-mirror", "registry/private-mirrors.txt", C.MIRROR_SOURCE}
    def selected_only(selected_root, name, **private_options):
        selected_root = Path(selected_root)
        if selected_root == root:
            assert name in allowed, "opaque owner payload must not be read"
        elif selected_root == root / "registry/platform/framework":
            assert name in (*C.FILES, C.INPUTS, C.VERIFIER)
        return original(selected_root, name, **private_options)
    monkeypatch.setattr(C, "read_file", selected_only)
    code, source = run_component_main(monkeypatch, public, root, slug, universe)
    assert code == 0
    assert C.verify(root / "registry/platform/framework", expected_digest=source["component_input_digest"])["production_authorized"] is False
    assert (root / "registry/plugins.json").read_bytes() == index
    assert (root / "registry" / universe / "scope.yaml").read_bytes() == scope
    assert (root / ".github/workflows/owner.yml").read_bytes() == workflow
    after = opaque.stat()
    assert (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    pin = json.loads((root / C.MIRROR_SOURCE).read_text())
    assert pin == source and pin["selection"] == "delivery-component"
    assert pin["admission_authorized"] is False


def test_component_refresh_does_not_overwrite_unknown_existing_component_path(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    path = root / "registry/platform/framework" / C.FILES[0]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unknown owner bytes")
    code, _ = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 2
    assert path.read_bytes() == b"unknown owner bytes"
    assert not (root / C.MIRROR_SOURCE).exists()


def test_interrupted_component_refresh_restarts_exact_inputs_without_rollback(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    original = C.write_public_file
    count = 0
    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("synthetic interrupted selected component write")
        return original(*args, **kwargs)
    monkeypatch.setattr(C, "write_public_file", interrupted)
    code, source = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 2 and not (root / C.MIRROR_SOURCE).exists()
    assert (root / "registry/platform/framework" / C.FILES[0]).exists()
    monkeypatch.setattr(C, "write_public_file", original)
    code, _ = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 0
    C.verify(root / "registry/platform/framework", expected_digest=source["component_input_digest"])


def test_old_platform_replacement_leaves_provenance_but_cannot_reuse_component_pass(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    code, source = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 0
    pin = (root / C.MIRROR_SOURCE).read_bytes()
    shutil.rmtree(root / "registry/platform")
    (root / "registry/platform/framework/schemas").mkdir(parents=True)
    for name in ("release-manifest.v1.schema.json", "release-manifest.v2.schema.json", "release-adoption.v1.schema.json"):
        shutil.copy2(ROOT / "schemas" / name, root / "registry/platform/framework/schemas" / name)
    assert (root / C.MIRROR_SOURCE).read_bytes() == pin
    with pytest.raises((C.ComponentError, OSError)):
        C.verify(root / "registry/platform/framework", expected_digest=source["component_input_digest"])


def test_public_writer_refuses_any_universe_or_unrelated_path_before_read(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "snapshot", lambda *a: pytest.fail("unowned path accessed"))
    with pytest.raises(C.ComponentError, match="unowned_component_output"):
        C.write_public_file(tmp_path, "registry/private-fixture/opaque.bin", b"bad", "100644", None)


@pytest.mark.parametrize("root", component_roots(), ids=("source", "plugin", "registry"))
def test_distributed_state_runs_existing_real_git_lease_restart_contention_and_failure_suite(root):
    # Preload the distributed modules before loading the unchanged canonical
    # integration controls. All writes are real Git against temporary bare repos;
    # REST remains a local GET fixture, never provider/native enforcement proof.
    program = """import importlib.util,sys,unittest
from pathlib import Path
sys.path.insert(0,sys.argv[1]); import release_manifest,release_state
assert Path(release_state.__file__).parent==Path(sys.argv[1])
sys.path.insert(0,sys.argv[2])
spec=importlib.util.spec_from_file_location('distributed_git_contract',Path(sys.argv[2])/'test_release_admission_git.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
assert module.state is release_state
suite=unittest.defaultTestLoader.loadTestsFromModule(module)
assert suite.countTestCases()==5
result=unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(not result.wasSuccessful())
"""
    result = subprocess.run([sys.executable, "-B", "-c", program, str(root / C.ACTION),
                             str(ROOT / "scripts/tests")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_copier_consumer_keeps_byte_bound_component_and_portable_recipe(tmp_path):
    target = tmp_path / "rendered"
    result = subprocess.run([sys.executable, "-B", "-m", "copier", "copy", "--defaults", "--quiet",
                             "--vcs-ref=HEAD", str(ROOT), str(target)], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = C.capture(ROOT)
    assert C.verify(target, expected_digest=expected["input_digest"])["input_files"] == 8
    for name in (*C.FILES, C.INPUTS, C.VERIFIER):
        assert C.read_file(target, name) == C.read_file(ROOT, name)
    assert not (target / "tests/test_delivery_component_distribution.py").exists()
    checks = json.loads((target / "config/agent-checks.json").read_text())
    assert len(checks["checks"]) == 2
    test_actual_distributed_modules_import_and_preserve_v1_v2_and_audit_non_authority(target)


def test_selected_writer_creates_missing_root_but_never_follows_root_or_parent_links(tmp_path):
    root = tmp_path / "missing/root"
    C.write_public_file(root, C.INPUTS, b"{}", "100644", None)
    assert (root / C.INPUTS).read_bytes() == b"{}"
    other = tmp_path / "other"
    other.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(other, target_is_directory=True)
    with pytest.raises(OSError):
        C.write_public_file(linked, C.INPUTS, b"changed", "100644", None)
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / ".github").symlink_to(other, target_is_directory=True)
    with pytest.raises(OSError):
        C.write_public_file(parent, C.INPUTS, b"changed", "100644", None)
    assert not list(other.iterdir())


def public_full_pack(tmp_path):
    root = public_pack(tmp_path)
    # Only public canonical inputs, plus the unreadable synthetic owner canary
    # outside their selection. Synthetic fixture commits grant no real trust.
    for name in ("registry/plugins.json", "scripts/registry-lint.py",
                 "schemas/registry-scope.schema.json", "registry/secrets-allowlist.txt"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
        git(root, "add", name)
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "synthetic complete public source")
    return root


def run_full_main(monkeypatch, public, root, slug, universe, *, refresh=True, regen=None):
    source = C.pack_source(public)
    monkeypatch.setattr(B, "PACK", public)
    monkeypatch.setattr(B, "check_visibility", lambda _: "private")
    monkeypatch.setattr(B, "_component_source", lambda _: (C, source, shutil.which("git")))
    # Full-refresh tests focus on public output/provenance and mixed index
    # preservation. Existing registry tests run the actual lint/ratchet gate.
    monkeypatch.setattr(B, "regen_allowlists", regen if regen is not None else lambda *args: True)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--root", str(root), "--slug", slug,
                                     "--universe", universe] + (["--refresh-platform"] if refresh else []))
    return B.main(), source


def test_new_full_refresh_retains_component_and_all_opaque_mixed_index_entries(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    assert run_component_main(monkeypatch, public, root, slug, "buro")[0] == 0
    monkeypatch.undo()
    opaque = [None, "opaque synthetic entry", {"scope": "buro", "unknown": {"retain": [1, 2]}}]
    index = {"schema_version": 1, "plugins": opaque,
             "owner-extra": {"bytes": "opaque"}, "scopes": {"buro": {"owner-policy": [3]}}}
    (root / "registry/plugins.json").write_text(json.dumps(index))
    before = (root / "registry/buro/opaque.bin").stat()
    code, source = run_full_main(monkeypatch, public, root, slug, "buro")
    assert code == 0
    assert C.verify_copied_platform(root, source)["input_files"] == 8
    pinned = json.loads((root / C.MIRROR_SOURCE).read_text())
    assert pinned == source and source["selection"] == "platform"
    after = json.loads((root / "registry/plugins.json").read_text())
    assert after["owner-extra"] == index["owner-extra"]
    assert after["scopes"]["buro"] == index["scopes"]["buro"]
    assert after["plugins"][-len(opaque):] == opaque
    assert (root / "registry/buro/opaque.bin").stat() == before
    workflow = (root / ".github/workflows/registry-lint.yml").read_text()
    assert "name: registry-lint" in workflow and "  registry-gate:" in workflow
    assert "__DELIVERY_COMPONENT_INPUT_DIGEST__" not in workflow
    assert "--expected-input-digest " + source["component_input_digest"] in workflow
    result = subprocess.run([sys.executable, "-B", str(root / "registry/platform/framework" / C.VERIFIER),
                             "--root", str(root / "registry/platform/framework"),
                             "--expected-input-digest", source["component_input_digest"]],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("invalid", ['{"plugins":', '{"plugins": [], "plugins": []}',
                                    '{"plugins": {}, "scopes": {}}'])
def test_full_refresh_bad_index_holds_before_platform_or_scaffold_change(tmp_path, monkeypatch, invalid):
    public = public_full_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    assert run_component_main(monkeypatch, public, root, slug, "buro")[0] == 0
    monkeypatch.undo()
    (root / "registry/plugins.json").write_text(invalid)
    old = C.read_file(root / "registry/platform/framework", C.INPUTS)
    old_pin = (root / C.MIRROR_SOURCE).read_bytes()
    code, _ = run_full_main(monkeypatch, public, root, slug, "buro")
    assert code == 2
    assert C.read_file(root / "registry/platform/framework", C.INPUTS) == old
    assert (root / C.MIRROR_SOURCE).read_bytes() == old_pin
    assert (root / "registry/plugins.json").read_text() == invalid
    assert not (root / "README.md").exists()


def test_full_copied_platform_verification_refuses_extra_public_file(tmp_path):
    public = public_full_pack(tmp_path)
    source = C.pack_source(public)
    target = tmp_path / "copy"
    shutil.copytree(public / "registry/platform", target / "registry/platform")
    assert C.verify_copied_platform(target, source)["input_files"] == 8
    (target / "registry/platform/unexpected-public-code.py").write_text("raise RuntimeError()")
    with pytest.raises(C.ComponentError, match="extra_or_missing"):
        C.verify_copied_platform(target, source)


def test_ignored_bytecode_never_becomes_immutable_public_source(tmp_path):
    public = public_full_pack(tmp_path)
    assert C.pack_source(public)["component_input_digest"]
    bytecode = public / "registry/platform/framework/scripts/__pycache__/run-judge.fixture.pyc"
    bytecode.parent.mkdir(parents=True, exist_ok=True)
    bytecode.write_bytes(b"synthetic untrusted bytecode")
    (public / ".git/info/exclude").write_text("__pycache__/\n")
    assert git(public, "check-ignore", "--", str(bytecode.relative_to(public))).returncode == 0
    with pytest.raises(C.ComponentError, match="untracked_or_missing_public_platform_input"):
        C.pack_source(public)


def test_source_downgrade_or_unknown_ancestry_holds_existing_component(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    assert run_component_main(monkeypatch, public, root, slug, "buro")[0] == 0
    before = (root / C.MIRROR_SOURCE).read_bytes()
    git(public, "checkout", "--detach", "-q", "HEAD^")
    code, _ = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 2
    assert (root / C.MIRROR_SOURCE).read_bytes() == before


@pytest.mark.parametrize("changed", ["tree", "object-format", "blob", "path", "digest", "mode", "duplicate"])
def test_invalid_or_ambiguous_installed_provenance_never_grants_replacement(tmp_path, monkeypatch, changed):
    public = public_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    assert run_component_main(monkeypatch, public, root, slug, "buro")[0] == 0
    pin = root / C.MIRROR_SOURCE
    value = json.loads(pin.read_text())
    if changed == "tree":
        value["pack_tree"] = "invalid"
    elif changed == "object-format":
        value["git_object_format"] = "sha256"
    elif changed in ("blob", "path"):
        value["inputs"][0]["git_blob" if changed == "blob" else "path"] = "invalid" if changed == "blob" else "../universe"
        value["input_digest"] = C.digest(value["inputs"])
    elif changed == "digest":
        value["component_input_digest"] = "invalid"
    if changed == "duplicate":
        pin.write_text('{"schema":"duplicate",' + json.dumps(value)[1:])
    else:
        pin.write_bytes(C.encoded(value))
    if changed == "mode":
        pin.chmod(0o755)
    before = pin.read_bytes()
    component_before = C.read_file(root / "registry/platform/framework", C.FILES[0])
    code, _ = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 2 and pin.read_bytes() == before
    assert C.read_file(root / "registry/platform/framework", C.FILES[0]) == component_before


def test_source_change_after_selected_writes_never_publishes_final_provenance(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    original = C.write_public_file
    changed = False
    def mutate_source(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            path = public / "scripts/bootstrap-mirror.py"
            path.write_bytes(path.read_bytes() + b"\n# synthetic source change\n")
            changed = True
        return result
    monkeypatch.setattr(C, "write_public_file", mutate_source)
    code, source = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 2 and changed
    assert not (root / C.MIRROR_SOURCE).exists()
    assert C.verify(root / "registry/platform/framework", expected_digest=source["component_input_digest"])["admission_authorized"] is False


def test_observable_destination_change_is_retained_without_overwrite_or_provenance(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    original = C.write_public_file
    foreign = b"retain synthetic concurrent public edit"
    changed = False
    target = root / "registry/platform/framework" / C.FILES[0]
    def mutate_destination(*args, **kwargs):
        nonlocal changed
        if not changed:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(foreign)
            changed = True
        return original(*args, **kwargs)
    monkeypatch.setattr(C, "write_public_file", mutate_destination)
    code, _ = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 2 and target.read_bytes() == foreign
    assert not (root / C.MIRROR_SOURCE).exists()


@pytest.mark.parametrize("root", component_roots(), ids=("source", "plugin", "registry"))
def test_distributed_verifier_cli_propagates_wrong_pin_as_failure(root):
    result = subprocess.run([sys.executable, "-B", str(root / C.VERIFIER), "--root", str(root),
                             "--expected-input-digest", "0" * 64],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and not result.stdout
    assert result.stderr.strip() == "delivery component unavailable or changed"


def test_existing_owner_recipe_and_required_contexts_keep_complete_distribution_scope():
    recipe = json.loads((ROOT / "config/agent-checks.json").read_text())
    release = next(row for row in recipe["checks"] if row["id"] == "release-manifest-contract")
    command = [".ai/agent-checks/release-venv/bin/python", "-B", "-m", "pytest", "-q",
               "tests/test_delivery_component_distribution.py"]
    assert command in release["commands"]
    import fnmatch
    for changed in ("scripts/lib/delivery_component.py", "scripts/bootstrap-mirror.py",
                    "scripts/build-plugin.py", "scripts/build-registry.py",
                    "tests/test_delivery_component_distribution.py",
                    "registry/platform/framework/" + C.INPUTS,
                    "plugin/manolii-framework/" + C.INPUTS,
                    "plugin/manolii-framework/" + C.VERIFIER,
                    "registry/platform/framework/" + C.VERIFIER):
        assert any(fnmatch.fnmatchcase(changed, pattern) for pattern in release["paths"]), changed
    registry = next(row for row in recipe["checks"] if row["id"] == "registry-bootstrap-contract")
    for changed in ("scripts/lib/delivery_component.py", "scripts/bootstrap-mirror.py",
                    "scripts/build-plugin.py", "scripts/build-registry.py"):
        assert any(fnmatch.fnmatchcase(changed, pattern) for pattern in registry["paths"]), changed


@pytest.mark.parametrize("changed", ["self_consistent_rows", "commit_tree", "git_blob"])
def test_installed_metadata_cannot_authorize_replacement_without_immutable_source_proof(tmp_path, monkeypatch, changed):
    public = public_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    assert run_component_main(monkeypatch, public, root, slug, "buro")[0] == 0
    target = root / "registry/platform/framework" / C.FILES[0]
    retained = target.read_bytes()
    pin = root / C.MIRROR_SOURCE
    record = json.loads(pin.read_text())
    name = "registry/platform/framework/" + C.FILES[0]
    if changed == "self_consistent_rows":
        retained += b"\n# synthetic unrelated local change\n"
        target.write_bytes(retained)
        for row in record["inputs"]:
            if row["path"] == name:
                row["sha256"] = C._row(name, retained, "100644")["sha256"]
        record["input_digest"] = C.digest(record["inputs"])
    elif changed == "commit_tree":
        record["pack_tree"] = "0" * 40
    else:
        for row in record["inputs"]:
            if row["path"] == name:
                row["git_blob"] = "0" * 40
        record["input_digest"] = C.digest(record["inputs"])
    pin.write_bytes(C.encoded(record))
    before = pin.read_bytes()
    code, _ = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 2
    assert target.read_bytes() == retained
    assert pin.read_bytes() == before


def test_previous_immutable_blob_size_is_bounded_before_content_read(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    source = C.pack_source(public, component_only=True)
    monkeypatch.setattr(B, "PACK", public)
    monkeypatch.setattr(C, "MAX_FILE", 1)
    original = C._git

    def metadata_only(*args, **kwargs):
        assert args[1:3] != ("cat-file", "blob"), "oversized previous blob contents read"
        return original(*args, **kwargs)

    monkeypatch.setattr(C, "_git", metadata_only)
    with pytest.raises(ValueError, match="bound"):
        B._require_source_advance(C, source, source, "/usr/bin/git")


def _walk_subject(tmp_path, operation):
    public = public_full_pack(tmp_path)
    source = C.pack_source(public)
    if operation == "pack_source":
        root = public
        invoke = lambda path: C.pack_source(path)
    else:
        root = tmp_path / "copied-public"
        shutil.copytree(public / "registry/platform", root / "registry/platform")
        invoke = lambda path: C.verify_copied_platform(path, source)
    return root, invoke


def _observe_scandir(monkeypatch, outside, *, before=None):
    original = os.scandir
    observed = []
    forbidden = {(p.stat().st_dev, p.stat().st_ino) for p in (outside, *outside.rglob("*")) if p.is_dir()}

    def checked(path):
        if before:
            before(path)
        info = os.fstat(path) if isinstance(path, int) else os.stat(path)
        if (info.st_dev, info.st_ino) in forbidden:
            observed.append((info.st_dev, info.st_ino))
        return original(path)

    monkeypatch.setattr(os, "scandir", checked)
    return observed


@pytest.mark.parametrize("operation", ["pack_source", "verify_copied_platform"])
@pytest.mark.parametrize("alias", ["owner_root", "registry", "platform"])
def test_public_tree_root_and_ancestor_aliases_refuse_before_outside_enumeration(tmp_path, monkeypatch, operation, alias):
    root, invoke = _walk_subject(tmp_path, operation)
    outside = tmp_path / "synthetic-private-metadata"
    (outside / "platform").mkdir(parents=True)
    (outside / "platform/never-enumerate").write_text("synthetic metadata only")
    selected = root
    if alias == "owner_root":
        outside = root
        selected = tmp_path / "owner-alias"
        selected.symlink_to(root, target_is_directory=True)
    else:
        path = root / ("registry" if alias == "registry" else "registry/platform")
        path.rename(path.with_name(path.name + "-retained-public"))
        path.symlink_to(outside, target_is_directory=True)
    observed = _observe_scandir(monkeypatch, outside)
    with pytest.raises((OSError, C.ComponentError)):
        invoke(selected)
    assert observed == [], "outside directory metadata was enumerated before refusal"


@pytest.mark.parametrize("operation", ["pack_source", "verify_copied_platform"])
def test_public_tree_parent_replacement_cannot_redirect_enumeration(tmp_path, monkeypatch, operation):
    root, invoke = _walk_subject(tmp_path, operation)
    outside = tmp_path / "synthetic-private-metadata"
    (outside / "platform").mkdir(parents=True)
    (outside / "platform/never-enumerate").write_text("synthetic metadata only")
    platform = root / "registry/platform"
    identity = (platform.stat().st_dev, platform.stat().st_ino)
    replaced = []

    def replace_parent(path):
        info = os.fstat(path) if isinstance(path, int) else os.stat(path)
        if not replaced and (info.st_dev, info.st_ino) == identity:
            (root / "registry").rename(root / "registry-retained-public")
            (root / "registry").symlink_to(outside, target_is_directory=True)
            replaced.append(True)

    observed = _observe_scandir(monkeypatch, outside, before=replace_parent)
    descriptors = set(os.listdir("/proc/self/fd"))
    with pytest.raises((OSError, C.ComponentError)):
        invoke(root)
    assert replaced == [True]
    assert observed == []
    assert set(os.listdir("/proc/self/fd")) == descriptors


@pytest.mark.parametrize("operation", ["pack_source", "verify_copied_platform"])
def test_public_tree_child_swap_refuses_before_opened_outside_enumeration(tmp_path, monkeypatch, operation):
    root, invoke = _walk_subject(tmp_path, operation)
    outside = tmp_path / "synthetic-private-metadata"
    outside.mkdir()
    (outside / "never-enumerate").write_text("synthetic metadata only")
    original = os.open
    replaced = []

    def open_after_swap(name, flags, *args, **kwargs):
        if name == "framework" and flags & os.O_DIRECTORY and not replaced:
            path = root / "registry/platform/framework"
            path.rename(path.with_name("framework-retained-public"))
            path.symlink_to(outside, target_is_directory=True)
            replaced.append(True)
        return original(name, flags, *args, **kwargs)

    observed = _observe_scandir(monkeypatch, outside)
    monkeypatch.setattr(os, "open", open_after_swap)
    descriptors = set(os.listdir("/proc/self/fd"))
    with pytest.raises((OSError, C.ComponentError)):
        invoke(root)
    assert replaced == [True]
    assert observed == []
    assert set(os.listdir("/proc/self/fd")) == descriptors


@pytest.mark.parametrize("operation", ["pack_source", "verify_copied_platform"])
def test_public_tree_real_directory_replacement_cannot_change_observed_child_identity(tmp_path, monkeypatch, operation):
    root, invoke = _walk_subject(tmp_path, operation)
    outside = tmp_path / "synthetic-private-metadata"
    outside.mkdir()
    (outside / "never-enumerate").write_text("synthetic metadata only")
    original = os.stat
    replaced = []

    def stat_before_replacement(name, *args, **kwargs):
        observed = original(name, *args, **kwargs)
        if name == "framework" and kwargs.get("dir_fd") is not None and not replaced:
            path = root / "registry/platform/framework"
            path.rename(path.with_name("framework-retained-public"))
            outside.rename(path)
            replaced.append(True)
        return observed

    observed = _observe_scandir(monkeypatch, outside)
    monkeypatch.setattr(os, "stat", stat_before_replacement)
    descriptors = set(os.listdir("/proc/self/fd"))
    with pytest.raises((OSError, C.ComponentError)):
        invoke(root)
    assert replaced == [True]
    assert observed == []
    assert set(os.listdir("/proc/self/fd")) == descriptors


@pytest.mark.parametrize("bound", ["MAX_TREE_ENTRIES", "MAX_TREE_DEPTH"])
def test_public_tree_enumeration_bounds_hold_without_descriptor_leaks(tmp_path, monkeypatch, bound):
    root, invoke = _walk_subject(tmp_path, "verify_copied_platform")
    monkeypatch.setattr(C, bound, 0)
    descriptors = set(os.listdir("/proc/self/fd"))
    with pytest.raises(C.ComponentError, match="bound"):
        invoke(root)
    assert set(os.listdir("/proc/self/fd")) == descriptors


def _disjoint_public_source(public, source):
    tree = git(public, "write-tree").stdout.strip()
    commit_object = subprocess.run(
        ["git", "-C", str(public), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit-tree", tree], input="synthetic disjoint public history\n", capture_output=True, text=True,
        timeout=30, check=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    previous = json.loads(json.dumps(source))
    previous.update(pack_sha=commit_object.stdout.strip(), pack_tree=tree)
    return previous


def test_graft_metadata_cannot_forge_previous_component_source_ancestry(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    source = C.pack_source(public, component_only=True)
    previous = _disjoint_public_source(public, source)
    root, slug = mirror(tmp_path, "buro")
    assert run_component_main(monkeypatch, public, root, slug, "buro")[0] == 0
    pin = root / C.MIRROR_SOURCE
    pin.write_bytes(C.encoded(previous))
    retained = pin.read_bytes()
    info = public / ".git/info"
    info.mkdir(exist_ok=True)
    (info / "grafts").write_text(source["pack_sha"] + " " + previous["pack_sha"] + "\n")
    assert C.pack_source(public, component_only=True) == source
    code, _ = run_component_main(monkeypatch, public, root, slug, "buro")
    assert code == 2
    assert pin.read_bytes() == retained


def test_git_source_proofs_fence_ambient_graph_overlays_and_never_enable_lazy_fetch(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    original = subprocess.check_output
    calls = []

    def checked(arguments, *args, **kwargs):
        environment = kwargs["env"]
        assert arguments[1:4] == ["--no-replace-objects", "-c", "core.commitGraph=false"]
        assert environment["GIT_GRAFT_FILE"] == os.devnull
        assert environment["GIT_SHALLOW_FILE"] == os.devnull
        assert environment["GIT_NO_LAZY_FETCH"] == "1"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_COUNT" not in environment
        calls.append(True)
        return original(arguments, *args, **kwargs)

    monkeypatch.setenv("GIT_GRAFT_FILE", str(tmp_path / "untrusted-graft"))
    monkeypatch.setenv("GIT_SHALLOW_FILE", str(tmp_path / "untrusted-shallow"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.commitGraph")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setattr(subprocess, "check_output", checked)
    assert C.pack_source(public, component_only=True)["admission_authorized"] is False
    assert calls


def test_raw_source_ancestry_walk_is_bounded_before_replacement(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    source = C.pack_source(public, component_only=True)
    previous = json.loads(json.dumps(source))
    previous.update(pack_sha=git(public, "rev-parse", "HEAD^").stdout.strip(),
                    pack_tree=git(public, "rev-parse", "HEAD^^{tree}").stdout.strip())
    monkeypatch.setattr(B, "PACK", public)
    monkeypatch.setattr(B, "_MAX_SOURCE_COMMITS", 0)
    with pytest.raises(ValueError, match="bound"):
        B._require_source_advance(C, previous, source, "/usr/bin/git")


def test_missing_raw_parent_object_holds_without_fetch_or_provenance_replacement(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    previous = C.pack_source(public, component_only=True)
    raw = ("tree " + previous["pack_tree"] + "\nparent " + "f" * 40
           + "\nauthor Fixture <fixture@example.invalid> 1 +0000"
           + "\ncommitter Fixture <fixture@example.invalid> 1 +0000\n\nsynthetic missing parent\n")
    result = subprocess.run(["git", "-C", str(public), "hash-object", "-t", "commit", "-w", "--stdin"],
                            input=raw, text=True, capture_output=True, timeout=30, check=True)
    git(public, "update-ref", "HEAD", result.stdout.strip())
    source = C.pack_source(public, component_only=True)
    monkeypatch.setattr(B, "PACK", public)
    original = C._git
    commands = []

    def no_fetch(*args, **kwargs):
        commands.append(args[1:])
        assert "fetch" not in args[1:]
        return original(*args, **kwargs)

    monkeypatch.setattr(C, "_git", no_fetch)
    with pytest.raises(ValueError, match="unproved"):
        B._require_source_advance(C, previous, source, "/usr/bin/git")
    assert commands


@pytest.mark.parametrize("payload", [b"opaque synthetic noncomponent ownership\x00", b'{"schema":"unknown-owner-record"}'])
def test_absent_platform_unknown_provenance_holds_before_any_full_scaffold_write(tmp_path, monkeypatch, payload):
    public = public_full_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    pin = root / C.MIRROR_SOURCE
    pin.write_bytes(payload)
    before = (root / "registry/plugins.json").read_bytes()
    marker = (root / "registry/.private-mirror").stat()
    code, _ = run_full_main(monkeypatch, public, root, slug, "buro")
    assert code == 2
    assert pin.read_bytes() == payload
    assert not (root / "registry/platform").exists()
    assert not (root / "README.md").exists()
    assert (root / "registry/plugins.json").read_bytes() == before
    assert (root / "registry/.private-mirror").stat() == marker


def test_absent_platform_valid_prior_provenance_still_requires_genuine_ancestry(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    source = C.pack_source(public)
    previous = _disjoint_public_source(public, source)
    root, slug = mirror(tmp_path, "buro")
    pin = root / C.MIRROR_SOURCE
    pin.write_bytes(C.encoded(previous))
    before = pin.read_bytes()
    code, _ = run_full_main(monkeypatch, public, root, slug, "buro")
    assert code == 2 and pin.read_bytes() == before
    assert not (root / "registry/platform").exists()
    assert not (root / "README.md").exists()


def test_absent_platform_valid_same_source_can_recover_exact_public_inputs(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    source = C.pack_source(public)
    root, slug = mirror(tmp_path, "buro")
    (root / C.MIRROR_SOURCE).write_bytes(C.encoded(source))
    code, actual = run_full_main(monkeypatch, public, root, slug, "buro")
    assert code == 0
    assert C.verify_copied_platform(root, actual)["production_authorized"] is False


def test_no_refresh_gate_binds_verified_retained_component_not_new_source_digest(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    assert run_component_main(monkeypatch, public, root, slug, "buro")[0] == 0
    monkeypatch.undo()
    code, first = run_full_main(monkeypatch, public, root, slug, "buro")
    assert code == 0
    monkeypatch.undo()
    action = public / "registry/platform/framework" / C.FILES[0]
    action.write_bytes(action.read_bytes() + b"\n# new public source\n")
    C.write_inputs(public / "registry/platform/framework")
    git(public, "add", "registry/platform/framework/" + C.FILES[0], "registry/platform/framework/" + C.INPUTS)
    git(public, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "synthetic later component")
    code, later = run_full_main(monkeypatch, public, root, slug, "buro", refresh=False)
    assert code == 0 and first["component_input_digest"] != later["component_input_digest"]
    workflow = (root / ".github/workflows/registry-lint.yml").read_text()
    assert "--expected-input-digest " + first["component_input_digest"] in workflow
    assert later["component_input_digest"] not in workflow
    assert json.loads((root / C.MIRROR_SOURCE).read_text()) == first
    assert C.verify(root / "registry/platform/framework", expected_digest=first["component_input_digest"])["input_files"] == 8


def test_no_refresh_unknown_legacy_component_holds_before_scaffold_replacement(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    legacy = root / "registry/platform/legacy-public.txt"
    legacy.parent.mkdir()
    legacy.write_bytes(b"synthetic retained legacy public component")
    code, _ = run_full_main(monkeypatch, public, root, slug, "buro", refresh=False)
    assert code == 2
    assert legacy.read_bytes() == b"synthetic retained legacy public component"
    assert not (root / "README.md").exists()


def test_full_refresh_preserves_unknown_provenance_introduced_after_preflight(tmp_path, monkeypatch):
    public = public_full_pack(tmp_path)
    root, slug = mirror(tmp_path, "buro")
    pin = root / C.MIRROR_SOURCE
    unknown = b"synthetic concurrent unrelated provenance; retain exactly"

    def concurrent_record(*args):
        pin.write_bytes(unknown)
        return True

    code, _ = run_full_main(monkeypatch, public, root, slug, "buro", regen=concurrent_record)
    assert code == 2
    assert pin.read_bytes() == unknown
    assert (root / "registry/platform").is_dir()  # approved partial preparation is retained


@pytest.mark.parametrize("component_only", [False, True])
@pytest.mark.parametrize("alias", ["root", "ancestor"])
def test_git_metadata_never_reads_through_raw_root_or_ancestor_alias(tmp_path, monkeypatch, component_only, alias):
    owned = tmp_path / "owned"
    owned.mkdir()
    public = public_pack(owned)
    if alias == "root":
        path = tmp_path / "root-alias"
        path.symlink_to(public, target_is_directory=True)
    else:
        parent = tmp_path / "parent-alias"
        parent.symlink_to(owned, target_is_directory=True)
        path = parent / public.name
    calls = []
    enumerations = []
    original_git = subprocess.check_output
    original_scandir = os.scandir

    def checked_git(*args, **kwargs):
        calls.append(args[0])
        return original_git(*args, **kwargs)

    def checked_scandir(*args, **kwargs):
        enumerations.append(args[0])
        return original_scandir(*args, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", checked_git)
    monkeypatch.setattr(os, "scandir", checked_scandir)
    with pytest.raises((C.ComponentError, OSError)):
        C.pack_source(path, component_only=component_only)
    assert calls == [], "Git observed metadata through a raw root/ancestor alias"
    assert enumerations == [], "outside directory entries were enumerated before refusal"


@pytest.mark.parametrize("component_only", [False, True])
@pytest.mark.parametrize("replaced", ["root", "ancestor"])
@pytest.mark.parametrize("when", ["before-child", "after-child"])
def test_git_cwd_is_retained_across_root_and_parent_replacement(tmp_path, monkeypatch, component_only, replaced, when):
    owned = tmp_path / "owned"
    owned.mkdir()
    public = public_pack(owned)
    expected_head = git(public, "rev-parse", "HEAD").stdout.strip().encode()
    expected_identity = C._directory_identity(os.stat(public))
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir()
    replacement = public_pack(replacement_parent)
    opaque = replacement / "opaque-index-only-metadata.txt"
    opaque.write_text("synthetic replacement index; never observed")
    git(replacement, "add", opaque.name)
    git(replacement, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "synthetic replacement-only index path")
    original = subprocess.check_output
    calls = []

    def replace():
        if replaced == "root":
            public.rename(owned / "retained-original")
        else:
            owned.rename(tmp_path / "retained-parent")
            owned.mkdir()
        replacement.rename(public)

    def checked(arguments, *args, **kwargs):
        calls.append(arguments)
        assert len(calls) == 1, "later Git metadata read after observable root drift"
        assert kwargs["close_fds"] is True
        if when == "before-child":
            replace()
        assert C._directory_identity(os.stat(kwargs["cwd"])) == expected_identity
        answer = original(arguments, *args, **kwargs)
        assert answer.strip() == expected_head, "Git read the replacement repository"
        if when == "after-child":
            replace()
        return answer

    monkeypatch.setattr(subprocess, "check_output", checked)
    with pytest.raises(C.ComponentError, match="binding_changed"):
        C.pack_source(public, component_only=component_only)
    assert len(calls) == 1
    assert opaque.name in git(public, "ls-files").stdout


@pytest.mark.parametrize("component_only", [False, True])
def test_readonly_git_disables_local_fsmonitor_and_hooks(tmp_path, component_only):
    public = public_pack(tmp_path)
    before = C.pack_source(public, component_only=component_only)
    marker = tmp_path / "fsmonitor-executed"
    hook = tmp_path / "marker-only-fsmonitor"
    hook.write_text("#!/bin/sh\nprintf invoked > '" + str(marker) + "'\nprintf 'token\\000'\n")
    hook.chmod(0o755)
    git(public, "config", "core.fsmonitor", str(hook))
    git(public, "config", "core.hooksPath", str(tmp_path / "untrusted-hooks"))
    assert C.pack_source(public, component_only=component_only) == before
    assert not marker.exists(), "read-only source collection executed repository-local configuration"


def test_retained_git_root_preserves_legitimate_linked_worktree_source_and_ancestry(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    expected = C.pack_source(public, component_only=True)
    linked = tmp_path / "linked-worktree"
    git(public, "worktree", "add", "--detach", "-q", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    actual = C.pack_source(linked, component_only=True)
    assert actual == expected
    assert C._git(linked, "cat-file", "-t", actual["pack_sha"]).strip() == b"commit"
    monkeypatch.setattr(B, "PACK", linked)
    assert B._prove_raw_source_ancestry(C, actual, actual, "/usr/bin/git") is None


@pytest.mark.parametrize("component_only", [False, True])
@pytest.mark.parametrize("replaced", ["root", "ancestor"])
def test_retained_source_root_is_rechecked_before_post_git_payload_or_tree_reads(tmp_path, monkeypatch, component_only, replaced):
    owned = tmp_path / "owned"
    owned.mkdir()
    public = public_pack(owned)
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir()
    replacement = public_pack(replacement_parent)
    observed = []
    replaced_now = False
    original = C.read_file if component_only else C._public_tree_files
    original_read, original_scandir = os.read, os.scandir

    def checked(*args, **kwargs):
        nonlocal replaced_now
        assert kwargs.get("_root_binding") is not None
        if replaced == "root":
            public.rename(owned / "retained-original")
        else:
            owned.rename(tmp_path / "retained-parent")
            owned.mkdir()
        replacement.rename(public)
        replaced_now = True
        return original(*args, **kwargs)

    def read(*args, **kwargs):
        if replaced_now:
            observed.append("read")
        return original_read(*args, **kwargs)

    def scandir(*args, **kwargs):
        if replaced_now:
            observed.append("scandir")
        return original_scandir(*args, **kwargs)

    monkeypatch.setattr(C, "read_file" if component_only else "_public_tree_files", checked)
    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(os, "scandir", scandir)
    with pytest.raises(C.ComponentError, match="binding_changed"):
        C.pack_source(public, component_only=component_only)
    assert observed == [], "replacement source inputs were observed after Git collection"


@pytest.mark.parametrize("alias", ["root", "ancestor"])
def test_standalone_git_metadata_also_refuses_alias_before_subprocess(tmp_path, monkeypatch, alias):
    owned = tmp_path / "owned"
    owned.mkdir()
    public = public_pack(owned)
    path = tmp_path / "alias"
    path.symlink_to(public if alias == "root" else owned, target_is_directory=True)
    if alias == "ancestor":
        path /= public.name
    calls = []
    original = subprocess.check_output

    def checked(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", checked)
    with pytest.raises((C.ComponentError, OSError)):
        C._git(path, "rev-parse", "HEAD")
    assert calls == []


def test_unavailable_retained_git_descriptor_path_holds_without_pathname_fallback(tmp_path, monkeypatch):
    public = public_pack(tmp_path)
    original = os.stat
    calls = []

    def checked(path, *args, **kwargs):
        if str(path).startswith(f"/proc/{os.getpid()}/fd/"):
            raise FileNotFoundError("synthetic descriptor-path unavailability")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", checked)
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: calls.append(args))
    with pytest.raises((C.ComponentError, OSError)):
        C.pack_source(public, component_only=True)
    assert calls == []
