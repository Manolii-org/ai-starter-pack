"""Offline policy admission through real runners; fake keys and no network."""
import importlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
policy = importlib.import_module("lib.agent_capabilities")

SECRET_NAMES = {'ANTHROPIC_API_KEY', 'ANTHROPIC_DIRECT_API_KEY', 'LLM_API_KEY', 'LITELLM_MASTER_KEY'}


def load(name, prefix=''):
    path = ROOT / prefix / 'scripts' / name
    spec = importlib.util.spec_from_file_location('admission_' + prefix.replace('/', '_') + name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GITHUB_REPOSITORY', 'ExternalExample/project')
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.delenv('CLIENT_AI_POLICY', raising=False)
    for name in SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name in ['LITELLM_PROXY_URL', 'ANTHROPIC_BASE_URL']:
        monkeypatch.delenv(name, raising=False)
    subprocess.run(['git', 'init', '-q'], check=True)
    manifest = {'version': 1, 'mode': 'capabilities', 'capabilities': ['llm-anthropic'],
                'consumer_scope': 'external', 'consumer_repository': 'ExternalExample/project'}

    def commit(value=manifest, path='.ai/capabilities.json'):
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value) if not isinstance(value, str) else value)
        subprocess.run(['git', 'add', path], check=True)
        subprocess.run(['git', '-c', 'user.email=offline@example.invalid', '-c', 'user.name=Offline',
                        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Policy fixture'], check=True)
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    return tmp_path, manifest, commit


@pytest.mark.parametrize('owner', ['Manolii-org', 'BURO-BUILT', 'CPDcheck', 'Impaktful'])
def test_estate_policy_denied_despite_working_tree_external_claim_and_key(external, monkeypatch, owner):
    root, manifest, commit = external
    repo = owner + '/project'
    commit({**manifest, 'consumer_repository': repo, 'consumer_scope': 'estate'})
    (root / '.ai/capabilities.json').write_text(json.dumps({**manifest, 'consumer_repository': repo}))
    monkeypatch.setenv('GITHUB_REPOSITORY', repo)
    monkeypatch.setenv('ANTHROPIC_DIRECT_API_KEY', 'offline-value')
    with pytest.raises(policy.RouteAdmissionError, match='policy evidence'):
        policy.require_direct_llm_admission()


@pytest.mark.parametrize('changes', [
    {'consumer_scope': 'estate'}, {'consumer_scope': None}, {'consumer_repository': 'Other/project'},
    {'consumer_repository': 12}, {'mode': 'legacy'}, {'mode': None}, {'version': True}, {'version': 2},
    {'capabilities': []}, {'capabilities': ['llm-proxy']},
    {'capabilities': ['llm-proxy', 'llm-anthropic']}, {'capabilities': ['unknown']},
    {'retrieval_overrides': []},
])
def test_wrong_policy_denied(external, changes):
    root, manifest, commit = external
    commit({**manifest, **changes})
    with pytest.raises(policy.RouteAdmissionError, match='policy evidence'):
        policy.require_direct_llm_admission()


def test_missing_and_working_tree_only_policy_denied(external):
    root, manifest, commit = external
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()
    p = root / '.ai/capabilities.json'
    p.parent.mkdir()
    p.write_text(json.dumps(manifest))
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


def test_external_committed_policy_and_no_credential_is_admitted(external):
    root, manifest, commit = external
    commit()
    assert policy.require_direct_llm_admission() is None
    for name in ['run-specialists.py', 'run-judge.py', 'run-pr-classifier.py']:
        assert load(name)._endpoint()[0] == ''
    assert load('run-broad-agents.py').get_api_key() is None


def test_conflicting_committed_manifests_denied(external):
    root, manifest, commit = external
    commit()
    commit({**manifest, 'capabilities': ['llm-proxy']}, 'config/agent-capabilities.json')
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


def test_working_tree_cannot_override_committed_denial(external):
    root, manifest, commit = external
    commit({**manifest, 'consumer_scope': 'estate'})
    (root / '.ai/capabilities.json').write_text(json.dumps(manifest))
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


def test_pr_uses_original_base_not_head_or_worktree(external, monkeypatch):
    root, manifest, commit = external
    base = commit({**manifest, 'consumer_scope': 'estate'})
    commit()
    event = root / 'event.json'
    event.write_text(json.dumps({'repository': {'full_name': 'ExternalExample/project'},
                                'pull_request': {'base': {'sha': base}}}))
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event))
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


