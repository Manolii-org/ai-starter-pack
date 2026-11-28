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
BACKUP_ROLES = frozenset({"assessment", "judge"})
BACKUP_REASONS = frozenset({
    "hosted_credential_unavailable", "profile_probe_unavailable",
    "profile_session_expiry_unverified", "profile_capacity_exhausted",
})

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
    return dict(value)


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


def credential_mode(policy: dict) -> str:
    mode = policy.get("credential_mode", "refresh")
    if mode not in ("refresh", "access_only"):
        raise ProfileError("hosted_credential_mode_invalid")
    return mode


def _load_login(home: Path, token: str, coords: dict, mode: str, *, backup=False) -> dict:
    raw = _read_stored(token, coords)
    auth = _parse(raw)
    if mode == "access_only":
        if backup and auth["tokens"].get("refresh_token"):
            raise ProfileError("profile_backup_refresh_material")
        # The CLI's private copy cannot consume the real refresh token even
        # if a provider request tries to retry authentication internally.
        auth["tokens"]["refresh_token"] = ""
        raw = json.dumps(auth)
    path = home / "auth.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(raw)
    return auth


def configure(policy: dict, home: Path, token: str) -> None:
    """Materialize the stored login privately for this run only."""
    _state.clear()
    coords = coordinates(policy)
    mode = credential_mode(policy)
    if not token:
        raise ProfileError("hosted_credential_unavailable")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ProfileError("profile_session_unprotected")
    seconds = max(1500, policy.get("max_seconds", 1500))
    start = None
    if "backup" not in policy:
        auth = _load_login(home, token, coords, mode)
        if mode == "access_only":
            profiles.require_session_lifetime(auth, seconds=seconds)
        start = fingerprint(auth)
    _state.update(home=home, coords=coords, token=token, start=start,
                  policy=policy, mode=mode, seconds=seconds)


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
    assignment = _assignment(repository, role, policy, workflow)
    assignment["assignment_sha256"] = profiles.digest(assignment)
    if "backup" in policy and role in BACKUP_ROLES:
        try:
            assignment["backup"] = _backup_assignment(assignment, policy["backup"])
        except ProfileError as exc:
            # A denied backup grant cannot invalidate the primary's grant.
            assignment["backup_unavailable"] = str(exc)
    return assignment


def _backup_assignment(primary: dict, backup: object) -> dict:
    if (not isinstance(backup, dict)
            or set(backup) != {"credential", "credential_mode", "consent"}
            or backup["credential_mode"] != "access_only"):
        raise ProfileError("profile_backup_policy_invalid")
    secondary = _assignment(primary["repository"], primary["role"], backup, primary["controller_workflow"])
    primary_coords, backup_coords = primary["credential"], secondary["credential"]
    if (primary["account_sha256"] == secondary["account_sha256"]
            or primary_coords["secret"] == backup_coords["secret"]
            or any(primary_coords[key] != backup_coords[key] for key in ("doppler_project", "doppler_config"))):
        raise ProfileError("profile_backup_not_distinct")
    return secondary


def _assignment(repository: str, role: str, policy: dict, workflow: str) -> dict:
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
        or consent.get("credit_balance_allowed", False) not in (True, False)
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
        "credential_mode": credential_mode(policy),
    }
    if consent.get("credit_balance_allowed") is True:
        # The owner admits an account that holds purchased credits; capacity()
        # still keeps every run on included usage.
        assignment["credit_balance_allowed"] = True
    return assignment


def persist() -> str:
    """Write a rotated login back once, only over the login this run started from."""
    home, coords, token, start = (_state.get(k) for k in ("home", "coords", "token", "start"))
    if home is None:
        raise ProfileError("hosted_credential_unavailable")
    if _state.get("mode") == "access_only":
        return "access_only"
    raw = (home / "auth.json").read_text(encoding="utf-8")
    current = _parse(raw)
    if fingerprint(current) == start:
        return "unchanged"
    profiles.read_auth(home, {"account_sha256": (_state["policy"].get("consent") or {}).get("account_sha256")})
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


def _recheck(assignment: dict, repository: str, role: str, *, backup=False) -> None:
    current = select(repository, role, _state.get("policy") or {})
    if (current["assignment_sha256"] != assignment.get("assignment_sha256")
            or (backup and current.get("backup") != assignment.get("backup"))):
        raise ProfileError("profile_assignment_changed")


def account_selection() -> dict:
    if "backup" not in _state.get("policy", {}) or _state.get("account_slot") is None:
        return {}
    selection = {"slot": _state["account_slot"]}
    if _state.get("primary_reason"):
        selection["primary_reason"] = _state["primary_reason"]
    if _state.get("backup_unavailable"):
        selection["backup_unavailable"] = _state["backup_unavailable"]
    return selection


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
    backup = assignment.get("backup")
    selected = _state.get("account_slot")
    if selected == "backup":
        if backup is None:
            raise ProfileError("profile_backup_role_unavailable")
        _recheck(assignment, repository, role, backup=True)
        auth, snapshot = _prepare_session(backup, home / "backup", backup=True)
    else:
        try:
            auth, snapshot = _prepare_session(assignment, home)
        except ProfileError as exc:
            if selected or backup is None or str(exc) not in BACKUP_REASONS:
                raise
            _recheck(assignment, repository, role, backup=True)
            auth, snapshot = _prepare_session(backup, home / "backup", backup=True)
            _state.update(account_slot="backup", primary_reason=str(exc))
        else:
            _state["account_slot"] = "primary"
            if assignment.get("backup_unavailable"):
                _state["backup_unavailable"] = assignment["backup_unavailable"]
    _recheck(assignment, repository, role, backup=_state["account_slot"] == "backup")
    yield auth, snapshot


def _prepare_session(assignment: dict, home: Path, *, backup=False) -> tuple[dict, dict]:
    if assignment["credential_mode"] == "access_only":
        if backup:
            home.mkdir(mode=0o700, exist_ok=True)
        # A different worker may have refreshed since this run was admitted.
        _load_login(home, _state["token"], assignment["credential"], "access_only", backup=backup)
        auth = profiles.read_auth(home, assignment, require_refresh=False)
        seconds = _state["seconds"]
        profiles.require_session_lifetime(auth, seconds=seconds)
        account, limits = profiles.account_probe(home, refresh=False)
        snapshot = profiles.capacity(account, limits, assignment)
        auth = profiles.read_auth(home, assignment, require_refresh=False)
        profiles.require_session_lifetime(auth, seconds=seconds)
        snapshot["credential"] = "access_only"
        return auth, snapshot
    if _state["start"] is None:
        auth = _load_login(home, _state["token"], assignment["credential"], "refresh")
        _state["start"] = fingerprint(auth)
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
        except ProfileError as exc:
            # A changed account takes precedence over an unavailable probe or
            # quota error, neither of which can authorize the backup in this case.
            if str(exc) == "profile_account_mismatch":
                raise
            outcome = "writeback_failed"
    snapshot["credential"] = outcome
    return auth, snapshot
