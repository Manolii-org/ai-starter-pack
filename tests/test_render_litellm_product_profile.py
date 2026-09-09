import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "render_litellm_product_profile", ROOT / "scripts/render-litellm-product-profile.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_owner_profile_is_merged_into_contract():
    contract = {
        "product_version": "0.4.3",
        "profiles": {"manolii": {"kind": "runtime"}},
        "core_aliases": {"tier-1-fast": {"required_profiles": ["manolii"]}},
    }
    scaffold = {"profile": "acme", "profile_definition": {"kind": "runtime"}}
    rendered = MODULE.render(contract, scaffold)
    assert rendered["profiles"]["acme"] == {"kind": "runtime"}
    assert "acme" not in contract["profiles"]
    assert rendered["core_aliases"]["tier-1-fast"]["required_profiles"] == ["manolii", "acme"]
    assert contract["core_aliases"]["tier-1-fast"]["required_profiles"] == ["manolii"]


def test_logical_profile_does_not_join_required_profiles():
    contract = {
        "product_version": "0.4.3",
        "profiles": {"manolii": {"kind": "runtime"}},
        "core_aliases": {"tier-1-fast": {"required_profiles": ["manolii"]}},
    }
    scaffold = {
        "profile": "acme-app",
        "profile_definition": {"kind": "logical", "runtime_profile": "manolii"},
    }
    rendered = MODULE.render(contract, scaffold)
    assert rendered["core_aliases"]["tier-1-fast"]["required_profiles"] == ["manolii"]


def test_placeholder_profile_is_rejected():
    scaffold = json.loads((ROOT / "config/litellm-product-profile.example.json").read_text())
    try:
        MODULE.render({"product_version": "0.4.3", "profiles": {}}, scaffold)
    except ValueError as exc:
        assert "concrete lowercase owner profile" in str(exc)
    else:
        raise AssertionError("placeholder profile was accepted")


def test_version_mismatch_is_rejected():
    try:
        MODULE.render(
            {"product_version": "0.4.3", "profiles": {}},
            {"product_version": "0.4.2", "profile": "acme", "profile_definition": {"kind": "runtime"}},
        )
    except ValueError as exc:
        assert "does not match contract" in str(exc)
    else:
        raise AssertionError("mismatched product_version was accepted")