@pytest.mark.parametrize('event', [{}, {'repository': {'full_name': 'Wrong/project'}},
    {'repository': {'full_name': 'ExternalExample/project'}, 'pull_request': {'base': {'sha': 'main'}}}])
def test_ci_missing_wrong_provenance_denied(external, monkeypatch, event):
    root, manifest, commit = external
    commit()
    p = root / 'event.json'
    p.write_text(json.dumps(event))
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(p))
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


def test_valid_ci_base_admitted_and_child_env_has_no_credentials(external, monkeypatch):
    root, manifest, commit = external
    base = commit()
    event = root / 'event.json'
    event.write_text(json.dumps({'repository': {'full_name': 'ExternalExample/project'},
                                'pull_request': {'base': {'sha': base}}}))
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event))
    monkeypatch.setenv('ANTHROPIC_DIRECT_API_KEY', 'offline-value')
    calls = []
    original = policy.subprocess.run
    def capture(*args, **kwargs):
        calls.append(kwargs['env'])
        return original(*args, **kwargs)
    monkeypatch.setattr(policy.subprocess, 'run', capture)
    policy.require_direct_llm_admission()
    assert len(calls) == 2
    assert all(not SECRET_NAMES.intersection(env) for env in calls)


class NoCredentialReads(dict):
    def get(self, key, default=None):
        assert key not in SECRET_NAMES, 'credential accessed before admission'
        return super().get(key, default)
    def __getitem__(self, key):
        assert key not in SECRET_NAMES, 'credential accessed before admission'
        return super().__getitem__(key)


@pytest.mark.parametrize('name', ['run-specialists.py', 'run-judge.py', 'run-pr-classifier.py', 'run-broad-agents.py'])
@pytest.mark.parametrize('protected', [False, True])
def test_denial_precedes_all_direct_key_reads(monkeypatch, name, protected):
    module = load(name)
    env = NoCredentialReads(GITHUB_REPOSITORY='Manolii-org/ai-starter-pack')
    if protected:
        env['CLIENT_AI_POLICY'] = 'engagement'
        env['LITELLM_PROXY_URL'] = 'https://proxy.example'
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', env)
        with pytest.raises(policy.RouteAdmissionError):
            if name == 'run-broad-agents.py':
                # get_api_key is the generic transport resolver; force direct.
                env.pop('LITELLM_PROXY_URL', None)
                module.get_api_key()
            elif name == 'run-pr-classifier.py':
                module._endpoint()
            else:
                module._endpoint(direct=protected)


@pytest.mark.parametrize('name', ['run-specialists.py', 'run-judge.py', 'run-pr-classifier.py', 'run-broad-agents.py'])
def test_existing_proxy_resolution_unchanged_without_external_manifest(monkeypatch, name):
    module = load(name)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', {'GITHUB_REPOSITORY': 'Manolii-org/ai-starter-pack',
            'LITELLM_PROXY_URL': 'https://proxy.example', 'LLM_API_KEY': 'offline-proxy'})
        if name == 'run-broad-agents.py':
            assert module.get_api_key() == 'offline-proxy'
        else:
            assert module._endpoint() == ('offline-proxy', 'https://proxy.example/v1/messages', True)


def test_specialist_real_role_denial_persists_missing_coverage(monkeypatch, tmp_path):
    module = load('run-specialists.py')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', NoCredentialReads(
            GITHUB_REPOSITORY='Manolii-org/ai-starter-pack', LITELLM_PROXY_URL='https://proxy.example'))
        patch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('network dispatch'))
        result = module._invoke_skill('security-boundary-test', 'complete source', tmp_path)
    marker = json.loads((tmp_path / 'security-boundary-test.json').read_text())
    assert result[1] and marker['first_party'] is True
    assert marker['skipped'] == 'route_policy_denied'


def test_broad_role_denial_before_key_and_no_proxy_substitution(monkeypatch):
    module = load('run-broad-agents.py')
    config = module.AgentConfig('security', 'sonnet', 'internal', 'sys', 'instructions', first_party=True)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', NoCredentialReads(
            GITHUB_REPOSITORY='Manolii-org/ai-starter-pack', LITELLM_PROXY_URL='https://proxy.example'))
        patch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('network dispatch'))
        result = module.invoke_agent(config, '', 'complete source')
    assert result == {'source': 'security', 'findings': [], 'first_party': True, 'skipped': 'route_policy_denied'}


