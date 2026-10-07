import copy
import gzip
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELEASE = load_script("feature-controls-release.py")
UPGRADE = load_script("feature-controls-upgrade.py")
SCHEMA = "contracts/feature-controls/bundle.schema.json"
OLD_SHA = "a" * 40
NPM_NAME = "@manolii/feature-controls"
PYTHON_NAME = "manolii-feature-controls"


def git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    return subprocess.run(["git", *args], cwd=root, env=env, check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "-m", "Synthetic fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "remote", "add", "origin", RELEASE.SOURCE_URL)
    write(root, "package.json", json.dumps({"name": NPM_NAME, "version": "0.1.0",
                                          "files": ["index.js"], "main": "index.js"}))
    write(root, "index.js", "module.exports = {};\n")
    write(root, "pack.manifest.yml", "version: 1.14.0\n")
    write(root, "pack-components.yml", "components:\n  agents:\n    version: 1.2.0\n")
    write(root, SCHEMA, json.dumps({"type": "object", "properties": {"schema_version": {"const": "1.0"}}}))
    write(root, RELEASE.PYTHON_SUBDIRECTORY + "/pyproject.toml",
          '[build-system]\nrequires = ["setuptools==75.8.2", "wheel==0.45.1"]\nbuild-backend = "setuptools.build_meta"\n'
          '[project]\nname = "manolii-feature-controls"\nversion = "0.1.0"\n')
    write(root, RELEASE.PYTHON_SUBDIRECTORY + "/src/feature_controls/__init__.py", "")
    return root, commit(root)


def fake_build(source, output):
    output.mkdir()
    npm = output / "manolii-feature-controls-0.1.0.tgz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        content = (source / "package.json").read_bytes()
        info = tarfile.TarInfo("package/package.json")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    npm.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    wheel = output / "manolii_feature_controls-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        info = zipfile.ZipInfo("manolii_feature_controls-0.1.0.dist-info/METADATA")
        archive.writestr(info, b"Name: manolii-feature-controls\nVersion: 0.1.0\n")
    return [npm, wheel]


@pytest.fixture
def prepared(source, tmp_path, monkeypatch):
    root, sha = source
    monkeypatch.setattr(RELEASE, "build_artifacts", fake_build)
    output = tmp_path / "release"
    manifest = RELEASE.prepare(root, output, sha, "0.1.0", "feature-controls-v0.1.0", SCHEMA, True)
    return root, output, manifest


def test_prepare_manifest_is_independent_of_pack_version(prepared):
    root, output, manifest = prepared
    assert manifest["sdk_version"] == "0.1.0"
    assert manifest["schema_version"] == "1.0"
    assert manifest["tag_identity"] == {"status": "pending", "object": None}
    assert manifest["source_revision"] == git(root, "rev-parse", "HEAD")
    assert RELEASE.verify_artifacts(output) == manifest
    RELEASE.verify_source(root, manifest, True)


def test_prepare_verifies_aggregate_wire_schema_definitions(source, tmp_path, monkeypatch):
    root, _ = source
    write(root, SCHEMA, json.dumps({"$defs": {
        "scope": {"type": "object"},
        "bundle": {"properties": {"schema_version": {"const": 1}}},
        "kill": {"properties": {"schema_version": {"const": 1}}},
    }}))
    sha = commit(root)
    monkeypatch.setattr(RELEASE, "build_artifacts", fake_build)
    output = tmp_path / "release"
    manifest = RELEASE.prepare(root, output, sha, "0.1.0", "feature-controls-v0.1.0", SCHEMA, True)
    assert manifest["schema_version"] == "1"
    assert RELEASE.verify_artifacts(output) == manifest
    RELEASE.verify_source(root, manifest, True)


@pytest.mark.parametrize("schema,error", [
    ({}, TypeError),
    ({"$defs": []}, TypeError),
    ({"$defs": {"bundle": True}}, TypeError),
    ({"properties": {"schema_version": {"const": True}}}, TypeError),
    ({"properties": {"schema_version": {"enum": [1]}}}, TypeError),
    ({"$defs": {
        "bundle": {"properties": {"schema_version": {"const": 1}}},
        "kill": {"properties": {"schema_version": {"const": 2}}},
    }}, ValueError),
    ({"properties": {"schema_version": {"const": 1}}, "$defs": {
        "bundle": {"properties": {"schema_version": {"const": "1"}}},
    }}, ValueError),
])
def test_wire_schema_identity_rejects_missing_malformed_or_mixed_versions(schema, error):
    with pytest.raises(error):
        RELEASE.schema_version(schema)


@pytest.mark.parametrize("mutation,reason", [
    ("dirty", "dirty"), ("sha", "HEAD"), ("tag", "component"), ("version", "version"),
    ("origin", "canonical"), ("missing-tag", "absent"), ("lightweight-tag", "annotated"),
    ("tag-source", "tag/source"),
])
def test_source_preconditions_fail_closed(source, tmp_path, monkeypatch, mutation, reason):
    root, sha = source
    monkeypatch.setattr(RELEASE, "build_artifacts", fake_build)
    version, tag, allowed = "0.1.0", "feature-controls-v0.1.0", True
    if mutation == "dirty":
        write(root, "untracked.txt", "drift")
    elif mutation == "sha":
        sha = OLD_SHA
    elif mutation == "tag":
        tag = "v0.1.0"
    elif mutation == "version":
        version, tag = "0.2.0", "feature-controls-v0.2.0"
    elif mutation == "origin":
        git(root, "remote", "set-url", "origin", "https://github.com/example-org/example-app.git")
    elif mutation == "missing-tag":
        allowed = False
    elif mutation == "lightweight-tag":
        git(root, "tag", tag)
    else:
        git(root, "tag", "-a", tag, "-m", "fixture")
        write(root, "index.js", "module.exports = {changed: true};")
        sha = commit(root)
    with pytest.raises(ValueError, match=reason):
        RELEASE.prepare(root, tmp_path / "release", sha, version, tag, SCHEMA, allowed)
    assert not (tmp_path / "release").exists()


def test_tag_object_drift_is_detected_and_global_alias_untouched(source, tmp_path, monkeypatch):
    root, sha = source
    tag = "feature-controls-v0.1.0"
    git(root, "tag", "-a", tag, "-m", "original")
    git(root, "tag", "v1")
    alias = git(root, "rev-parse", "v1")
    monkeypatch.setattr(RELEASE, "build_artifacts", fake_build)
    manifest = RELEASE.prepare(root, tmp_path / "release", sha, "0.1.0", tag, SCHEMA)
    git(root, "tag", "-f", "-a", tag, "-m", "replacement")
    with pytest.raises(ValueError, match="tag object"):
        RELEASE.verify_source(root, manifest, False)
    assert git(root, "rev-parse", "v1") == alias


def test_non_reproducible_build_has_no_output(source, tmp_path, monkeypatch):
    root, sha = source
    calls = []

    def build(snapshot, output):
        artifacts = fake_build(snapshot, output)
        calls.append(output)
        if len(calls) == 2:
            artifacts[1].write_bytes(artifacts[1].read_bytes() + b"drift")
        return artifacts

    monkeypatch.setattr(RELEASE, "build_artifacts", build)
    with pytest.raises(ValueError, match="not reproducible"):
        RELEASE.prepare(root, tmp_path / "release", sha, "0.1.0", "feature-controls-v0.1.0", SCHEMA, True)
    assert not (tmp_path / "release").exists()


def test_build_cannot_replace_the_committed_contract(source, tmp_path, monkeypatch):
    root, sha = source

    def build(snapshot, output):
        artifacts = fake_build(snapshot, output)
        write(snapshot, SCHEMA, '{"properties":{"schema_version":{"const":"99"}}}')
        return artifacts

    monkeypatch.setattr(RELEASE, "build_artifacts", build)
    with pytest.raises(ValueError, match="source/contract"):
        RELEASE.prepare(root, tmp_path / "release", sha, "0.1.0", "feature-controls-v0.1.0", SCHEMA, True)
    assert not (tmp_path / "release").exists()


@pytest.mark.parametrize("member", ["../outside", "another/package.json", "package/package.json"])
def test_npm_archive_members_cannot_escape_or_duplicate_metadata(tmp_path, member):
    file = tmp_path / "bad.tgz"
    with tarfile.open(file, "w:gz") as archive:
        for name in ("package/package.json", member):
            content = json.dumps({"name": NPM_NAME, "version": "0.1.0"}).encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    with pytest.raises(ValueError):
        RELEASE.artifact_identity(file, "npm")


@pytest.mark.parametrize("mutation", ["digest", "extra-file", "traversal", "version", "source", "schema", "symlink", "duplicate-artifact"])
def test_release_integrity_failures(prepared, mutation):
    _, output, manifest = prepared
    record = manifest["artifacts"][0]
    if mutation == "digest":
        (output / record["file"]).write_bytes(b"corrupt")
    elif mutation == "extra-file":
        (output / "not-in-inventory").write_text("unexpected")
    elif mutation == "traversal":
        record["file"] = "../outside.tgz"
    elif mutation == "version":
        manifest["packages"]["npm"]["version"] = "1.14.0"
    elif mutation == "source":
        manifest["source_revision"] = "main"
    elif mutation == "schema":
        manifest["schema_version"] = "2.0"
    elif mutation == "symlink":
        file = output / record["file"]
        copy = output.parent / "outside.tgz"
        copy.write_bytes(file.read_bytes())
        file.unlink()
        file.symlink_to(copy)
    else:
        manifest["artifacts"][1]["file"] = record["file"]
    (output / "release.json").write_text(json.dumps(manifest))
    with pytest.raises((ValueError, TypeError)):
        RELEASE.verify_artifacts(output)


def test_wrong_contract_source_rejected_even_when_manifest_digests_are_recomputed(prepared):
    root, output, manifest = prepared
    schema = output / "contract.schema.json"
    schema.write_text('{"properties":{"schema_version":{"const":"99"}}}')
    manifest["schema_version"] = "99"
    manifest["contract"]["sha256"] = RELEASE.digest(schema)
    (output / "release.json").write_text(json.dumps(manifest))
    RELEASE.verify_artifacts(output)
    with pytest.raises(ValueError, match="source/contract"):
        RELEASE.verify_source(root, manifest, True)


@pytest.mark.parametrize("path", ["../out", "/out", "a/../out", "a\\out", "", ".", "./out", "a//out", "C:/out", "out\n"])
def test_unsafe_paths_rejected(path):
    with pytest.raises(ValueError):
        RELEASE.safe_relative(path)


@pytest.mark.parametrize("version", ["v0.1.0", "01.1.0", "0.1", "0.1.0-dev", "0.1.0-rc.01", "0.1.0+secret"])
def test_unsupported_version_forms_rejected(version):
    with pytest.raises(ValueError):
        RELEASE.python_version(version)


def test_prerelease_versions_normalize_without_using_pack_version():
    assert RELEASE.python_version("0.1.0-rc.1") == "0.1.0rc1"
    assert RELEASE.python_version("0.1.0-alpha.2") == "0.1.0a2"


def test_existing_or_in_repo_output_rejected(source, tmp_path):
    root, sha = source
    for output in (root / "build", tmp_path):
        with pytest.raises(ValueError, match="new directory"):
            RELEASE.prepare(root, output, sha, "0.1.0", "feature-controls-v0.1.0", SCHEMA, True)


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is required for artifact integration")
def test_real_npm_tarball_and_python_wheel_are_reproducible(source, tmp_path):
    root, sha = source
    manifest = RELEASE.prepare(root, tmp_path / "real-release", sha, "0.1.0", "feature-controls-v0.1.0", SCHEMA, True)
    assert {record["format"] for record in manifest["artifacts"]} == {"npm", "python"}
    assert RELEASE.verify_artifacts(tmp_path / "real-release") == manifest


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is required for Git bridge installation")
def test_full_sha_git_bridge_installs_without_registry_publication(source, tmp_path):
    root, sha = source
    home, consumer_root = tmp_path / "home", tmp_path / "installer"
    home.mkdir()
    consumer_root.mkdir()
    write(consumer_root, "package.json", '{"name":"synthetic-consumer","version":"1.0.0","private":true}')
    # Fixture-only transport rewrite: committed dependency strings remain canonical.
    env = {"PATH": os.environ["PATH"], "HOME": str(home),
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{root}.insteadOf",
           "GIT_CONFIG_VALUE_0": RELEASE.SOURCE_URL}
    npm_pin = UPGRADE.pin_for("npm", NPM_NAME, sha)
    RELEASE.run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", npm_pin], consumer_root, env)
    installed = json.loads((consumer_root / "node_modules" / NPM_NAME / "package.json").read_text())
    assert installed["name"] == NPM_NAME and installed["version"] == "0.1.0"
    lock = json.loads((consumer_root / "package-lock.json").read_text())
    assert lock["packages"]["node_modules/" + NPM_NAME]["resolved"].endswith("#" + sha)
    assert UPGRADE.check_npm_lock(consumer_root, {"file": "package.json", "section": "dependencies", "name": NPM_NAME},
                                  npm_pin, sha)
    python_target = consumer_root / "python-site"
    RELEASE.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile",
                 "--target", str(python_target), UPGRADE.pin_for("python", PYTHON_NAME, sha)], consumer_root, env)
    assert (python_target / "feature_controls" / "__init__.py").exists()
    direct_url = json.loads(next(python_target.glob("*.dist-info/direct_url.json")).read_text())
    assert direct_url["vcs_info"]["commit_id"] == sha
    assert direct_url["subdirectory"] == RELEASE.PYTHON_SUBDIRECTORY
    assert git(root, "status", "--porcelain") == ""


