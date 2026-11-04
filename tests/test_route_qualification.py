"""Unqualified catalog models cannot become serving routes or fallback hops."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def configuration():
    return (yaml.safe_load((ROOT / "deploy/litellm-proxy/config.yaml").read_text()),
            json.loads((ROOT / ".claude/model-routing.json").read_text()))


def test_serving_graph_has_no_unqualified_or_dangling_hop():
    config, _ = configuration()
    rows = {row["model_name"]: row for row in config["model_list"]}
    assert "tier-0-oss-heavy" not in rows
    assert "tier-2-agentic-fallback-fireworks" not in rows
    assert "tier-3-tool-fallback-kimi-fireworks" not in rows
    assert all("kimi-k3" not in row["litellm_params"]["model"] for row in rows.values())
    assert "*" not in rows
    assert not config["litellm_settings"].get("default_fallbacks")
    assert not config.get("router_settings", {}).get("default_fallbacks")
    for fallback in config["litellm_settings"]["fallbacks"]:
        for source, targets in fallback.items():
            assert source in rows, source
            assert targets and all(target in rows for target in targets), (source, targets)
    for target in config.get("router_settings", {}).get("model_group_alias", {}).values():
        assert target in rows


def test_ordinary_agentic_reuses_existing_qualified_route_without_heavy_promotion():
    config, routing = configuration()
    rows = {row["model_name"]: row for row in config["model_list"]}
    assert rows["tier-2-agentic"]["litellm_params"] == rows["tier-2-agentic-pro"]["litellm_params"]
    fallback = {key: value for row in config["litellm_settings"]["fallbacks"] for key, value in row.items()}
    assert fallback["tier-2-agentic"] == fallback["tier-2-agentic-pro"] == ["tier-2-agentic-pro-fallback-openrouter"]
    ordinary, qualified = (routing["tier_definitions"][name] for name in ("tier-2-agentic", "tier-2-agentic-pro"))
    for key in ("provider", "model", "context_window", "intelligence_score",
                "intelligence_index_version", "cost_per_m_input", "cost_per_m_output", "data_clearance"):
        assert ordinary[key] == qualified[key], key
    original_probe = qualified["fallback_chain"]["primary"]
    reused_probe = ordinary["fallback_chain"]["primary"]
    assert original_probe["smoke_tested"] == reused_probe["smoke_tested"] == "2026-10-28"
    assert original_probe["notes"] == reused_probe["notes"]
    assert routing["tier_definitions"]["tier-0-oss-heavy"]["enabled"] is False
    assert ordinary["data_clearance"] == ["internal", "public"]


def test_every_active_metadata_route_excludes_the_unqualified_candidate():
    _, routing = configuration()
    for section in ("tier_definitions", "tier_aliases", "proxy_intercepted_models"):
        for name, row in routing[section].items():
            if not isinstance(row, dict) or row.get("enabled") is False:
                continue
            assert "kimi-k3" not in json.dumps(row), (section, name)
    assert "tier-0-oss-heavy" not in routing["data_classification"]["internal"]["allowed_tiers"]
    assert routing["fallback"]["tier_map"]["standard"]["model_id"] == "accounts/fireworks/models/minimax-m3"
    assert routing["fallback"]["tier_map"]["heavy"]["enabled"] is False
    assert routing["browser_automation"]["stagehand_llm"]["secondary"]["enabled"] is False
    assert routing["proxy_intercepted_models"]["sonnet-internal"]["enabled"] is False


def test_dispatch_guidance_does_not_silently_substitute_for_unavailable_heavy_agent():
    _, routing = configuration()
    spec = importlib.util.spec_from_file_location("qualification_classifier", ROOT / "scripts/classify-message.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rule = module._build_dispatch_rule(routing)
    assert "test-hardener" not in rule.split("sonnet=", 1)[0]
    assert "Unavailable pending qualification: test-hardener; do not substitute another model." in rule
    assert "tier-0-oss-heavy" not in module._MODEL_PRICING


@pytest.mark.parametrize("model", [None, "haiku", "tier-0-oss-heavy", "tier-2-agentic-pro"])
def test_hook_blocks_unavailable_heavy_agent_without_substitution(model):
    outcome = run_hook("test-hardener", model)
    assert outcome["decision"] == "block"
    assert "unavailable" in outcome["reason"]
    assert "Do not substitute another model" in outcome["reason"]


def run_hook(agent, model):
    tool_input = {"subagent_type": agent, "prompt": "SCOPE_BUDGET:\nallowed_paths: tests/"}
    if model is not None:
        tool_input["model"] = model
    process = subprocess.run(
        [sys.executable, str(ROOT / "scripts/pre-tool-use.py")],
        input=json.dumps({"tool_name": "Agent", "tool_input": tool_input}),
        text=True, capture_output=True, check=True, timeout=10,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT), "CLAUDE_PLUGIN_ROOT": ""},
    )
    return json.loads(process.stdout) if process.stdout.strip() else None


def test_hook_preserves_ordinary_agentic_route_and_rejects_explicit_disabled_route():
    assert run_hook("orchestrator", "haiku") is None
    assert run_hook("orchestrator", "tier-2-agentic") is None
    assert run_hook("orchestrator", "tier-0-oss-heavy")["decision"] == "block"
