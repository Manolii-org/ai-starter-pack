#!/usr/bin/env python3
"""Independent sources/graphs and combined-runtime admission regression tests."""
import copy
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/actions/validate-release-manifest'))
release = importlib.import_module('release_manifest')
state = importlib.import_module('release_state')


def subject():
    value = json.loads((ROOT / 'scripts/tests/fixtures/release-manifest.v1.json').read_text())['subject']
    for key in ('source_sha', 'qualification', 'artifacts'):
        value.pop(key)
    value.update(entity='example', config_sha256='8' * 64, decision_sequence=['release'],
                 components=[], unchanged_components=[], qualifications=[],
                 execution={'run_id': 10, 'run_attempt': 1, 'job': 'execute', 'workflow_sha': value['control_sha']})
    source = {'repo': 'example/api', 'sha': 'a' * 40, 'tree': 'b' * 40}
    for index, name in enumerate(('api', 'worker')):
        value['components'].append({'id': name, 'source': {**source, 'repo': 'example/' + name, 'sha': str(index + 1) * 40},
            'promotion_sha': None, 'target': name, 'provider': 'fixture', 'artifact_id': name + '-candidate',
            'artifact_sha256': str(index + 3) * 64, 'config_sha256': '4' * 64,
            'before_artifact_id': name + '-previous', 'recovery_artifact_id': name + '-previous',
            'before_config_sha256': '4' * 64, 'before_features_sha256': release.digest([]),
            'recovery_artifact_sha256': '0' * 64, 'feature_states': [],
            'databases': ['db-' + name], 'features_sha256': release.digest([]),
            'depends_on': ['api'] if index else []})
    value['databases'] = [
        {'id': 'db-' + name, 'adapter': engine, 'source': {**source, 'repo': 'example/' + name},
         'writer': 'trusted-' + name, 'baseline': ['old-' + name], 'pending': ['new-' + name],
         'expected_after': ['old-' + name, 'new-' + name], 'migration_sha256': str(index + 6) * 64,
         'backup_required': True, 'fixture_version': 'synthetic-v1', 'compatible_components': [name]}
        for index, (name, engine) in enumerate((('api', 'alembic'), ('worker', 'journal')))]
    value['qualifications'] = [{'repo': 'example/control', 'workflow': 'qualification.yml', 'provider': 'github',
        'run_id': 10, 'run_attempt': 1, 'evidence_sha256': '9' * 64,
        'tested_subject_sha256': release.digest(release.qualification_subject(value)),
        'completed_at': '2026-10-31T23:00:00Z', 'retain_until': '2026-11-02T00:00:00Z',
        'first_attempt': 'success', 'retry_count': 0, 'status': 'success', 'obligations': []}]
    return value


def prepare(value=None):
    return release.prepare(subject() if value is None else value, schema_version=2)