@pytest.fixture
def consumer(tmp_path):
    workspace = tmp_path / "consumers"
    root = workspace / "example-app"
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "remote", "add", "origin", "https://github.com/example-org/example-app.git")
    npm_pin = UPGRADE.pin_for("npm", NPM_NAME, OLD_SHA)
    python_pin = UPGRADE.pin_for("python", PYTHON_NAME, OLD_SHA)
    write(root, "package.json", json.dumps({"name": "example-app", "dependencies": {NPM_NAME: npm_pin, "existing-lib": "1.0.0"}}, indent=2) + "\n")
    write(root, "requirements.txt", python_pin + "\nexisting-lib==1.0.0\n")
    commit(root)
    inventory = {"manifest_version": 1, "consumers": [{
        "repository": "example-org/example-app", "ecosystem": "example", "environment": "staging",
        "base": "main", "checkout": "example-app", "opt_in": True,
        "installed": {"sdk_version": "0.1.0", "source_revision": OLD_SHA, "pins": [
            {"manager": "npm", "file": "package.json", "section": "dependencies", "name": NPM_NAME, "pin": npm_pin},
            {"manager": "python", "file": "requirements.txt", "name": PYTHON_NAME, "pin": python_pin},
        ]},
    }]}
    return workspace, root, inventory


