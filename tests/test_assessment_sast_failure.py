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
    semgrep = {'version': '1.185.0', 'results': [], 'errors': [], 'paths': {'scanned': ['fixture.py']}}
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
@pytest.mark.parametrize(('scanner', 'bad'), [
    ('semgrep', 'skipped-rules'), ('semgrep', 'invalid-scanned-file'),
    ('bandit', 'missing-totals'), ('bandit', 'invalid-file-metrics'),
    ('bandit', 'inconsistent-total'), ('bandit', 'invalid-total'),
])
def test_partial_completion_never_becomes_successful_zero_findings(tmp_path, workflow, scanner, bad):
    data = dict(zip(['semgrep', 'bandit'], reports(tmp_path)))[scanner]
    if bad == 'skipped-rules':
        data['skipped_rules'] = [{'id': 'synthetic.partial'}]
    elif bad == 'invalid-scanned-file':
        data['paths']['scanned'] = [None]
    elif bad == 'missing-totals':
        del data['metrics']['_totals']
    elif bad == 'invalid-file-metrics':
        data['metrics']['fixture.py'] = None
    elif bad == 'inconsistent-total':
        data['metrics']['_totals']['loc'] = 2
    else:
        data['metrics']['_totals']['loc'] = True
    (tmp_path / (scanner + '-results.json')).write_text(json.dumps(data))
    result = run_step(workflow, 'Merge SAST findings', tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / '.ai/sast-findings.json').exists()


@pytest.mark.parametrize('workflow', WORKFLOWS)
@pytest.mark.parametrize(('code', 'error_type'), [(2, 'Timeout'), (3, ['PartialParsing', []])])
def test_native_warning_shapes_remain_incomplete_despite_findings_exit(tmp_path, workflow, code, error_type):
    # The observed Semgrep 1.182.1 run emitted exit 1 and findings alongside these
    # completion warnings. A success banner or level=warn is not full coverage.
    semgrep, _ = reports(tmp_path)
    semgrep['results'] = [{'check_id': 'fixture.rule', 'path': 'fixture.py', 'start': {'line': 1},
                           'extra': {'message': 'Synthetic finding', 'severity': 'WARNING'}}]
    semgrep['errors'] = [{'code': code, 'level': 'warn', 'type': error_type,
                          'path': 'fixture.py', 'message': 'Synthetic incomplete scan'}]
    (tmp_path / 'semgrep-results.json').write_text(json.dumps(semgrep))
    (tmp_path / '.ai').mkdir()
    (tmp_path / '.ai/sast-findings.json').write_text('[]')
    result = run_step(workflow, 'Merge SAST findings', tmp_path, SEMGREP_EXIT='1')
    assert result.returncode != 0
    assert 'semgrep: incomplete or malformed report' in result.stderr
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
    assert 'semgrep==1.185.0' in install and 'bandit==1.9.4' in install
    contract = (ROOT / 'scripts/lib/sast_evidence.py').read_text()
    assert '"semgrep": "1.185.0", "bandit": "1.9.4"' in contract
    assert '--config auto --json --error --output' in next(s['run'] for s in steps[0] if s['name'] == 'Run Semgrep')
    assert '--timeout 30 --timeout-threshold 0' in next(s['run'] for s in steps[0] if s['name'] == 'Run Semgrep')
    for spec in specs:
        assert "needs.classify.result == 'success'" in spec['jobs']['sast']['if']
        assert 'continue-on-error' not in next(s for s in spec['jobs']['sast']['steps'] if s.get('name') == 'Run Semgrep')


def test_changed_ref_reaches_git_as_data(tmp_path):
    spec = yaml.load((ROOT / '.github/workflows/pr-assessment.yml').read_text(), Loader=yaml.BaseLoader)
    step = next(s for s in spec['jobs']['classify']['steps'] if s.get('name') == 'Get PR diff')
    assert step['env']['PR_BASE_REF'] == '${{ github.base_ref }}'
    git = tmp_path / 'git'
    git.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                   "with open('args.jsonl','a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                   "print('fixture.py')\n")
    git.chmod(0o755)
    ref = 'feature/$(touch injected)'
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c',
                             step['run'].replace('/tmp/pr.diff', str(tmp_path / 'pr.diff'))],
                            cwd=tmp_path, env={'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
                            'PR_BASE_REF': ref, 'GITHUB_OUTPUT': str(tmp_path / 'outputs')},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'injected').exists()
    calls = [json.loads(line) for line in (tmp_path / 'args.jsonl').read_text().splitlines()]
    assert calls == [['diff', f'origin/{ref}...HEAD'], ['diff', '--name-only', f'origin/{ref}...HEAD']]


@pytest.mark.parametrize(('name', 'key'), [('Install dependencies', 'INSTALL_COMMAND'), ('Build', 'BUILD_COMMAND')])
@pytest.mark.parametrize('fails', [False, True])
def test_e2e_command_environment_preserves_multiline_code_and_pipefail(tmp_path, name, key, fails):
    spec = yaml.load((ROOT / '.github/workflows/e2e-playwright-reusable.yml').read_text(), Loader=yaml.BaseLoader)
    step = next(s for s in spec['jobs']['e2e']['steps'] if s.get('name') == name)
    expected_input = 'install_command' if key == 'INSTALL_COMMAND' else 'build_command'
    assert step['env'][key] == '${{ inputs.' + expected_input + ' }}'
    command = 'printf "%s" "$FIXTURE_DATA" > result\n'
    command += 'false | true\nprintf unsafe > after\n' if fails else 'printf complete > after\n'
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step['run']], cwd=tmp_path,
                            env={'PATH': os.environ['PATH'], key: command,
                                 'FIXTURE_DATA': 'quoted $(touch injected) data'},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == (1 if fails else 0)
    assert (tmp_path / 'result').read_text() == 'quoted $(touch injected) data'
    assert not (tmp_path / 'injected').exists()
    assert (tmp_path / 'after').exists() is not fails


def test_runner_policy_alone_selects_its_owning_recipe():
    import runpy
    matches = runpy.run_path(str(ROOT / 'scripts/agent-check.py'))['matches']
    config = json.loads((ROOT / 'config/agent-checks.json').read_text())
    check = next(c for c in config['checks'] if c['id'] == 'assessment-sast-failure-contract')
    assert matches(['tests/test_pr_assessment_runner_policy.py'], check['paths'])
    assert 'tests/test_pr_assessment_runner_policy.py' in check['commands'][0]
