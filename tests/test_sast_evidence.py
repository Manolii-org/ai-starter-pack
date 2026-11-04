"""Dormant SAST helper contracts; synthetic scanners, no workflow adoption."""
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def write_scanner_contract(directory):
    rules = {"rules": [{"id": "fixture.expected-inventory", "languages": ["python", "javascript"],
                        "severity": "MEDIUM", "message": "Offline rule", "pattern": "$X"}]}
    raw = load().encoded(rules)
    (directory / "scanner-rules.json").write_bytes(raw)
    contract = {"versions": load().SCANNER_VERSIONS, "registry": "https://semgrep.dev/c/auto",
                "rules_sha256": hashlib.sha256(raw).hexdigest(), "languages": [
                    {"keys": ["python"], "exts": [".py", ".pyi"], "shebangs": ["python3"]},
                    {"keys": ["javascript"], "exts": [".js"], "shebangs": []}]}
    (directory / "scanner-contract.json").write_bytes(load().encoded(contract))


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
        return {'version': '1.182.1', 'results': [row, copy.deepcopy(row)] if findings else [],
                'errors': [], 'paths': {'scanned': ['app.py', 'scripts/lib/sast_evidence.py']}, 'skipped_rules': []}
    row = dict(test_id='B101', filename='./app.py', line_number=1,
               issue_text='Review assertion', issue_severity='LOW')
    return {'results': [row, copy.deepcopy(row)] if findings else [], 'errors': [],
            'metrics': {'_totals': {'loc': 2}, './app.py': {'loc': 1}, './scripts/lib/sast_evidence.py': {'loc': 1}}}



@pytest.mark.parametrize('scanner', ['semgrep', 'bandit'])
@pytest.mark.parametrize('exit_code,has_findings,complete', [(0, False, True), (0, True, True),
                                                         (1, True, {'semgrep': False, 'bandit': True}), (1, False, False),
                                                         (2, True, False), (130, True, False),
                                                         (-15, True, False), (None, True, False)])
def test_observed_execution_and_findings_exits(scanner, exit_code, has_findings, complete):
    module = load()
    state, rows = module.normalize(scanner, module.encoded(report(scanner, has_findings)), exit_code, '1.182.1')
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
    version = '1.182.1'
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
    runtime = tmp_path / "scanner-runtime"
    runtime.mkdir()
    write_scanner_contract(runtime)
    env["RUNNER_TEMP"] = str(runtime)
    return root, env, git



def fake_run(module, *, findings=False, failure=None):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        scanner = command[0]
        if command[1:] == ['--version']:
            return subprocess.CompletedProcess(command, 0, (module.SCANNER_VERSIONS[command[0]] + '\n').encode(), b'')
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
    state, rows = module.normalize('bandit', module.encoded(data), 0, '1.182.1')
    assert state['status'] == 'incomplete' and rows == []



def test_bandit_empty_file_with_observed_zero_loc_is_still_executed():
    module = load()
    data = report('bandit')
    data['metrics'] = {'_totals': {'loc': 0}, 'empty.py': {'loc': 0}}
    state, rows = module.normalize('bandit', module.encoded(data), 0, '1.182.1')
    assert state['status'] == 'complete' and state['scanned_paths'] == ['empty.py'] and rows == []





def test_semgrep_warning_level_partial_parse_is_incomplete_without_omitting_findings():
    module = load()
    data = report('semgrep', True)
    data['errors'] = [{'code': 3, 'level': 'warn', 'type': ['PartialParsing', []],
                       'message': 'Partial analysis', 'path': 'app.py'}]
    state, rows = module.normalize('semgrep', module.encoded(data), 0, '1.182.1')
    assert state['status'] == 'incomplete' and state['reason'] == 'scanner_reported_errors'
    assert len(rows) == 2








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
            return subprocess.CompletedProcess(command, 0, (module.SCANNER_VERSIONS[command[0]] + '\n').encode(), b'')
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