def test_inventory_schema_is_exportable_and_matches_generic_example(consumer):
    _, _, inventory = consumer
    jsonschema.Draft202012Validator.check_schema(UPGRADE.INVENTORY_SCHEMA)
    jsonschema.validate(inventory, UPGRADE.INVENTORY_SCHEMA)
    assert UPGRADE.validate_inventory(inventory) == inventory["consumers"]
    result = subprocess.run([sys.executable, str(ROOT / "scripts/feature-controls-upgrade.py"), "schema"],
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == UPGRADE.INVENTORY_SCHEMA


def test_proposals_are_read_only_scoped_and_preserve_unrelated_dependencies(consumer, prepared):
    workspace, root, inventory = consumer
    _, _, manifest = prepared
    before = {name: (root / name).read_bytes() for name in ("package.json", "requirements.txt")}
    report = UPGRADE.propose(inventory, manifest, workspace, "example-org", "example", "staging")
    item = report["consumers"][0]
    assert report["wire_schema_version"] == manifest["schema_version"]
    assert item["pin_record"] is None
    assert item["base"] == "main"
    assert item["base_revision"] == git(root, "rev-parse", "HEAD")
    assert len(item["changes"]) == 2
    assert item["follow_up"]
    assert all("-" + inventory["consumers"][0]["installed"]["pins"][1]["pin"] not in change["patch"]
               for change in item["changes"] if change["file"] == "package.json")
    assert all((root / name).read_bytes() == data for name, data in before.items())
    assert git(root, "status", "--porcelain") == ""
    assert {change["file"] for change in item["changes"]} == set(before)


@pytest.mark.parametrize("mutation,reason", [
    ("not-opted-in", "no opted"), ("owner", "boundary"), ("ecosystem", "no opted"),
    ("environment", "no opted"), ("origin", "origin"), ("base", "declared base"),
    ("dirty", "dirty"), ("checkout-path", "unsafe"), ("file-path", "unsafe"),
    ("missing-file", "absent"), ("wrong-name", "release package"),
    ("duplicate", "duplicate"), ("pin-drift", "pin drift"), ("short-sha", "full SHA"),
    ("mutable-pin", "canonical Git"), ("unknown-file", "package.json"), ("symlink", "escapes"),
    ("ignored-file", "command failed"), ("assume-unchanged", "consumer commit"),
])
def test_upgrade_boundaries_fail_closed(consumer, prepared, mutation, reason):
    workspace, root, inventory = consumer
    _, _, manifest = prepared
    entry = inventory["consumers"][0]
    owner = "example-org"
    if mutation == "not-opted-in":
        entry["opt_in"] = False
    elif mutation == "owner":
        owner = "another-org"
    elif mutation in {"ecosystem", "environment"}:
        entry[mutation] = "another"
    elif mutation == "origin":
        git(root, "remote", "set-url", "origin", "https://github.com/example-org/another.git")
    elif mutation == "base":
        entry["base"] = "develop"
    elif mutation == "dirty":
        write(root, "untracked.txt", "local work")
    elif mutation == "checkout-path":
        entry["checkout"] = "../source"
    elif mutation == "file-path":
        entry["installed"]["pins"][0]["file"] = "../package.json"
    elif mutation == "missing-file":
        entry["installed"]["pins"][0]["file"] = "missing/package.json"
    elif mutation == "wrong-name":
        pin = entry["installed"]["pins"][0]
        pin["name"] = "different"
    elif mutation == "duplicate":
        inventory["consumers"].append(copy.deepcopy(entry))
    elif mutation == "pin-drift":
        write(root, "package.json", '{"dependencies": {}}')
        commit(root)
    elif mutation == "short-sha":
        entry["installed"]["source_revision"] = "a" * 7
    elif mutation == "mutable-pin":
        entry["installed"]["pins"][0]["pin"] = "github:Manolii-org/ai-starter-pack#main"
    elif mutation == "unknown-file":
        entry["installed"]["pins"][0]["file"] = "flags.json"
    elif mutation == "ignored-file":
        entry["installed"]["pins"][0]["file"] = "ignored/package.json"
        write(root, "ignored/package.json", (root / "package.json").read_text())
        write(root, ".gitignore", "ignored/\n")
        commit(root)
    elif mutation == "assume-unchanged":
        git(root, "update-index", "--assume-unchanged", "requirements.txt")
        write(root, "requirements.txt", (root / "requirements.txt").read_text() + "another-lib==2.0.0\n")
    else:
        outside = workspace / "another-package.json"
        outside.write_bytes((root / "package.json").read_bytes())
        (root / "package.json").unlink()
        (root / "package.json").symlink_to(outside)
        commit(root)
    with pytest.raises(ValueError, match=reason):
        UPGRADE.propose(inventory, manifest, workspace, owner, "example", "staging")


def test_check_reports_drift_then_accepts_exact_installed_pins_and_lock(consumer, prepared):
    workspace, root, inventory = consumer
    _, _, manifest = prepared
    item = UPGRADE.propose(inventory, manifest, workspace, "example-org", "example", "staging", True)["consumers"][0]
    assert not item["current"] and item["pin_record"] is None
    pins = inventory["consumers"][0]["installed"]["pins"]
    for pin in pins:
        path = root / pin["file"]
        wanted = UPGRADE.pin_for(pin["manager"], pin["name"], manifest["source_revision"])
        content, _ = UPGRADE.replace_pin(path.read_text(), pin, wanted, False)
        path.write_text(content)
    wanted = UPGRADE.pin_for("npm", NPM_NAME, manifest["source_revision"])
    write(root, "package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {
        "": {"dependencies": {NPM_NAME: wanted}}, "node_modules/" + NPM_NAME: {"resolved": wanted},
    }}))
    commit(root)
    item = UPGRADE.propose(inventory, manifest, workspace, "example-org", "example", "staging", True)["consumers"][0]
    assert item["current"]
    assert item["pin_record"] == {"sourceSHA": manifest["source_revision"], "sdkVersion": manifest["sdk_version"],
                                  "wireSchemaVersion": manifest["schema_version"], "consumerSHA": git(root, "rev-parse", "HEAD")}
    write(root, "package-lock.json", json.dumps({"packages": {}}))
    commit(root)
    item = UPGRADE.propose(inventory, manifest, workspace, "example-org", "example", "staging", True)["consumers"][0]
    assert not item["current"] and item["pin_record"] is None
    git(root, "update-index", "--assume-unchanged", "package-lock.json")
    write(root, "package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {
        "": {"dependencies": {NPM_NAME: wanted}}, "node_modules/" + NPM_NAME: {"resolved": wanted},
    }}))
    assert git(root, "status", "--porcelain") == ""
    with pytest.raises(ValueError, match="consumer commit"):
        UPGRADE.propose(inventory, manifest, workspace, "example-org", "example", "staging", True)


