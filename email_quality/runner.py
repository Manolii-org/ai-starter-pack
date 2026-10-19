"""Provider lifecycle and consumer-owned sender seam. Receipts contain metadata only."""
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from .profile import QualityError, number
from .state import private_write


def delivery(kind, response):
    try:
        identifier = response['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', identifier):
            raise QualityError('PROVIDER_INVALID')
        recipients = ([response['inbox_address']] if kind == 'spam'
                      else [m['address'] for m in response['mailboxes']])
        if not recipients or len(recipients) > 100 or len(set(recipients)) != len(recipients):
            raise QualityError('PROVIDER_INVALID')
        if any(not isinstance(r, str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', r)
               for r in recipients):
            raise QualityError('PROVIDER_INVALID')
        mailbox_ids = [m['id'] for m in response['mailboxes']] if kind == 'inbox' else []
        if (any(type(i) is not int or i < 1 for i in mailbox_ids)
                or len(set(mailbox_ids)) != len(mailbox_ids)):
            raise QualityError('PROVIDER_INVALID')
        return {'schema_version': '1.0', 'kind': kind, 'test_id': identifier,
                'recipients': recipients, 'mailbox_ids': mailbox_ids,
                'body_marker': identifier if kind == 'inbox' else ''}
    except (TypeError, KeyError):
        raise QualityError('PROVIDER_INVALID') from None


def remaining(deadline):
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise QualityError('RUN_TIMEOUT')
    return budget


def start(client, state, profile, kind, deadline=None):
    checks = state.data['checks']
    if kind in checks:
        if checks[kind].get('phase') == 'creating':
            raise QualityError('CREATE_OUTCOME_UNKNOWN')
        if checks[kind].get('phase') != 'created_unvalidated':
            return checks[kind]
    if kind not in checks:
        limits = client.status(**({'timeout': remaining(deadline)} if deadline is not None else {}))
        allowance_field = 'spam_checks' if kind == 'spam' else 'inbox_placements'
        if limits[allowance_field] <= profile['minimum_remaining']:
            raise QualityError('QUOTA_RESERVE')
        if deadline is not None:
            remaining(deadline)
        checks[kind] = {'phase': 'creating'}
        state.save()  # Persist BEFORE POST; network ambiguity must not create duplicate billable work.
        try:
            budget = {'timeout': remaining(deadline)} if deadline is not None else {}
        except QualityError:
            # No POST was attempted; preserve that knowledge for a safe later run.
            del checks[kind]
            state.save()
            raise
        response = client.create(kind, **budget)
        checks[kind] = {'phase': 'created_unvalidated', 'response': response}
        state.save()  # Preserve provider response for safe shape-repair without another POST.
    data = delivery(kind, checks[kind]['response'])
    checks[kind] = {'phase': 'created', 'delivery': data}
    private_write(state.directory / ('delivery-' + kind + '.json'), data)
    state.save()
    return checks[kind]


def assess(profile, kind, data):
    if not isinstance(data, dict):
        raise QualityError('PROVIDER_INVALID')
    if data.get('status') in ('failed', 'error', 'cancelled'):
        raise QualityError('PROVIDER_TEST_FAILED')
    if data.get('status') not in ('completed', 'complete', 'finished', 'done', 'success'):
        if data.get('status') not in ('pending', 'waiting', 'running', 'processing', 'queued', 'created'):
            raise QualityError('PROVIDER_INVALID')
        return None
    if kind == 'spam':
        score = data.get('score')
        if not number(score, 0, 100):
            raise QualityError('PROVIDER_INVALID')
        return {'kind': kind, 'score': score, 'passed': score >= profile['minimum_score']}
    results = data.get('results')
    if not isinstance(results, list) or not results or len(results) > 100:
        raise QualityError('PROVIDER_INVALID')
    # Preserve provider categories separately. Inbox placement includes legitimate
    # inbox tabs; explicit Inbox remains independently visible in the receipt.
    categories = ('inbox', 'updates', 'newsletter', 'promotions', 'social', 'forums',
                  'spam', 'missing', 'unknown')
    counts = dict.fromkeys(categories, 0)
    placements = []
    for row in results:
        if not isinstance(row, dict):
            raise QualityError('PROVIDER_INVALID')
        status = row.get('status')
        if status in ('error', 'failed', 'cancelled'):
            raise QualityError('PROVIDER_TEST_FAILED')
        if status != 'success' and status not in categories[:-1]:
            raise QualityError('PROVIDER_INVALID')
        value = row.get('tab') if status == 'success' else status
        if not isinstance(value, str) or not value.strip():
            raise QualityError('PROVIDER_INVALID')
        category = value.strip().lower()
        category = {'primary': 'inbox', 'junk': 'spam'}.get(category, category)
        if category not in counts:
            category = 'unknown'
        counts[category] += 1
        placement = {'placement': category}
        mailbox = row.get('mailbox')
        if isinstance(mailbox, dict) and type(mailbox.get('id')) is int and mailbox['id'] > 0:
            placement['mailbox_id'] = mailbox['id']
        placements.append(placement)
    explicit_ratio = counts['inbox'] / len(results)
    inbox_count = sum(counts[c] for c in ('inbox', 'updates', 'newsletter',
                                         'promotions', 'social', 'forums'))
    ratio = inbox_count / len(results)
    return {'kind': kind, 'inbox_ratio': explicit_ratio, 'explicit_inbox_ratio': explicit_ratio,
            'non_spam_ratio': ratio,
            'mailbox_count': len(results), 'placement_counts': counts,
            'placements': placements,
            'passed': counts['unknown'] == 0
            and explicit_ratio >= profile['minimum_inbox_ratio']
            and ratio >= profile.get('minimum_non_spam_ratio', 0)}


def wait(client, state, profile, kind, deadline=None):
    record = state.data['checks'].get(kind, {})
    if record.get('phase') == 'completed':
        return record['result']
    if record.get('phase') not in ('created', 'sent'):
        raise QualityError('STATE_SCOPE_MISMATCH')
    if deadline is None:
        deadline = time.monotonic() + profile['timeout_seconds']
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QualityError('RESULT_TIMEOUT')
        payload = client.result(kind, record['delivery']['test_id'], remaining)
        if not isinstance(payload, dict) or payload.get('id') != record['delivery']['test_id']:
            raise QualityError('PROVIDER_INVALID')
        if kind == 'inbox' and payload.get('status') in ('completed', 'complete', 'finished', 'done', 'success'):
            rows = payload.get('results')
            try:
                actual = [row['mailbox']['id'] for row in rows]
                expected = record['delivery']['mailbox_ids']
                if (any(type(i) is not int for i in actual)
                        or len(actual) != len(expected) or set(actual) != set(expected)):
                    raise QualityError('PROVIDER_INVALID')
            except (TypeError, KeyError):
                raise QualityError('PROVIDER_INVALID') from None
        result = assess(profile, kind, payload)
        if result is not None:
            record.update(phase='completed', result=result)
            state.save()
            return result
        time.sleep(min(profile['poll_seconds'], max(0, deadline - time.monotonic())))


def send(state, record, sender, timeout, deadline=None):
    if record['phase'] in ('sent', 'completed'):
        return
    if record['phase'] != 'created':
        raise QualityError('SEND_OUTCOME_UNKNOWN')
    executable = Path(sender).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise QualityError('SENDER_INVALID')
    path = state.directory / 'delivery.json'
    private_write(path, record['delivery'])
    record['phase'] = 'sending'
    state.save()
    # Explicit allowlist prevents provider/admin credentials reaching consumer code.
    env = {k: v for k, v in os.environ.items()
           if k in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'HTTP_PROXY', 'HTTPS_PROXY',
                    'NO_PROXY', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS')
           or k.startswith('EMAIL_QUALITY_SENDER_')}
    try:
        if deadline is not None:
            try:
                timeout = min(timeout, remaining(deadline))
            except QualityError:
                # The sender has not started, so this intent can be safely reset.
                record['phase'] = 'created'
                state.save()
                raise
        try:
            process = subprocess.Popen([str(executable), str(path)], env=env, start_new_session=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            # Popen could not execute the sender; no message could have been sent.
            record['phase'] = 'created'
            state.save()
            raise QualityError('SENDER_INVALID') from None
        try:
            if process.wait(timeout=timeout):
                raise QualityError('SENDER_FAILED')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # The isolated group already exited; there is nothing to terminate.
                process.poll()
            process.wait(timeout=5)
        record['phase'] = 'sent'
        state.save()
    except subprocess.TimeoutExpired:
        raise QualityError('SEND_OUTCOME_UNKNOWN') from None
    finally:
        path.unlink(missing_ok=True)
