"""Provider-free production-interface adapter tests; no reviewer qualification."""
import copy
import importlib.util
import json
from pathlib import Path
import socket
import sys
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('deep_adapter_fixtures', ROOT / 'tests/test_broad_agents_complete_input.py')
common = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = common
_spec.loader.exec_module(common)
runner, deep_repo, deep_output = common.runner, common.deep_repo, common.deep_output


@pytest.fixture(params=['', 'plugin/manolii-framework', 'registry/platform/framework'])
def interface(request, runner, monkeypatch):
    directory = request.param
    module = runner
    if directory:
        if not (ROOT / directory).is_dir():
            pytest.skip('generated Pack artifacts are not installed in consumers')
        spec = importlib.util.spec_from_file_location('adapter_' + directory.replace('/', '_'),
                                                    ROOT / directory / 'scripts/run-broad-agents.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    def denied(*args, **kwargs):
        pytest.fail('provider/network/credential access attempted')
    for name in ('get_api_key', '_proxy_base', '_urlopen_https'):
        monkeypatch.setattr(module, name, denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', denied)
    path = ROOT / directory / ('agents/security-deep-dive.md' if directory else '.claude/agents/security-deep-dive.md')
    role = module.parse_agent_file(path)
    capacities = {alias: 1_048_576 for alias in module._deep_leg_contract().ROUTES}
    return module, role, capacities


def packet_for(interface, deep_repo):
    module, _, _ = interface
    return module.build_security_deep_dive_input(deep_repo[0], **deep_repo[2])


def observed(request, packet, scores=(0.95, 0.3), *, stop='end_turn'):
    return {'leg': request['leg'], 'request_sha256': request['request_sha256'], 'state': 'response',
            'response': {'type': 'message', 'content': [{'type': 'text', 'text': json.dumps(deep_output(packet, scores))}],
                         'stop_reason': stop}}


def dispatch_offline(requests, dispatcher):
    """Injection exists ONLY in tests, never a production dispatch bypass."""
    return [dispatcher(row) for row in requests]


def test_real_seam_delivers_identical_complete_packet_and_preserves_each_leg(interface, deep_repo, monkeypatch):
    module, role, capacities = interface
    packet = packet_for(interface, deep_repo)
    requests = module.build_security_deep_dive_requests(packet, role, capacities=capacities)
    assert [r['leg'] for r in requests] == ['tier-1-fast', 'sonnet-internal']
    assert len({r['payload']['messages'][0]['content'] for r in requests}) == 1
    assert all(json.loads(r['payload']['messages'][0]['content']) == packet for r in requests)
    assert len({e['finding']['finding_id'] for e in packet['entries']}) == 1
    assert len({e['entry_id'] for e in packet['entries']}) == 2
    calls, parses = [], []
    original = module.parse_security_deep_dive_output
    def parse(*args, **kwargs):
        parses.append(kwargs['stop_reason'])
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'parse_security_deep_dive_output', parse)
    def offline(request):
        calls.append(copy.deepcopy(request))
        return observed(request, packet)
    outcomes = dispatch_offline(requests, offline)
    result = module.parse_security_deep_dive_responses(packet, role, outcomes, capacities=capacities)
    assert len(calls) == 2 and parses == ['end_turn', 'end_turn']
    assert result['status'] == 'provisional_complete' and result['disagreements'] == []
    assert result['escalation_required'] == [packet['entries'][0]['entry_id']]
    for leg in result['legs']:
        assert len(leg['result']['promoted']) == len(leg['result']['dismissed']) == 1
        assert leg['stop_reason'] == 'end_turn'
        assert leg['physical_attempts'] is None and leg['provider_execution'] == 'unknown'
        assert not {'source', 'findings', 'first_party', 'protected_review'} & leg.keys()
    assert result['provider_execution'] == result['effectiveness'] == 'unknown'
    assert not {'source', 'findings', 'first_party', 'protected_review'} & result.keys()
    with pytest.raises(ValueError):
        module._parse_findings(json.dumps(result))
    with pytest.raises(ValueError):
        module._deep_leg_contract().validate(result, 'security-deep-dive', packet['head'], packet['diff']['sha256'])


def test_disagreement_and_missing_context_are_not_voted_away(interface, deep_repo):
    module, role, capacities = interface
    packet = packet_for(interface, deep_repo)
    requests = module.build_security_deep_dive_requests(packet, role, capacities=capacities)
    replies = [observed(requests[0], packet), observed(requests[1], packet, (0.1, 0.8))]
    result = module.parse_security_deep_dive_responses(packet, role, replies, capacities=capacities)
    assert set(result['disagreements']) == {e['entry_id'] for e in packet['entries']}
    assert result['escalation_required'] == [packet['entries'][0]['entry_id']]
    answer = deep_output(packet)
    answer['entries'][0].update(decision='incomplete', true_positive_likelihood=None,
                                missing_context=['Validation dependency unavailable'])
    replies[1]['response']['content'][0]['text'] = json.dumps(answer)
    result = module.parse_security_deep_dive_responses(packet, role, replies, capacities=capacities)
    assert result['status'] == 'incomplete'
    assert len(result['legs'][1]['result']['incomplete']) == 1
    assert result['escalation_required'] == [packet['entries'][0]['entry_id']]


@pytest.mark.parametrize('mutation', ['missing', 'unknown', 'not_attempted', 'truncated', 'missing_stop',
                                    'omitted_entry', 'wrong_binding', 'malformed', 'empty', 'tool_block',
                                    'duplicate_key', 'error_envelope', 'error_type'])
def test_incomplete_leg_retains_other_leg_without_retry(interface, deep_repo, mutation, capsys):
    module, role, capacities = interface
    packet = packet_for(interface, deep_repo)
    requests = module.build_security_deep_dive_requests(packet, role, capacities=capacities)
    replies = [observed(row, packet) for row in requests]
    response = replies[0]['response']
    if mutation == 'missing':
        replies.pop(0)
    elif mutation in {'unknown', 'not_attempted'}:
        replies[0].pop('response')
        replies[0]['state'] = mutation
    elif mutation == 'truncated':
        response['stop_reason'] = 'max_tokens'
    elif mutation == 'missing_stop':
        response.pop('stop_reason')
    elif mutation == 'empty':
        response['content'] = []
    elif mutation == 'tool_block':
        response['content'] = [{'type': 'tool_use', 'name': 'unavailable'}]
    elif mutation == 'error_envelope':
        response['error'] = {'message': 'Untrusted diagnostic that must not be logged'}
    elif mutation == 'error_type':
        response['type'] = 'error'
    else:
        answer = deep_output(packet)
        if mutation == 'omitted_entry':
            answer['entries'].pop()
        elif mutation == 'wrong_binding':
            answer['input_sha256'] = '0' * 64
        text = json.dumps(answer)
        if mutation == 'malformed':
            text = 'Untrusted diagnostic that must not be logged {'
        elif mutation == 'duplicate_key':
            text = text[:-1] + ',"entries":[]}'
        response['content'][0]['text'] = text
    before = copy.deepcopy(replies)
    result = module.parse_security_deep_dive_responses(packet, role, replies, capacities=capacities)
    assert result['status'] == 'incomplete' and replies == before
    assert result['legs'][0]['result'] is None
    assert result['legs'][1]['result']['status'] == 'provisional_complete'
    assert all(leg['physical_attempts'] is None for leg in result['legs'])
    if mutation == 'truncated':
        assert result['legs'][0]['stop_reason'] == 'max_tokens'
    captured = capsys.readouterr()
    assert 'Untrusted diagnostic' not in json.dumps(result) + captured.out + captured.err


@pytest.mark.parametrize('mutation', ['unknown_alias', 'duplicate_leg', 'stale_request', 'invalid_state',
                                    'extra_metadata', 'changed_capacity', 'changed_role', 'changed_packet'])
def test_binding_and_observation_conflicts_fail_closed(interface, deep_repo, mutation):
    module, role, capacities = interface
    packet = packet_for(interface, deep_repo)
    requests = module.build_security_deep_dive_requests(packet, role, capacities=capacities)
    replies = [observed(row, packet) for row in requests]
    if mutation == 'unknown_alias':
        replies[0]['leg'] = 'unqualified'
    elif mutation == 'duplicate_leg':
        replies.append(copy.deepcopy(replies[0]))
    elif mutation == 'stale_request':
        replies[0]['request_sha256'] = '0' * 64
    elif mutation == 'invalid_state':
        replies[0]['state'] = []
    elif mutation == 'extra_metadata':
        replies[0]['invocation_id'] = 'not-authenticated'
    elif mutation == 'changed_capacity':
        capacities[requests[0]['leg']] += 1
    elif mutation == 'changed_role':
        role.system_prompt += ' Changed contract.'
    else:
        packet['sources'][0]['text'] += 'Changed immutable bytes\n'
    with pytest.raises(ValueError):
        module.parse_security_deep_dive_responses(packet, role, replies, capacities=capacities)


def test_exact_envelope_budget_and_API_checks(interface, deep_repo, monkeypatch):
    module, role, capacities = interface
    packet = packet_for(interface, deep_repo)
    requests = module.build_security_deep_dive_requests(packet, role, capacities=capacities)
    for row in requests:
        payload = row['payload']
        assert payload['max_tokens'] == 2848 and row['timeout_seconds'] == 90
        assert 'Bash(' not in payload['system'] and 'Write to `.ai/candidates' not in payload['system']
        assert all(check in payload['system'] for check in ['user-controlled', 'configuration-derived',
                   'hardcoded', 'validation', 'exploitation', 'Challenge', '0.7', '0.9', 'missing_context'])
        capacities[row['leg']] = len(json.dumps(payload).encode()) + payload['max_tokens']
    assert len(module.build_security_deep_dive_requests(packet, role, capacities=capacities)) == 2
    capacities[requests[0]['leg']] -= 1
    with pytest.raises(ValueError, match='capacity'):
        module.build_security_deep_dive_requests(packet, role, capacities=capacities)
    capacities[requests[0]['leg']] += 1
    monkeypatch.setattr(module, '_MAX_REQUEST_BYTES', len(json.dumps(requests[0]['payload']).encode()) - 1)
    with pytest.raises(ValueError, match='capacity'):
        module.build_security_deep_dive_requests(packet, role, capacities=capacities)


@pytest.mark.parametrize('mutation', ['missing_capacity', 'boolean_capacity', 'missing_contract',
                                    'duplicate_contract', 'wrong_role', 'no_first_party', 'restricted'])
def test_unavailable_contract_refuses_preparation(interface, deep_repo, mutation):
    module, role, capacities = interface
    packet = packet_for(interface, deep_repo)
    if mutation == 'missing_capacity':
        capacities.pop(next(iter(capacities)))
    elif mutation == 'boolean_capacity':
        capacities[next(iter(capacities))] = True
    elif mutation == 'missing_contract':
        role.instructions = 'No API contract'
    elif mutation == 'duplicate_contract':
        role.instructions += '\n' + role.instructions
    elif mutation == 'wrong_role':
        role.name = 'systems-consistency'
    elif mutation == 'no_first_party':
        role.first_party = False
    else:
        role.data_sensitivity = 'restricted'
    with pytest.raises(ValueError):
        module.build_security_deep_dive_requests(packet, role, capacities=capacities)


@pytest.mark.parametrize('client_policy', [False, True])
def test_preparation_does_not_wire_or_weaken_live_admission(interface, deep_repo, monkeypatch, client_policy):
    module, role, capacities = interface
    packet = packet_for(interface, deep_repo)
    module.build_security_deep_dive_requests(packet, role, capacities=capacities)
    monkeypatch.setenv('SECURITY_REVIEW_MODE', 'estate-ci-v1')
    if client_policy:
        monkeypatch.setenv('CLIENT_AI_POLICY', '1')
    else:
        monkeypatch.delenv('CLIENT_AI_POLICY', raising=False)
    monkeypatch.setattr(module, '_proxy_base', lambda: 'https://proxy.example')
    def denied():
        raise module.RouteAdmissionError('offline policy refusal')
    monkeypatch.setattr(module, 'require_direct_llm_admission', denied)
    result = module.invoke_agent(role, '', 'Complete supplied evidence')
    assert result['first_party'] is True and result['findings'] == []
    assert result['skipped'] == ('route_policy_denied' if client_policy else 'protected_review_incomplete')
    assert module._deep_leg_contract().QUALIFICATION_ROLES == {'security-boundary-test'}
