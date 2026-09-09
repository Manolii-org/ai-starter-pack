import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_litellm_product_asset", ROOT / "scripts/build-litellm-product-asset.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
RELEASES = ROOT / "litellm-product/releases"


def _versions() -> list[str]:
    return sorted(path.name for path in RELEASES.iterdir() if path.is_dir())


def test_every_release_is_valid():
    versions = _versions()
    assert "0.4.2" in versions
    assert "0.4.3" in versions
    for version in versions:
        release_dir, metadata = MODULE.load_release(version)
        MODULE.validate_release(release_dir, metadata)


def test_checked_release_is_valid_and_profile_matches():
    latest = _versions()[-1]
    release_dir, metadata = MODULE.load_release(latest)
    MODULE.validate_release(release_dir, metadata)
    profile = json.loads((ROOT / "config/litellm-product-profile.example.json").read_text())
    assert profile["product_version"] == metadata["product_version"] == latest
    assert profile["release"] == f"litellm-product/releases/{latest}/release.json"


def test_immutable_prior_release_is_unchanged():
    _, metadata = MODULE.load_release("0.4.2")
    assert metadata["source_revision"] == "7c606f4a346311156070915084f563c719c3f850"
    assert metadata["product_version"] == "0.4.2"


def test_current_release_pins_verified_manolii_revision():
    _, metadata = MODULE.load_release("0.4.3")
    assert metadata["source_revision"] == "ab78c746adf228f0a4475dac4b3b30a0f76a3101"


def test_asset_is_deterministic(tmp_path):
    release_dir, metadata = MODULE.load_release("0.4.3")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    assert MODULE.build_asset(release_dir, metadata, first) == MODULE.build_asset(release_dir, metadata, second)
    assert first.read_bytes() == second.read_bytes()


def test_asset_excludes_unvalidated_sibling(tmp_path):
    release_dir, metadata = MODULE.load_release("0.4.3")
    import shutil

    copied = tmp_path / "release"
    shutil.copytree(release_dir, copied)
    (copied / "internal-note.txt").write_text("must not ship")
    asset = tmp_path / "asset.tar.gz"
    MODULE.build_asset(copied, metadata, asset)
    import tarfile

    with tarfile.open(asset) as archive:
        assert all(not name.endswith("internal-note.txt") for name in archive.getnames())


def test_source_drift_fails_closed(tmp_path):
    release_dir, metadata = MODULE.load_release("0.4.3")
    copied = tmp_path / "release"
    import shutil

    shutil.copytree(release_dir, copied)
    target = copied / "source/config/litellm-product-contract.json"
    target.write_bytes(target.read_bytes() + b"\n")
    try:
        MODULE.validate_release(copied, metadata)
    except ValueError as exc:
        assert "source digest mismatch" in str(exc)
    else:
        raise AssertionError("drifted product source was accepted")


def test_manifest_digest_is_pinned():
    release_dir, metadata = MODULE.load_release("0.4.3")
    content = (release_dir / "source/config/litellm-product-source-manifest.json").read_bytes()
    assert hashlib.sha256(content).hexdigest() == metadata["manifest_sha256"]
