"""Versioned, secret-free consumer configuration."""
import hashlib
import json
import math
import os
import re
from pathlib import Path


class QualityError(Exception):
    """A public typed failure; never include provider content in the message."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def load(path):
    try:
        p = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise QualityError('CONFIG_INVALID') from None
    required = {'schema_version', 'provider', 'org', 'repository', 'environment',
                'account_mode', 'shared_account_acknowledged', 'checks',
                'minimum_remaining', 'timeout_seconds', 'poll_seconds', 'minimum_score',
                'minimum_inbox_ratio'}
    if not isinstance(p, dict) or set(p) != required:
        raise QualityError('CONFIG_INVALID')
    valid = (
        p['schema_version'] == '1.0' and p['provider'] == 'unspam'
        and isinstance(p['org'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}', p['org'])
        and isinstance(p['repository'], str)
        and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', p['repository'])
        and p['repository'].split('/')[0].lower() == p['org'].lower()
        and p['environment'] in ('test', 'ci', 'preview', 'staging', 'preprod')
        and p['account_mode'] in ('shared', 'dedicated')
        and type(p['shared_account_acknowledged']) is bool
        and (p['account_mode'] != 'shared' or p['shared_account_acknowledged'])
        and isinstance(p['checks'], list) and 0 < len(p['checks']) <= 2
        and all(c in ('spam', 'inbox') for c in p['checks'])
        and len(set(p['checks'])) == len(p['checks'])
        and type(p['minimum_remaining']) is int and 0 <= p['minimum_remaining'] <= 100000
        and number(p['timeout_seconds'], 1, 900) and number(p['poll_seconds'], 1, 60)
        and number(p['minimum_score'], 0, 100) and number(p['minimum_inbox_ratio'], 0, 1)
    )
    actual = os.environ.get('GITHUB_REPOSITORY')
    if not valid or (actual and actual.lower() != p['repository'].lower()):
        raise QualityError('CONFIG_INVALID')
    return p


def digest(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