def update_event_head(env, git, *, base=None):
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    event['pull_request']['head']['sha'] = git('rev-parse', 'HEAD')
    if base is not None:
        event['pull_request']['base']['sha'] = base
    Path(env['GITHUB_EVENT_PATH']).write_text(json.dumps(event))



@pytest.mark.parametrize('mutation', ['missing', 'wrong-version', 'changed-rules', 'duplicate-rule', 'unknown-language'])
def test_installer_snapshot_cannot_manufacture_a_completed_scan(source, tmp_path, mutation):
    root, env, _ = source
    module = load()
    contract_path = Path(env['RUNNER_TEMP']) / 'scanner-contract.json'
    if mutation == 'missing':
        contract_path.unlink()
    else:
        contract = json.loads(contract_path.read_text())
        if mutation == 'wrong-version':
            contract['versions']['semgrep'] = '1.36.0'
        elif mutation == 'changed-rules':
            contract['rules_sha256'] = '0' * 64
        elif mutation == 'duplicate-rule':
            rule_path = contract_path.parent / 'scanner-rules.json'
            rules = json.loads(rule_path.read_text())
            rules['rules'].append(copy.deepcopy(rules['rules'][0]))
            raw = module.encoded(rules)
            rule_path.write_bytes(raw)
            contract['rules_sha256'] = module.digest(raw)
        else:
            contract['languages'] = []
        contract_path.write_bytes(module.encoded(contract))
    run, calls = fake_run(module)
    with pytest.raises((OSError, ValueError)):
        module.produce(root, tmp_path / 'artifact', env, run=run)
    assert calls == []


def test_legacy_medium_crash_preserves_all_5572_entries_and_refuses_completion(source, tmp_path):
    root, env, _ = source
    module = load()
    row = report('semgrep', True)['results'][0]
    row['extra']['severity'] = 'MEDIUM'
    data = report('semgrep')
    data['results'] = [copy.deepcopy(row) for _ in range(5572)]
    data['errors'] = [{'type': 'legacy_severity_crash', 'level': 'error'}]
    state, entries = module.normalize('semgrep', module.encoded(data), 2, '1.36.0')
    assert state['status'] == 'incomplete' and state['reason'] == 'execution_failed'
    assert len(entries) == 5572 and all(e['severity'] == 'MEDIUM' for e in entries)
    assert len({e['finding_id'] for e in entries}) == 1  # no deduplication
    run, calls = fake_run(module, findings=True)
    def crashed(command, **kwargs):
        observed = run(command, **kwargs)
        if command[0] == 'semgrep' and '--output' in command:
            Path(command[command.index('--output') + 1]).write_bytes(module.encoded(data))
            return subprocess.CompletedProcess(command, 2)
        return observed
    artifact = tmp_path / 'artifact'
    evidence = module.produce(root, artifact, env, run=crashed)
    assert not evidence['complete'] and len(evidence['entries']) == 5574  # plus both Bandit entries
    assert len(calls) == 4
    with pytest.raises(ValueError):
        module.verify(root, artifact, env, module.digest((artifact / 'sast-evidence.json').read_bytes()),
                      evidence['findings_sha256'], 'success')


@pytest.mark.parametrize('scanner', ['semgrep', 'bandit'])
def test_parseable_partial_target_inventory_retains_findings_but_is_not_complete(source, tmp_path, scanner):
    root, env, _ = source
    module = load()
    run, calls = fake_run(module, findings=True)
    def partial(command, **kwargs):
        observed = run(command, **kwargs)
        if command[0] == scanner and '--version' not in command:
            data = report(scanner, True)
            if scanner == 'semgrep':
                data['paths']['scanned'] = ['app.py']
            else:
                data['metrics'] = {'_totals': {'loc': 1}, './app.py': {'loc': 1}}
            flag = '--output' if scanner == 'semgrep' else '-o'
            Path(command[command.index(flag) + 1]).write_bytes(module.encoded(data))
        return observed
    evidence = module.produce(root, tmp_path / 'artifact', env, run=partial)
    assert not evidence['complete'] and len(evidence['entries']) == 4 and len(calls) == 4
    state = next(s for s in evidence['scanners'] if s['scanner'] == scanner)
    assert state['reason'] == 'incomplete_target_inventory'
    assert state['expected_paths'] == ['app.py', 'scripts/lib/sast_evidence.py']






