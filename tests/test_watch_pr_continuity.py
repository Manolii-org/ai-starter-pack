"""Execute the authored/generated checkpoint program, not a duplicate writer."""
import json
import fcntl
import hashlib
import re
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / '.claude/commands/watch-pr.md.jinja',
           ROOT / 'plugin/manolii-framework/commands/watch-pr.md',
           ROOT / 'registry/platform/framework/commands/watch-pr.md']


def script_for(source):
    return (ROOT if source == SOURCES[0] else source.parent.parent) / 'scripts/pr-watch-observe.py'


def program(source):
    return script_for(source).read_text()


def environment(**changes):
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
    return {**env, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'WATCH_OWNER': 'owner-a', 'WATCH_REPOSITORY': 'sample/project',
            'WATCH_PR': '7', 'WATCH_HEAD': 'a' * 40, 'WATCH_PHASE': 'watching', **changes}


def run(source, directory, **changes):
    return subprocess.run([sys.executable, '-I', str(script_for(source))], cwd=directory,
                          env=environment(**changes), capture_output=True, text=True, timeout=10)


def store(checkpoint):
    paths = list(checkpoint.parent.glob(checkpoint.name + '.pr-watches-*.json'))
    assert len(paths) == 1
    return paths[0]


@pytest.fixture
def checkpoint(tmp_path):
    path = tmp_path / '.ai/sessions/active-task.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(status='active', type='parent-task', round=4,
        continuation=dict(version=1, owner='owner-a', repository='sample/project',
                          commitments=['implementation', 'post-merge-verification']),
        native_owner_budget=dict(requests=4, reservations=['retained']),
        owner_follow_up=dict(pending=['new-feedback']),
        interrupt_value=dict(reason='production approval only'))))
    return path


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('phase', ['watching', 'merged', 'closed', 'verifying'])
def test_observation_preserves_parent_holds_counters_and_future_work(checkpoint, source, phase):
    before = json.loads(checkpoint.read_text())
    result = run(source, checkpoint.parents[2], WATCH_PHASE=phase)
    assert result.returncode == 0, result.stderr
    after = json.loads(checkpoint.read_text())
    assert {key: after[key] for key in before} == before
    assert json.loads(store(checkpoint).read_text())['watches']['sample/project#7']['phase'] == phase
    assert checkpoint.read_text() == json.dumps(before)
    assert after['status'] == 'active' and after['round'] == 4


@pytest.mark.parametrize('source', SOURCES)
def test_unchanged_observation_does_not_rewrite_or_spend(checkpoint, source):
    assert run(source, checkpoint.parents[2]).returncode == 0
    target = store(checkpoint)
    before, inode, modified = target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns
    assert run(source, checkpoint.parents[2]).returncode == 0
    assert target.read_bytes() == before
    assert target.stat().st_ino == inode and target.stat().st_mtime_ns == modified


@pytest.mark.parametrize('source', SOURCES)
def test_new_head_retains_existing_watch_extensions_and_lifetime_budget(checkpoint, source):
    assert run(source, checkpoint.parents[2]).returncode == 0
    parent = checkpoint.read_bytes()
    target = store(checkpoint)
    task = json.loads(target.read_text())
    task['watches']['sample/project#7']['repair_budget'] = dict(consumed=4, maximum=5)
    target.write_text(json.dumps(task))
    assert run(source, checkpoint.parents[2], WATCH_HEAD='b' * 40).returncode == 0
    after = json.loads(target.read_text())
    assert after['watches']['sample/project#7']['repair_budget'] == dict(consumed=4, maximum=5)
    assert checkpoint.read_bytes() == parent


@pytest.mark.parametrize('failure', ['malformed', 'missing_owner', 'foreign_owner', 'foreign_repository',
                                     'unknown_version', 'missing_repository', 'invalid_head', 'invalid_phase', 'unknown_watches'])