class ReleaseVectorTests(unittest.TestCase):
    def test_native_binding_needs_exact_subject_controller_attempt_and_deadline_not_history(self):
        record = prepare()
        value = record['subject']
        context = {'repo': value['repo'], 'environment': value['environment'],
                   'controller_sha': value['control_sha'], 'release_id': record['release_id'],
                   'preparation_run_id': value['preparation']['run_id'],
                   'preparation_run_attempt': value['preparation']['run_attempt'],
                   'execution_run_id': value['execution']['run_id'],
                   'execution_run_attempt': value['execution']['run_attempt'],
                   'execution_job': value['execution']['job']}
        args = {'expected_release_id': record['release_id'], 'expected_controller_sha': value['control_sha'],
                'execution_context': context, 'now': datetime(2026, 11, 1, tzinfo=timezone.utc)}
        self.assertEqual(release.validate_native_binding(record, **args), record)
        for key, altered in [('release_id', '0' * 64), ('controller_sha', '0' * 40),
                             ('preparation_run_attempt', 2), ('execution_run_attempt', 2),
                             ('execution_job', 'other'), ('environment', 'different')]:
            with self.assertRaises(release.ManifestError):
                release.validate_native_binding(record, **{**args, 'execution_context': {**context, key: altered}})
        with self.assertRaises(release.ManifestError):
            release.validate_native_binding(record, **{**args, 'now': datetime(2026, 11, 2, tzinfo=timezone.utc)})

    def test_development_off_and_ready_kill_states_are_explicit_and_bound(self):
        value = subject()
        features = [{'id': 'ready-a', 'readiness': 'ready', 'default_enabled': True,
                     'intentional_off': True, 'killed': True, 'control_generation': 4},
                    {'id': 'unfinished-b', 'readiness': 'development', 'default_enabled': False,
                     'intentional_off': False, 'killed': False, 'control_generation': 0}]
        value['components'][0].update(feature_states=features, features_sha256=release.digest(features))
        value['qualifications'][0]['tested_subject_sha256'] = release.digest(release.qualification_subject(value))
        record = prepare(value)
        self.assertTrue(record['subject']['components'][0]['feature_states'][0]['killed'])
        features[1]['default_enabled'] = True
        value['components'][0]['features_sha256'] = release.digest(features)
        value['qualifications'][0]['tested_subject_sha256'] = release.digest(release.qualification_subject(value))
        with self.assertRaises(release.ManifestError):
            prepare(value)

    def test_independent_sources_and_graphs_one_subject(self):
        record = prepare()
        self.assertNotEqual(record['subject']['components'][0]['source']['sha'], record['subject']['components'][1]['source']['sha'])
        self.assertNotEqual(record['subject']['databases'][0]['migration_sha256'], record['subject']['databases'][1]['migration_sha256'])
        self.assertEqual(release.validate(record, expected_release_id=record['release_id']), record)

    def test_one_component_uses_identical_reader(self):
        value = subject()
        value['components'].pop()
        value['databases'].pop()
        value['qualifications'][0]['tested_subject_sha256'] = release.digest(release.qualification_subject(value))
        self.assertEqual(len(prepare(value)['subject']['components']), 1)

    def test_changed_combined_inputs_invalidate_qualification_even_after_rehash(self):
        mutations = [lambda v: v['components'][1]['source'].update(sha='0' * 40),
            lambda v: v['components'][0].update(config_sha256='0' * 64),
            lambda v: v['components'][0].update(features_sha256='0' * 64),
            lambda v: v['databases'][1].update(migration_sha256='0' * 64),
            lambda v: v.update(control_sha='0' * 40), lambda v: v.update(policy_sha256='0' * 64)]
        for mutate in mutations:
            value = subject()
            mutate(value)
            with self.assertRaises(release.ManifestError):
                prepare(value)

    def test_illegal_vectors_and_expired_evidence_fail_closed(self):
        mutations = [lambda v: v['components'].append(copy.deepcopy(v['components'][0])),
            lambda v: v['components'][0]['source'].update(repo='other/product'),
            lambda v: v['components'][0].update(depends_on=['worker']),
            lambda v: v['databases'][0].update(pending=['old-api']),
            lambda v: v['databases'][0].update(compatible_components=['worker']),
            lambda v: v['qualifications'][0].update(retain_until='2026-11-01T00:00:00Z'),
            lambda v: v['qualifications'][0].update(status='skipped'),
            lambda v: v['qualifications'][0].update(first_attempt='failure'),
            lambda v: v['qualifications'][0].update(obligations=['not-run'])]
        for mutate in mutations:
            value = subject()
            mutate(value)
            value['qualifications'][0]['tested_subject_sha256'] = release.digest(release.qualification_subject(value))
            with self.assertRaises(release.ManifestError):
                prepare(value)

    def test_v1_schema_bytes_and_default_prepare_remain_compatible(self):
        value = json.loads((ROOT / 'scripts/tests/fixtures/release-manifest.v1.json').read_text())
        self.assertEqual(release.prepare(value['subject']), value)
        schema = json.loads((ROOT / 'schemas/release-manifest.v1.schema.json').read_text())
        self.assertTrue(list(release.jsonschema.Draft202012Validator(schema).iter_errors(prepare())))

    def test_v2_cannot_be_silently_admitted_by_legacy_state(self):
        with self.assertRaises(state.StateUnavailable):
            state.reserve({'schema_version': 1, 'active': None, 'continuations': {}}, prepare(),
                          now=datetime(2026, 11, 1, tzinfo=timezone.utc))

    def test_completed_replay_is_refused_after_intervening_release_and_restart(self):
        empty = {'schema_version': 2, 'active': None, 'continuations': {}, 'admitted': []}
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        first = prepare()
        reserved = state.reserve(empty, first, now=now)
        self.assertIsNone(empty['active'])
        reserved['active']['phase'] = 'success'
        with self.assertRaises(state.StateConflict):
            state.reserve(reserved, first, now=now)
        second_value = subject()
        second_value['preparation']['run_id'] += 1
        second = prepare(second_value)
        reserved = state.reserve(reserved, second, now=now)
        reserved['active']['phase'] = 'success'
        restarted = json.loads(json.dumps(reserved))
        with self.assertRaises(state.StateConflict):
            state.reserve(restarted, first, now=now)
        self.assertEqual(restarted['admitted'], [first['release_id'], second['release_id']])

    def test_partial_stage_holds_new_selection(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        reserved = state.reserve({'schema_version': 2, 'active': None, 'continuations': {}, 'admitted': []}, prepare(), now=now)
        reserved['active']['phase'] = 'schema-partial'
        value = subject()
        value['preparation']['run_id'] += 1
        with self.assertRaises(state.StateConflict):
            state.reserve(reserved, prepare(value), now=now)


if __name__ == '__main__':
    unittest.main()