def test_judge_retains_unmet_review_and_post_target_does_not_change_identity(monkeypatch, tmp_path):
    module = load('run-judge.py')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'Manolii-org/ai-starter-pack')
    monkeypatch.setenv('GITHUB_TOKEN', 'offline-github')
    judge = module.Judge(str(tmp_path), 1, 'a' * 40)
    judge.repo = 'ExternalExample/project'
    judge._has_first_party_candidates = True
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', NoCredentialReads(
            GITHUB_REPOSITORY='Manolii-org/ai-starter-pack', LITELLM_PROXY_URL='https://proxy.example'))
        patch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('network dispatch'))
        assert judge.invoke_judge_agent([]) is None
    assert judge.repo == 'ExternalExample/project'


def test_external_actual_security_role_full_input_models_and_attempts_preserved(external, monkeypatch):
    root, manifest, commit = external
    commit()
    module = load('run-specialists.py')
    monkeypatch.setenv('ANTHROPIC_DIRECT_API_KEY', 'offline-direct')
    sent = []
    answer = json.dumps({'content': [{'type': 'text', 'text': '{"source":"security-boundary-test","findings":[]}'}]}).encode()
    def fake(req, **kwargs):
        sent.append(json.loads(req.data))
        return io.BytesIO(answer)
    monkeypatch.setattr(module, '_urlopen_https', fake)
    diff = 'diff --git a/source.py b/source.py\n' + '+assert protected\n' * 5000
    result = module._invoke_skill('security-boundary-test', diff, root)
    assert result[1] is None and len(sent) == 1
    assert sent[0]['model'] == 'claude-haiku-4-5-20251001'
    assert sent[0]['max_tokens'] == 800
    assert diff in sent[0]['messages'][0]['content']
    assert json.loads((root / 'security-boundary-test.json').read_text())['findings'] == []


def test_reusable_hydrates_shared_policy_dependency():
    import yaml
    data = yaml.safe_load((ROOT / '.github/workflows/pr-assessment-reusable.yml').read_text())
    assert 'scripts/lib/agent_capabilities.py' in data['env']['ASSESSMENT_RUNTIME_FILES'].split()


def test_generated_policy_and_entry_points_match_canonical():
    for prefix in ['plugin/manolii-framework', 'registry/platform/framework']:
        for path in ['scripts/lib/agent_capabilities.py', 'scripts/run-pr-classifier.py',
                     'scripts/run-specialists.py', 'scripts/run-judge.py', 'scripts/run-broad-agents.py']:
            assert (ROOT / prefix / path).read_bytes() == (ROOT / path).read_bytes()


@pytest.mark.parametrize('value', ['{broken', '[]', 'null'])
def test_malformed_committed_policy_denied(external, value):
    root, manifest, commit = external
    commit(value)
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


def test_missing_launcher_identity_denied_before_manifest(external, monkeypatch):
    root, manifest, commit = external
    commit()
    monkeypatch.delenv('GITHUB_REPOSITORY')
    with pytest.raises(policy.RouteAdmissionError, match='repository evidence'):
        policy.require_direct_llm_admission()


def test_policy_read_timeout_denied_without_credential_access(external, monkeypatch):
    root, manifest, commit = external
    commit()
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('git', 5)
    monkeypatch.setattr(policy.subprocess, 'run', timeout)
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


def test_specialist_cli_persists_policy_denial_before_any_key_read(monkeypatch, tmp_path):
    module = load('run-specialists.py')
    diff = tmp_path / 'input.diff'
    diff.write_text('+assert complete\n' * 4000)
    output = tmp_path / 'candidates'
    monkeypatch.setattr(sys, 'argv', ['run-specialists.py', '--skills', 'security-boundary-test',
        '--diff', str(diff), '--candidates-dir', str(output)])
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', NoCredentialReads(
            GITHUB_REPOSITORY='Manolii-org/ai-starter-pack', LITELLM_PROXY_URL='https://proxy.example'))
        patch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('network dispatch'))
        with pytest.raises(SystemExit) as exc:
            module.main()
    assert exc.value.code == 0  # existing exit semantics
    # A written denial marker is not success evidence.
    marker = json.loads((output / 'security-boundary-test.json').read_text())
    assert marker['first_party'] is True and marker['skipped'] == 'route_policy_denied'


