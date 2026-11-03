"""Exercise distributed template transports with synthetic credentials only."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "scripts"


def load(name, monkeypatch):
    spec = importlib.util.spec_from_file_location("template_" + name.replace("-", "_"), TEMPLATES / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    for key in ("CLIENT_AI_POLICY", "LLM_API_KEY", "LITELLM_MASTER_KEY", "ANTHROPIC_API_KEY",
                "ANTHROPIC_DIRECT_API_KEY", "LITELLM_PROXY_URL", "ANTHROPIC_BASE_URL", "ANTHROPIC_DIRECT_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("name", ["run-pr-classifier", "run-specialists", "run-judge"])
def test_endpoints_require_proxy_and_never_use_direct_credentials(name, monkeypatch):
    module = load(name, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-synthetic")
    monkeypatch.setenv("ANTHROPIC_DIRECT_API_KEY", "sk-ant-synthetic")
    with pytest.raises(module.RouteAdmissionError, match="review.*denied"):
        module._endpoint()
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    assert module._endpoint()[0] == ""
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-key")
    assert module._endpoint() == ("synthetic-proxy-key", "https://proxy.example/v1/messages", True)
    if name != "run-pr-classifier":
        with pytest.raises(module.RouteAdmissionError, match="review.*denied"):
            module._endpoint(direct=True)
    monkeypatch.setenv("CLIENT_AI_POLICY", "first-party-required")
    with pytest.raises(module.RouteAdmissionError, match="review.*denied"):
        module._endpoint()


@pytest.mark.parametrize("name", ["run-pr-classifier", "run-specialists", "run-judge"])
@pytest.mark.parametrize("base", ["https://api.anthropic.com", "https://API.ANTHROPIC.COM./", "http://proxy.example", "https://" + "user:password" + "@proxy.example"])
def test_unsafe_endpoint_is_unavailable(name, base, monkeypatch):
    module = load(name, monkeypatch)
    monkeypatch.setenv("LITELLM_PROXY_URL", base)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-key")
    with pytest.raises(module.RouteAdmissionError, match="review.*denied"):
        module._endpoint()


@pytest.mark.parametrize("first_party,policy", [(True, False), (False, True)])
def test_broad_direct_only_review_leaves_incomplete_marker(first_party, policy, monkeypatch):
    module = load("run-broad-agents", monkeypatch)
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    if policy:
        monkeypatch.setenv("CLIENT_AI_POLICY", "first-party-required")
    transport = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(module, "_urlopen_https", transport)
    agent = module.AgentConfig("security", "sonnet", "internal", "system", "instructions", first_party=first_party)
    assert module.invoke_agent(agent, "synthetic-proxy-key", "synthetic diff") == {
        "source": "security", "findings": [], "first_party": True, "skipped": "route_policy_denied"}
    transport.assert_not_called()


def test_broad_proxy_request_uses_alias_and_bearer(monkeypatch):
    module = load("run-broad-agents", monkeypatch)
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    captured = []
    def transport(request, **kwargs):
        captured.append(request)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"content": [{"type": "text", "text": '{"findings": []}'}]}).encode()
        return response
    monkeypatch.setattr(module, "_urlopen_https", transport)
    agent = module.AgentConfig("architecture", "sonnet", "internal", "system", "instructions")
    result = module.invoke_agent(agent, "synthetic-proxy-key", "synthetic diff")
    assert result["findings"] == []
    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == "https://proxy.example/v1/messages"
    assert request.get_header("Authorization") == "Bearer synthetic-proxy-key"
    assert request.get_header("X-api-key") is None
    assert json.loads(request.data)["model"] == "sonnet"


@pytest.mark.parametrize("base,key", [("", "synthetic"), ("https://api.anthropic.com", "synthetic"),
                                     ("https://proxy.example", "Bearer sk-ant-synthetic")])
def test_broad_never_dispatches_without_safe_proxy(base, key, monkeypatch):
    module = load("run-broad-agents", monkeypatch)
    monkeypatch.setenv("LITELLM_PROXY_URL", base)
    transport = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(module, "_urlopen_https", transport)
    agent = module.AgentConfig("architecture", "sonnet", "internal", "system", "instructions")
    result = module.invoke_agent(agent, key, "synthetic diff")
    assert result["findings"] == []
    assert result["skipped"] == ("invalid_proxy_credential" if base == "https://proxy.example" else "route_policy_denied")
    transport.assert_not_called()


@pytest.mark.parametrize("base,key,policy", [("", "synthetic", ""), ("https://api.anthropic.com", "synthetic", ""),
    ("https://proxy.example", "sk-ant-synthetic", ""), ("https://proxy.example", "synthetic", "required")])
def test_stop_critic_rejects_direct_paths_before_client_creation(base, key, policy, monkeypatch):
    text = (TEMPLATES / "session-stop-checklist.sh").read_text()
    section = text[text.index('_run_intent_drift() {'):]
    code = section.split("python3 - <<'PYEOF'", 1)[1].split("\n", 1)[1].split("\nPYEOF", 1)[0]
    client = Mock(side_effect=AssertionError("must not create client"))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=client))
    monkeypatch.setenv("LITELLM_PROXY_URL", base)
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("CLIENT_AI_POLICY", policy)
    with pytest.raises(RuntimeError, match="requires"):
        exec(compile(code, "template-stop-critic", "exec"), {})
    client.assert_not_called()


def test_stop_critic_preserves_proxy_and_uses_alias(monkeypatch):
    text = (TEMPLATES / "session-stop-checklist.sh").read_text()
    section = text[text.index('_run_intent_drift() {'):]
    code = section.split("python3 - <<'PYEOF'", 1)[1].split("\n", 1)[1].split("\nPYEOF", 1)[0]
    create = Mock(return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text='{"classification":"on-scope"}')]))
    client = Mock(return_value=SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=client))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "Bearer <placeholder>")
    exec(compile(code, "template-stop-critic", "exec"), {})
    assert client.call_count == 1
    assert client.call_args.kwargs == {"api_key": "<placeholder>", "base_url": "https://proxy.example"}
    assert create.call_args.kwargs["model"] == "sonnet"
    assert create.call_args.kwargs["timeout"] == 15.0
    assert __import__("os").environ["ANTHROPIC_BASE_URL"] == "https://proxy.example"


def test_classifier_dispatch_guidance_keeps_restricted_data_off_models(monkeypatch):
    module = load("classify-message", monkeypatch)
    routing = json.loads((ROOT / ".claude/model-routing.json").read_text())
    rule = module._build_dispatch_rule(routing)
    assert "no-model=restricted governance no-AI" in rule
    assert "never dispatch restricted data to any model" in rule
    assert "restricted/client" not in rule
    assert "haiku=" in rule and "sonnet=" in rule
