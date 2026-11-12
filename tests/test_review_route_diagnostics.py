"""Missing credentials name the selected transport, never a legacy role hint."""
import importlib.util
import logging
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('diagnostic_' + name.replace('-', '_'), ROOT / 'scripts' / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for key in ('GITHUB_ACTIONS', 'CLIENT_AI_POLICY', 'SECURITY_REVIEW_MODE', 'ANTHROPIC_API_KEY',
                'ANTHROPIC_DIRECT_API_KEY', 'LITELLM_MASTER_KEY', 'LLM_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LITELLM_PROXY_URL', 'https://router.example')


@pytest.mark.parametrize('mode,client,proxied,direct_expected', [
    ('governed', False, True, False),
    ('direct', False, False, True),
    ('direct', False, True, False),  # An admitted read-only proxy grant.
    ('governed', True, True, False),
])
def test_judge_missing_key_diagnostic_follows_transport(tmp_path, monkeypatch, caplog, mode, client, proxied, direct_expected):
    module = load('run-judge.py')
    monkeypatch.setenv('SECURITY_REVIEW_MODE', mode)
    if client:
        monkeypatch.setenv('CLIENT_AI_POLICY', 'engagement')
    else:
        url = 'https://router.example/v1/messages' if proxied else 'https://api.anthropic.com/v1/messages'
        monkeypatch.setattr(module, '_endpoint', lambda **_: ('', url, proxied))
    judge = module.Judge(str(tmp_path), 1, 'a' * 40)
    judge._has_first_party_candidates = True
    monkeypatch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('missing credential dispatched'))
    with caplog.at_level(logging.ERROR):
        assert judge.invoke_judge_agent([]) is None
    text = caplog.text
    assert ('ANTHROPIC_DIRECT_API_KEY' in text) is direct_expected
    if client:
        assert 'compatible declared route' in text
    elif not direct_expected:
        assert 'router credential unavailable' in text
        assert 'Anthropic' not in text


@pytest.mark.parametrize('proxied', [True, False])
def test_specialist_legacy_hint_uses_actual_transport_for_diagnostic(monkeypatch, proxied):
    module = load('run-specialists.py')
    url = 'https://router.example/v1/messages' if proxied else 'https://api.anthropic.com/v1/messages'
    monkeypatch.setattr(module, '_endpoint', lambda **_: ('', url, proxied))
    with pytest.raises(RuntimeError) as error:
        module._call_api('Review', 'Diff', 'sonnet', 800, first_party=True)
    assert ('ANTHROPIC_DIRECT_API_KEY' in str(error.value)) is (not proxied)
    if proxied:
        assert 'router credential unavailable' in str(error.value)


def test_specialist_readonly_proxy_missing_key_never_requests_direct_key(tmp_path, monkeypatch, capsys):
    module = load('run-specialists.py')
    monkeypatch.setenv('SECURITY_REVIEW_MODE', 'direct')
    monkeypatch.setattr(module, '_load_skill', lambda _: ({'model': 'sonnet', 'first_party': True}, 'Review'))
    monkeypatch.setattr(module, '_endpoint', lambda **_: ('', 'https://router.example/v1/messages', True))
    monkeypatch.setattr(module, '_call_api', lambda *a, **k: pytest.fail('missing credential dispatched'))
    _, error = module._invoke_skill('security-boundary-test', 'Diff', tmp_path)
    text = capsys.readouterr().out + error
    assert 'router credential unavailable' in text
    assert 'ANTHROPIC_DIRECT_API_KEY' not in text
