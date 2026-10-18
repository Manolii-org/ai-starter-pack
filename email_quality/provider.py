"""Bounded Unspam REST adapter. Mutating calls are never automatically retried."""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

from .profile import QualityError

BASE = 'https://api.unspam.email/ext/v2'
PATHS = {'spam': '/spam-check', 'inbox': '/inbox-placement'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Unspam:
    def __init__(self):
        self.token = os.environ.get('UNSPAM_API_TOKEN', '')
        base = os.environ.get('UNSPAM_API_BASE_URL', BASE).rstrip('/')
        if base != BASE:
            raise QualityError('CONFIG_INVALID')
        if not self.token or self.token != self.token.strip() or any(c.isspace() for c in self.token):
            raise QualityError('AUTHORIZATION_DENIED')
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, *, create=False, timeout=15):
        req = urllib.request.Request(BASE + path, data=b'{}' if create else None,
                                     headers={'Authorization': 'Bearer ' + self.token,
                                              'Content-Type': 'application/json',
                                              'Accept': 'application/json'})
        try:
            with self.opener.open(req, timeout=max(0.1, min(timeout, 15))) as response:
                raw = response.read(1048577)
                if len(raw) > 1048576:
                    raise QualityError('PROVIDER_INVALID')
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise QualityError('AUTHORIZATION_DENIED') from None
            if error.code == 429:
                raise QualityError('QUOTA_OR_RATE_LIMIT') from None
            raise QualityError('PROVIDER_UNAVAILABLE') from None
        except (OSError, ValueError):
            raise QualityError('PROVIDER_UNAVAILABLE') from None

    def status(self, timeout=15):
        data = self.request('/status', timeout=timeout)
        try:
            limits = data['subscription']['limits']
            expected = os.environ.get('UNSPAM_ACCOUNT_EMAIL')
            if expected and data.get('email') != expected:
                raise QualityError('AUTHORIZATION_DENIED')
            if limits.get('has_api') is not True:
                raise QualityError('AUTHORIZATION_DENIED')
            for key in ('spam_checks', 'inbox_placements'):
                if type(limits.get(key)) is not int or limits[key] < 0:
                    raise QualityError('PROVIDER_INVALID')
            return {k: limits[k] for k in ('spam_checks', 'inbox_placements')}
        except (TypeError, KeyError, AttributeError):
            raise QualityError('PROVIDER_INVALID') from None

    def create(self, kind, timeout=15):
        return self.request(PATHS[kind], create=True, timeout=timeout)

    def result(self, kind, identifier, timeout):
        return self.request(PATHS[kind] + '/' + quote(identifier, safe=''), timeout=timeout)