@pytest.mark.parametrize("field,value", [("skipped_rules", None), ("skipped_rules", {}), ("version", "1.36.0")])
def test_semgrep_missing_completion_fields_or_stale_version_retains_findings(field, value):
    module = load()
    data = report("semgrep", True)
    if value is None:
        del data[field]
    else:
        data[field] = value
    state, findings = module.normalize("semgrep", module.encoded(data), 0, "1.182.1")
    assert state["status"] == "incomplete" and len(findings) == 2


def test_explicit_immutable_paths_prevent_default_test_directory_omission(source, tmp_path):
    root, env, git = source
    (root / 'tests').mkdir()
    (root / 'tests/test_app.py').write_text('assert True\n')
    git('add', '.')
    git('commit', '-qm', 'test directory coverage')
    update_event_head(env, git)
    module = load()
    run, calls = fake_run(module)
    def full_inventory(command, **kwargs):
        observed = run(command, **kwargs)
        if '--version' not in command:
            name = command[0]
            report_path = Path(command[command.index('--output' if name == 'semgrep' else '-o') + 1])
            data = report(name)
            if name == 'semgrep':
                assert command[command.index('--') + 1:] == ['app.py', 'scripts/lib/sast_evidence.py', 'tests/test_app.py']
                data['paths']['scanned'].append('tests/test_app.py')
            else:
                data['metrics']['./tests/test_app.py'] = {'loc': 1}
                data['metrics']['_totals']['loc'] = 3
            report_path.write_bytes(module.encoded(data))
        return observed
    evidence = module.produce(root, tmp_path / 'artifact', env, run=full_inventory)
    assert evidence['complete'] and len(calls) == 4
    assert evidence['scanners'][0]['expected_paths'] == ['app.py', 'scripts/lib/sast_evidence.py', 'tests/test_app.py']


def test_no_applicable_language_is_explicitly_unexecuted(source, tmp_path):
    root, env, git = source
    git('rm', '-r', 'app.py', 'scripts')
    (root / 'guide.md').write_text('Documentation only.\n')
    git('add', '.')
    git('commit', '-qm', 'documentation scope')
    update_event_head(env, git)
    module = load()
    run, calls = fake_run(module)
    evidence = module.produce(root, tmp_path / 'artifact', env, run=run)
    assert evidence['complete'] and calls == []
    assert [s['reason'] for s in evidence['scanners']] == ['no_applicable_language', 'no_python_sources']
    assert all(s['exit_code'] is None for s in evidence['scanners'])


@pytest.mark.parametrize("document", [[], None, 17, "unknown"])
@pytest.mark.parametrize("target", ["contract", "rules"])
def test_malformed_snapshot_roots_keep_cli_static_and_make_zero_scanner_calls(source, tmp_path, monkeypatch, capsys, document, target):
    import sys
    root, env, _ = source
    module = load()
    contract_path = Path(env["RUNNER_TEMP"]) / "scanner-contract.json"
    if target == "contract":
        contract_path.write_bytes(module.encoded(document))
    else:
        path = contract_path.parent / "scanner-rules.json"
        raw = module.encoded(document)
        path.write_bytes(raw)
        contract = json.loads(contract_path.read_text())
        contract["rules_sha256"] = module.digest(raw)
        contract_path.write_bytes(module.encoded(contract))
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        pytest.fail("scanner must not run for malformed snapshot")
    produce = module.produce
    monkeypatch.setattr(module, "produce", lambda root, output, env: produce(root, output, env, run=forbidden))
    monkeypatch.setattr(sys, "argv", ["sast_evidence.py", "produce", "--root", str(root), "--directory", str(tmp_path / "artifact")])
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert module.main() == 1
    assert capsys.readouterr().err == "SAST evidence unavailable or incomplete\n"
    assert calls == [] and not (tmp_path / "artifact").exists()


