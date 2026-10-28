"""Every full Claude model ID the guard hook accepts must also be routable by
the bundled LiteLLM proxy and priced by the session cost logger. When the
Claude 5 family was added to the hook alone, proxied dispatch of an accepted ID
failed as an unknown model and its sub-agent spend was booked at the Haiku
proxy rate."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
# Routed through the proxy on purpose (OSS-intercepted Haiku); not direct spend.
PROXY_INTERCEPTED = {"claude-haiku-4-5-20251001"}


def _guard_claude_ids() -> set[str]:
    tree = ast.parse((ROOT / "scripts/pre-tool-use.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "ANTHROPIC_MODELS" for t in node.targets):
            return {m for m in ast.literal_eval(node.value) if m.startswith("claude-")}
    raise AssertionError("ANTHROPIC_MODELS not found")


def _cost_logger(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
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
