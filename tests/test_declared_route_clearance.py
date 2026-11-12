"""Invoke actual hook/linter processes with owner-declared route policy."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('model,provider,clearance,sensitivity,allowed', [
    ('sonnet', 'other', 'restricted_us_oss_ok', 'restricted_us_oss_ok', True),
    ('approved-route', 'other', 'restricted_us_oss_ok', 'restricted_us_oss_ok', True),
    ('claude-sonnet-5-5', 'anthropic', 'internal', 'restricted_us_oss_ok', False),
    ('claude-sonnet-5-5', 'anthropic', 'restricted_us_oss_ok', 'restricted_us_oss_ok', True),
    ('claude-sonnet-5-5', 'anthropic', ['public', 'internal', 'restricted_us_oss_ok'], 'restricted_us_oss_ok', True),
    ('claude-sonnet-5-5', 'anthropic', {'data_sensitivity_max': 'internal', 'data_clearance': ['restricted_us_oss_ok']}, 'restricted_us_oss_ok', False),
    ('fable', 'platform', 'restricted_us_oss_ok', 'restricted_us_oss_ok', True),
    ('fable', 'platform_intercepted', 'restricted_us_oss_ok', 'restricted_us_oss_ok', False),
    ('shared-backend', 'duplicate', 'restricted_us_oss_ok', 'restricted_us_oss_ok', False),
    ('high-route', 'duplicate', 'restricted_us_oss_ok', 'restricted_us_oss_ok', True),
    ('approved-route', 'other', {'data_sensitivity_max': []}, 'restricted_us_oss_ok', False),
    ('approved-route', 'other', {'data_clearance': [{}]}, 'restricted_us_oss_ok', False),
    ('approved-route', 'other', {'data_sensitivity_max': [], 'data_clearance': ['restricted_us_oss_ok']}, 'restricted_us_oss_ok', False),
    ('approved-route', 'other', {'data_clearance': [{}]}, 'restricted', False),
    ('fable', 'platform_bad_string', 'restricted_us_oss_ok', 'restricted_us_oss_ok', False),
    ('fable', 'platform_bad_list', 'restricted_us_oss_ok', 'restricted_us_oss_ok', False),
    ('fable', 'platform_bad_resolution', 'restricted_us_oss_ok', 'restricted_us_oss_ok', False),
    ('fable', 'platform_bad_target', 'restricted_us_oss_ok', 'restricted_us_oss_ok', False),
    ('fable', 'platform_bad_intercept', 'restricted_us_oss_ok', 'restricted_us_oss_ok', False),
    ('approved-route', 'other', 'internal', 'restricted_us_oss_ok', False),
    ('sonnet', 'other', 'restricted', 'restricted', False),
    ('sonnet', 'other', 'restricted', 'restricted_us_oss_ok', False),
    ('claude-sonnet-5-5', 'anthropic', 'restricted', 'restricted', False),
])
def test_declared_clearance_controls_hook_and_linter(tmp_path, model, provider, clearance, sensitivity, allowed):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in ('pre-tool-use.py', 'lint-agent-routing.py'):
        (scripts / name).write_bytes((ROOT / 'scripts' / name).read_bytes())
    agents = tmp_path / '.claude/agents'
    agents.mkdir(parents=True)
    agent = agents / 'fixture.md'
    agent.write_text(f'---\nname: fixture\nmodel: {model}\ndata_sensitivity: {sensitivity}\n---\nReview supplied source.')
    meta = (clearance if isinstance(clearance, dict) else
            {'data_clearance': clearance} if isinstance(clearance, list) else
            {'data_sensitivity_max': clearance})
    cfg = {
        'data_classification': {'public': {}, 'internal': {}, 'restricted_us_oss_ok': {}, 'restricted': {}},
        'tier_definitions': {model: {'provider': provider, **meta}},
        'agent_routing': {'fixture': model},
    }
    if provider.startswith('platform'):
        cfg['tier_definitions'] = {}
        cfg['platforms'] = {'configured_host': {'available_models': [model], **meta}}
        if provider == 'platform_intercepted':
            cfg['proxy_intercepted_models'] = {model: {'data_sensitivity_max': 'internal'}}
        platform = cfg['platforms']['configured_host']
        if provider == 'platform_bad_string':
            platform['available_models'] = model
        if provider == 'platform_bad_list':
            platform['available_models'] = [{}]
        if provider == 'platform_bad_resolution':
            platform['alias_resolution'] = []
        if provider == 'platform_bad_target':
            platform['alias_resolution'] = {model: {}}
        if provider == 'platform_bad_intercept':
            cfg['proxy_intercepted_models'] = {model: 'malformed route'}
    if provider == 'duplicate':
        cfg['tier_definitions'] = {
            'low-route': {'model': 'shared-backend', 'data_sensitivity_max': 'internal'},
            'high-route': {'model': 'shared-backend', **meta},
        }
    (tmp_path / '.claude/model-routing.json').write_text(json.dumps(cfg))
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_path))
    env.pop('CLAUDE_PLUGIN_ROOT', None)
    request = {'tool_name': 'Agent', 'tool_input': {
        'subagent_type': 'fixture', 'model': model,
        'prompt': 'Review supplied source.\n\nSCOPE_BUDGET:\nallowed_paths: README.md\n',
    }}
    hook = subprocess.run([sys.executable, str(scripts / 'pre-tool-use.py')],
        input=json.dumps(request), text=True, capture_output=True, cwd=tmp_path, env=env, timeout=15)
    assert hook.returncode == 0, hook.stderr
    decision = json.loads(hook.stdout) if hook.stdout.strip() else {}
    assert (decision.get('decision') != 'block') is allowed, decision
    lint = subprocess.run([sys.executable, str(scripts / 'lint-agent-routing.py'), str(agent)],
        text=True, capture_output=True, cwd=tmp_path, env=env, timeout=15)
    assert (lint.returncode == 0) is allowed, lint.stdout + lint.stderr
    if not allowed:
        assert 'Anthropic-native' not in lint.stdout
