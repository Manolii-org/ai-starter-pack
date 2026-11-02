"""Trusted selected-role coverage through actual runner/Judge CLI interfaces."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('coverage_' + name.replace('-', '_'), ROOT / 'scripts' / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launch(tmp_path, monkeypatch):
    event = tmp_path / 'event.json'
    head, base = 'a'*40, 'b'*40
    event.write_text(json.dumps({'repository': {'full_name': 'example/public-pack'},
                                'pull_request': {'head': {'sha': head}, 'base': {'sha': base}}}))
    for key, value in {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_PATH': str(event),
                       'GITHUB_REPOSITORY': 'example/public-pack', 'GITHUB_RUN_ID': '123',
                       'GITHUB_RUN_ATTEMPT': '1', 'REVIEW_SKILLS': 'shell-security,security-boundary-test',
                       'REVIEW_AGENTS': 'security-deep-dive,systems-consistency'}.items():
        monkeypatch.setenv(key, value)
    judge = load('run-judge.py')
    output = tmp_path / 'candidates'
    output.mkdir()
    instance = judge.Judge(str(output), 222, head)
    instance.judge_log_dir = tmp_path / 'logs'
    seen = []
    monkeypatch.setattr(instance, '_post_advisory_warning', lambda detail: seen.append(('incomplete', detail)) or False)
    monkeypatch.setattr(instance, '_post_no_findings_comment', lambda: seen.append(('clean', '')))
    def artifact(role, **extra):
        record = judge._policy_module.bind_review_candidate({'source': role, 'findings': [], **extra})
        (output / (role + '.json')).write_text(json.dumps(record))
        return record
    return judge, instance, artifact, seen


def complete(artifact):
    for role in ('shell-security', 'security-boundary-test', 'security-deep-dive', 'systems-consistency'):
        artifact(role)


def test_exact_three_incomplete_markers_never_become_clean(launch):
    _, instance, artifact, seen = launch
    artifact('shell-security')
    artifact('security-boundary-test', first_party=True, skipped='route_policy_denied')
    artifact('security-deep-dive', first_party=True, skipped='route_policy_denied')
    artifact('systems-consistency', skipped='output_limit')
    assert instance.run() == 0  # Intentional advisory/check distinction stays intact.
    assert seen and all(kind == 'incomplete' for kind, _ in seen)
    assert instance._skipped_first_party == ['security-boundary-test', 'security-deep-dive']
    assert instance._skipped_other == ['systems-consistency']


@pytest.mark.parametrize('missing', [('security-deep-dive',), ('shell-security', 'security-boundary-test'),
                                    ('security-deep-dive', 'systems-consistency')])
def test_missing_role_or_entire_phase_cannot_disappear(launch, missing):
    _, instance, artifact, seen = launch
    complete(artifact)
    for role in missing:
        (instance.candidates_dir / (role + '.json')).unlink()
    instance.run()
    assert instance._candidate_load_errors == len(missing)
    assert seen and seen[0][0] == 'incomplete'


@pytest.mark.parametrize('change', ['head', 'base', 'run', 'attempt', 'repository', 'missing', 'forged-source', 'invalid-findings', 'bad-skip', 'partial'])
def test_malformed_stale_or_mixed_artifact_stays_incomplete(launch, change):
    _, instance, artifact, seen = launch
    complete(artifact)
    row = artifact('systems-consistency')
    if change in ('head', 'base', 'run', 'attempt', 'repository'):
        row['_assessment'][change] = 'wrong'
    elif change == 'missing':
        del row['_assessment']
    elif change == 'forged-source':
        row['source'] = 'shell-security'
    elif change == 'invalid-findings':
        del row['findings']
    elif change == 'bad-skip':
        row['skipped'] = False
    else:
        row['status'] = 'partial'
    (instance.candidates_dir / 'systems-consistency.json').write_text(json.dumps(row))
    instance.run()
    assert instance._candidate_load_errors and seen[0][0] == 'incomplete'


def test_complete_zero_findings_and_explicit_empty_phase(launch, monkeypatch):
    _, instance, artifact, seen = launch
    monkeypatch.setenv('REVIEW_AGENTS', '')
    artifact('shell-security')
    artifact('security-boundary-test')
    assert instance.run() == 0 and seen == [('clean', '')]


@pytest.mark.parametrize('missing', ['REVIEW_AGENTS', 'REVIEW_SKILLS'])
def test_absent_trusted_control_is_not_an_empty_phase(launch, monkeypatch, missing):
    _, instance, artifact, seen = launch
    complete(artifact)
    monkeypatch.delenv(missing)
    instance.run()
    assert instance._candidate_load_errors and seen[0][0] == 'incomplete'


def test_model_cannot_choose_provenance(launch):
    judge, _, _, _ = launch
    row = judge._policy_module.bind_review_candidate({'_assessment': {'head': 'forged'}})
    assert row['_assessment']['head'] == 'a'*40


def test_broad_cli_uses_runner_stamps_and_removes_own_control_file(launch, tmp_path, monkeypatch):
    _, instance, _, _ = launch
    broad = load('run-broad-agents.py')
    source = tmp_path / 'input.diff'
    source.write_text('+complete changed source\n')
    monkeypatch.setattr(broad, 'invoke_agent', lambda *args: {'source': 'systems-consistency', 'findings': [], '_assessment': {'head': 'forged'}})
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, 'argv', ['run-broad-agents.py', '--agents', 'systems-consistency', '--diff', str(source), '--candidates-dir', str(instance.candidates_dir)])
    assert broad.main() == 0
    row = json.loads((instance.candidates_dir / 'systems-consistency.json').read_text())
    assert row['_assessment']['head'] == 'a'*40
    assert not (instance.candidates_dir / '_agents-manifest.json').exists()


def test_specialist_cli_writes_failed_protected_role_with_current_binding(launch, tmp_path, monkeypatch):
    _, instance, _, _ = launch
    specialists = load('run-specialists.py')
    source = tmp_path / 'input.diff'
    source.write_text('+full source\n')
    # Exercise admission, not the provider. Existing policy has no direct grant.
    monkeypatch.setattr(specialists, 'require_direct_llm_admission', lambda: (_ for _ in ()).throw(specialists.RouteAdmissionError('direct review denied: missing committed capability policy evidence')))
    monkeypatch.delenv('CLIENT_AI_POLICY', raising=False)
    monkeypatch.setattr(specialists, '_urlopen_https', lambda *a, **k: pytest.fail('network'))
    monkeypatch.setattr(sys, 'argv', ['run-specialists.py', '--skills', 'security-boundary-test', '--diff', str(source), '--candidates-dir', str(instance.candidates_dir)])
    with pytest.raises(SystemExit) as stopped:
        specialists.main()
    assert stopped.value.code == 0
    row = json.loads((instance.candidates_dir / 'security-boundary-test.json').read_text())
    assert row['skipped'] == 'route_policy_denied'
    assert row['_assessment']['head'] == 'a'*40


@pytest.mark.parametrize('workflow', ['pr-assessment.yml', 'pr-assessment-reusable.yml'])
def test_workflow_passes_both_trusted_phase_outputs_to_real_judge(workflow):
    path = ROOT / '.github/workflows' / workflow
    if not path.exists() and workflow == 'pr-assessment-reusable.yml':
        pytest.skip('reusable workflow is pack-only; consumer caller checked separately')
    jobs = yaml.safe_load(path.read_text())['jobs']
    step = next(row for row in jobs['judge']['steps'] if row.get('name') == 'Run judge')
    assert step['env']['REVIEW_SKILLS'] == '${{ needs.classify.outputs.invoke_skills }}'
    assert step['env']['REVIEW_AGENTS'] == '${{ needs.classify.outputs.invoke_agents }}'
    assert 'python3 scripts/run-judge.py' in step['run']


def test_duplicate_fields_do_not_override_incomplete_marker(launch):
    _, instance, artifact, seen = launch
    complete(artifact)
    path = instance.candidates_dir / 'systems-consistency.json'
    raw = path.read_text().replace('"findings": []', '"skipped":"output_limit","findings":[],"skipped":""')
    path.write_text(raw)
    instance.run()
    assert instance._candidate_load_errors and seen[0][0] == 'incomplete'


def test_real_judge_cli_cannot_promote_a_missing_phase(launch, monkeypatch):
    module, instance, artifact, seen = launch
    artifact('shell-security')
    artifact('security-boundary-test')
    monkeypatch.setattr(module, 'Judge', lambda **kwargs: instance)
    monkeypatch.setattr(sys, 'argv', ['run-judge.py', '--pr-number', '222', '--sha', 'a'*40, '--repo', 'example/public-pack', '--candidates-dir', str(instance.candidates_dir)])
    assert module.main() == 0
    assert seen[0][0] == 'incomplete'


@pytest.mark.parametrize('reason', ['route_policy_denied', 'api_error'])
def test_policy_absence_and_provider_error_remain_distinct(launch, reason):
    _, instance, artifact, seen = launch
    complete(artifact)
    artifact('security-boundary-test', first_party=True, skipped=reason)
    instance.run()
    row = json.loads((instance.candidates_dir / 'security-boundary-test.json').read_text())
    assert row['skipped'] == reason and seen[0][0] == 'incomplete'


def test_real_advisory_response_is_not_downstream_acceptance(launch, monkeypatch):
    import io
    module, instance, artifact, _ = launch
    complete(artifact)
    artifact('security-boundary-test', first_party=True, skipped='route_policy_denied')
    artifact('security-deep-dive', first_party=True, skipped='route_policy_denied')
    artifact('systems-consistency', skipped='output_limit')
    instance.token = 'offline-fixture'
    sent = []
    def post(request, **kwargs):
        sent.append(json.loads(request.data))
        return io.BytesIO(b'{"id":123}')
    monkeypatch.setattr(module, '_urlopen_https', post)
    monkeypatch.setattr(instance, '_post_advisory_warning', module.Judge._post_advisory_warning.__get__(instance))
    monkeypatch.setattr(instance, '_dismiss_stale_judge_reviews', lambda *args: None)
    instance.run()
    assert len(sent) == 1 and sent[0]['event'] == 'COMMENT'
    assert 'security-boundary-test' in sent[0]['body'] and 'systems-consistency' in sent[0]['body']
    assert '**Merge danger:** unknown' in sent[0]['body']
    spec = importlib.util.spec_from_file_location('downstream_coverage_evidence', ROOT / 'scripts/ci/review_evidence.py')
    downstream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(downstream)
    reviews = [{'commit_id': instance.sha, 'state': 'COMMENTED', 'body': sent[0]['body'],
                'user': {'login': 'chatgpt-codex-connector[bot]', 'type': 'Bot'}}]
    assert downstream.bot_review_verdict(reviews, instance.sha) == 'none'