@pytest.mark.parametrize("document", [[], None, 17])
def test_malformed_evidence_roots_keep_consumer_cli_static(source, tmp_path, monkeypatch, capsys, document):
    import sys
    root, env, _ = source
    module = load()
    run, calls = fake_run(module, findings=True)
    directory = tmp_path / "artifact"
    evidence = module.produce(root, directory, env, run=run)
    raw = module.encoded(document)
    (directory / "sast-evidence.json").write_bytes(raw)
    monkeypatch.setattr(sys, "argv", ["sast_evidence.py", "verify", "--root", str(root), "--directory", str(directory),
        "--sast-result", "success", "--evidence-sha256", module.digest(raw), "--findings-sha256", evidence["findings_sha256"]])
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert module.main() == 1
    assert capsys.readouterr().err == "SAST evidence unavailable or incomplete\n"
    assert len(calls) == 4 and not (root / ".ai/sast-findings.json").exists()


import yaml

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
    write_scanner_contract(runtime)
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
        code = 2 if case == 'failure' else 130 if case == 'cancel' else 0 if case in ('zero', 'missing', 'malformed') else (1 if scanner == 'bandit' else 0)
        script.write_text('#!/usr/bin/python3\nimport json,sys,pathlib\n'
                          'if "--version" in sys.argv: print("1.182.1" if pathlib.Path(sys.argv[0]).name == "semgrep" else "1.9.4");sys.exit(0)\n'
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
        prepare = next(s for s in steps if s.get('name') == 'Drop committed SAST evidence before download')
        assert prepare['env']['SAST_ARTIFACT_ID'] == '${{ needs.sast.outputs.artifact_id }}'
        prepared = subprocess.run(['bash','-e','-c',prepare['run']], cwd=root,
                                  env={**env,'SAST_RESULT':'success','SAST_ARTIFACT_ID':'123'},capture_output=True)
        assert prepared.returncode == 0, prepared.stderr.decode()
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
    child = '#!/usr/bin/python3\nimport json,os,pathlib,sys\nif sys.argv[1:] != ["-I","-"]:\n pathlib.Path(os.environ["RUNNER_TEMP"],"install-args").write_text("\\n".join(sys.argv[1:])+"\\n")\n'
    shim.write_text('#!/usr/bin/python3\nimport pathlib,sys\n'
                    'assert sys.argv[1:3] == ["-m","venv"]\n'
                    'target=pathlib.Path(sys.argv[3]);(target/"bin").mkdir(parents=True)\n'
                    f'(target/"bin/python").write_text({child!r})\n'
                    '(target/"bin/python").chmod(0o700)\n')
    shim.chmod(0o700)
    env={**os.environ,'PATH':str(binaries)+':/usr/bin:/bin','RUNNER_TEMP':str(tmp_path),'GITHUB_PATH':str(tmp_path/'path-output')}
    run=subprocess.run(['bash','-e','-c',install['run']],cwd=tmp_path,env=env,capture_output=True)
    assert run.returncode == 0, run.stderr.decode()
    assert (tmp_path/'path-output').read_text() == str(tmp_path/'sast-scanners/bin')+'\n'
    assert (tmp_path/'install-args').read_text().splitlines() == ['-m','pip','install','--quiet','--disable-pip-version-check','semgrep==1.182.1','bandit==1.9.4']


@pytest.mark.parametrize('job', ['classify', 'specialists', 'broad-agents'])
def test_real_restore_fetch_keeps_merge_base_when_empty_head_forks_behind_base(source, tmp_path, job):
    path = ROOT/'.github/workflows/pr-assessment.yml'
    document = yaml.safe_load(path.read_text())
    if job not in document['jobs']:
        pytest.skip('consumer delegates assessment to its reusable')
    root, env, git = source
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    fork = event['pull_request']['base']['sha']
    git('checkout','-q','--detach',fork)
    git('checkout','-qb','empty-review')
    git('commit','-qm','empty metadata commit','--allow-empty')
    head = git('rev-parse','HEAD')
    git('checkout','-q','--detach',fork)
    git('checkout','-qb','moved-base')
    (root/'base-only.txt').write_text('base advanced\n')
    git('add','.')
    git('commit','-qm','advance base')
    base = git('rev-parse','HEAD')
    event['pull_request']['base']['sha'] = base
    event['pull_request']['head']['sha'] = head
    Path(env['GITHUB_EVENT_PATH']).write_text(json.dumps(event))
    module = load()
    restore = next(s for s in document['jobs'][job]['steps'] if s.get('name') == 'Restore trusted runtime from PR base')
    actual_fetch = next(line.strip() for line in restore['run'].splitlines() if line.strip().startswith('git fetch '))
    for label, command in [('negative-control','git fetch --depth=1 origin "$BASE_SHA"'), ('actual',actual_fetch)]:
        checkout = tmp_path/label
        subprocess.run(['git','clone','-q',root.as_uri(),str(checkout)],check=True,capture_output=True)
        subprocess.run(['git','checkout','-q','--detach',head],cwd=checkout,check=True,capture_output=True)
        fetched = subprocess.run(['bash','-e','-c',command],cwd=checkout,env={**env,'BASE_SHA':base},capture_output=True)
        assert fetched.returncode == 0
        if label == 'negative-control':
            with pytest.raises(subprocess.CalledProcessError):
                module.verify(checkout,tmp_path,env,'','','skipped')
        else:
            result = module.verify(checkout,tmp_path,env,'','','skipped')
            assert result['status'] == 'skipped' and result['reason'] == 'empty_diff'


@pytest.mark.parametrize('workflow', ['pr-assessment.yml', 'pr-assessment-reusable.yml'])
@pytest.mark.parametrize('job', ['specialists', 'broad-agents', 'judge'])
@pytest.mark.parametrize('result,artifact_id', [('success',''),('success','0'),('success','unknown'),
                                               ('success','123,456'),('success',' 123'),('failure','123')])
def test_actual_consumer_stops_missing_or_unqualified_id_before_download(source, workflow, job, result, artifact_id):
    root, env, _ = source
    path = ROOT/'.github/workflows'/workflow
    if not path.exists():
        pytest.skip('Pack-only workflow')
    document = yaml.safe_load(path.read_text())
    if job not in document['jobs']:
        pytest.skip('consumer delegates assessment to its reusable')
    prepare = next(s for s in document['jobs'][job]['steps'] if s.get('name') == 'Drop committed SAST evidence before download')
    marker = root/'.ai/sast/cache'
    marker.parent.mkdir(parents=True)
    marker.write_text('untrusted old scratch')
    checked = subprocess.run(['bash','-e','-c',prepare['run']],cwd=root,
                             env={**env,'SAST_RESULT':result,'SAST_ARTIFACT_ID':artifact_id},capture_output=True)
    assert checked.returncode == 1
    assert checked.stderr == b'::error::Qualified SAST artifact ID is required\n'
    assert marker.read_text() == 'untrusted old scratch'  # fails before any subsequent step


@pytest.mark.parametrize('workflow', ['pr-assessment.yml', 'pr-assessment-reusable.yml'])
@pytest.mark.parametrize('job', ['specialists', 'broad-agents', 'judge'])
def test_actual_empty_diff_skip_needs_no_artifact_but_still_checks_source(source, workflow, job):
    root, env, _ = source
    path = ROOT/'.github/workflows'/workflow
    if not path.exists():
        pytest.skip('Pack-only workflow')
    document = yaml.safe_load(path.read_text())
    if job not in document['jobs']:
        pytest.skip('consumer delegates assessment to its reusable')
    steps = document['jobs'][job]['steps']
    prepare = next(s for s in steps if s.get('name') == 'Drop committed SAST evidence before download')
    check = next(s for s in steps if s.get('name') == 'Validate SAST producer evidence')
    download = next(s for s in steps if s.get('with',{}).get('artifact-ids'))
    assert download['if'] == "needs.sast.result != 'skipped'"
    controls = {**env,'SAST_RESULT':'skipped','SAST_ARTIFACT_ID':'',
                'SAST_EVIDENCE_SHA256':'','SAST_FINDINGS_SHA256':''}
    prepared = subprocess.run(['bash','-e','-c',prepare['run']],cwd=root,env=controls,capture_output=True)
    assert prepared.returncode == 0
    not_empty = subprocess.run(['bash','-e','-c',check['run']],cwd=root,env=controls,capture_output=True)
    assert not_empty.returncode == 1 and not (root/'.ai/sast-findings.json').exists()
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    event['pull_request']['base']['sha'] = event['pull_request']['head']['sha']
    Path(env['GITHUB_EVENT_PATH']).write_text(json.dumps(event))
    empty = subprocess.run(['bash','-e','-c',check['run']],cwd=root,env=controls,capture_output=True)
    assert empty.returncode == 0 and not (root/'.ai/sast-findings.json').exists()



@pytest.mark.parametrize('workflow', ['pr-assessment.yml', 'pr-assessment-reusable.yml'])
@pytest.mark.parametrize('case', ['rules', 'wrapped', 'missing', 'wrong-version'])
def test_actual_installer_snapshot_binds_pinned_catalog_and_rules(tmp_path, monkeypatch, workflow, case):
    import importlib.metadata
    import sys
    import types
    import urllib.request
    document = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
    install = next(s for s in document['jobs']['sast']['steps'] if s.get('name') == 'Install scanner CLIs')
    script = install['run'].split("<<'PY_SNAPSHOT'\n", 1)[1].split('\nPY_SNAPSHOT', 1)[0]
    package = tmp_path / 'package'
    (package / 'semgrep_interfaces').mkdir(parents=True)
    catalog = [{'keys': ['python'], 'exts': ['.py'], 'shebangs': ['python3']}]
    (package / 'semgrep_interfaces/lang.json').write_text(json.dumps(catalog))
    monkeypatch.setitem(sys.modules, 'semgrep', types.SimpleNamespace(__file__=str(package / '__init__.py')))
    versions = {'semgrep': '1.182.1', 'bandit': '1.9.4'}
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: '0.0.0' if case == 'wrong-version' else versions[name])
    rules = {'rules': [{'id': 'fixture-rule', 'languages': ['python']}]}
    payload = {} if case == 'missing' else {'rule_config': json.dumps(rules)} if case == 'wrapped' else rules
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size):
            assert size == 32 * 1024 * 1024 + 1
            return json.dumps(payload).encode()
    class Opener:
        def open(self, request, timeout):
            assert case != 'wrong-version'
            assert request.full_url == 'https://semgrep.dev/c/auto' and timeout == 30
            return Response()
    def opener(handler):
        request = urllib.request.Request('https://semgrep.dev/c/auto')
        from email.message import Message
        for url in ('http://semgrep.dev/c/p/default', 'https://evil.test/rules',
                    'https://semgrep.dev@evil.test/rules', 'https://semgrep.dev:444/rules'):
            with pytest.raises(ValueError):
                handler.redirect_request(request, None, 302, 'Found', Message(), url)
        redirected = handler.redirect_request(request, None, 302, 'Found', Message(),
                                             'https://semgrep.dev/c/p/default')
        assert redirected.full_url == 'https://semgrep.dev/c/p/default'
        assert handler.max_redirections == 3
        return Opener()
    monkeypatch.setattr(urllib.request, 'build_opener', opener)
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    if case in ('missing', 'wrong-version'):
        with pytest.raises(ValueError): exec(compile(script, '<actual-installer>', 'exec'), {})
        assert not (tmp_path / 'scanner-contract.json').exists()
    else:
        exec(compile(script, '<actual-installer>', 'exec'), {})
        contract = json.loads((tmp_path / 'scanner-contract.json').read_bytes())
        raw = (tmp_path / 'scanner-rules.json').read_bytes()
        assert json.loads(raw) == rules
        assert contract == {'versions': versions, 'registry': 'https://semgrep.dev/c/auto',
                            'languages': catalog, 'rules_sha256': hashlib.sha256(raw).hexdigest()}
