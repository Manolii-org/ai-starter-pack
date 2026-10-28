"""Route invariants for the bundled LiteLLM proxy template.

Together stopped serving Kimi K2.6 and Qwen 3.5 397B serverless, and the
template kept routing to both: every hop returned HTTP 400 while the registry
still described working chains. These checks keep the config's fallback names,
the registry's fallback chains and the OpenRouter host pin consistent."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


@cache
def _config() -> dict:
    return yaml.safe_load((ROOT / "deploy/litellm-proxy/config.yaml").read_text(encoding="utf-8"))


@cache
def _models() -> dict:
    return {m["model_name"]: m["litellm_params"] for m in _config()["model_list"]}


def _routing() -> dict:
    return json.loads((ROOT / ".claude/model-routing.json").read_text(encoding="utf-8"))


# PRC-origin weight families that may only run on pinned OpenRouter hosts.
PINNED_FAMILIES = ("moonshotai/", "qwen/", "minimax/")
# Registry chains that describe hops the template config does not define.
# Pre-existing drift; remove an entry when its tier is reconciled.
KNOWN_REGISTRY_DRIFT = {
    ("tier-4-extract", "tier-4-extract-fallback-v4flash"),
    ("tier-4-extract", "tier-4-extract-fallback-gemma"),
    ("tier-4-extract", "tier-4-extract-fallback-together"),
    ("tier-4-extract", "tier-4-extract-fallback-llama"),
    ("tier-5-latency", "tier-5-latency-fallback-together"),
}


def test_every_fallback_target_is_a_declared_route():
    for entry in _config()["litellm_settings"]["fallbacks"]:
        for primary, targets in entry.items():
            missing = [name for name in [primary, *targets] if name not in _models()]
            assert not missing, f"{primary} chain names undeclared routes: {missing}"


def test_no_route_uses_together_serverless():
    together = [name for name, p in _models().items() if p["model"].startswith("together_ai/")]
    assert not together, f"Together serverless no longer serves these models: {together}"


def test_prc_origin_openrouter_routes_are_pinned_to_us_zdr_hosts():
    for name, params in _models().items():
        model = params["model"]
        if not (model.startswith("openrouter/") and model.removeprefix("openrouter/").startswith(PINNED_FAMILIES)):
            continue
        provider = (params.get("extra_body") or {}).get("provider") or {}
        assert provider.get("zdr") is True, name
        assert provider.get("data_collection") == "deny", name
        assert provider.get("only"), f"{name} must list its allowed hosts"


def test_registry_fallback_aliases_exist_in_config():
    models = _models()
    drift = set()
    for tier, definition in _routing()["tier_definitions"].items():
        if tier not in models:
            continue
        for slot in definition.get("fallback_chain", {}).values():
            alias = slot.get("litellm_alias")
            if alias and alias not in models:
                drift.add((tier, alias))
    assert drift - KNOWN_REGISTRY_DRIFT == set(), sorted(drift - KNOWN_REGISTRY_DRIFT)
    assert KNOWN_REGISTRY_DRIFT - drift == set(), f"reconciled; drop from KNOWN_REGISTRY_DRIFT: {sorted(KNOWN_REGISTRY_DRIFT - drift)}"