def test_broad_cli_persists_policy_denial_before_any_key_read(monkeypatch, tmp_path):
    module = load('run-broad-agents.py')
    manifest = tmp_path / 'routing.json'
    manifest.write_text(json.dumps({'depth': 'broad', 'invoke_agents': ['security-deep-dive']}))
    diff = tmp_path / 'input.diff'
    diff.write_text('+assert complete\n')
    agents = tmp_path / 'agents'
    agents.mkdir()
    (agents / 'security-deep-dive.md').write_text('---\nname: security-deep-dive\nmodel: sonnet\nfirst_party: true\n---\nReview boundaries\n')
    output = tmp_path / 'output'
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', NoCredentialReads(
            GITHUB_REPOSITORY='Manolii-org/ai-starter-pack', LITELLM_PROXY_URL='https://proxy.example'))
        patch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('network dispatch'))
        assert module.run_broad_agents(manifest, diff, output, agents) == 0
    marker = json.loads((output / 'security-deep-dive.json').read_text())
    assert marker['first_party'] is True and marker['skipped'] == 'route_policy_denied'


def test_classifier_cli_policy_failure_does_not_fabricate_fallback(monkeypatch, tmp_path):
    module = load('run-pr-classifier.py')
    diff = tmp_path / 'input.diff'
    diff.write_text('+assert complete\n')
    output = tmp_path / 'classification.json'
    monkeypatch.setattr(sys, 'argv', ['run-pr-classifier.py', '--diff', str(diff),
        '--title', 'Review', '--output', str(output)])
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', NoCredentialReads(GITHUB_REPOSITORY='Manolii-org/ai-starter-pack'))
        patch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('network dispatch'))
        with pytest.raises(SystemExit) as exc:
            module.main()
    assert exc.value.code == 1 and not output.exists()


def test_judge_cli_repo_override_cannot_change_route_identity(monkeypatch, tmp_path):
    module = load('run-judge.py')
    monkeypatch.setattr(sys, 'argv', ['run-judge.py', '--candidates-dir', str(tmp_path),
        '--pr-number', '1', '--sha', 'a' * 40, '--repo', 'ExternalExample/project'])
    def run(self):
        assert self.repo == 'ExternalExample/project'
        assert module.os.environ['GITHUB_REPOSITORY'] == 'Manolii-org/ai-starter-pack'
        self._has_first_party_candidates = True
        assert self.invoke_judge_agent([]) is None
        return 0
    monkeypatch.setattr(module.Judge, 'run', run)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', NoCredentialReads(GITHUB_REPOSITORY='Manolii-org/ai-starter-pack'))
        patch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('network dispatch'))
        assert module.main() == 0


@pytest.mark.parametrize('name', ['run-specialists.py', 'run-judge.py', 'run-pr-classifier.py', 'run-broad-agents.py'])
def test_old_helper_preserves_proxy_but_never_admits_direct(monkeypatch, name):
    import types
    monkeypatch.setitem(sys.modules, 'lib.agent_capabilities', types.ModuleType('lib.agent_capabilities'))
    module = load(name)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'environ', {'GITHUB_REPOSITORY': 'ExampleOrg/project',
            'LITELLM_PROXY_URL': 'https://proxy.example', 'LLM_API_KEY': 'offline-proxy'})
        if name == 'run-broad-agents.py':
            assert module.get_api_key() == 'offline-proxy'
        else:
            assert module._endpoint()[0] == 'offline-proxy'
        module.os.environ.pop('LITELLM_PROXY_URL')
        with pytest.raises(module.RouteAdmissionError, match='policy runtime missing'):
            if name == 'run-broad-agents.py':
                module.get_api_key()
            else:
                module._endpoint()


def test_admitted_external_missing_key_keeps_protected_coverage_gap(external, monkeypatch):
    root, manifest, commit = external
    commit()
    module = load('run-specialists.py')
    monkeypatch.setattr(module, '_urlopen_https', lambda *a, **k: pytest.fail('network dispatch'))
    result = module._invoke_skill('security-boundary-test', 'complete source', root)
    marker = json.loads((root / 'security-boundary-test.json').read_text())
    assert result[1] and marker['first_party'] is True
    assert marker['skipped'] == 'no_direct_key'


def test_direct_admission_requires_explicit_mode_without_changing_resolver(external):
    root, manifest, commit = external
    manifest.pop('mode')
    commit()
    assert policy.resolve(root / '.ai/capabilities.json')['ANTHROPIC_API_KEY']['capability'] == 'llm-anthropic'
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