def test_invalid_custody_never_replaces_the_original(checkpoint, failure):
    task = json.loads(checkpoint.read_text())
    env = {}
    if failure == 'missing_owner':
        del task['continuation']['owner']
    if failure == 'foreign_owner':
        task['continuation']['owner'] = 'owner-b'
    if failure == 'foreign_repository':
        task['continuation']['repository'] = 'other/project'
    if failure == 'missing_repository':
        del task['continuation']['repository']
    if failure == 'unknown_version':
        task['continuation']['version'] = 2
    if failure == 'invalid_head':
        env['WATCH_HEAD'] = 'unverified'
    if failure == 'invalid_phase':
        env['WATCH_PHASE'] = 'done'
    if failure == 'unknown_watches':
        task['pr_watches'] = ['unknown']
    checkpoint.write_text('malformed JSON' if failure == 'malformed' else json.dumps(task))
    before = checkpoint.read_bytes()
    assert run(SOURCES[0], checkpoint.parents[2], **env).returncode != 0
    assert checkpoint.read_bytes() == before


def test_missing_original_checkpoint_is_not_reconstructed(tmp_path):
    assert run(SOURCES[0], tmp_path).returncode != 0
    assert not (tmp_path / '.ai').exists()


def test_concurrent_observations_keep_both_prs_and_parent(checkpoint):
    before = json.loads(checkpoint.read_text())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, SOURCES[0], checkpoint.parents[2], WATCH_PR=str(n)) for n in (7, 8)]
        assert all(future.result().returncode == 0 for future in futures)
    after = json.loads(checkpoint.read_text())
    assert set(json.loads(store(checkpoint).read_text())['watches']) == {'sample/project#7', 'sample/project#8'}
    assert {key: after[key] for key in before} == before


def test_generated_commands_keep_review_and_wait_policy():
    for source in SOURCES:
        text = source.read_text()
        assert 'Qualifying independent current-head review' in text
        assert 'Without qualified transport, create no new timer' in text
        assert 'Preserve existing user-authorized schedules' in text
        assert 'Count failed repair attempts' in text
        assert 'At least 5 minutes elapsed' not in text
        assert 'Otherwise increment round' not in text
        assert 'Set checkpoint `status: "merged"`' not in text
        assert 'except Exception:' not in program(source)


def test_foreign_watch_owner_is_not_overwritten(checkpoint):
    task = json.loads(checkpoint.read_text())
    task['pr_watches'] = {'sample/project#7': dict(owner='owner-b', phase='watching')}
    checkpoint.write_text(json.dumps(task))
    before = checkpoint.read_bytes()
    assert run(SOURCES[0], checkpoint.parents[2]).returncode != 0
    assert checkpoint.read_bytes() == before


@pytest.mark.parametrize('field,value', [('repository', 'other/project'), ('pr', 8), ('pr', True)])
def test_foreign_watch_scope_requires_original_owner_reconciliation(checkpoint, field, value):
    assert run(SOURCES[0], checkpoint.parents[2]).returncode == 0
    target = store(checkpoint)
    task = json.loads(target.read_text())
    task['watches']['sample/project#7'][field] = value
    target.write_text(json.dumps(task))
    before, parent = target.read_bytes(), checkpoint.read_bytes()
    assert run(SOURCES[0], checkpoint.parents[2]).returncode != 0
    assert target.read_bytes() == before and checkpoint.read_bytes() == parent


def test_checkpoint_link_cannot_redirect_the_writer(checkpoint):
    target = checkpoint.with_name('other-task.json')
    checkpoint.rename(target)
    checkpoint.symlink_to(target)
    before = target.read_bytes()
    assert run(SOURCES[0], checkpoint.parents[2]).returncode != 0
    assert target.read_bytes() == before and checkpoint.is_symlink()


def test_capacity_retains_all_unfinished_watches(checkpoint):
    task = json.loads(checkpoint.read_text())
    task['pr_watches'] = {f'sample/project#{n}': dict(owner='owner-a', phase='watching') for n in range(1, 101)}
    checkpoint.write_text(json.dumps(task))
    before = checkpoint.read_bytes()
    assert run(SOURCES[0], checkpoint.parents[2], WATCH_PR='101').returncode != 0
    assert checkpoint.read_bytes() == before