def test_json_duplicate_keys_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        RELEASE.load_json('{"name":"first","name":"second"}')


def test_upgrade_cli_proposal_and_check_exit_codes_are_read_only(consumer, prepared):
    workspace, root, inventory = consumer
    source_root, release, _ = prepared
    inventory_path = workspace.parent / "inventory.json"
    inventory_path.write_text(json.dumps(inventory))
    args = [sys.executable, str(ROOT / "scripts/feature-controls-upgrade.py"),
            "--inventory", str(inventory_path), "--release", str(release),
            "--source-root", str(source_root), "--workspace", str(workspace),
            "--owner", "example-org", "--ecosystem", "example", "--environment", "staging"]
    for mode, status in (("propose", 0), ("check", 1)):
        result = subprocess.run([*args, mode], capture_output=True, text=True, check=False, timeout=30)
        assert result.returncode == status, result.stderr
        assert json.loads(result.stdout)["mode"] == ("proposal" if mode == "propose" else "check")
    assert git(root, "status", "--porcelain") == ""


def test_proposal_patch_handles_missing_trailing_newline_without_writing(consumer, prepared):
    workspace, root, inventory = consumer
    _, _, manifest = prepared
    file = root / "package.json"
    file.write_text(file.read_text().rstrip("\n"))
    commit(root)
    report = UPGRADE.propose(inventory, manifest, workspace, "example-org", "example", "staging")
    patch = "".join(change["patch"] for change in report["consumers"][0]["changes"])
    assert "No newline at end of file" in patch
    subprocess.run(["git", "apply", "--check", "-"], cwd=root, input=patch, text=True, check=True, timeout=10)
    assert git(root, "status", "--porcelain") == ""


