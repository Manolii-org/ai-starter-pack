"""GitHub-hosted subscription mode: the account login lives in the entity's Doppler.

The self-hosted mode (``codex_review_profiles``) keeps one authoritative login on
a dedicated host behind root-owned registry and consent files. Hosted mode keeps
the same guarantees with different custody of the same facts:

* **Account**: the login is read from one Doppler secret per entity (the
  coordinates are in the consumer's reviewed default-branch policy) and must
  belong to the account the owner consented with (``account_sha256``).
* **Consent**: the owner's grant is the ``consent`` object in that policy file,
  which only lands through the repository's own review on the default branch.
  The controller runs from that default branch, so PR source cannot grant it.
* **One refresh writer**: the official CLI refreshes the login inside the run,
  only near expiry; with a read/write token a rotated login is written back to
  the same Doppler secret only while the stored login is still the one this run
  started from (best-effort lineage guard; the read and the write are separate
  requests, so it narrows rather than eliminates a concurrent-writer race). With
  a read-only token the run uses its refreshed copy and writes nothing.
* **Funding**: purchased credits and automatic purchases must be disabled in
  the consent, and the CLI's live capacity probe must confirm included usage
  (unchanged from host mode).

No credential reaches the guest container, a receipt, a log or an artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import codex_review_profiles as profiles

ProfileError = profiles.ProfileError
SCHEMA = "codex-automation-hosted-consent/v1"
DOPPLER = "https://api.doppler.com/v3/configs/config"
NAME = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,99}\Z")
WORKER = ".github/workflows/codex-automation-worker.yml@refs/heads/"
MAX_AUTH_BYTES = 65536
REFRESH_WINDOW = 3600

_state: dict = {}


def enabled() -> bool:
    return os.environ.get("CODEX_AUTOMATION_HOSTED") == "1"


def fingerprint(auth: dict) -> str:
    tokens = auth.get("tokens") or {}
    return hashlib.sha256(json.dumps(tokens, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def coordinates(policy: dict) -> dict:
    value = policy.get("credential")
    if (
        not isinstance(value, dict)
        or set(value) != {"doppler_project", "doppler_config", "secret"}
        or not all(isinstance(value[k], str) for k in value)
        or not NAME.fullmatch(value["doppler_project"])
        or not NAME.fullmatch(value["doppler_config"])
        or not SECRET_NAME.fullmatch(value["secret"])
    ):
        raise ProfileError("hosted_credential_coordinates_invalid")
    return value


def _request(token: str, url: str, data: dict | None = None) -> dict:
    if not url.startswith(DOPPLER + "/"):
        raise ProfileError("hosted_credential_unavailable")
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, method="GET" if data is None else "POST")
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        # Fixed https://api.doppler.com prefix (checked above); no caller-chosen scheme.
        with urllib.request.urlopen(request, timeout=30) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            raw = response.read(2 * MAX_AUTH_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ProfileError("hosted_credential_unavailable") from None
    if len(raw) > 2 * MAX_AUTH_BYTES:
        raise ProfileError("hosted_credential_unavailable")
    try:
        value = json.loads(raw)
    except ValueError:
        raise ProfileError("hosted_credential_unavailable") from None
    if not isinstance(value, dict):
        raise ProfileError("hosted_credential_unavailable")
    return value


def _read_stored(token: str, coords: dict) -> str:
    query = urllib.parse.urlencode({"project": coords["doppler_project"], "config": coords["doppler_config"],
                                    "name": coords["secret"]})
    value = _request(token, DOPPLER + "/secret?" + query).get("value")
    raw = value.get("raw") if isinstance(value, dict) else None
    if not isinstance(raw, str) or not 0 < len(raw.encode()) <= MAX_AUTH_BYTES:
        raise ProfileError("hosted_credential_unavailable")
    return raw


def _parse(raw: str) -> dict:
    try:
        auth = json.loads(raw)
    except ValueError:
        raise ProfileError("profile_account_mismatch") from None
    if not isinstance(auth, dict) or not isinstance(auth.get("tokens"), dict):
        raise ProfileError("profile_account_mismatch")
    return auth


def configure(policy: dict, home: Path, token: str) -> None:
    """Materialize the stored login privately for this run only."""
    coords = coordinates(policy)
    if not token:
        raise ProfileError("hosted_credential_unavailable")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ProfileError("profile_session_unprotected")
    raw = _read_stored(token, coords)
    auth = _parse(raw)
    path = home / "auth.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(raw)
    _state.update(home=home, coords=coords, token=token, start=fingerprint(auth), policy=policy)


def select(repository: str, role: str, policy: dict) -> dict:
    """Owner consent from the reviewed default-branch policy; no host registry."""
    if not isinstance(repository, str) or not isinstance(role, str):
        raise ProfileError("profile_scope_invalid")
    repository = repository.lower()
    if not profiles.REPO.fullmatch(repository) or role not in profiles.AUTOMATION_ROLES:
        raise ProfileError("profile_scope_invalid")
    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "")
    pieces = workflow.split("/", 2)
    if len(pieces) == 3:
        workflow = pieces[0].lower() + "/" + pieces[1].lower() + "/" + pieces[2]
    if not workflow.startswith(repository + "/" + WORKER):
        raise ProfileError("profile_host_access_unverified")
    consent = policy.get("consent")
    now = int(time.time())
    if (
        not isinstance(consent, dict)
        or consent.get("schema") != SCHEMA
        or not isinstance(consent.get("owner"), str) or not consent["owner"].strip()
        or not profiles.valid(profiles.HASH, consent.get("account_sha256"))
        or consent.get("revoked") is not False
        or type(consent.get("expires_at")) is not int or consent["expires_at"] <= now
        or not isinstance(consent.get("roles"), list) or role not in consent["roles"]
        or not isinstance(consent.get("repositories"), list)
        or repository not in [r.lower() for r in consent["repositories"] if isinstance(r, str)]
        or consent.get("purchased_credits_disabled") is not True
        or consent.get("automatic_purchases_disabled") is not True
    ):
        raise ProfileError("profile_consent_unavailable")
    assignment = {
        "mode": "hosted",
        "repository": repository,
        "role": role,
        "owner": consent["owner"],
        "account_sha256": consent["account_sha256"],
        "consent_sha256": profiles.digest(consent),
        "controller_workflow": workflow,
        "credential": coordinates(policy),
    }
    assignment["assignment_sha256"] = profiles.digest(assignment)
    return assignment


def persist() -> str:
    """Write a rotated login back once, only over the login this run started from."""
    home, coords, token, start = (_state.get(k) for k in ("home", "coords", "token", "start"))
    if home is None:
        raise ProfileError("hosted_credential_unavailable")
    raw = (home / "auth.json").read_text(encoding="utf-8")
    current = _parse(raw)
    if fingerprint(current) == start:
        return "unchanged"
    stored = _parse(_read_stored(token, coords))
    if fingerprint(stored) != start:
        # Another writer already advanced the login; never restore over it.
        return "superseded"
    try:
        _request(token, DOPPLER + "/secrets", {
            "project": coords["doppler_project"], "config": coords["doppler_config"],
            "secrets": {coords["secret"]: raw}})
    except ProfileError:
        # A read-only token cannot be the refresh writer. The run continues on
        # its in-memory login, exactly like the consumer's existing read-only
        # review; the stored login is left untouched for its owning writer.
        return "writeback_unavailable"
    if fingerprint(_parse(_read_stored(token, coords))) != fingerprint(current):
        raise ProfileError("hosted_credential_writeback_unverified")
    _state["start"] = fingerprint(current)
    return "persisted"


def _recheck(assignment: dict, repository: str, role: str) -> None:
    current = select(repository, role, _state.get("policy") or {})
    if current["assignment_sha256"] != assignment.get("assignment_sha256"):
        raise ProfileError("profile_assignment_changed")


@contextmanager
def session(assignment, repository, role, *, wait_seconds=0):
    """Same account checks as host mode, against this run's private copy."""
    home = _state.get("home")
    if home is None or assignment.get("mode") != "hosted":
        raise ProfileError("hosted_credential_unavailable")
    if repository.lower() != assignment["repository"] or role != assignment["role"]:
        raise ProfileError("profile_assignment_changed")
    # Consent can expire (or the grant change) between selection and this
    # session; re-check it before the login is probed and again before it is
    # handed to the caller.
    _recheck(assignment, repository, role)
    initial = profiles.read_auth(home, assignment)
    # Refresh only when the stored access token is near expiry. Every
    # consumer of a login reads the same stored copy, so refreshing on every
    # run would make parallel runs race on one refresh token; near-expiry
    # refresh happens about once per token lifetime.
    expiry = profiles.access_expiry(initial)
    refresh = expiry is None or expiry < int(time.time()) + REFRESH_WINDOW
    outcome = "unchanged"
    try:
        account, limits = profiles.account_probe(home, refresh=refresh)
        snapshot = profiles.capacity(account, limits, assignment)
        auth = profiles.read_auth(home, assignment)
        profiles.require_session_lifetime(auth)
    finally:
        # A login the CLI rotated must reach Doppler even when a later check
        # (capacity, account, lifetime) refuses this run; never drop it.
        try:
            outcome = persist()
        except ProfileError:
            outcome = "writeback_failed"
    snapshot["credential"] = outcome
    _recheck(assignment, repository, role)
    yield auth, snapshot
