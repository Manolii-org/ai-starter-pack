"""Exercise real SAST workflow commands with offline scanner/report fixtures."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ('pr-assessment.yml', 'pr-assessment-reusable.yml')


def run_step(workflow, name, tmp_path, **environment):
    spec = yaml.load((ROOT / '.github/workflows' / workflow).read_text(), Loader=yaml.BaseLoader)
    step = next(s for s in spec['jobs']['sast']['steps'] if s.get('name') == name)
    command = step['run'].replace('/tmp/', str(tmp_path) + '/')
    env = {**os.environ, 'BANDIT_REQUIRED': 'true', 'SEMGREP_EXIT': '0', 'BANDIT_EXIT': '0',
           'GITHUB_OUTPUT': str(tmp_path / 'outputs'), **environment}
    return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', command], cwd=tmp_path,
                          env=env, capture_output=True, text=True, timeout=10)


def reports(tmp_path):
    semgrep = {'version': '1.182.1', 'results': [], 'errors': [], 'paths': {'scanned': ['fixture.py']}}
    bandit = {'results': [], 'errors': [], 'metrics': {'_totals': {'loc': 1}, 'fixture.py': {'loc': 1}}}
    (tmp_path / 'semgrep-results.json').write_text(json.dumps(semgrep))
    (tmp_path / 'bandit-results.json').write_text(json.dumps(bandit))
    return semgrep, bandit


@pytest.mark.parametrize('workflow', WORKFLOWS)
@pytest.mark.parametrize('scanner', ['semgrep', 'bandit'])
@pytest.mark.parametrize('bad', ['missing', 'malformed', 'tool-error', 'missing-results', 'bad-row', 'missing-scope'])
def test_incomplete_report_never_becomes_successful_zero_findings(tmp_path, workflow, scanner, bad):
    data = dict(zip(['semgrep', 'bandit'], reports(tmp_path)))[scanner]
    target = tmp_path / (scanner + '-results.json')
    if bad == 'missing':
        target.unlink()
    elif bad == 'malformed':
        target.write_text('{')
    else:
        if bad == 'tool-error':
            data['errors'] = [{'message': 'synthetic scanner error'}]
        elif bad == 'missing-results':
            del data['results']
        elif bad == 'bad-row':
            data['results'] = [{}]
        else:
            data.pop('paths' if scanner == 'semgrep' else 'metrics')
        target.write_text(json.dumps(data))
    result = run_step(workflow, 'Merge SAST findings', tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / '.ai/sast-findings.json').exists()


@pytest.mark.parametrize('workflow', WORKFLOWS)
def test_complete_clean_reports_remain_valid(tmp_path, workflow):
    reports(tmp_path)
    result = run_step(workflow, 'Merge SAST findings', tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / '.ai/sast-findings.json').read_text()) == []


@pytest.mark.parametrize('workflow', WORKFLOWS)
def test_finding_exits_keep_all_entries_and_schema(tmp_path, workflow):
    semgrep, bandit = reports(tmp_path)
    s = {'check_id': 'fixture.rule', 'path': 'fixture.py', 'start': {'line': 3},
         'extra': {'message': 'Synthetic finding', 'severity': 'WARNING'}}
    b = {'test_id': 'B101', 'filename': 'fixture.py', 'line_number': 3,
         'issue_text': 'Synthetic assertion', 'issue_severity': 'LOW'}
    semgrep['results'] = [s, s]
    bandit['results'] = [b, b]
    for scanner, data in [('semgrep', semgrep), ('bandit', bandit)]:
        (tmp_path / (scanner + '-results.json')).write_text(json.dumps(data))
    result = run_step(workflow, 'Merge SAST findings', tmp_path, SEMGREP_EXIT='1', BANDIT_EXIT='1')
    assert result.returncode == 0, result.stderr
    rows = json.loads((tmp_path / '.ai/sast-findings.json').read_text())
    assert len(rows) == 4
    assert rows[0] == rows[1] and rows[2] == rows[3]
    assert rows[0] == {'finding_id': 'semgrep-fixture.rule-fixture.py', 'rule': 'fixture.rule',
                       'file': 'fixture.py', 'line': 3, 'message': 'Synthetic finding', 'severity': 'WARNING'}
    assert rows[2]['finding_id'] == 'bandit-B101-fixture.py'


@pytest.mark.parametrize('workflow', WORKFLOWS)
@pytest.mark.parametrize('scanner', ['semgrep', 'bandit'])
@pytest.mark.parametrize('status', [0, 1, 2, 125])
def test_native_status_is_captured_and_tool_errors_fail(tmp_path, workflow, scanner, status):
    fake = tmp_path / scanner
    fake.write_text('#!/bin/sh\nexit "$SYNTHETIC_EXIT"\n')
    fake.chmod(0o755)
    result = run_step(workflow, 'Run Semgrep' if scanner == 'semgrep' else 'Run Bandit (Python)',
                      tmp_path, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
                      SYNTHETIC_EXIT=str(status))
    assert result.returncode == (0 if status in (0, 1) else status)
    output = tmp_path / 'outputs'
    if status in (0, 1):
        assert output.read_text() == f'exit_code={status}\n'
    else:
        assert not output.exists()


@pytest.mark.parametrize('workflow', WORKFLOWS)
@pytest.mark.parametrize('exit_code', ['', '2', '1'])
def test_missing_error_or_findings_without_results_status_is_incomplete(tmp_path, workflow, exit_code):
    reports(tmp_path)
    (tmp_path / '.ai').mkdir()
    (tmp_path / '.ai/sast-findings.json').write_text('[]')  # Old evidence cannot survive an incomplete rerun.
    result = run_step(workflow, 'Merge SAST findings', tmp_path, SEMGREP_EXIT=exit_code)
    assert result.returncode != 0
    assert not (tmp_path / '.ai/sast-findings.json').exists()


@pytest.mark.parametrize('workflow', WORKFLOWS)
def test_bandit_skip_requires_explicit_no_python_applicability(tmp_path, workflow):
    reports(tmp_path)
    (tmp_path / 'bandit-results.json').unlink()
    result = run_step(workflow, 'Merge SAST findings', tmp_path, BANDIT_REQUIRED='false', BANDIT_EXIT='')
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / '.ai/sast-findings.json').read_text()) == []
    result = run_step(workflow, 'Merge SAST findings', tmp_path, BANDIT_REQUIRED='', BANDIT_EXIT='')
    assert result.returncode != 0


def test_scanner_contract_and_portable_commands_stay_in_sync():
    specs = [yaml.load((ROOT / '.github/workflows' / f).read_text(), Loader=yaml.BaseLoader) for f in WORKFLOWS]
    steps = [[s for s in spec['jobs']['sast']['steps'] if 'run' in s] for spec in specs]
    assert steps[0] == steps[1]
    install = next(s['run'] for s in steps[0] if s['name'] == 'Install report-producing scanners')
    assert 'semgrep==1.182.1' in install and 'bandit==1.9.4' in install
    contract = (ROOT / 'scripts/lib/sast_evidence.py').read_text()
    assert '"semgrep": "1.182.1", "bandit": "1.9.4"' in contract
    assert '--config auto --json --error --output' in next(s['run'] for s in steps[0] if s['name'] == 'Run Semgrep')
    for spec in specs:
        assert "needs.classify.result == 'success'" in spec['jobs']['sast']['if']
        assert 'continue-on-error' not in next(s for s in spec['jobs']['sast']['steps'] if s.get('name') == 'Run Semgrep')