@pytest.mark.parametrize('name', ['run-specialists.py', 'run-judge.py', 'run-pr-classifier.py'])
def test_external_protected_endpoint_keeps_direct_route_and_key(external, monkeypatch, name):
    root, manifest, commit = external
    commit()
    module = load(name)
    monkeypatch.setenv('LITELLM_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('LLM_API_KEY', 'offline-proxy')
    monkeypatch.setenv('ANTHROPIC_DIRECT_API_KEY', 'offline-direct')
    if name == 'run-pr-classifier.py':
        monkeypatch.setenv('CLIENT_AI_POLICY', 'existing-engagement')
        endpoint = module._endpoint()
    else:
        endpoint = module._endpoint(direct=True)
    assert endpoint == ('offline-direct', 'https://api.anthropic.com/v1/messages', False)


def test_external_broad_protected_role_keeps_model_and_budget(external, monkeypatch):
    root, manifest, commit = external
    commit()
    module = load('run-broad-agents.py')
    monkeypatch.setenv('LITELLM_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('ANTHROPIC_DIRECT_API_KEY', 'offline-direct')
    sent = []
    answer = json.dumps({'content': [{'type': 'text', 'text': '{"findings":[]}'}]}).encode()
    def fake(req, **kwargs):
        sent.append((req, kwargs))
        return io.BytesIO(answer)
    monkeypatch.setattr(module, '_urlopen_https', fake)
    config = module.AgentConfig('security', 'sonnet', 'internal', 'sys', 'Review', first_party=True)
    result = module.invoke_agent(config, '', 'complete source')
    assert result['findings'] == [] and len(sent) == 1
    req, kwargs = sent[0]
    body = json.loads(req.data)
    assert req.full_url == 'https://api.anthropic.com/v1/messages'
    assert body['model'] == 'claude-sonnet-4-6' and body['max_tokens'] == 2400
    assert kwargs['timeout'] == module.TIMEOUT_SECS == 240


def test_failed_policy_enumeration_never_means_absence(external, monkeypatch):
    root, manifest, commit = external
    commit()
    monkeypatch.setattr(policy.subprocess, 'run', lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 128, stdout=b''))
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()


def test_valid_first_policy_plus_failed_second_blob_denied(external, monkeypatch):
    root, manifest, commit = external
    commit()
    commit(manifest, 'config/agent-capabilities.json')
    original = policy.subprocess.run
    reads = []
    def fail_second(args, **kwargs):
        assert '--no-lazy-fetch' in args
        if 'cat-file' in args:
            reads.append(args[-1])
            if len(reads) == 2:
                return subprocess.CompletedProcess(args, 128, stdout=b'')
        return original(args, **kwargs)
    monkeypatch.setattr(policy.subprocess, 'run', fail_second)
    with pytest.raises(policy.RouteAdmissionError):
        policy.require_direct_llm_admission()
    assert len(reads) == 2


def test_all_reusable_runner_checkouts_retain_base_objects():
    """Every admission caller must retain the immutable PR base locally."""
    import yaml
    data = yaml.safe_load((ROOT / '.github/workflows/pr-assessment-reusable.yml').read_text())
    for name in ['classify', 'specialists', 'broad-agents', 'judge']:
        checkout = next(step for step in data['jobs'][name]['steps']
                        if step.get('uses', '').startswith('actions/checkout@'))
        assert checkout['with']['fetch-depth'] == 0, name
        if name == 'judge':
            assert checkout['with']['persist-credentials'] is False


def test_external_pr_policy_base_requires_nonshallow_checkout(external, monkeypatch, tmp_path_factory):
    """Real shallow checkout refuses unavailable policy; full history admits it."""
    root, manifest, commit = external
    base = commit()
    commit({**manifest, 'fixture_note': 'newer head'})
    shallow = tmp_path_factory.mktemp('shallow-parent') / 'consumer'
    subprocess.run(['git', 'clone', '-q', '--depth=1', '--no-local',
                    root.as_uri(), str(shallow)], check=True)
    missing = subprocess.run(['git', '-C', str(shallow), 'cat-file', '-e', base],
                             capture_output=True)
    assert missing.returncode != 0
    event = root / 'event.json'
    event.write_text(json.dumps({'repository': {'full_name': 'ExternalExample/project'},
                                'pull_request': {'base': {'sha': base}}}))
    monkeypatch.chdir(shallow)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event))
    with pytest.raises(policy.RouteAdmissionError, match='policy source unavailable'):
        policy.require_direct_llm_admission()
    # Only a local fixture is fetched; admission itself must never lazy-fetch.
    subprocess.run(['git', 'fetch', '-q', '--unshallow', 'origin'], check=True)
    assert policy.require_direct_llm_admission() is None
