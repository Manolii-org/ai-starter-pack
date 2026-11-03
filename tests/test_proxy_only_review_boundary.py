"""Exercise distributed template transports with synthetic credentials only."""
import importlib.util
import json
import runpy
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


@pytest.fixture
def stop_code(tmp_path, monkeypatch):
    monkeypatch.setenv("_DRIFT_GUARD_ROOT", str(ROOT))
    text = (TEMPLATES / "session-stop-checklist.sh").read_text()
    section = text[text.index('_run_intent_drift() {'):]
    code = section.split("python3 - <<'PYEOF'", 1)[1].split("\n", 1)[1].split("\nPYEOF", 1)[0]
    script = tmp_path / "stop_critic.py"
    script.write_text(code)
    return script


@pytest.mark.parametrize("base,key,policy", [("", "synthetic", ""), ("https://api.anthropic.com", "synthetic", ""),
    ("https://proxy.example", "sk-ant-synthetic", ""), ("https://proxy.example", "synthetic", "required"),
    ("https://proxy.example?extra=1", "synthetic", ""), ("https://proxy.example/#fragment", "synthetic", "")])
def test_stop_critic_rejects_direct_paths_before_client_creation(base, key, policy, monkeypatch, stop_code):
    transport = Mock(side_effect=AssertionError("must not create transport"))
    monkeypatch.setattr("urllib.request.build_opener", transport)
    monkeypatch.setenv("LITELLM_PROXY_URL", base)
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("CLIENT_AI_POLICY", policy)
    with pytest.raises(RuntimeError, match="requires"):
        runpy.run_path(str(stop_code))
    transport.assert_not_called()


def test_stop_critic_preserves_proxy_and_uses_only_proxy_auth(monkeypatch, stop_code, capsys):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"content": [
        {"type": "thinking", "thinking": "synthetic reasoning"},
        {"type": "text", "text": '{"classification":'},
        {"type": "text", "text": '"on-scope"}'},
    ]}).encode()
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr("urllib.request.build_opener", build)
    # Older SDKs independently inherit this first-party bearer credential.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-provider-auth")
    sdk = Mock(side_effect=AssertionError("SDK authentication must not run"))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=sdk))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "Bearer <placeholder>")
    runpy.run_path(str(stop_code))
    sdk.assert_not_called()
    assert "unavailable" not in capsys.readouterr().out
    assert opener.open.call_count == 1
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://proxy.example/v1/messages"
    assert request.get_header("Authorization") == "Bearer <placeholder>"
    assert request.get_header("X-api-key") is None
    assert "synthetic-provider-auth" not in str(request.header_items())
    assert json.loads(request.data)["model"] == "sonnet"
    assert 0 < opener.open.call_args.kwargs["timeout"] <= 15.0
    assert json.loads(request.data)["max_tokens"] == 4196
    redirect = build.call_args.args[0]
    assert redirect.redirect_request(request, None, 302, "redirect", {}, "https://other.example") is None
    assert __import__("os").environ["ANTHROPIC_BASE_URL"] == "https://proxy.example"


def test_classifier_dispatch_guidance_keeps_restricted_data_off_models(monkeypatch):
    module = load("classify-message", monkeypatch)
    routing = json.loads((ROOT / ".claude/model-routing.json").read_text())
    rule = module._build_dispatch_rule(routing)
    assert "no-model=restricted governance no-AI" in rule
    assert "never dispatch restricted data to any model" in rule
    assert "restricted/client" not in rule
    assert "haiku=" in rule and "sonnet=" in rule


