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


def test_checked_release_is_valid_and_profile_matches():
    release_dir, metadata = MODULE.load_release("0.4.1")
    MODULE.validate_release(release_dir, metadata)
    profile = json.loads((ROOT / "config/litellm-product-profile.example.json").read_text())
    assert profile["product_version"] == metadata["product_version"]
    assert profile["release"] == "litellm-product/releases/0.4.1/release.json"


def test_asset_is_deterministic(tmp_path):
    release_dir, metadata = MODULE.load_release("0.4.1")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    assert MODULE.build_asset(release_dir, metadata, first) == MODULE.build_asset(release_dir, metadata, second)
    assert first.read_bytes() == second.read_bytes()


def test_source_drift_fails_closed(tmp_path):
    release_dir, metadata = MODULE.load_release("0.4.1")
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
    release_dir, metadata = MODULE.load_release("0.4.1")
    content = (release_dir / "source/config/litellm-product-source-manifest.json").read_bytes()
    assert hashlib.sha256(content).hexdigest() == metadata["manifest_sha256"]