@pytest.mark.parametrize('location', ['legacy', 'sidecar'])
def test_existing_null_entry_is_never_reconstructed(checkpoint, location):
    parent = checkpoint.read_bytes()
    if location == 'legacy':
        target = checkpoint
        data = json.loads(parent)
        data['pr_watches'] = {'sample/project#7': None}
    else:
        assert run(SOURCES[0], checkpoint.parents[2]).returncode == 0
        target = store(checkpoint)
        data = json.loads(target.read_text())
        data['watches']['sample/project#7'] = None
    target.write_text(json.dumps(data))
    before = target.read_bytes()
    assert run(SOURCES[0], checkpoint.parents[2]).returncode != 0
    assert target.read_bytes() == before
    if location == 'sidecar':
        assert checkpoint.read_bytes() == parent


def test_unlocked_parent_update_cannot_be_lost_to_observer(checkpoint):
    assert run(SOURCES[0], checkpoint.parents[2]).returncode == 0
    target = store(checkpoint)
    with target.with_name(target.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen([sys.executable, '-I', str(script_for(SOURCES[0]))],
            cwd=checkpoint.parents[2], env=environment(WATCH_HEAD='b'*40),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            changed = json.loads(checkpoint.read_text())
            changed['continuation']['commitments'].append('new parent acceptance')
            changed['native_owner_budget']['requests'] = 5
            checkpoint.write_text(json.dumps(changed))  # A legacy unlocked writer.
            retained = checkpoint.read_bytes()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
        process.communicate(timeout=10)
        assert process.returncode == 0
    assert checkpoint.read_bytes() == retained


def bootstrap(source):
    return next(block for block in re.findall(r'```bash\n(.*?)\n```', source.read_text(), re.DOTALL)
                if 'pr-watch-observer:verified-bootstrap/v1' in block)


def test_verified_bootstrap_pin_covers_exact_bundled_bytes():
    for source in SOURCES:
        expected = re.search(r"expected='([0-9a-f]{64})'", bootstrap(source))[1]
        assert expected == hashlib.sha256(script_for(source).read_bytes()).hexdigest()
        assert 'python3 -I "$stage"' in bootstrap(source)


def commit_helper(root, content):
    env = environment()
    subprocess.run(['git', 'init', '-q'], cwd=root, env=env, check=True, capture_output=True)
    helper = root / 'scripts/pr-watch-observe.py'
    helper.parent.mkdir(exist_ok=True)
    helper.write_bytes(content)
    subprocess.run(['git', 'add', 'scripts/pr-watch-observe.py'], cwd=root, env=env, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'Fixed observation fixture'], cwd=root, env=env, check=True)
    return helper


@pytest.mark.parametrize('source', SOURCES)
def test_untrusted_committed_helper_is_not_executed(checkpoint, source):
    root = checkpoint.parents[2]
    commit_helper(root, b"from pathlib import Path\nPath('executed-untrusted').touch()\n")
    before = checkpoint.read_bytes()
    result = subprocess.run(['bash', '-c', bootstrap(source)], cwd=root, env=environment(),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert not (root / 'executed-untrusted').exists()
    assert checkpoint.read_bytes() == before
    assert not list((root / '.git').glob('pr-watch-observer.*'))


@pytest.mark.parametrize('source', SOURCES)
def test_bootstrap_uses_verified_object_not_worktree_or_pythonpath(checkpoint, source):
    root = checkpoint.parents[2]
    helper = commit_helper(root, script_for(source).read_bytes())
    malicious = b"from pathlib import Path\nPath('executed-untrusted').touch()\n"
    helper.write_bytes(malicious)
    (root / 'json.py').write_bytes(malicious)
    (root / 'sitecustomize.py').write_bytes(malicious)
    before = checkpoint.read_bytes()
    result = subprocess.run(['bash', '-c', bootstrap(source)], cwd=root,
        env=environment(PYTHONPATH=str(root)), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not (root / 'executed-untrusted').exists()
    assert checkpoint.read_bytes() == before
    assert store(checkpoint).is_file()
    assert not list((root / '.git').glob('pr-watch-observer.*'))
