"""Transcript costs follow the actual client plane and bundled proxy routes."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NATIVE = (
    "claude-haiku-4-5-20251001", "claude-haiku-4-5",
    "claude-haiku-4-5-20251001-anthropic",
)


def load(relative):
    spec = importlib.util.spec_from_file_location("cost_route_test", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clean_plane(monkeypatch):
    for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CLIENT_USE_PROXY", "LITELLM_PROXY_URL"):
        monkeypatch.delenv(key, raising=False)


def transcript(directory, models):
    rows = [json.dumps({"uuid": str(i), "message": {
        "model": model, "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    }}) for i, model in enumerate(models)]
    path = directory / "agent.jsonl"
    path.write_text("\n".join(rows))
    return path


@pytest.mark.parametrize("base,flag,named,expected", [
    ("", "0", "https://proxy.example.test", False),
    ("", "1", "", True),
    ("https://proxy.example.test", "0", "", True),
    ("https://api.anthropic.com", "1", "", False),
    ("https://api.anthropic.com.evil.test", "0", "", True),
    ("https://API.ANTHROPIC.COM./", "1", "", False),
])
def test_client_plane_matches_shared_contract(monkeypatch, base, flag, named, expected):
    for key, value in (("ANTHROPIC_BASE_URL", base), ("CLAUDE_CLIENT_USE_PROXY", flag),
                       ("LITELLM_PROXY_URL", named)):
        monkeypatch.setenv(key, value)
    logger = load("scripts/session-cost-logger.py")
    plane = load("scripts/lib/proxy_plane.py")
    assert logger._PROXY_ACTIVE is expected
    assert logger._PROXY_ACTIVE == plane.oss_alias_rates_active()


@pytest.mark.parametrize("proxy", [False, True])
@pytest.mark.parametrize("model", NATIVE)
def test_native_haiku_retains_native_rates_and_bucket(monkeypatch, tmp_path, model, proxy):
    monkeypatch.setenv("CLAUDE_CLIENT_USE_PROXY", str(int(proxy)))
    logger = load("scripts/session-cost-logger.py")
    path = transcript(tmp_path, [model])
    result = logger.parse_subagents(tmp_path)
    assert result["subagent_ant_cost"] == 6
    assert result["subagent_proxy_cost"] == 0
    assert result["subagent_total_cost"] == 6
    assert logger.parse_session(path)["main_thread_cost"] == 6


def test_proxy_alias_rates_follow_active_routes_and_mixed_totals(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.test")
    logger = load("scripts/session-cost-logger.py")
    routing = json.loads((ROOT / ".claude/model-routing.json").read_text())
    routes = {item["model_name"]: item["litellm_params"]["model"] for item in
              yaml.safe_load((ROOT / "deploy/litellm-proxy/config.yaml").read_text())["model_list"]}
    fast = routing["tier_definitions"]["tier-1-fast"]
    sonnet = routing["proxy_intercepted_models"]["sonnet"]
    for alias, owner in (("haiku", fast), ("sonnet", sonnet)):
        assert logger._SUBAGENT_PRICING[alias] == {
            "input": owner["cost_per_m_input"] / 1_000_000,
            "output": owner["cost_per_m_output"] / 1_000_000,
        }
    assert routes["haiku"] == "fireworks_ai/" + fast["model"]
    assert routes["sonnet"] == "fireworks_ai/" + sonnet["resolves_to"].split(":", 1)[1]
    assert routes[NATIVE[0]] == "anthropic/" + NATIVE[0]
    assert routes[NATIVE[2]] == "anthropic/" + NATIVE[0]
    path = transcript(tmp_path, [NATIVE[0], "haiku", "sonnet"])
    (tmp_path / "duplicate.jsonl").write_text(path.read_text())
    assert logger.parse_subagents(tmp_path) == {
        "subagent_count": 3, "subagent_input_tokens": 3_000_000,
        "subagent_output_tokens": 3_000_000, "subagent_ant_cost": 6,
        "subagent_proxy_cost": 9.22, "subagent_total_cost": 15.22,
    }


@pytest.mark.parametrize("proxy,cost", [(False, 6), (True, 4)])
def test_unknown_model_keeps_warning_and_plane_specific_estimate(monkeypatch, tmp_path, proxy, cost):
    monkeypatch.setenv("CLAUDE_CLIENT_USE_PROXY", str(int(proxy)))
    logger = load("scripts/session-cost-logger.py")
    transcript(tmp_path, ["unknown-model"])
    with pytest.warns(UserWarning, match="Unknown sub-agent model"):
        result = logger.parse_subagents(tmp_path)
    assert result["subagent_proxy_cost"] == cost
    assert result["subagent_ant_cost"] == 0
    assert result["subagent_total_cost"] == cost


@pytest.mark.parametrize("model", ["kimi-k3", "accounts/fireworks/models/kimi-k3",
                                  "fireworks_ai/accounts/fireworks/models/kimi-k3"])
def test_historical_candidate_model_cost_is_not_dispatch_authority(tmp_path, model):
    logger = load("scripts/session-cost-logger.py")
    transcript(tmp_path, [model])
    result = logger.parse_subagents(tmp_path)
    assert result["subagent_ant_cost"] == 0
    assert result["subagent_proxy_cost"] == 18
    assert result["subagent_total_cost"] == 18


@pytest.mark.parametrize("model", ["minimax-m3", "accounts/fireworks/models/minimax-m3",
                                  "fireworks_ai/accounts/fireworks/models/minimax-m3",
                                  "minimax/minimax-m3", "openrouter/minimax/minimax-m3",
                                  "tier-2-agentic", "tier-2-agentic-pro",
                                  "tier-2-agentic-pro-fallback-openrouter"])
def test_ordinary_agentic_reuses_existing_m3_rate_and_provider_bucket(tmp_path, model):
    logger = load("scripts/session-cost-logger.py")
    transcript(tmp_path, [model])
    result = logger.parse_subagents(tmp_path)
    assert result["subagent_ant_cost"] == 0
    assert result["subagent_proxy_cost"] == 1.5
    assert result["subagent_total_cost"] == 1.5
