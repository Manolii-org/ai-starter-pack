"""Dormant SAST helper-contract coverage; synthetic scanners, no network or keys."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

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
                                                         (1, True, {'semgrep': False, 'bandit': True}), (1, False, False),
                                                         (2, True, False), (130, True, False),
                                                         (-15, True, False), (None, True, False)])
def test_observed_execution_and_findings_exits(scanner, exit_code, has_findings, complete):
    module = load()
    state, rows = module.normalize(scanner, module.encoded(report(scanner, has_findings)), exit_code, '1.100.0')
    expected = complete[scanner] if isinstance(complete, dict) else complete
    assert (state['status'] == 'complete') is expected
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
        return subprocess.CompletedProcess(command, 130 if failure == 'cancel' else 1 if findings and scanner == 'bandit' else 0)
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


def test_semgrep_warning_level_partial_parse_is_incomplete_without_omitting_findings():
    module = load()
    data = report('semgrep', True)
    data['errors'] = [{'code': 3, 'level': 'warn', 'type': ['PartialParsing', []],
                       'message': 'Partial analysis', 'path': 'app.py'}]
    state, rows = module.normalize('semgrep', module.encoded(data), 0, '1.100.0')
    assert state['status'] == 'incomplete' and state['reason'] == 'scanner_reported_errors'
    assert len(rows) == 2


def update_event_head(env, git, *, base=None):
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    event['pull_request']['head']['sha'] = git('rev-parse', 'HEAD')
    if base is not None:
        event['pull_request']['base']['sha'] = base
    Path(env['GITHUB_EVENT_PATH']).write_text(json.dumps(event))


def test_inline_suppression_cannot_drop_findings_from_producer_contract(source, tmp_path):
    root, env, git = source
    module = load()
    (root/'app.py').write_text('assert False  # nosec B101; nosemgrep\n')
    git('add', '.')
    git('commit', '-qm', 'inline suppression fixture')
    update_event_head(env, git)
    calls = []
    def scanner(command, **kwargs):
        calls.append(command)
        name = command[0]
        if '--version' in command:
            return subprocess.CompletedProcess(command, 0, b'1.100.0\n', b'')
        flag = '--disable-nosem' if name == 'semgrep' else '--ignore-nosec'
        unsuppressed = flag in command
        path = Path(command[command.index('--output' if name == 'semgrep' else '-o')+1])
        path.write_bytes(module.encoded(report(name, unsuppressed)))
        return subprocess.CompletedProcess(command, 1 if unsuppressed and name == 'bandit' else 0)
    artifact = tmp_path/'artifact'
    evidence = module.produce(root, artifact, env, run=scanner)
    assert evidence['complete'] and len(evidence['entries']) == 4
    assert len(calls) == 4
    verified = module.verify(root, artifact, env,
                             module.digest((artifact/'sast-evidence.json').read_bytes()),
                             evidence['findings_sha256'], 'success')
    assert verified == evidence


@pytest.mark.parametrize('relative', ['.bandit', '.semgrepignore', 'nested/.gitignore', '.semgrep/rule.yaml'])
@pytest.mark.parametrize('change', ['unchanged', 'added', 'modified', 'removed', 'mode'])
def test_scanner_configuration_requires_unchanged_trusted_base(source, tmp_path, relative, change):
    root, env, git = source
    module = load()
    target = root/relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if change != 'added':
        target.write_text('accepted configuration\n')
    git('add', '.')
    git('commit', '-qm', 'accepted configuration', '--allow-empty')
    base = git('rev-parse', 'HEAD')
    if change == 'added':
        target.write_text('candidate configuration\n')
    elif change == 'modified':
        target.write_text('changed configuration\n')
    elif change == 'removed':
        target.unlink()
    elif change == 'mode':
        target.chmod(0o755)
    git('add', '.')
    git('commit', '-qm', 'candidate configuration', '--allow-empty')
    update_event_head(env, git, base=base)
    run, calls = fake_run(module)
    if change == 'unchanged':
        artifact = tmp_path/'artifact'
        evidence = module.produce(root, artifact, env, run=run)
        assert evidence['complete'] and len(calls) == 4
        assert evidence['configuration_inputs'] == [{'path': relative, 'sha256': module.digest(target.read_bytes())}]
        assert module.verify(root, artifact, env,
                             module.digest((artifact/'sast-evidence.json').read_bytes()),
                             evidence['findings_sha256'], 'success') == evidence
    else:
        with pytest.raises(ValueError, match='configuration'):
            module.produce(root, tmp_path/'artifact', env, run=run)
        assert calls == [] and not (tmp_path/'artifact').exists()


@pytest.mark.parametrize('relative', ['.bandit', '.semgrepignore', 'nested/.gitignore', '.semgrep/rule.yaml'])
@pytest.mark.parametrize('redirect', [False, True])
def test_untracked_configuration_never_reaches_scanners(source, tmp_path, relative, redirect):
    root, env, _ = source
    module = load()
    target = root/relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if redirect:
        target.symlink_to(tmp_path/'unavailable')
    else:
        target.write_text('untrusted local configuration\n')
    run, calls = fake_run(module)
    with pytest.raises(ValueError, match='configuration'):
        module.produce(root, tmp_path/'artifact', env, run=run)
    assert calls == [] and not (tmp_path/'artifact').exists()


@pytest.mark.parametrize('mutation', ['omitted', 'altered', 'changed-source', 'untracked', 'working-bytes'])
def test_consumer_rechecks_configuration_before_publishing(source, tmp_path, mutation):
    root, env, git = source
    module = load()
    (root/'.bandit').write_text('accepted configuration\n')
    git('add', '.')
    git('commit', '-qm', 'accepted configuration')
    update_event_head(env, git, base=git('rev-parse', 'HEAD'))
    run, _ = fake_run(module)
    artifact = tmp_path/'artifact'
    evidence = module.produce(root, artifact, env, run=run)
    if mutation == 'changed-source':
        (root/'.bandit').write_text('changed configuration\n')
        git('add', '.')
        git('commit', '-qm', 'changed configuration')
        update_event_head(env, git)
        evidence['identity'] = module.identity(root, env)
        evidence['configuration_inputs'] = [{'path': '.bandit', 'sha256': module.digest((root/'.bandit').read_bytes())}]
    elif mutation == 'untracked':
        (root/'.semgrepignore').write_text('app.py\n')
    elif mutation == 'working-bytes':
        (root/'.bandit').write_text('changed working configuration\n')
    else:
        evidence['configuration_inputs'] = [] if mutation == 'omitted' else [{'path': '.bandit', 'sha256': '0'*64}]
    (artifact/'sast-evidence.json').write_bytes(module.encoded(evidence))
    (root/'.ai').mkdir(exist_ok=True)
    with pytest.raises(ValueError, match='configuration'):
        module.verify(root, artifact, env, module.digest((artifact/'sast-evidence.json').read_bytes()),
                      evidence['findings_sha256'], 'success', publish=True)
    assert not (root/'.ai/sast-findings.json').exists()


def test_scanner_cannot_introduce_untracked_configuration_during_execution(source, tmp_path):
    root, env, _ = source
    module = load()
    run, calls = fake_run(module)
    def mutating_scan(command, **kwargs):
        observed = run(command, **kwargs)
        if '--output' in command:
            (root/'.bandit').write_text('unexpected scanner configuration\n')
        return observed
    artifact = tmp_path/'artifact'
    with pytest.raises(ValueError, match='configuration'):
        module.produce(root, artifact, env, run=mutating_scan)
    assert len(calls) == 4
    assert not (artifact/'sast-evidence.json').exists()


@pytest.mark.parametrize('phase', ['produce', 'verify'])
@pytest.mark.parametrize('mutation', ['added', 'modified', 'removed'])
def test_index_configuration_must_match_immutable_head(source, tmp_path, phase, mutation):
    root, env, git = source
    module = load()
    target = root/'.bandit'
    if mutation != 'added':
        target.write_text('accepted configuration\n')
        git('add', '.')
        git('commit', '-qm', 'accepted configuration')
        update_event_head(env, git, base=git('rev-parse', 'HEAD'))
    artifact = tmp_path/'artifact'
    run, calls = fake_run(module)
    if phase == 'verify':
        evidence = module.produce(root, artifact, env, run=run)
        calls.clear()
    if mutation == 'added':
        target.write_text('candidate configuration\n')
        git('add', '.bandit')
    elif mutation == 'modified':
        blob = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'],
                                       input=b'changed index configuration\n', cwd=root).decode().strip()
        git('update-index', '--cacheinfo', '100644', blob, '.bandit')
    else:
        git('update-index', '--force-remove', '.bandit')
    with pytest.raises(ValueError, match='configuration'):
        if phase == 'produce':
            module.produce(root, artifact, env, run=run)
        else:
            (root/'.ai').mkdir(exist_ok=True)
            module.verify(root, artifact, env, module.digest((artifact/'sast-evidence.json').read_bytes()),
                          evidence['findings_sha256'], 'success', publish=True)
    assert calls == [] and not (root/'.ai/sast-findings.json').exists()


def test_semgrep_exit_one_retains_findings_but_never_certifies_default_command(source, tmp_path):
    root, env, _ = source
    module = load()
    assert '--error' not in module.CONFIG['semgrep']
    run, calls = fake_run(module, findings=True)
    def unexplained_exit(command, **kwargs):
        observed = run(command, **kwargs)
        if command[0] == 'semgrep' and '--output' in command:
            return subprocess.CompletedProcess(command, 1)
        return observed
    artifact = tmp_path/'artifact'
    evidence = module.produce(root, artifact, env, run=unexplained_exit)
    assert not evidence['complete'] and len(evidence['entries']) == 4 and len(calls) == 4
    assert evidence['scanners'][0]['status'] == 'incomplete'
    assert evidence['scanners'][0]['reason'] == 'execution_failed'
    assert evidence['scanners'][1]['status'] == 'complete'
    with pytest.raises(ValueError, match='artifact'):
        module.verify(root, artifact, env, module.digest((artifact/'sast-evidence.json').read_bytes()),
                      evidence['findings_sha256'], 'success')
