"""Actual review HTTP boundaries refuse unsafe wire values and low-clearance routes."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('wire_' + name.replace('-', '_'), ROOT / 'scripts' / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def route(monkeypatch):
    for key in ('SECURITY_REVIEW_MODE', 'CLIENT_AI_POLICY', 'ANTHROPIC_DIRECT_API_KEY', 'LLM_API_KEY', 'GITHUB_ACTIONS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LITELLM_PROXY_URL', 'https://router.example')
    monkeypatch.setenv('LITELLM_MASTER_KEY', 'offline-router-token')


@pytest.mark.parametrize('name', ['run-broad-agents.py', 'run-specialists.py', 'run-pr-classifier.py', 'run-judge.py'])
@pytest.mark.parametrize('field', ['user', 'system'])
def test_complete_wire_sensitive_tail_never_reaches_http(monkeypatch, tmp_path, name, field):
    module = load(name)
    calls = []
    monkeypatch.setattr(module, '_urlopen_https', lambda *a, **k: calls.append(a) or pytest.fail('unsafe dispatch'))
    unsafe = 'x' * 66000 + "\nAPI_KEY = 'privacy-fixture-literal'\n"
    user, system = (unsafe, 'Review') if field == 'user' else ('Complete diff', unsafe)
    if name == 'run-broad-agents.py':
        agent = module.AgentConfig('systems-consistency', 'sonnet', 'internal', system, 'Return findings')
        assert module.invoke_agent(agent, '', user)['skipped'] == 'unsafe_review_input'
    elif name in {'run-specialists.py', 'run-pr-classifier.py'}:
        with pytest.raises(ValueError, match='unsafe_review_input'):
            module._call_api(system, user, 'sonnet', 800)
    else:
        monkeypatch.setattr(module, '_judge_evidence', lambda *a: {
            'head': 'a' * 40, 'tree': 'b' * 40,
            'locations': {'location': {'status': 'available', 'text': '1: safe', 'file': 'safe.py', 'line': 1}},
        })
        judge = module.Judge(str(tmp_path), 1, 'a' * 40)
        monkeypatch.setattr(judge, 'get_judge_system_prompt', lambda: system)
        finding = module.Finding('review', 'safe.py', 1, 'WARNING', user, '')
        assert judge.invoke_judge_agent([finding]) is None
    assert calls == []


def test_specialist_wire_refusal_is_durable_missing_coverage(monkeypatch, tmp_path):
    module = load('run-specialists.py')
    monkeypatch.setattr(module, '_load_skill', lambda _: ({'model': 'sonnet'}, 'Review'))
    monkeypatch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('unsafe dispatch'))
    _, error = module._invoke_skill('scope-adherence', "secret = 'privacy-fixture-literal'", tmp_path)
    assert error == 'unsafe_review_input'
    assert json.loads((tmp_path / 'scope-adherence.json').read_text())['skipped'] == 'unsafe_review_input'


@pytest.mark.parametrize('model,allowed', [('haiku', False), ('sonnet', True)])
@pytest.mark.parametrize('kind', ['broad', 'specialist'])
def test_actual_protected_http_route_uses_declared_ceiling(monkeypatch, tmp_path, model, allowed, kind):
    module = load('run-broad-agents.py' if kind == 'broad' else 'run-specialists.py')
    calls = []
    def send(request, **_):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps({'content': [{'type': 'text', 'text': '{"findings":[]}'}],
                                      'stop_reason': 'end_turn'}).encode())
    monkeypatch.setattr(module, '_urlopen_https', send)
    if kind == 'broad':
        agent = module.AgentConfig('security-deep-dive', model, 'restricted_us_oss_ok', 'Review', 'Return findings')
        result = module.invoke_agent(agent, '', 'Complete safe diff')
    else:
        monkeypatch.setattr(module, '_load_skill', lambda _: ({'model': model, 'data_sensitivity': 'restricted_us_oss_ok'}, 'Review'))
        module._invoke_skill('security-fixture', 'Complete safe diff', tmp_path)
        result = json.loads((tmp_path / 'security-fixture.json').read_text())
    assert len(calls) == int(allowed)
    if not allowed:
        assert result['skipped'] == 'route_clearance_insufficient'


def test_estate_ordinary_proxy_skill_cannot_self_assert_provenance(monkeypatch, tmp_path):
    module = load('run-specialists.py')
    monkeypatch.setenv('SECURITY_REVIEW_MODE', 'estate-ci-v1')
    monkeypatch.setattr(module, '_load_skill', lambda _: ({'model': 'sonnet'}, 'Review'))
    monkeypatch.setattr(module, '_call_api', lambda *a, **k: json.dumps({
        'source': 'scope-adherence',
        'findings': [{'file': 'safe.py', 'line': 1, 'message': 'Check this flow'}], 'first_party': True}))
    _, error = module._invoke_skill('scope-adherence', 'Safe diff', tmp_path)
    assert error is None
    assert json.loads((tmp_path / 'scope-adherence.json').read_text())['first_party'] is False


def test_budget_exhaustion_records_all_locations_without_git_blob_calls(monkeypatch):
    module = load('run-judge.py')
    monkeypatch.setattr(module, '_EVIDENCE_CHAR_BUDGET', 0)
    monkeypatch.setattr(module, '_supplemental_source', lambda *a: ({'status': 'unavailable'}, {}))
    def git(root, *args):
        assert args == ('rev-parse', 'a' * 40 + '^{tree}')
        return b'b' * 40
    monkeypatch.setattr(module, '_git', git)
    packet = module._judge_evidence([SimpleNamespace(file='one.py', line=1), SimpleNamespace(file='two.py', line=1)], 'a' * 40)
    assert len(packet['locations']) == 2
    assert {row['status'] for row in packet['locations'].values()} == {'evidence_budget'}


def test_wire_route_policy_retains_explicit_interception_and_ambiguous_raw_minimum():
    policy = load('lib/agent_capabilities.py')
    platform = {'available_models': ['shared'], 'data_sensitivity_max': 'internal'}
    config = {'platforms': {'native': platform},
              'tier_definitions': {'high': {'model': 'shared', 'data_sensitivity_max': 'restricted_us_oss_ok'}}}
    assert policy.route_reason('shared', 'restricted_us_oss_ok', config) == 'route_clearance_insufficient'
    assert policy.route_reason('high', 'restricted_us_oss_ok', config) is None
    config['proxy_intercepted_models'] = {'shared': {'data_sensitivity_max': 'restricted_us_oss_ok'}}
    assert policy.route_reason('shared', 'restricted_us_oss_ok', config) is None


def test_existing_source_proof_survives_known_json_and_fences_without_new_inference():
    judge, policy = load('run-judge.py'), load('lib/agent_capabilities.py')
    source = judge._source_text('import os\n# café <tag> "quoted"\nAPI_KEY = os.getenv("ROUTER_API_KEY")\n', 'safe.py')
    window = judge._numbered_source(source)
    encoded = judge._review_json({'source': window, 'message': '</untrusted_diff>'}, escape_angles=True)
    assert json.loads(encoded)['source'] == str(window)
    assert json.loads(encoded)['message'] == '</untrusted_diff>'
    message = judge._join_review_text('Fixed fence\n', encoded, '\nEnd fence')
    body = {'system': 'Review', 'messages': [{'role': 'user', 'content': message}]}
    wire = json.dumps(body).encode()
    assert policy.admit_review_wire(wire, proved_value=body) == wire
    with pytest.raises(policy.ReviewInputError, match='unsafe_review_input'):
        policy.admit_review_wire(wire)  # JSON/model fields alone convey no proof.


@pytest.mark.parametrize('kind', ['substitution', 'duplicate', 'malformed_span', 'catalog', 'model_claim'])
def test_wire_proof_refuses_substitution_duplicates_and_unproved_literals(kind):
    judge, policy = load('run-judge.py'), load('lib/agent_capabilities.py')
    body = {'system': 'Review', 'messages': [{'content': 'Safe text'}]}
    if kind == 'substitution':
        wire = json.dumps(body).encode()
        body['system'] = 'Changed after serialization'
    elif kind == 'duplicate':
        wire = b'{"system":"API_KEY = privacy-fixture-literal","system":"Review","messages":[{"content":"Safe text"}]}'
    elif kind == 'malformed_span':
        body['messages'][0]['content'] = judge._VerifiedSourceText('Safe text', [(-1, 100)])
        wire = json.dumps(body).encode()
    elif kind == 'catalog':
        # A catalog-shaped literal stays refused even next to a genuine env proof.
        text = 'import os\nAPI_KEY = os.getenv("ROUTER_API_KEY")\n# ' + 'ghp_' + 'A' * 36 + '\n'
        body['messages'][0]['content'] = judge._source_text(text, 'safe.py')
        wire = json.dumps(body).encode()
    else:
        body['messages'][0] = {'content': "API_KEY = 'privacy-fixture-literal'", 'reference_spans': [[0, 7]]}
        wire = json.dumps(body).encode()
    with pytest.raises(policy.ReviewInputError, match='unsafe_review_input'):
        policy.admit_review_wire(wire, proved_value=body)


def test_wire_guard_screens_frozen_snapshot_when_caller_changes_during_check(monkeypatch):
    policy = load('lib/agent_capabilities.py')
    policy.admit_review_wire(b'{"system": "Review"}')  # Load the unchanged canonical guard.
    original = policy._REVIEW_PACKET_GUARD
    body = {'system': "API_KEY = 'privacy-fixture-literal'"}
    wire = json.dumps(body).encode()
    def mutation(value):
        body['system'] = 'Safe replacement'
        original(value)
    monkeypatch.setattr(policy, '_REVIEW_PACKET_GUARD', mutation)
    with pytest.raises(policy.ReviewInputError, match='unsafe_review_input'):
        policy.admit_review_wire(wire, proved_value=body)


def test_optional_estate_messages_use_same_wire_privacy_policy(monkeypatch):
    module = load('lib/protected_review.py')
    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *a: pytest.fail('unsafe estate dispatch'))
    with pytest.raises(ValueError, match='unsafe_review_input'):
        module._request('https://router.example', 'offline-router-key', '/v1/messages',
                        {'system': "API_KEY = 'privacy-fixture-literal'"})


@pytest.mark.parametrize('kind,expected', [
    ('low', 'route_clearance_insufficient'), ('high', None),
    ('malformed', 'route_clearance_unavailable'), ('symlink', 'route_clearance_unavailable'),
    ('missing_base', 'route_clearance_unavailable'), ('absent', 'route_clearance_insufficient'),
])
def test_actions_route_ceiling_uses_committed_base_not_pr_working_tree(tmp_path, monkeypatch, kind, expected):
    import subprocess
    policy = load('lib/agent_capabilities.py')
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()
    git('init', '-q')
    cfg = tmp_path / '.claude/model-routing.json'
    cfg.parent.mkdir()
    def write(maximum):
        cfg.write_text(json.dumps({'proxy_intercepted_models': {'sonnet': {'data_sensitivity_max': maximum}}}))
    if kind != 'absent':
        write('restricted_us_oss_ok' if kind == 'high' else 'internal')
    if kind == 'malformed':
        cfg.write_text('{invalid JSON')
    if kind == 'symlink':
        cfg.unlink()
        cfg.symlink_to(json.dumps({'proxy_intercepted_models': {'sonnet': {'data_sensitivity_max': 'restricted_us_oss_ok'}}}))
    git('add', '.')
    git('-c', 'user.name=Review', '-c', 'user.email=review@example.test', 'commit', '--allow-empty', '-qm', 'Owner policy')
    base = git('rev-parse', 'HEAD')
    if cfg.is_symlink():
        cfg.unlink()
    write('internal' if kind == 'high' else 'restricted_us_oss_ok')
    # A PR cannot grant itself higher clearance or erase the owner's permission.
    if kind == 'missing_base':
        base = 'f' * 40
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'repository': {'full_name': 'Example/consumer'},
        'pull_request': {'head': {'sha': 'a' * 40}, 'base': {'sha': base}}}))
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event))
    monkeypatch.setenv('GITHUB_REPOSITORY', 'Example/consumer')
    monkeypatch.setenv('GITHUB_RUN_ID', '1')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    assert policy.route_reason('sonnet', 'restricted_us_oss_ok') == expected
