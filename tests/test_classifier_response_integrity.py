"""Offline response completeness and routing integrity for the actual classifier."""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('classifier_integrity', ROOT / 'scripts/run-pr-classifier.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
NARROW = {'invoke_skills': ['scope-adherence'], 'invoke_agents': [], 'depth': 'narrow', 'skip_skills': [], 'reason': 'ordinary example'}
BROAD = {'invoke_skills': ['security-boundary-test'], 'invoke_agents': ['security-deep-dive'], 'depth': 'broad', 'skip_skills': [], 'reason': 'security boundary changed'}


def transport(monkeypatch, responses, proxied=True):
    monkeypatch.setattr(MODULE, '_endpoint', lambda: ('offline', 'https://proxy.example/v1/messages', proxied))
    requests = []
    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        assert kwargs['timeout'] == 60
        return io.BytesIO(json.dumps(responses[len(requests) - 1]).encode())
    monkeypatch.setattr(MODULE, '_urlopen_https', respond)
    return requests


def payload(text, stop='end_turn'):
    return {'content': [{'type': 'text', 'text': text}], 'stop_reason': stop}


def test_classifier_uses_all_blocks_and_final_security_routing(monkeypatch):
    response = {'stop_reason': 'end_turn', 'content': [
        {'type': 'text', 'text': 'Example: ' + json.dumps(NARROW) + '\nFinal: '},
        {'type': 'thinking', 'thinking': 'synthetic reasoning'},
        {'type': 'text', 'text': json.dumps(BROAD)},
    ]}
    requests = transport(monkeypatch, [response])
    result = MODULE._parse_manifest(MODULE._call_api('system', 'synthetic input', 'haiku', 800))
    assert result['depth'] == 'broad'
    assert result['invoke_skills'] == ['security-boundary-test']
    assert result['invoke_agents'] == ['security-deep-dive']
    assert len(requests) == 1


@pytest.mark.parametrize('proxied', [False, True])
def test_parseable_example_under_output_limit_never_routes_narrow(monkeypatch, proxied):
    response = payload(json.dumps(NARROW) + '\nFinal: {"invoke_skills":', 'max_tokens')
    requests = transport(monkeypatch, [response, response], proxied)
    with pytest.raises(ValueError, match='output limit'):
        MODULE._call_api('system', 'synthetic input', 'haiku', 800)
    expected = [800] if not proxied else [800 + MODULE._PROXY_THINKING_HEADROOM,
                                          2 * (800 + MODULE._PROXY_THINKING_HEADROOM)]
    assert [row['max_tokens'] for row in requests] == expected


def test_existing_proxy_retry_can_return_the_complete_security_manifest(monkeypatch):
    requests = transport(monkeypatch, [payload(json.dumps(NARROW), 'max_tokens'), payload(json.dumps(BROAD))])
    result = MODULE._parse_manifest(MODULE._call_api('system', 'synthetic input', 'haiku', 800))
    assert result['depth'] == 'broad' and result['invoke_agents'] == ['security-deep-dive']
    assert len(requests) == 2


@pytest.mark.parametrize('bad', [None, 'opaque', {'type': 'text', 'text': None}])
def test_later_malformed_block_cannot_leave_an_earlier_clean_example(monkeypatch, bad):
    response = {'content': [{'type': 'text', 'text': json.dumps(NARROW)}, bad], 'stop_reason': 'end_turn'}
    requests = transport(monkeypatch, [response])
    with pytest.raises(ValueError, match='block'):
        MODULE._call_api('system', 'synthetic input', 'haiku', 800)
    assert len(requests) == 1


@pytest.mark.parametrize('client_policy', [False, True])
def test_cli_output_limit_preserves_conservative_fallback_or_policy_refusal(monkeypatch, tmp_path, client_policy):
    response = payload(json.dumps(NARROW), 'max_tokens')
    requests = transport(monkeypatch, [response, response], proxied=not client_policy)
    if client_policy:
        monkeypatch.setenv('CLIENT_AI_POLICY', 'required')
    else:
        monkeypatch.delenv('CLIENT_AI_POLICY', raising=False)
    source = tmp_path / 'input.diff'
    source.write_text('diff --git a/auth.py b/auth.py\n@@ -1 +1 @@\n-permit = True\n+permit = False\n')
    output = tmp_path / 'manifest.json'
    monkeypatch.setattr(sys, 'argv', ['run-pr-classifier.py', '--diff', str(source), '--output', str(output)])
    if client_policy:
        with pytest.raises(SystemExit) as stopped:
            MODULE.main()
        assert stopped.value.code == 1 and not output.exists()
        assert len(requests) == 1
    else:
        MODULE.main()
        manifest = json.loads(output.read_text())
        assert manifest['depth'] == 'broad'
        assert 'security-boundary-test' in manifest['invoke_skills']
        assert 'security-deep-dive' in manifest['invoke_agents']
        assert len(requests) == 2
