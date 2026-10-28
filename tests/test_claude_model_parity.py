"""Every full Claude model ID the guard hook accepts must also be routable by
the bundled LiteLLM proxy and priced by the session cost logger. When the
Claude 5 family was added to the hook alone, proxied dispatch of an accepted ID
failed as an unknown model and its sub-agent spend was booked at the Haiku
proxy rate."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
# Routed through the proxy on purpose (OSS-intercepted Haiku); not direct spend.
PROXY_INTERCEPTED = {"claude-haiku-4-5-20251001"}


def _literal(path: str, name: str):
    for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def _guard_anthropic_names() -> set[str]:
    """Every model name the guard accepts as Anthropic: full IDs and aliases."""
    return set(_literal("scripts/pre-tool-use.py", "ANTHROPIC_MODELS"))


def _guard_claude_ids() -> set[str]:
    return {m for m in _guard_anthropic_names() if m.startswith("claude-")}


def _alias_resolution() -> dict[str, str]:
    routing = json.loads((ROOT / ".claude/model-routing.json").read_text(encoding="utf-8"))
    aliases = routing["platforms"]["claude_code"]["alias_resolution"]
    return {k: v for k, v in aliases.items() if k != "note"}


def _list_prices() -> dict[str, dict]:
    """Anthropic list prices per MTok — the reference table in classify-message."""
    return _literal("scripts/classify-message.py", "_PRICING_DEFAULT")


def _cost_logger(monkeypatch, base_url: str | None = None):
    if base_url is None:
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
    spec = importlib.util.spec_from_file_location("session_cost_logger", ROOT / "scripts/session-cost-logger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_ids_have_proxy_routes() -> None:
    config = yaml.safe_load((ROOT / "deploy/litellm-proxy/config.yaml").read_text(encoding="utf-8"))
    routes = {m["model_name"] for m in config["model_list"]}
    assert _guard_claude_ids() - routes == set()


def test_guard_ids_are_priced_and_classified(monkeypatch) -> None:
    logger = _cost_logger(monkeypatch)
    ids = _guard_claude_ids()
    assert ids - set(logger._SUBAGENT_PRICING) == set()
    assert ids - PROXY_INTERCEPTED - logger._ANTHROPIC_DIRECT_MODELS == set()


def test_cost_logger_prices_match_list_prices(monkeypatch) -> None:
    """Main-thread and direct sub-agent rates both follow the list prices;
    review rounds kept finding one table updated and another stale."""
    logger = _cost_logger(monkeypatch)
    prices = _list_prices()
    for model in _guard_claude_ids() | {"haiku"}:
        expected = prices[model]
        if model.startswith("claude-"):
            main = logger._MAIN_PER_M[model]
            assert (main["in"], main["out"]) == (expected["in"], expected["out"]), model
        sub = logger._SUBAGENT_PRICING[model]
        got = (round(sub["input"] * 1_000_000, 6), round(sub["output"] * 1_000_000, 6))
        assert got == (expected["in"], expected["out"]), model


def test_every_accepted_name_is_priced_as_the_model_it_resolves_to(monkeypatch) -> None:
    """Aliases the guard accepts (sonnet, opus, fable) and the haiku alias must be
    priced as the Anthropic model they resolve to and counted as direct spend
    when no proxy is active. Review rounds kept finding one more name unpriced."""
    logger = _cost_logger(monkeypatch)
    prices, aliases = _list_prices(), _alias_resolution()
    for name in _guard_anthropic_names() | {"haiku"}:
        expected = prices[aliases.get(name, name)]
        sub = logger._SUBAGENT_PRICING[name]
        got = (round(sub["input"] * 1_000_000, 6), round(sub["output"] * 1_000_000, 6))
        assert got == (expected["in"], expected["out"]), name
        if name not in PROXY_INTERCEPTED:
            assert name in logger._ANTHROPIC_DIRECT_MODELS, name


def test_proxy_intercepted_aliases_are_proxy_spend(monkeypatch) -> None:
    logger = _cost_logger(monkeypatch, "https://litellm.example.test")
    for alias in ("haiku", "sonnet"):
        assert alias not in logger._ANTHROPIC_DIRECT_MODELS
    assert logger._SUBAGENT_PRICING["sonnet"]["input"] == 0.30 / 1_000_000