def test_upgrade_refuses_downgrade(consumer, prepared):
    workspace, _, inventory = consumer
    _, _, manifest = prepared
    inventory["consumers"][0]["installed"]["sdk_version"] = "1.0.0"
    with pytest.raises(ValueError, match="downgrade"):
        UPGRADE.propose(inventory, manifest, workspace, "example-org", "example", "staging")


@pytest.mark.parametrize("resolved,valid", [
    (f"git+{RELEASE.SOURCE_URL}#{OLD_SHA}", True),
    (f"git+ssh://git@github.com/{RELEASE.SOURCE_REPOSITORY}.git#{OLD_SHA}", True),
    (f"github:{RELEASE.SOURCE_REPOSITORY}#{OLD_SHA}", True),
    (f"git+https://github.com/another-org/ai-starter-pack.git#{OLD_SHA}", False),
    (f"git+https://github.com.evil.invalid/{RELEASE.SOURCE_REPOSITORY}.git#{OLD_SHA}", False),
    (f"git+https://github.com/{RELEASE.SOURCE_REPOSITORY}.git#main", False),
    (f"git+{RELEASE.SOURCE_URL}#{'b' * 40}", False),
])
def test_npm_lock_accepts_transport_normalization_but_not_source_or_sha_drift(consumer, resolved, valid):
    _, root, inventory = consumer
    pin = inventory["consumers"][0]["installed"]["pins"][0]
    write(root, "package-lock.json", json.dumps({"packages": {
        "": {"dependencies": {NPM_NAME: pin["pin"]}}, "node_modules/" + NPM_NAME: {"resolved": resolved},
    }}))
    assert UPGRADE.check_npm_lock(root, pin, pin["pin"], OLD_SHA) is valid