@pytest.mark.parametrize("outcome", ["recovered", "starved", "deadline"])
def test_stop_critic_reasoning_retry_is_bounded(outcome, tmp_path, monkeypatch, capsys, stop_code):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-key")
    ticks = iter([100.0, 100.0, 116.0] if outcome == "deadline" else [100.0, 100.0, 101.0, 101.0, 102.0])
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    calls = []

    def transport(request, **kwargs):
        calls.append((request, kwargs))
        data = {"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": "synthetic"}]}
        if len(calls) == 2 and outcome == "recovered":
            data = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({
                "classification": "scope-creep", "reason": "synthetic scope expansion"})}]}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(data).encode()
        return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: SimpleNamespace(open=transport))
    runpy.run_path(str(stop_code))
    expected = 1 if outcome == "deadline" else 2
    assert len(calls) == expected
    assert [json.loads(request.data)["max_tokens"] for request, _ in calls] == [4196, 8392][:expected]
    assert [kwargs["timeout"] for _, kwargs in calls] == [15.0, 14.0][:expected]
    assert all(request.full_url == "https://proxy.example/v1/messages" for request, _ in calls)
    alerts = list((tmp_path / ".ai/drift-alerts").glob("*.json"))
    output = capsys.readouterr().out
    if outcome == "recovered":
        assert len(alerts) == 1
        assert json.loads(alerts[0].read_text())["classification"] == "scope-creep"
        assert "unavailable" not in output
    else:
        assert alerts == []
        assert "Critic unavailable; no scope verdict recorded" in output


