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
    contract = {"profiles": {"manolii": {"kind": "runtime"}}}
    scaffold = {"profile": "acme", "profile_definition": {"kind": "runtime"}}
    rendered = MODULE.render(contract, scaffold)
    assert rendered["profiles"]["acme"] == {"kind": "runtime"}
    assert "acme" not in contract["profiles"]


def test_placeholder_profile_is_rejected():
    scaffold = json.loads((ROOT / "config/litellm-product-profile.example.json").read_text())
    try:
        MODULE.render({"profiles": {}}, scaffold)
    except ValueError as exc:
        assert "concrete lowercase owner profile" in str(exc)
    else:
        raise AssertionError("placeholder profile was accepted")
