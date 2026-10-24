"""Real SAST producer/consumer interfaces; synthetic scanners, no network or keys."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(path=ROOT / 'scripts/lib/sast_evidence.py'):
    spec = importlib.util.spec_from_file_location('sast_test_' + hashlib.sha256(str(path).encode()).hexdigest(), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network forbidden'))


def report(scanner, findings=False):
    if scanner == 'semgrep':
        row = dict(check_id='rule.example', path='app.py', start={'line': 1},
                   extra={'message': 'Review input validation', 'severity': 'WARNING'})
        return {'version': '1.100.0', 'results': [row, copy.deepcopy(row)] if findings else [],
                'errors': [], 'paths': {'scanned': ['app.py']}, 'skipped_rules': []}
    row = dict(test_id='B101', filename='./app.py', line_number=1,
               issue_text='Review assertion', issue_severity='LOW')
    return {'results': [row, copy.deepcopy(row)] if findings else [], 'errors': [],
            'metrics': {'_totals': {'loc': 1}, './app.py': {'loc': 1}}}


@pytest.mark.parametrize('scanner', ['semgrep', 'bandit'])
@pytest.mark.parametrize('exit_code,has_findings,complete', [(0, False, True), (0, True, True),
                                                         (1, True, True), (1, False, False),
                                                         (2, True, False), (130, True, False),
                                                         (-15, True, False), (None, True, False)])
def test_observed_execution_and_findings_exits(scanner, exit_code, has_findings, complete):
    module = load()
    state, rows = module.normalize(scanner, module.encoded(report(scanner, has_findings)), exit_code, '1.100.0')
    assert (state['status'] == 'complete') is complete
    assert len(rows) == (2 if has_findings else 0)
    if has_findings:
        assert rows[0]['finding_id'] == rows[1]['finding_id']  # legitimate legacy duplicates
    if exit_code is None:
        assert state['reason'] == 'outcome_unknown'


@pytest.mark.parametrize('scanner', ['semgrep', 'bandit'])
@pytest.mark.parametrize('bad', ['missing', 'truncated', 'duplicate', 'missing-results', 'missing-errors',
                                 'errors', 'scope', 'zero-targets', 'bad-row', 'unknown-version'])
def test_partial_parseable_and_malformed_reports_are_not_clean(scanner, bad):
    module = load()
    data = report(scanner, True)
    version = '1.100.0'
    if bad == 'missing-results':
        del data['results']
    elif bad == 'missing-errors':
        del data['errors']
    elif bad == 'errors':
        data['errors'] = [{'type': 'parse_error'}]
    elif bad == 'scope':
        del data['paths' if scanner == 'semgrep' else 'metrics']
    elif bad == 'zero-targets':
        if scanner == 'semgrep':
            data['paths']['scanned'] = []
        else:
            data['metrics'] = {'_totals': {}}
    elif bad == 'bad-row':
        data['results'].insert(1, {})
    elif bad == 'unknown-version':
        version = None
    raw = module.encoded(data)
    raw = b'' if bad == 'missing' else b'{' if bad == 'truncated' else b'{"results":[],"results":[]}' if bad == 'duplicate' else raw
    state, rows = module.normalize(scanner, raw, 0, version)
    assert state['status'] == 'incomplete'
    if bad in ('missing-errors', 'errors', 'scope', 'zero-targets', 'bad-row', 'unknown-version'):
        assert len(rows) == 2  # a bad row cannot swallow the following valid row


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root).decode().strip()
    git('init', '-q')
    git('config', 'user.email', 'fixture@example.test')
    git('config', 'user.name', 'Offline fixture')
    (root / 'app.py').write_text('assert True\n')
    target = root / 'scripts/lib/sast_evidence.py'
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / 'scripts/lib/sast_evidence.py', target)
    git('add', '.')
    git('commit', '-qm', 'fixture base')
    base = git('rev-parse', 'HEAD')
    (root / 'app.py').write_text('assert False\n')
    git('add', '.')
    git('commit', '-qm', 'fixture head')
    head = git('rev-parse', 'HEAD')
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'repository': {'full_name': 'example/test'},
                                'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}))
    env = {**os.environ, 'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request',
           'GITHUB_EVENT_PATH': str(event), 'GITHUB_REPOSITORY': 'example/test',
           'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
           'GITHUB_WORKFLOW_REF': 'example/test/.github/workflows/pr.yml@refs/pull/1/merge',
           'GITHUB_WORKFLOW_SHA': head}
    return root, env, git


def fake_run(module, *, findings=False, failure=None):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        scanner = command[0]
        if command[1:] == ['--version']:
            return subprocess.CompletedProcess(command, 0, b'1.100.0\n', b'')
        path = Path(command[command.index('--output' if scanner == 'semgrep' else '-o') + 1])
        path.write_bytes(module.encoded(report(scanner, findings)))
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 600)
        return subprocess.CompletedProcess(command, 130 if failure == 'cancel' else 1 if findings else 0)
    return run, calls


def test_production_producer_and_consumer_preserve_all_duplicate_entries(source, tmp_path):
    root, env, _ = source
    module = load()
    run, calls = fake_run(module, findings=True)
    directory = tmp_path / 'artifact'
    evidence = module.produce(root, directory, env, run=run)
    findings = (directory / 'sast-findings.json').read_bytes()
    raw = (directory / 'sast-evidence.json').read_bytes()
    assert evidence['complete'] and len(evidence['entries']) == 4
    assert len({(e['position'], e['sha256']) for e in evidence['entries']}) == 4
    assert module.verify(root, directory, env, module.digest(raw), module.digest(findings), 'success') == evidence
    assert len(calls) == 4 and not list(directory.glob('bandit.json'))


@pytest.mark.parametrize('failure', ['timeout', 'cancel'])
def test_process_timeout_or_cancel_stays_incomplete_without_retry(source, tmp_path, failure):
    root, env, _ = source
    module = load()
    run, calls = fake_run(module, findings=True, failure=failure)
    directory = tmp_path / 'artifact'
    evidence = module.produce(root, directory, env, run=run)
    assert evidence['complete'] is False and len(calls) == 4
    assert all(s['status'] == 'incomplete' for s in evidence['scanners'])
    with pytest.raises(ValueError):
        module.verify(root, directory, env, module.digest((directory/'sast-evidence.json').read_bytes()), evidence['findings_sha256'], 'success')


@pytest.mark.parametrize('field', ['head', 'base', 'run', 'attempt', 'repository', 'workflow_sha'])
def test_stale_or_mixed_context_cannot_manufacture_completion(source, tmp_path, field):
    root, env, _ = source
    module = load()
    run, _ = fake_run(module)
    directory = tmp_path / 'artifact'
    evidence = module.produce(root, directory, env, run=run)
    raw = (directory / 'sast-evidence.json').read_bytes()
    forged = copy.deepcopy(evidence)
    forged['identity'][field] = 'stale'
    (directory / 'sast-evidence.json').write_bytes(module.encoded(forged))
    with pytest.raises(ValueError):
        # Job outputs remain the actual producer digests, not candidate claims.
        module.verify(root, directory, env, module.digest(raw), evidence['findings_sha256'], 'success')


@pytest.mark.parametrize('mutation', ['findings', 'entry', 'runtime', 'claim-complete', 'missing'])
def test_candidate_hashes_and_flags_do_not_replace_trusted_job_outputs(source, tmp_path, mutation):
    root, env, _ = source
    module = load()
    run, _ = fake_run(module)
    directory = tmp_path / 'artifact'
    evidence = module.produce(root, directory, env, run=run)
    raw = (directory / 'sast-evidence.json').read_bytes()
    if mutation == 'findings':
        (directory / 'sast-findings.json').write_text('[{}]')
    elif mutation == 'missing':
        (directory / 'sast-evidence.json').unlink()
    else:
        evidence['entries' if mutation == 'entry' else 'runtime_sha256' if mutation == 'runtime' else 'complete'] = 'candidate-claim'
        (directory / 'sast-evidence.json').write_bytes(module.encoded(evidence))
    with pytest.raises((ValueError, OSError)):
        module.verify(root, directory, env, module.digest(raw), module.digest(module.encoded([])), 'success')


def test_source_mutation_prevents_scan_and_model_calls(source, tmp_path):
    root, env, _ = source
    module = load()
    (root / 'app.py').write_text('changed working bytes')
    with pytest.raises(ValueError, match='immutable blob'):
        module.produce(root, tmp_path / 'out', env, run=lambda *a, **k: pytest.fail('scanner must not execute'))


def test_redirected_consumer_files_are_never_read(source, tmp_path):
    root, _, _ = source
    module = load()
    (root / '.ai').symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(ValueError, match='redirected'):
        module.prepare(root, root / '.ai/sast')


def test_no_python_sources_is_a_designed_unexecuted_skip(source, tmp_path):
    root, env, git = source
    git('rm', '-r', 'app.py', 'scripts')
    (root/'app.js').write_text('const value = 1;\n')
    git('add', '.')
    git('commit', '-qm', 'non-Python fixture')
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    event['pull_request']['head']['sha'] = git('rev-parse', 'HEAD')
    Path(env['GITHUB_EVENT_PATH']).write_text(json.dumps(event))
    module = load()
    run, calls = fake_run(module)
    def javascript_scan(command, **kwargs):
        result = run(command, **kwargs)
        if '--output' in command:
            raw = report('semgrep')
            raw['paths']['scanned'] = ['app.js']
            Path(command[command.index('--output')+1]).write_bytes(module.encoded(raw))
        return result
    evidence = module.produce(root, tmp_path/'artifact', env, run=javascript_scan)
    assert evidence['complete'] is True and len(calls) == 2
    assert evidence['scanners'][1]['status'] == 'skipped'
    assert evidence['scanners'][1]['reason'] == 'no_python_sources'
    assert evidence['scanners'][1]['exit_code'] is None


@pytest.mark.parametrize('path', ['../outside.py', '/outside.py', 'absent.py'])
def test_scanner_report_cannot_claim_a_different_immutable_source(source, tmp_path, path):
    root, env, _ = source
    module = load()
    run, _ = fake_run(module, findings=True)
    def wrong_source(command, **kwargs):
        result = run(command, **kwargs)
        if '--output' in command:
            data = report('semgrep', True)
            data['results'][0]['path'] = path
            Path(command[command.index('--output')+1]).write_bytes(module.encoded(data))
        return result
    evidence = module.produce(root, tmp_path/'artifact', env, run=wrong_source)
    assert evidence['complete'] is False and evidence['scanners'][0]['reason'] == 'source_scope_mismatch'
    assert len(evidence['entries']) == 4  # no finding silently omitted


def test_empty_diff_is_explicitly_skipped_and_nonempty_skip_is_refused(source, tmp_path):
    root, env, git = source
    module = load()
    with pytest.raises(ValueError, match='required SAST was skipped'):
        module.verify(root, tmp_path, env, '', '', 'skipped')
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    event['pull_request']['base']['sha'] = git('rev-parse', 'HEAD')
    Path(env['GITHUB_EVENT_PATH']).write_text(json.dumps(event))
    result = module.verify(root, tmp_path, env, '', '', 'skipped')
    assert result['status'] == 'skipped' and result['reason'] == 'empty_diff'


@pytest.mark.parametrize('workflow', ['pr-assessment.yml', 'pr-assessment-reusable.yml'])
@pytest.mark.parametrize('case', ['zero', 'findings', 'missing', 'malformed', 'failure', 'cancel', 'reported-errors', 'partial-metrics'])
def test_real_workflow_producer_cli_and_all_consumer_gates(source, tmp_path, workflow, case):
    path = ROOT / '.github/workflows' / workflow
    if not path.exists():
        pytest.skip('reusable workflow is Pack-only')
    root, env, _ = source
    document = yaml.safe_load(path.read_text())
    jobs = document['jobs']
    if 'sast' not in jobs:
        pytest.skip('rendered consumer delegates the producer to its pinned reusable workflow')
    producer = jobs['sast']
    assert producer['steps'][0]['with']['ref'] == '${{ github.event.pull_request.head.sha }}'
    assert producer['outputs']['artifact_id'] == '${{ steps.artifact.outputs.artifact-id }}'
    assert 'continue-on-error' not in next(s for s in producer['steps'] if s.get('id') == 'scan')
    upload = next(s for s in producer['steps'] if s.get('id') == 'artifact')
    assert upload['if'] == 'always()' and upload['with']['if-no-files-found'] == 'error'
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    env.update(RUNNER_TEMP=str(runtime), BASE_SHA=json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())['pull_request']['base']['sha'])
    if workflow == 'pr-assessment-reusable.yml':
        guard = next(s for s in producer['steps'] if s.get('name') == 'Require fresh SAST runtime checkout')
        assert subprocess.run(['bash','-e','-c',guard['run']], cwd=root, env=env, capture_output=True).returncode == 0
        checkout = next(s for s in producer['steps'] if s.get('name') == 'Check out trusted SAST runtime')
        assert checkout['with']['repository'] == 'Manolii-org/ai-starter-pack'
        assert checkout['with']['ref'] == '${{ inputs.pack_ref }}'
        # Offline substitute for only the checkout action, not the load/producer.
        pack = root/'.pack-sast'
        helper = pack/'scripts/lib/sast_evidence.py'
        helper.parent.mkdir(parents=True)
        shutil.copyfile(ROOT/'scripts/lib/sast_evidence.py', helper)
        for args in [('init','-q'), ('config','user.email','fixture@example.test'),
                     ('config','user.name','Offline fixture'), ('add','.'), ('commit','-qm','trusted fixture')]:
            subprocess.run(['git',*args], cwd=pack, check=True, capture_output=True)
    loader = next(s for s in producer['steps'] if s.get('name') == 'Load trusted SAST normalizer')
    loaded = subprocess.run(['bash','-e','-c',loader['run']], cwd=root, env=env, capture_output=True)
    assert loaded.returncode == 0, loaded.stderr.decode()
    assert (runtime/'sast-evidence.py').read_bytes() == (ROOT/'scripts/lib/sast_evidence.py').read_bytes()
    binaries = tmp_path/'bin'
    binaries.mkdir()
    for scanner in ('semgrep', 'bandit'):
        script = binaries/scanner
        data = report(scanner, case != 'zero')
        if case == 'reported-errors':
            data['errors'] = [{'type': 'parse_error'}]
        if case == 'partial-metrics' and scanner == 'bandit':
            data['metrics'] = {'_totals': None, './app.py': None}
        payload = '{' if case == 'malformed' else json.dumps(data)
        code = 2 if case == 'failure' else 130 if case == 'cancel' else 0 if case in ('zero', 'missing', 'malformed') else 1
        script.write_text('#!/usr/bin/python3\nimport json,sys,pathlib\n'
                          'if "--version" in sys.argv: print("1.100.0");sys.exit(0)\n'
                          f'flag={"--output" if scanner == "semgrep" else "-o"!r}\n'
                          + (f'pathlib.Path(sys.argv[sys.argv.index(flag)+1]).write_text({payload!r})\n' if case != 'missing' else '') +
                          f'sys.exit({code})\n')
        script.chmod(0o700)
    env.update(PATH=str(binaries)+':/usr/bin:/bin', RUNNER_TEMP=str(runtime), GITHUB_OUTPUT=str(tmp_path/'outputs'))
    step = next(s for s in producer['steps'] if s.get('id') == 'scan')
    executed = subprocess.run(['bash', '-e', '-c', step['run']], cwd=root, env=env, capture_output=True)
    good = case in ('zero', 'findings')
    assert executed.returncode == (0 if good else 1), executed.stderr.decode()
    outputs = dict(line.split('=', 1) for line in Path(env['GITHUB_OUTPUT']).read_text().splitlines())
    assert len(json.loads((runtime/'sast-output/sast-findings.json').read_text())) == (0 if case in ('zero', 'missing', 'malformed') else 4)
    for job in ('specialists', 'broad-agents', 'judge'):
        steps = jobs[job]['steps']
        validation = next(s for s in steps if s.get('name') == 'Validate SAST producer evidence')
        assert validation['env']['SAST_EVIDENCE_SHA256'] == '${{ needs.sast.outputs.evidence_sha256 }}'
        assert validation['env']['SAST_FINDINGS_SHA256'] == '${{ needs.sast.outputs.findings_sha256 }}'
        download = next(s for s in steps if s.get('with', {}).get('artifact-ids'))
        assert len([s for s in steps if 'download-artifact@' in s.get('uses', '')
                    and (s.get('with', {}).get('name') == 'sast-findings' or s.get('with', {}).get('artifact-ids'))]) == 1
        assert download['with']['artifact-ids'] == '${{ needs.sast.outputs.artifact_id }}'
        assert not {'run-id', 'repository', 'github-token'} & set(download['with'])
        assert steps.index(download) < steps.index(validation)
        assert steps.index(validation) < next(i for i,s in enumerate(steps) if s.get('name') == {'specialists':'Run specialist skills','broad-agents':'Run broad agents','judge':'Run judge'}[job])
        destination = root/'.ai/sast'
        helper = root/'scripts/lib/sast_evidence.py'
        prepared = subprocess.run([sys.executable, '-I', str(helper), 'prepare', '--directory', str(destination)], cwd=root, env=env, capture_output=True)
        assert prepared.returncode == 0
        shutil.copytree(runtime/'sast-output', destination, dirs_exist_ok=True)
        checked_env = {**env, 'SAST_RESULT':'success', 'SAST_EVIDENCE_SHA256':outputs['evidence_sha256'],
                       'SAST_FINDINGS_SHA256':outputs['findings_sha256']}
        checked = subprocess.run(['bash','-e','-c',validation['run']], cwd=root, env=checked_env, capture_output=True)
        assert checked.returncode == (0 if good else 1), checked.stderr.decode()
        if good:
            assert (root/'.ai/sast-findings.json').read_bytes() == (destination/'sast-findings.json').read_bytes()
        else:
            assert not (root/'.ai/sast-findings.json').exists()
        (destination/'sast-findings.json').write_text('[]')
        rejected = subprocess.run(['bash','-e','-c',validation['run']], cwd=root, env=checked_env, capture_output=True)
        assert rejected.returncode == 1 and rejected.stderr == b'SAST evidence unavailable or incomplete\n'
    guard = next(s for s in jobs['judge']['steps'] if s.get('name') == 'Fail when an upstream assessment stage failed')
    assert "needs.sast.result == 'failure'" in guard['if'] and "needs.sast.result == 'cancelled'" in guard['if']


@pytest.mark.parametrize('layout', ['plugin/manolii-framework', 'registry/platform/framework'])
def test_generated_producer_verifier_is_identical(layout, source, tmp_path):
    other = ROOT/layout/'scripts/lib/sast_evidence.py'
    if not other.exists():
        pytest.skip('generated helper absent from this rendered consumer')
    assert other.read_bytes() == (ROOT/'scripts/lib/sast_evidence.py').read_bytes()
    module = load(other)
    root, env, _ = source
    run, calls = fake_run(module, findings=True)
    directory = tmp_path/'generated-artifact'
    evidence = module.produce(root, directory, env, run=run)
    raw = (directory/'sast-evidence.json').read_bytes()
    verified = module.verify(root, directory, env, module.digest(raw), evidence['findings_sha256'], 'success')
    assert verified['complete'] is True and len(verified['entries']) == 4 and len(calls) == 4


@pytest.mark.parametrize('kind', ['symlink', 'dangling', 'file', 'directory'])
def test_reusable_checkout_refuses_candidate_redirect_before_any_write(tmp_path, kind):
    path = ROOT/'.github/workflows/pr-assessment-reusable.yml'
    if not path.exists():
        pytest.skip('reusable workflow is Pack-only')
    steps = yaml.safe_load(path.read_text())['jobs']['sast']['steps']
    guard = next(s for s in steps if s.get('name') == 'Require fresh SAST runtime checkout')
    checkout = next(s for s in steps if s.get('name') == 'Check out trusted SAST runtime')
    assert steps.index(guard) < steps.index(checkout)
    work = tmp_path/'source'
    work.mkdir()
    outside = tmp_path/'outside'
    outside.mkdir()
    sentinel = outside/'sentinel'
    sentinel.write_text('untouched')
    candidate = work/'.pack-sast'
    if kind in ('symlink', 'dangling'):
        candidate.symlink_to(outside if kind == 'symlink' else tmp_path/'missing', target_is_directory=True)
    elif kind == 'file':
        candidate.write_text('candidate')
    else:
        candidate.mkdir()
    run = subprocess.run(['bash','-e','-c',guard['run']], cwd=work, capture_output=True)
    assert run.returncode == 1 and sentinel.read_text() == 'untouched'
    assert run.stderr == b'::error::SAST runtime checkout must be fresh\n'


def test_direct_loader_uses_base_object_not_candidate_helper(source, tmp_path):
    root, env, git = source
    path = ROOT/'.github/workflows/pr-assessment.yml'
    document = yaml.safe_load(path.read_text())
    if 'sast' not in document['jobs']:
        pytest.skip('consumer delegates SAST to its reusable')
    base = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())['pull_request']['base']['sha']
    candidate = root/'scripts/lib/sast_evidence.py'
    candidate.write_text('raise RuntimeError("candidate normalizer must not execute")\n')
    git('add','.')
    git('commit','-qm','candidate helper replacement')
    env.update(BASE_SHA=base, RUNNER_TEMP=str(tmp_path))
    loader = next(s for s in document['jobs']['sast']['steps'] if s.get('name') == 'Load trusted SAST normalizer')
    loaded = subprocess.run(['bash','-e','-c',loader['run']], cwd=root, env=env, capture_output=True)
    assert loaded.returncode == 0
    assert (tmp_path/'sast-evidence.py').read_bytes() == (ROOT/'scripts/lib/sast_evidence.py').read_bytes()
    assert (tmp_path/'sast-evidence.py').read_bytes() != candidate.read_bytes()


@pytest.mark.parametrize('metrics', [{'_totals': None, 'app.py': None},
                                    {'_totals': {}, 'app.py': {}},
                                    {'_totals': {'loc': True}, 'app.py': {'loc': 1}},
                                    {'_totals': {'loc': -1}, 'app.py': {'loc': -1}},
                                    {'_totals': {'loc': 2}, 'app.py': {'loc': 1}}])
def test_bandit_partial_metrics_never_count_as_completion(metrics):
    module = load()
    data = report('bandit')
    data['metrics'] = metrics
    state, rows = module.normalize('bandit', module.encoded(data), 0, '1.100.0')
    assert state['status'] == 'incomplete' and rows == []


def test_bandit_empty_file_with_observed_zero_loc_is_still_executed():
    module = load()
    data = report('bandit')
    data['metrics'] = {'_totals': {'loc': 0}, 'empty.py': {'loc': 0}}
    state, rows = module.normalize('bandit', module.encoded(data), 0, '1.100.0')
    assert state['status'] == 'complete' and state['scanned_paths'] == ['empty.py'] and rows == []


@pytest.mark.parametrize('workflow', ['pr-assessment.yml', 'pr-assessment-reusable.yml'])
def test_actual_installer_is_job_temporary_not_global(tmp_path, workflow):
    path = ROOT/'.github/workflows'/workflow
    if not path.exists():
        pytest.skip('Pack-only workflow')
    document = yaml.safe_load(path.read_text())
    if 'sast' not in document['jobs']:
        pytest.skip('consumer delegates SAST to its reusable')
    install = next(s for s in document['jobs']['sast']['steps'] if s.get('name') == 'Install scanner CLIs')
    binaries = tmp_path/'bin'
    binaries.mkdir()
    shim = binaries/'python3'
    shim.write_text('#!/usr/bin/python3\nimport pathlib,sys\n'
                    'assert sys.argv[1:3] == ["-m","venv"]\n'
                    'target=pathlib.Path(sys.argv[3]);(target/"bin").mkdir(parents=True)\n'
                    '(target/"bin/python").write_text("#!/bin/sh\\nprintf \'%s\\\\n\' \\"$@\\" > \\"$RUNNER_TEMP/install-args\\"\\n")\n'
                    '(target/"bin/python").chmod(0o700)\n')
    shim.chmod(0o700)
    env={**os.environ,'PATH':str(binaries)+':/usr/bin:/bin','RUNNER_TEMP':str(tmp_path),'GITHUB_PATH':str(tmp_path/'path-output')}
    run=subprocess.run(['bash','-e','-c',install['run']],cwd=tmp_path,env=env,capture_output=True)
    assert run.returncode == 0, run.stderr.decode()
    assert (tmp_path/'path-output').read_text() == str(tmp_path/'sast-scanners/bin')+'\n'
    assert (tmp_path/'install-args').read_text().splitlines() == ['-m','pip','install','--quiet','--disable-pip-version-check','semgrep','bandit']