@pytest.mark.parametrize("delay", ["late-result", "trickling-body"])
def test_stop_critic_rejects_late_results_and_interrupts_blocking_reads(delay, tmp_path, monkeypatch, capsys, stop_code):
    import signal
    import time

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-key")
    previous = signal.getsignal(signal.SIGALRM)
    original_timer = signal.setitimer
    armed = []
    if delay == "late-result":
        ticks = iter([100.0, 100.0, 116.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    else:
        def short_real_timer(kind, seconds, interval=0.0):
            armed.append(seconds)
            return original_timer(kind, 0.01 if seconds else 0.0, interval)
        monkeypatch.setattr(signal, "setitimer", short_real_timer)
    body = json.dumps({"content": [{"type": "text", "text": json.dumps({
        "classification": "scope-creep", "reason": "synthetic late response"})}], "stop_reason": "end_turn"}).encode()
    def read():
        if delay == "trickling-body":
            time.sleep(0.1)
        return body
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.side_effect = read
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: opener)
    runpy.run_path(str(stop_code))
    assert opener.open.call_count == 1
    assert not (tmp_path / ".ai/drift-alerts").exists()
    assert "Critic unavailable; no scope verdict recorded" in capsys.readouterr().out
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    if delay == "trickling-body":
        assert armed == [15.0, 0.0]


@pytest.mark.parametrize("field", ["_DRIFT_PROMPT", "_DRIFT_STATS"])
@pytest.mark.parametrize("kind", ["catalog", "assignment"])
def test_stop_critic_screens_sensitive_input_before_transport(field, kind, monkeypatch, stop_code):
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-key")
    # A catalog-shaped synthetic token, never a live credential.
    unsafe = ("Credential maintenance: ghp_" + "x" * 36 if kind == "catalog"
              else "SERVICE_API_KEY" + " = " + "synthetic-value-for-refusal")
    monkeypatch.setenv(field, unsafe)
    build = Mock(side_effect=AssertionError("sensitive input must not create transport"))
    monkeypatch.setattr("urllib.request.build_opener", build)
    with pytest.raises(ValueError):
        runpy.run_path(str(stop_code))
    build.assert_not_called()


def test_stop_critic_does_not_dispatch_without_its_privacy_guard(tmp_path, monkeypatch, stop_code):
    monkeypatch.setenv("_DRIFT_GUARD_ROOT", str(tmp_path / "missing-guard"))
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-key")
    build = Mock(side_effect=AssertionError("missing privacy guard must not create transport"))
    monkeypatch.setattr("urllib.request.build_opener", build)
    with pytest.raises((OSError, RuntimeError)):
        runpy.run_path(str(stop_code))
    build.assert_not_called()


def legacy_runner(name, tmp_path, monkeypatch, helper):
    path = tmp_path / 'scripts' / (name + '.py')
    path.parent.mkdir(exist_ok=True)
    path.write_bytes((TEMPLATES / (name + '.py')).read_bytes())
    if helper:
        policy = path.parent / 'lib/agent_capabilities.py'
        policy.parent.mkdir()
        policy.write_text('class RouteAdmissionError(ValueError): pass\n')
    if name == 'run-judge':
        companion = path.parent / 'lib/protected_review.py'
        companion.parent.mkdir(exist_ok=True)
        companion.write_bytes((TEMPLATES / 'lib/protected_review.py').read_bytes())
    spec = importlib.util.spec_from_file_location('legacy_' + name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('helper', [False, True])
@pytest.mark.parametrize('name', ['run-broad-agents', 'run-specialists'])
def test_legacy_hosted_runtime_refuses_before_transport(name, helper, tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    transport = Mock(side_effect=AssertionError('must not create transport'))
    monkeypatch.setattr('urllib.request.build_opener', transport)
    with pytest.raises(ValueError, match='candidate provenance runtime missing'):
        legacy_runner(name, tmp_path, monkeypatch, helper)
    transport.assert_not_called()


@pytest.mark.parametrize('helper', [False, True])
def test_legacy_local_broad_proxy_result_is_written(helper, tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    module = legacy_runner('run-broad-agents', tmp_path, monkeypatch, helper)
    agent = module.AgentConfig('systems-consistency', 'sonnet', 'internal', 'system', 'instructions')
    monkeypatch.setattr(module, 'parse_agent_file', lambda _: agent)
    monkeypatch.setattr(module, 'get_changed_files', lambda: [])
    monkeypatch.setattr(module, 'invoke_agent', lambda *a: {
        'source': agent.name, 'findings': [], '_assessment': {'attempt': 'forged'}})
    source = tmp_path / 'input.diff'
    source.write_text('+complete source\n')
    output = tmp_path / 'candidates'
    monkeypatch.setattr(sys, 'argv', ['run-broad-agents.py', '--agents', agent.name,
        '--diff', str(source), '--candidates-dir', str(output)])
    assert module.main() == 0
    assert json.loads((output / (agent.name + '.json')).read_text()) == {
        'source': agent.name, 'findings': []}


@pytest.mark.parametrize('helper', [False, True])
def test_legacy_local_specialist_writes_results_and_both_skip_markers(helper, tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    module = legacy_runner('run-specialists', tmp_path, monkeypatch, helper)
    monkeypatch.setenv('LITELLM_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('LLM_API_KEY', 'synthetic-proxy-key')
    monkeypatch.setattr(module, '_load_skill', lambda _: ({'model': 'haiku'}, 'instructions'))
    monkeypatch.setattr(module, '_call_api', lambda *a, **k: json.dumps({
        'source': 'ordinary', 'findings': [], '_assessment': {'attempt': 'forged'}}))
    output = tmp_path / 'candidates'
    output.mkdir()
    assert module._invoke_skill('ordinary', '+complete source\n', output) == ('ordinary', None)
    assert json.loads((output / 'ordinary.json').read_text()) == {'source': 'ordinary', 'findings': []}
    module._write_skip_marker('security-boundary-test', output, 'api_error')
    first_party = json.loads((output / 'security-boundary-test.json').read_text())
    assert first_party['first_party'] is True and first_party['skipped'] == 'api_error'
    module._write_skipped_marker('ordinary', output, 'api_error')
    assert json.loads((output / 'ordinary.json').read_text())['skipped'] == 'api_error'
    with pytest.raises(module.RouteAdmissionError):
        module.configured_direct_endpoint()


@pytest.mark.parametrize('helper', [False, True])
@pytest.mark.parametrize('hosted', [False, True])
def test_legacy_judge_local_compatibility_retains_hosted_provenance_guard(helper, hosted, tmp_path, monkeypatch):
    if hosted:
        monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    else:
        monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    module = legacy_runner('run-judge', tmp_path, monkeypatch, helper)
    output = tmp_path / 'candidates'
    output.mkdir()
    (output / 'ordinary.json').write_text(json.dumps({'source': 'ordinary', 'findings': []}))
    instance = module.Judge(str(output), 1, 'a' * 40)
    instance.judge_log_dir = tmp_path / 'judge-logs'
    seen = []
    monkeypatch.setattr(instance, '_post_no_findings_comment', lambda: seen.append('clean'))
    monkeypatch.setattr(instance, '_post_advisory_warning', lambda detail: seen.append('incomplete') or False)
    monkeypatch.setattr(module, '_urlopen_https', Mock(side_effect=AssertionError('no provider dispatch')))
    assert instance.run() == 0
    assert seen == (['incomplete'] if hosted else ['clean'])
    assert bool(instance._candidate_load_errors) is hosted