def test_renovate_sdk_policy_overrides_pin_automerge_only_for_sdk():
    preset = json.loads((ROOT / "default.json").read_text())
    assert preset["packageRules"][0]["automerge"] is True
    rule = preset["packageRules"][-1]
    assert NPM_NAME in rule["matchPackageNames"]
    assert PYTHON_NAME in rule["matchPackageNames"]
    assert rule["automerge"] is False
    assert rule["platformAutomerge"] is False
    assert rule["dependencyDashboardApproval"] is True
    assert rule["ignoreUnstable"] is True
    assert "matchUpdateTypes" not in rule


@pytest.mark.parametrize("existing_consumer", [False, True])
def test_copier_excludes_runtime_sources_and_preserves_consumer_dependencies_and_configs(tmp_path, existing_consumer):
    template, destination = tmp_path / "template", tmp_path / "consumer"
    template.mkdir()
    destination.mkdir()
    shutil.copyfile(ROOT / "copier.yml", template / "copier.yml")
    excluded = [
        "package.json", "package-lock.json", ".npmignore", "eslint.config.mjs", "tsconfig.json",
        "packages/feature-controls/typescript/package.json", "contracts/feature-controls/bundle.schema.json",
        "contracts/feature-controls-bundle.schema.json",
        "scripts/feature-controls-release.py", "scripts/feature-controls-upgrade.py",
        "tests/test_feature_controls_distribution.py", "tests/feature_controls/test_runtime.py",
        "build/runtime.js",
    ]
    for relative in excluded:
        write(template, relative, "must not ship")
    write(template, "docs/feature-controls-distribution.md", "operator documentation")
    write(template, ".claude/skills/example/harness/package.json", '{"name":"agent-harness"}')
    write(template, ".claude/skills/example/harness/eslint.config.mjs", "nested eslint config")
    write(template, ".claude/skills/example/harness/tsconfig.json", '{"compilerOptions":{}}')
    existing = {"package.json": '{"name":"consumer","dependencies":{"existing-lib":"1.0.0"}}\n',
                "package-lock.json": '{"lockfileVersion":3}\n',
                "eslint.config.mjs": "export default [{ rules: { semi: ['error', 'always'] } }];\r\n",
                "apps/example/package.json": '{"name":"consumer-nested","private":true}\n'} if existing_consumer else {}
    before = {}
    for relative, content in existing.items():
        write(destination, relative, content)
        before[relative] = (destination / relative).read_bytes()
    result = subprocess.run([sys.executable, "-m", "copier", "copy", "--defaults", "--overwrite", "--quiet",
                             str(template), str(destination)], capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert (destination / "docs/feature-controls-distribution.md").read_text() == "operator documentation"
    assert (destination / ".claude/skills/example/harness/package.json").read_text() == '{"name":"agent-harness"}'
    assert (destination / ".claude/skills/example/harness/eslint.config.mjs").read_text() == "nested eslint config"
    assert (destination / ".claude/skills/example/harness/tsconfig.json").read_text() == '{"compilerOptions":{}}'
    for relative, content in before.items():
        assert (destination / relative).read_bytes() == content
    for relative in excluded:
        if relative not in before:
            assert not (destination / relative).exists()
    assert {"/package.json", "/package-lock.json", "/eslint.config.mjs", "/tsconfig.json",
            "scripts/feature-controls-release.py"} <= set(
        yaml.safe_load((ROOT / "copier.yml").read_text())["_exclude"])
