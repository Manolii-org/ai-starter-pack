"""Response replacement contracts; every provider call is an in-memory fixture."""
import asyncio
import copy
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def guard(monkeypatch):
    fake = ModuleType('litellm')
    class Unavailable(Exception):
        def __init__(self, **kwargs):
            super().__init__(kwargs['message'])
    fake.ServiceUnavailableError = Unavailable
    async def forbidden(**kwargs):
        pytest.fail('unmocked provider call')
    fake.acompletion = forbidden
    logger = ModuleType('litellm.integrations.custom_logger')
    logger.CustomLogger = object
    types = ModuleType('litellm.proxy._types')
    types.UserAPIKeyAuth = object
    for name, module in {'litellm': fake, 'litellm.integrations.custom_logger': logger,
                         'litellm.proxy._types': types}.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location('fallback_envelope', ROOT / 'deploy/litellm-proxy/sonnet_advisor_guardrail.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def objectify(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: objectify(v) for k, v in value.items()})
    if isinstance(value, list):
        return [objectify(v) for v in value]
    return value


def replacement(reason='stop'):
    return {'id': 'replacement-response', 'model': 'observed-replacement-model',
            'choices': [{'message': {'content': '{"findings":[]}'}, 'finish_reason': reason}],
            'usage': {'prompt_tokens': 17, 'completion_tokens': 8, 'total_tokens': 25}}


def primary(shape):
    data = {'id': 'primary-response', 'model': 'primary-model', 'usage': {'stale': 999}}
    if shape == 'messages':
        data.update(content=[], stop_reason='max_tokens', stop_sequence='stale')
    else:
        data['choices'] = [{'message': {'content': ''}, 'finish_reason': 'length'}]
    return data


def invoke(guard, monkeypatch, destination, fallback):
    sent = []
    async def call(**kwargs):
        sent.append(kwargs)
        return fallback
    monkeypatch.setattr(guard.litellm, 'acompletion', call)
    callback = guard.SonnetAdvisorGuardrail()
    callback._fallback_api_key = 'offline-fixture'
    result = asyncio.run(callback._inline_fallback({'messages': [], 'max_tokens': 2400}, destination))
    assert len(sent) == 1
    assert sent[0]['model'] == guard._FALLBACK_MODEL
    assert sent[0]['max_tokens'] == 2400 and sent[0]['timeout'] == 120
    return result


@pytest.mark.parametrize('shape', ['messages', 'chat'])
@pytest.mark.parametrize('objects', [False, True])
@pytest.mark.parametrize('reason,expected', [('stop', 'end_turn'), ('length', 'max_tokens')])
def test_replacement_carries_its_observed_completion_not_primary(guard, monkeypatch, shape, objects, reason, expected):
    dest = primary(shape)
    fb = replacement(reason)
    if objects:
        dest, fb = objectify(dest), objectify(fb)
    text = invoke(guard, monkeypatch, dest, fb)
    def get(item, key):
        return item[key] if isinstance(item, dict) else getattr(item, key)
    assert text == '{"findings":[]}'
    assert get(dest, 'id') == 'replacement-response'
    assert get(dest, 'model') == 'observed-replacement-model'
    usage = get(dest, 'usage')
    if shape == 'messages':
        assert get(dest, 'stop_reason') == expected
        assert get(dest, 'stop_sequence') is None
        assert get(usage, 'input_tokens') == 17 and get(usage, 'output_tokens') == 8
    else:
        assert get(get(dest, 'choices')[0], 'finish_reason') == reason
        assert get(usage, 'total_tokens') == 25


@pytest.mark.parametrize('bad', ['missing-stop', 'empty', 'malformed', 'tool-calls'])
def test_unsupported_or_unproven_completion_fails_closed(guard, monkeypatch, bad):
    fb = replacement()
    if bad == 'missing-stop':
        del fb['choices'][0]['finish_reason']
    elif bad == 'empty':
        fb['choices'][0]['message']['content'] = ''
    elif bad == 'tool-calls':
        fb['choices'][0]['finish_reason'] = 'tool_calls'
    else:
        fb['choices'] = [{}]
    dest = primary('messages')
    original = copy.deepcopy(dest)
    with pytest.raises(guard.litellm.ServiceUnavailableError):
        invoke(guard, monkeypatch, dest, objectify(fb))
    assert dest == original


def test_missing_usage_model_and_id_remain_unknown(guard, monkeypatch):
    fb = replacement()
    for field in ('usage', 'model', 'id'):
        del fb[field]
    dest = primary('messages')
    invoke(guard, monkeypatch, dest, objectify(fb))
    assert dest['usage'] is None and dest['model'] is None and dest['id'] is None


def test_messages_replacement_into_chat_translates_observed_metadata(guard, monkeypatch):
    fb = {'content': [{'type': 'text', 'text': 'replacement'}], 'stop_reason': 'max_tokens',
          'usage': {'input_tokens': 4, 'output_tokens': 3}, 'model': 'observed-model'}
    dest = primary('chat')
    assert invoke(guard, monkeypatch, dest, fb) == 'replacement'
    assert dest['choices'][0]['finish_reason'] == 'length'
    assert dest['usage'] == {'prompt_tokens': 4, 'completion_tokens': 3, 'total_tokens': 7}


@pytest.mark.parametrize('shape', ['empty-choices', 'multiple-choices', 'unknown'])
@pytest.mark.parametrize('objects', [False, True])
def test_invalid_destination_is_not_partially_relabelled(guard, monkeypatch, shape, objects):
    dest = primary('chat')
    if shape == 'empty-choices':
        dest['choices'] = []
    elif shape == 'multiple-choices':
        dest['choices'].append(copy.deepcopy(dest['choices'][0]))
    else:
        del dest['choices']
    if objects:
        dest = objectify(dest)
    before = copy.deepcopy(dest)
    with pytest.raises(guard.litellm.ServiceUnavailableError):
        invoke(guard, monkeypatch, dest, objectify(replacement()))
    assert dest == before


@pytest.mark.parametrize('shape', ['messages', 'chat'])
@pytest.mark.parametrize('frozen', [False, True])
@pytest.mark.parametrize('initial_usage', [None, {'prompt_tokens': 2, 'completion_tokens': 3}])
def test_real_typed_envelope_serialization_and_attribute_contract(guard, monkeypatch, shape, frozen, initial_usage):
    from pydantic import BaseModel, ConfigDict
    class Usage(BaseModel):
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        total_tokens: int = 0
        input_tokens: int | None = None
        output_tokens: int | None = None
    class TextBlock(BaseModel):
        type: str
        text: str
    class Message(BaseModel):
        content: str
    class Choice(BaseModel):
        message: Message
        finish_reason: str
    class Chat(BaseModel):
        model_config = ConfigDict(frozen=frozen)
        choices: list[Choice]
        usage: Usage | None
        model: str | None
        id: str | None
    class Messages(BaseModel):
        model_config = ConfigDict(frozen=frozen)
        content: list[TextBlock]
        usage: Usage | None
        model: str | None
        id: str | None
        stop_reason: str
        stop_sequence: str | None = None
    data = primary(shape)
    data['usage'] = initial_usage
    cls = Messages if shape == 'messages' else Chat
    dest = cls.model_validate(data)
    invoke(guard, monkeypatch, dest, objectify(replacement()))
    assert isinstance(dest.usage, Usage)
    assert dest.model_dump_json(warnings='error')
    if shape == 'messages':
        assert dest.content[0].text == '{"findings":[]}'
        assert dest.usage.input_tokens == 17 and dest.stop_reason == 'end_turn'
    else:
        assert dest.choices[0].message.content == '{"findings":[]}'
        assert dest.usage.prompt_tokens == 17 and dest.choices[0].finish_reason == 'stop'


def test_primary_hidden_generation_metadata_is_not_relabelled_as_replacement(guard, monkeypatch):
    dest = primary('chat')
    dest['_hidden_params'] = {'model_id': 'primary-deployment', 'response_cost': 99}
    invoke(guard, monkeypatch, dest, objectify(replacement()))
    assert dest['_hidden_params'] == {}  # Unknown physical-call cost, never zero.


@pytest.mark.parametrize('shape', ['chat', 'messages'])
@pytest.mark.parametrize('total', [None, 25, 999, False])
def test_missing_total_is_derived_only_from_observed_counts_and_conflict_refuses(guard, monkeypatch, total, shape):
    fb = replacement()
    if total is None:
        del fb['usage']['total_tokens']
    else:
        fb['usage']['total_tokens'] = total
    dest = primary(shape)
    if total is False or total == 999:
        before = copy.deepcopy(dest)
        with pytest.raises(guard.litellm.ServiceUnavailableError):
            invoke(guard, monkeypatch, dest, objectify(fb))
        assert dest == before
    else:
        invoke(guard, monkeypatch, dest, objectify(fb))
        if shape == 'chat':
            assert dest['usage']['total_tokens'] == 25
        else:
            assert dest['usage'] == {'input_tokens': 17, 'output_tokens': 8}


def test_owning_ci_installs_bounded_typed_envelope_dependency():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/plugin-eval-gate.yml').read_text())
    commands = [step.get('run', '') for job in workflow['jobs'].values() for step in job.get('steps', [])]
    assert any('pip install' in command and '"pydantic>=2,<3"' in command for command in commands)
    config = yaml.safe_load((ROOT / 'copier.yml').read_text())
    assert 'tests/test_advisor_fallback_envelope.py' in config['_exclude']
