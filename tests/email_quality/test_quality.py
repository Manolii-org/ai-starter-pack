"""Contract, wire, adversarial and real sender-subprocess tests (offline)."""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import jsonschema

from email_quality.cli import main
from email_quality.profile import QualityError, load
from email_quality.provider import Unspam
from email_quality.runner import assess, delivery, send, start, wait
from email_quality.state import State

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = json.loads((ROOT / 'config/email-quality.example.json').read_text())
RECEIPT = json.loads((ROOT / 'schemas/email-quality/receipt.schema.json').read_text())


class FakeClient:
    def __init__(self):
        self.creates = 0
        self.reads = 0

    def status(self, timeout=15):
        return {'spam_checks': 20, 'inbox_placements': 20}

    def create(self, kind, timeout=15):
        self.creates += 1
        return {'id': 'opaque-id', 'inbox_address': 'synthetic@example.test',
                'mailboxes': [{'id': 1, 'address': 'seed@example.test'}]}

    def result(self, kind, identifier, timeout):
        self.reads += 1
        return {'id': identifier, 'status': 'completed', 'score': 90,
                'results': [{'mailbox': {'id': 1}, 'status': 'inbox'}]}


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = copy.deepcopy(EXAMPLE)
        self.profile_path = self.root / 'profile.json'
        self.profile_path.write_text(json.dumps(self.profile))
        self.env = patch.dict(os.environ, {'UNSPAM_API_TOKEN': 'sentinel-provider-token'}, clear=False)
        self.env.start()
        self.repo_env = patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example/application'})
        self.repo_env.start()

    def tearDown(self):
        self.repo_env.stop()
        self.env.stop()
        self.tmp.cleanup()

    def state(self):
        return State(self.root / 'state', self.profile)

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['--profile', str(self.profile_path), '--state', str(self.root / 'state'), *args])
        result = json.loads(output.getvalue())
        jsonschema.validate(result, RECEIPT)
        self.assertNotIn('sentinel-provider-token', output.getvalue())
        return code, result

    def test_example_profile_and_schema_agree(self):
        jsonschema.validate(self.profile, json.loads((ROOT / 'schemas/email-quality/profile.schema.json').read_text()))
        self.assertEqual(load(self.profile_path), self.profile)

    def test_invalid_profiles(self):
        for key, value in [('minimum_score', float('nan')), ('minimum_remaining', True),
                           ('timeout_seconds', float('inf')), ('environment', 'prd'),
                           ('shared_account_acknowledged', False), ('checks', []),
                           ('checks', ['spam', 'spam']), ('repository', 'other/application'),
                           ('checks', ['screenshots']), ('minimum_inbox_ratio', -1)]:
            with self.subTest(key=key, value=value):
                data = dict(self.profile, **{key: value})
                self.profile_path.write_text(json.dumps(data))
                with self.assertRaises(QualityError):
                    load(self.profile_path)

    def test_unknown_profile_key_is_rejected(self):
        self.profile_path.write_text(json.dumps(dict(self.profile, token='must-not-be-here')))
        with self.assertRaises(QualityError):
            load(self.profile_path)

    def test_status_is_ready_not_delivery_pass(self):
        with patch('email_quality.cli.Unspam', return_value=FakeClient()):
            code, result = self.invoke('status')
        self.assertEqual((code, result['outcome']), (0, 'ready'))

    def test_run_shares_one_deadline_and_preserves_completed_results(self):
        clock = [0]
        client = FakeClient()
        budgets = []
        def sender(state, record, path, timeout, **kwargs):
            budgets.append(timeout)
            clock[0] += 60 if record['delivery']['kind'] == 'spam' else 30
            record['phase'] = 'sent'
        def result(kind, identifier, timeout):
            budgets.append(timeout)
            if kind == 'spam':
                clock[0] += 60
                return {'id': identifier, 'status': 'completed', 'score': 90}
            clock[0] += timeout
            return {'id': identifier, 'status': 'pending'}
        client.result = result
        with patch('email_quality.cli.Unspam', return_value=client), \
                patch('email_quality.cli.time.monotonic', side_effect=lambda: clock[0]), \
                patch('email_quality.cli.send', side_effect=sender), \
                patch('email_quality.runner.time.sleep'):
            code, receipt = self.invoke('--sender', 'trusted', 'run')
        self.assertEqual(code, 1)
        self.assertEqual(receipt['outcome'], 'blocked')
        self.assertEqual(receipt['error_code'], 'RESULT_TIMEOUT')
        self.assertEqual(receipt['results'], [{'kind': 'spam', 'score': 90, 'passed': True}])
        self.assertEqual(budgets, [180, 120, 60, 30])
        self.assertLessEqual(receipt['duration_seconds'], self.profile['timeout_seconds'])

    def test_expired_budget_does_not_create_another_paid_check(self):
        clock = [0]
        client = FakeClient()
        def sender(*args, **kwargs):
            clock[0] = 180
        with patch('email_quality.cli.Unspam', return_value=client), \
                patch('email_quality.cli.time.monotonic', side_effect=lambda: clock[0]), \
                patch('email_quality.cli.send', side_effect=sender):
            code, receipt = self.invoke('--sender', 'trusted', 'run')
        self.assertEqual((code, receipt['error_code']), (1, 'RESULT_TIMEOUT'))
        self.assertEqual(client.creates, 1)

    def test_budget_expiring_during_quota_check_does_not_post(self):
        clock = [0]
        client = FakeClient()
        def status(timeout=15):
            clock[0] = 180
            return {'spam_checks': 20, 'inbox_placements': 20}
        client.status = status
        state = self.state()
        try:
            with patch('email_quality.runner.time.monotonic', side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(QualityError, 'RUN_TIMEOUT'):
                    start(client, state, self.profile, 'spam', deadline=180)
            self.assertEqual(client.creates, 0)
            self.assertEqual(state.data['checks'], {})
        finally:
            state.close()

    def test_slow_create_checkpoint_never_posts_after_deadline(self):
        clock = [0]
        client = FakeClient()
        state = self.state()
        original_save = state.save
        def slow_save():
            original_save()
            clock[0] = 2
        state.save = slow_save
        try:
            with patch('email_quality.runner.time.monotonic', side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(QualityError, 'RUN_TIMEOUT'):
                    start(client, state, self.profile, 'spam', deadline=1)
            self.assertEqual(client.creates, 0)
            self.assertEqual(json.loads((state.directory / 'state.json').read_text())['checks'], {})
        finally:
            state.close()

    def test_slow_sender_checkpoint_never_launches_after_deadline(self):
        sender = self.root / 'sender'
        sender.write_text('#!/bin/sh\nexit 0\n')
        sender.chmod(0o700)
        clock = [0]
        state = self.state()
        record = start(FakeClient(), state, self.profile, 'spam')
        original_save = state.save
        def slow_save():
            original_save()
            clock[0] = 2
        state.save = slow_save
        try:
            with patch('email_quality.runner.time.monotonic', side_effect=lambda: clock[0]), \
                    patch('email_quality.runner.subprocess.Popen') as spawn:
                with self.assertRaisesRegex(QualityError, 'RUN_TIMEOUT'):
                    send(state, record, sender, 1, deadline=1)
            spawn.assert_not_called()
            self.assertEqual(record['phase'], 'created')
            self.assertFalse((state.directory / 'delivery.json').exists())
        finally:
            state.close()

    def test_sender_subprocess_roundtrip_and_resume(self):
        sender = self.root / 'sender'
        sender.write_text('#!/usr/bin/env python3\nimport os,json,sys\np=json.load(open(sys.argv[1]))\nassert "UNSPAM_API_TOKEN" not in os.environ\nassert "GH_TOKEN" not in os.environ\nassert p["recipients"]\nassert p["kind"] != "inbox" or p["body_marker"] == p["test_id"]\nprint("must-not-appear-in-receipt")\n')
        sender.chmod(0o700)
        client = FakeClient()
        with patch('email_quality.cli.Unspam', return_value=client):
            code, result = self.invoke('--sender', str(sender), 'run')
            again, resumed = self.invoke('--sender', str(sender), 'run')
        self.assertEqual((code, again), (0, 0))
        self.assertEqual(client.creates, 2)
        self.assertEqual(client.reads, 2)
        self.assertEqual(result['results'], resumed['results'])
        self.assertNotIn('recipients', json.dumps(result))
        self.assertNotIn('opaque-id', json.dumps(result))
        self.assertFalse((self.root / 'state/delivery.json').exists())

    def test_start_writes_private_delivery_without_asserting_pass(self):
        with patch('email_quality.cli.Unspam', return_value=FakeClient()):
            code, result = self.invoke('--kind', 'inbox', 'start')
        self.assertEqual((code, result['outcome']), (0, 'started'))
        path = self.root / 'state/delivery-inbox.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        data = json.loads(path.read_text())
        self.assertEqual(data['body_marker'], data['test_id'])

    def test_quota_reserve_blocks_before_mutation(self):
        client = FakeClient()
        client.status = lambda: {'spam_checks': 4, 'inbox_placements': 0}
        state = self.state()
        try:
            with self.assertRaisesRegex(QualityError, 'QUOTA_RESERVE'):
                start(client, state, self.profile, 'spam')
            self.assertEqual(client.creates, 0)
        finally:
            state.close()

    def test_ambiguous_post_is_not_replayed(self):
        client = FakeClient()
        def fail(kind):
            client.creates += 1
            raise QualityError('PROVIDER_UNAVAILABLE')
        client.create = fail
        state = self.state()
        try:
            with self.assertRaises(QualityError):
                start(client, state, self.profile, 'spam')
            with self.assertRaisesRegex(QualityError, 'CREATE_OUTCOME_UNKNOWN'):
                start(client, state, self.profile, 'spam')
            self.assertEqual(client.creates, 1)
        finally:
            state.close()

    def test_scope_binding_lock_and_symlinks(self):
        state = self.state()
        state.save()
        try:
            with self.assertRaisesRegex(QualityError, 'RUN_BUSY'):
                self.state()
        finally:
            state.close()
        with self.assertRaisesRegex(QualityError, 'STATE_SCOPE_MISMATCH'):
            State(self.root / 'state', dict(self.profile, minimum_score=99))
        (self.root / 'link').symlink_to(self.root / 'state', target_is_directory=True)
        with self.assertRaises(QualityError):
            State(self.root / 'link', self.profile)

    def test_wrong_result_id_and_malformed_scores_fail_closed(self):
        client = FakeClient()
        state = self.state()
        try:
            start(client, state, self.profile, 'spam')
            client.result = lambda *args: {'id': 'wrong', 'status': 'completed', 'score': 100}
            with self.assertRaisesRegex(QualityError, 'PROVIDER_INVALID'):
                wait(client, state, self.profile, 'spam')
        finally:
            state.close()
        for score in [None, True, float('nan'), float('inf'), 101, '90']:
            with self.subTest(score=score), self.assertRaises(QualityError):
                assess(self.profile, 'spam', {'status': 'completed', 'score': score})

    def test_assertion_failure_is_not_infrastructure_failure(self):
        result = assess(self.profile, 'spam', {'status': 'completed', 'score': 20})
        self.assertFalse(result['passed'])
        result = assess(self.profile, 'inbox', {'status': 'completed', 'results': [{'status': 'spam'}]})
        self.assertFalse(result['passed'])
        self.assertEqual(result['inbox_ratio'], 0)

    def test_live_provider_success_and_tab_are_distinct(self):
        result = assess(self.profile, 'inbox', {'status': 'success', 'results': [
            {'status': 'success', 'tab': 'Inbox'}, {'status': 'success', 'tab': 'Spam'}]})
        self.assertEqual(result['inbox_ratio'], 0.5)
        self.assertFalse(result['passed'])
        with self.assertRaises(QualityError):
            assess(self.profile, 'inbox', {'status': 'success', 'results': [{'status': 'success'}]})

    def test_unknown_and_empty_results_are_not_success(self):
        for data in [{'status': 'completed', 'results': []}, {'status': 'surprise'},
                     {'status': 'completed', 'results': [{'status': 'unknown'}]}]:
            with self.subTest(data=data), self.assertRaises(QualityError):
                assess(self.profile, 'inbox', data)

    def test_delivery_rejects_path_injection_and_bad_recipients(self):
        for data in [{'id': '../status', 'inbox_address': 'safe@example.test'},
                     {'id': 'safe', 'inbox_address': 'bad\nBcc: victim@example.test'}]:
            with self.assertRaises(QualityError):
                delivery('spam', data)

    def test_sender_failure_does_not_replay_send(self):
        sender = self.root / 'fail'
        sender.write_text('#!/bin/sh\nexit 1\n')
        sender.chmod(0o700)
        state = self.state()
        try:
            record = start(FakeClient(), state, self.profile, 'spam')
            with self.assertRaisesRegex(QualityError, 'SENDER_FAILED'):
                send(state, record, sender, 1)
            with self.assertRaisesRegex(QualityError, 'SEND_OUTCOME_UNKNOWN'):
                send(state, record, sender, 1)
        finally:
            state.close()

    def test_failed_sender_spawn_resumes_without_another_paid_test(self):
        self.profile['checks'] = ['spam']
        self.profile_path.write_text(json.dumps(self.profile))
        sender = self.root / 'bad-interpreter'
        sender.write_text('#!/missing/email-quality/interpreter\n')
        sender.chmod(0o700)
        client = FakeClient()
        with patch('email_quality.cli.Unspam', return_value=client):
            code, receipt = self.invoke('--sender', str(sender), 'run')
            self.assertEqual((code, receipt['error_code']), (1, 'SENDER_INVALID'))
            data = json.loads((self.root / 'state/state.json').read_text())
            self.assertEqual(data['checks']['spam']['phase'], 'created')
            sender.write_text('#!/bin/sh\nexit 0\n')
            resumed, result = self.invoke('--sender', str(sender), 'run')
        self.assertEqual((resumed, result['outcome']), (0, 'passed'))
        self.assertEqual(client.creates, 1)
        self.assertFalse((self.root / 'state/delivery.json').exists())

    def test_symlink_checkpoint_never_overwrites_target(self):
        state = self.state()
        target = self.root / 'victim'
        target.write_text('preserve')
        (state.directory / 'state.tmp').symlink_to(target)
        try:
            with self.assertRaises(OSError):
                state.save()
            self.assertEqual(target.read_text(), 'preserve')
        finally:
            state.close()

    def test_pending_poll_is_bounded(self):
        client = FakeClient()
        client.result = lambda *args: {'id': 'opaque-id', 'status': 'waiting'}
        state = self.state()
        try:
            start(client, state, self.profile, 'spam')
            with patch('email_quality.runner.time.monotonic', side_effect=[0, 0, 200, 200]), patch('email_quality.runner.time.sleep'):
                with self.assertRaisesRegex(QualityError, 'RESULT_TIMEOUT'):
                    wait(client, state, dict(self.profile, timeout_seconds=1), 'spam')
        finally:
            state.close()

    def test_partial_duplicate_or_foreign_mailbox_results_fail(self):
        for ids in ([1], [1, 1], [1, 3], [True, 2], [1.0, 2]):
            with self.subTest(ids=ids):
                state = State(self.root / ('state-' + str(ids)), self.profile)
                client = FakeClient()
                original = client.create
                def create(kind):
                    data = original(kind)
                    data['mailboxes'].append({'id': 2, 'address': 'other@example.test'})
                    return data
                client.create = create
                client.result = lambda *args: {'id': 'opaque-id', 'status': 'completed',
                    'results': [{'mailbox': {'id': i}, 'status': 'inbox'} for i in ids]}
                try:
                    start(client, state, self.profile, 'inbox')
                    with self.assertRaisesRegex(QualityError, 'PROVIDER_INVALID'):
                        wait(client, state, self.profile, 'inbox')
                finally:
                    state.close()

    def test_sender_timeout_kills_descendants(self):
        import shlex
        import time
        pidfile = self.root / 'child.pid'
        sender = self.root / 'descendant'
        sender.write_text('#!/bin/sh\nsleep 30 &\nprintf "%s" "$!" > '
                          + shlex.quote(str(pidfile)) + '\nwait\n')
        sender.chmod(0o700)
        state = self.state()
        try:
            record = start(FakeClient(), state, self.profile, 'spam')
            with self.assertRaisesRegex(QualityError, 'SEND_OUTCOME_UNKNOWN'):
                send(state, record, sender, 2)
            pid = int(pidfile.read_text())  # A missing handshake must fail, never pass vacuously.
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                    # Linux may retain an orphan as a zombie until init reaps it.
                    stopped = Path('/proc/' + str(pid) + '/stat').read_text().split()[2] == 'Z'
                except (ProcessLookupError, FileNotFoundError):
                    stopped = True
                if stopped:
                    break
                time.sleep(0.01)
            self.assertTrue(stopped, 'sender descendant is still running')
        finally:
            state.close()

    def test_workflow_secrets_and_trust_contract(self):
        text = (ROOT / '.github/workflows/email-quality-reusable.yml').read_text()
        self.assertIn('github.event.repository.default_branch', text)
        self.assertNotIn('pull_request_target', text)
        self.assertIn('cancel-in-progress: false', text)
        self.assertIn('retention-days: 7', text)
        self.assertNotIn('secrets: inherit', text)
        self.assertIn('UNSPAM_API_TOKEN: ${{ secrets.UNSPAM_API_TOKEN }}', text)


class WireTests(unittest.TestCase):
    def client(self):
        with patch.dict(os.environ, {'UNSPAM_API_TOKEN': 'wire-secret'}, clear=True):
            return Unspam()

    def test_rejects_origin_override_and_missing_token(self):
        for env in [{}, {'UNSPAM_API_TOKEN': 'x', 'UNSPAM_API_BASE_URL': 'https://attacker.example'},
                    {'UNSPAM_API_TOKEN': 'bad\nheader'}]:
            with patch.dict(os.environ, env, clear=True), self.assertRaises(QualityError):
                Unspam()

    def test_wire_shape_and_safe_http_errors(self):
        client = self.client()
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return io.BytesIO(b'{"id":"x"}')
        client.opener = Opener()
        client.create('spam')
        req = client.opener.request
        self.assertEqual(req.full_url, 'https://api.unspam.email/ext/v2/spam-check')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.data, b'{}')
        self.assertEqual(req.get_header('Authorization'), 'Bearer wire-secret')
        for status, code in [(302, 'PROVIDER_UNAVAILABLE'), (401, 'AUTHORIZATION_DENIED'),
                             (403, 'AUTHORIZATION_DENIED'), (429, 'QUOTA_OR_RATE_LIMIT'),
                             (500, 'PROVIDER_UNAVAILABLE')]:
            error = urllib.error.HTTPError('secret-url', status, 'wire-secret', {}, None)
            with patch.object(client.opener, 'open', side_effect=error):
                with self.assertRaisesRegex(QualityError, code) as caught:
                    client.create('spam')
                self.assertNotIn('wire-secret', str(caught.exception))

    def test_cli_http_sender_and_polling_compose(self):
        import threading
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import urlsplit
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / 'profile.json'
            profile.write_text(json.dumps(EXAMPLE))
            marker = root / 'sent'
            sender = root / 'sender'
            sender.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nimport json,sys,os\n'
                + 'assert "UNSPAM_API_TOKEN" not in os.environ\n'
                + 'data=json.load(open(sys.argv[1]))\n'
                + 'assert data["kind"] != "inbox" or data["body_marker"]\n'
                + 'Path(' + repr(str(marker)) + ').write_text(data["kind"])\n')
            sender.chmod(0o700)
            calls = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    return None

                def respond(self, create):
                    calls.append((self.command, self.path))
                    if self.headers.get('Authorization') != 'Bearer wire-secret':
                        self.send_error(401)
                        return
                    kind = 'spam' if 'spam-check' in self.path else 'inbox'
                    if self.path.endswith('/status'):
                        value = {'subscription': {'limits': {'has_api': True,
                            'spam_checks': 20, 'inbox_placements': 20}}}
                    elif create:
                        value = {'id': kind, 'inbox_address': 'seed@example.test',
                            'mailboxes': [{'id': 1, 'address': 'seed@example.test'}]}
                    else:
                        value = {'id': kind, 'status': 'success' if marker.exists() else 'pending',
                            'score': 90, 'results': [{'mailbox': {'id': 1}, 'status': 'success', 'tab': 'Inbox'}]}
                    raw = json.dumps(value).encode()
                    self.send_response(201 if create else 200)
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def do_GET(self):
                    self.respond(False)

                def do_POST(self):
                    self.respond(True)

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            class LocalWire:
                def open(self, request, timeout):
                    url = 'http://127.0.0.1:' + str(server.server_port) + urlsplit(request.full_url).path
                    wire = urllib.request.Request(url, data=request.data, headers=dict(request.header_items()))
                    return urllib.request.urlopen(wire, timeout=timeout)
            try:
                client = self.client()
                client.opener = LocalWire()
                output = io.StringIO()
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': EXAMPLE['repository']}), patch('email_quality.cli.Unspam', return_value=client), contextlib.redirect_stdout(output):
                    code = main(['--profile', str(profile), '--state', str(root / 'state'), '--sender', str(sender), 'run'])
                self.assertEqual(code, 0)
                result = json.loads(output.getvalue())
                jsonschema.validate(result, RECEIPT)
                self.assertEqual(len(result['results']), 2)
                self.assertEqual(sum(method == 'POST' for method, path in calls), 2)
                self.assertNotIn('wire-secret', output.getvalue())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_status_requires_typed_allowances(self):
        client = self.client()
        for limits in [{}, {'has_api': True, 'spam_checks': True, 'inbox_placements': 20},
                       {'has_api': True, 'spam_checks': 20, 'inbox_placements': -1}]:
            with patch.object(client, 'request', return_value={'subscription': {'limits': limits}}):
                with self.assertRaises(QualityError):
                    client.status()

    def test_colon_identifiers_are_encoded_as_one_path_segment(self):
        client = self.client()
        with patch.object(client, 'request', return_value={}) as request:
            client.result('inbox', 'inbox:123-abc', 5)
            self.assertEqual(request.call_args.args[0], '/inbox-placement/inbox%3A123-abc')
        data = delivery('inbox', {'id': 'inbox:123-abc', 'mailboxes': [{'id': 1, 'address': 'seed@example.test'}]})
        self.assertEqual(data['body_marker'], 'inbox:123-abc')

    def test_response_size_is_bounded(self):
        client = self.client()
        with patch.object(client.opener, 'open', return_value=io.BytesIO(b'x' * 1048577)):
            with self.assertRaisesRegex(QualityError, 'PROVIDER_INVALID'):
                client.status()


if __name__ == '__main__':
    unittest.main()
