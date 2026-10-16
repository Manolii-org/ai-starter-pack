"""Qualification must not confuse denied startup, malformed output or leaks with pass."""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('isolation_probe', ROOT / 'tests/codex_review_isolation_probe.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize('output,exit_code,qualified', [
    ({'private_file': True, 'symlink': True, 'process_environment': True, 'descriptor_alias': True, 'network': True}, 0, True),
    ({'private_file': True, 'symlink': False, 'process_environment': True, 'descriptor_alias': True, 'network': True}, 0, False),
    ({'private_file': True, 'symlink': True, 'process_environment': True, 'descriptor_alias': True, 'network': True}, 1, False),
    ({'private_file': True, 'symlink': True, 'process_environment': True}, 0, False),
    ({'private_file': True, 'symlink': True, 'process_environment': True, 'descriptor_alias': True, 'network': 'true'}, 0, False),
    ('not JSON', 0, False),
])
def test_subprocess_boundary_requires_all_proofs(tmp_path, monkeypatch, output, exit_code, qualified):
    monkeypatch.setenv('SYNTHETIC_SECRET_TOKEN', 'must-not-reach-subprocess')
    def run(command, **kwargs):
        assert 'SYNTHETIC_SECRET_TOKEN' not in kwargs['env']
        assert kwargs['env']['CODEX_HOME'] not in command[-1]
        assert command[command.index('sandbox') + 1] == '--'
        return subprocess.CompletedProcess(command, exit_code,
            json.dumps(output) if isinstance(output, dict) else output, 'startup diagnostic')
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.qualify(tmp_path / 'codex')['qualified'] is qualified


@pytest.mark.parametrize('failure,reason', [
    (subprocess.TimeoutExpired('synthetic', 1), 'sandbox_timeout'),
    (FileNotFoundError('synthetic missing executable'), 'sandbox_unavailable'),
])
def test_unavailable_sandbox_never_qualifies(tmp_path, monkeypatch, failure, reason):
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr(probe.subprocess, 'run', run)
    report = probe.qualify(tmp_path / 'codex')
    assert report['qualified'] is False and report['reason'] == reason


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux proc/network qualification')
def test_real_attack_program_fails_in_unsandboxed_control(tmp_path):
    # This runs the actual attack program against a readable sentinel. Failure
    # here demonstrates the program does not manufacture successful denials.
    for name in ('source', 'private'):
        (tmp_path / name).mkdir()
    (tmp_path / 'source/allowed.txt').write_text('source-visible')
    (tmp_path / 'private/sentinel').write_text(probe.DESCRIPTOR_CANARY)
    (tmp_path / 'source/private-link').symlink_to(tmp_path / 'private/sentinel')
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        with (tmp_path / 'private/sentinel').open('rb') as descriptor:
            result = subprocess.run(['python3', '-c', probe.PROBE, str(tmp_path),
                                     str(server.getsockname()[1]), str(descriptor.fileno())],
                                    capture_output=True, stdin=subprocess.DEVNULL, text=True,
                                    pass_fds=(descriptor.fileno(),), check=False)
    assert result.returncode == 1 and result.stderr == ''
    report = json.loads(result.stdout)
    assert report == {'private_file': False, 'symlink': False, 'process_environment': False,
                      'descriptor_alias': False, 'network': False}


def test_probe_workflow_never_gets_credentials_or_calls_a_model():
    text = (ROOT / '.github/workflows/codex-review-isolation-proof.yml').read_text()
    workflow = yaml.safe_load(text)
    assert workflow['permissions'] == {'contents': 'read'}
    assert 'secrets.' not in text and 'exec review' not in text
    job = workflow['jobs']['prove']
    assert job['timeout-minutes'] == 6
    assert all(step.get('with', {}).get('persist-credentials') is False
               for step in job['steps'] if str(step.get('uses', '')).startswith('actions/checkout@'))
    assert '@openai/codex@0.151.0' in text
    assert 'if: always()' in text
