"""Hermetic virtual-inbox contracts and backend adapters."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Protocol

VERSION = "1.0"
ERRORS = {
    "CONFIG_INVALID", "CAPABILITY_UNSUPPORTED", "CAPTURE_INFRA_UNAVAILABLE",
    "MESSAGE_TIMEOUT", "MESSAGE_AMBIGUOUS", "MESSAGE_REPLAYED",
    "ASSERTION_FAILED", "CLEANUP_INCOMPLETE", "AUTHORIZATION_DENIED",
}
_SCOPE_KEYS = ("entity", "repository", "environment", "run_id")
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_CONTENT_BYTES = 1024 * 1024
HOSTED_MAX_BODY_BYTES = 2 * 1024 * 1024


def _apply_remaining_read_timeout(response: Any, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    # Elapsed windows still get one tiny syscall so a complete empty JSON body
    # can finish. Truncation is MESSAGE_TIMEOUT via TimeoutError, not remaining<=0.
    timeout = 0.01 if remaining <= 0 else max(0.01, remaining)
    for candidate in (
        response,
        getattr(response, "fp", None),
        getattr(getattr(response, "fp", None), "raw", None),
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
    ):
        if candidate is None:
            continue
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            try:
                setter(timeout)
            except OSError:
                continue
        elif hasattr(candidate, "timeout"):
            try:
                candidate.timeout = timeout
            except (AttributeError, TypeError, OSError):
                continue


def _read_bounded_http_body(response: Any, limit: int = HOSTED_MAX_BODY_BYTES, deadline: float | None = None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    chunk_size = 1 if deadline is not None else 4096
    overrun_started: float | None = None
    while True:
        if deadline is not None:
            _apply_remaining_read_timeout(response, deadline)
        try:
            chunk = response.read(chunk_size)
        except (TimeoutError, OSError) as error:
            if deadline is not None and (isinstance(error, TimeoutError) or "timed out" in str(error).lower()):
                raise CaptureError("MESSAGE_TIMEOUT", "bounded wait expired") from None
            raise
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "hosted response exceeded size limit")
        chunks.append(chunk)
        if deadline is not None and time.monotonic() >= deadline:
            if overrun_started is None:
                overrun_started = time.monotonic()
            if time.monotonic() - overrun_started > 0.2:
                raise CaptureError("MESSAGE_TIMEOUT", "bounded wait expired")
    return b"".join(chunks)


class CaptureError(Exception):
    """A typed, sensitive-data-safe capture failure."""

    def __init__(self, code: str, safe_detail: str = "operation failed"):
        self.code = code if code in ERRORS else "CONFIG_INVALID"
        self.safe_detail = safe_detail
        super().__init__(f"{self.code}: {safe_detail}")

    def as_dict(self) -> dict[str, Any]:
        """Return the public error envelope without message content."""
        return {
            "schema_version": VERSION,
            "code": self.code,
            "detail": self.safe_detail,
            "retryable": self.code in {"CAPTURE_INFRA_UNAVAILABLE", "MESSAGE_TIMEOUT"},
        }


_HERMETIC_BACKENDS = {"memory", "mailpit", "maildev", "inbucket"}
_HOSTED_ENVIRONMENTS = {"test", "ci", "preview", "preprod", "staging"}
_PRODUCTION_ENVIRONMENTS = {"prod", "production", "prd"}
_FIDELITIES = {"provider-to-capture", "deployed-substitution"}



_PROFILE_JSON_KEYS = {
    "schema_version", "mode", "backend", "endpoint", "environment", "ttl_seconds", "fidelity",
}

def _is_credential_profile_key(key: str) -> bool:
    """Reject env-style and short credential keys in profile JSON."""
    folded = key.strip().lower().replace("-", "_")
    if folded.startswith("email_capture_"):
        folded = folded[len("email_capture_"):]
    tokens = set(folded.split("_"))
    if tokens & {"token", "password", "secret", "authorization", "api_key", "apikey", "bearer"}:
        return True
    return folded in {"token", "hosted_token", "api_key", "authorization"} or "hosted_token" in folded

@dataclass(frozen=True)
class Profile:
    """Validated capture configuration."""

    mode: str
    backend: str
    endpoint: str
    environment: str
    ttl_seconds: int = 900
    fidelity: str = "provider-to-capture"

    @classmethod
    def load(cls, path: str | None = None) -> "Profile":
        """Load a v1 profile and fail closed on mixed, deployed, or unsafe config."""
        raw = (
            json.loads(Path(path).read_text())
            if path
            else {
                key.lower().removeprefix("email_capture_"): value
                for key, value in os.environ.items()
                if key.startswith("EMAIL_CAPTURE_")
            }
        )
        if not isinstance(raw, dict):
            raise CaptureError("CONFIG_INVALID", "profile must be an object")
        if path is None:
            canonical = any(key in os.environ for key in ("EMAIL_CAPTURE_MODE", "EMAIL_CAPTURE_BACKEND", "EMAIL_CAPTURE_ENDPOINT"))
            legacy = any(key in os.environ for key in ("MAILDEV_URL", "MAILDEV_WEB_URL", "INBUCKET_URL", "EMAIL_CAPTURE_API_URL", "SMTP_HOST", "SMTP_PORT"))
            if canonical and legacy:
                raise CaptureError("CONFIG_INVALID", "mixed canonical and legacy configuration")
        if path is None:
            raw = {
                key: value for key, value in raw.items()
                if key in _PROFILE_JSON_KEYS and not _is_credential_profile_key(key)
            }
        else:
            if any(_is_credential_profile_key(key) for key in raw):
                raise CaptureError("CONFIG_INVALID", "credentials must not appear in profile JSON")
            unknown = set(raw) - _PROFILE_JSON_KEYS
            if unknown:
                raise CaptureError("CONFIG_INVALID", "profile JSON contains unknown properties")
        version = raw.get("schema_version", VERSION)
        mode = raw.get("mode", "off")
        backend_name = raw.get("backend", "memory")
        if mode == "hosted":
            if "environment" not in raw or not str(raw.get("environment") or "").strip():
                raise CaptureError("CONFIG_INVALID", "hosted capture requires an explicit environment")
            environment = raw["environment"]
        else:
            environment = raw.get("environment", os.environ.get("NODE_ENV", "test"))
        ttl = raw.get("ttl_seconds", 900)
        endpoint = raw.get("endpoint", "")
        fidelity = raw.get("fidelity", "provider-to-capture")
        values = (version, mode, backend_name, environment, endpoint, fidelity)
        if not all(isinstance(value, str) for value in values):
            raise CaptureError("CONFIG_INVALID", "profile fields have invalid types")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 86400:
            raise CaptureError("CONFIG_INVALID", "TTL must be between 1 and 86400 seconds")
        if version.split(".")[0] != "1":
            raise CaptureError("CONFIG_INVALID", "unsupported schema major version")
        if mode not in {"off", "hermetic", "hosted"}:
            raise CaptureError("CONFIG_INVALID", "unknown mode or backend")
        if mode == "hermetic" and backend_name not in _HERMETIC_BACKENDS:
            raise CaptureError("CONFIG_INVALID", "unknown mode or backend")
        if mode == "hosted" and backend_name != "hosted":
            raise CaptureError("CONFIG_INVALID", "hosted mode requires hosted backend")
        if mode == "off" and backend_name not in _HERMETIC_BACKENDS | {"hosted"}:
            raise CaptureError("CONFIG_INVALID", "unknown mode or backend")
        if fidelity not in _FIDELITIES:
            raise CaptureError("CONFIG_INVALID", "unknown fidelity")
        env = environment.lower()
        if mode == "hermetic" and env in {"prod", "production", "staging"}:
            raise CaptureError("CONFIG_INVALID", "capture forbidden in deployed environment")
        if mode == "hosted" and env in _PRODUCTION_ENVIRONMENTS:
            raise CaptureError("CONFIG_INVALID", "hosted capture forbidden in production")
        if mode == "hosted" and env not in _HOSTED_ENVIRONMENTS:
            raise CaptureError("CONFIG_INVALID", "hosted capture environment is not allowlisted")
        if mode == "hosted":
            _validate_hosted_endpoint(endpoint, env)
            if not os.environ.get("EMAIL_CAPTURE_HOSTED_TOKEN"):
                raise CaptureError("AUTHORIZATION_DENIED", "hosted token must come from EMAIL_CAPTURE_HOSTED_TOKEN")
        if mode == "hermetic" and backend_name != "memory":
            _validate_local_endpoint(endpoint)
        return cls(mode, backend_name, endpoint, environment, ttl, fidelity)


class Backend(Protocol):
    """Receiver service-provider interface."""

    def capabilities(self) -> dict[str, Any]: ...
    def list(self, allocation: dict[str, Any]) -> list[dict[str, Any]]: ...
    def purge(self, allocation: dict[str, Any]) -> None: ...
    def health(self) -> bool: ...


def _remaining_request_timeout(backend: Any) -> float:
    """Apply the await deadline to every HTTP request, not only the first.

    When the await window has already elapsed, return a tiny timeout so the
    last poll can finish. ``await_messages`` then returns success for
    ``count=0`` (bounded negative) or ``MESSAGE_TIMEOUT`` for a positive wait.
    Raising here made Mailpit negatives fail even when the mailbox stayed empty.
    """
    timeout = float(getattr(backend, "request_timeout", 10.0))
    deadline = getattr(backend, "_deadline", None)
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.01
    return max(0.01, min(timeout, remaining))


def _validate_local_endpoint(endpoint: str) -> None:
    """Restrict receivers to job-local cleartext HTTP without credentials."""
    if not endpoint:
        raise CaptureError("CONFIG_INVALID", "HTTP backend requires endpoint")
    parsed = urllib.parse.urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CaptureError("AUTHORIZATION_DENIED", "receiver must be local HTTP")


def _validate_hosted_endpoint(endpoint: str, environment: str) -> None:
    """Allow HTTPS capture APIs; loopback HTTP only in the local test environment."""
    if not endpoint:
        raise CaptureError("CONFIG_INVALID", "HTTP backend requires endpoint")
    parsed = urllib.parse.urlparse(endpoint)
    try:
        parsed.port
    except ValueError as exc:
        raise CaptureError("CONFIG_INVALID", "hosted endpoint authority is invalid") from exc
    if parsed.hostname:
        try:
            parsed.hostname.encode("idna")
        except UnicodeError as exc:
            raise CaptureError("CONFIG_INVALID", "hosted endpoint hostname is invalid") from exc
        hostname = parsed.hostname
        loopback = hostname in {"localhost", "127.0.0.1", "::1"}
        labels = hostname.split(".")
        dns_ok = loopback or (
            not any(ch.isspace() or ch == "%" for ch in hostname)
            and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels)
        )
        if ".." in hostname or hostname.startswith(".") or hostname.endswith(".") or not dns_ok:
            raise CaptureError("CONFIG_INVALID", "hosted endpoint hostname is invalid")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise CaptureError("AUTHORIZATION_DENIED", "hosted endpoint must not embed credentials")
    if parsed.scheme == "https" and parsed.hostname:
        if "." not in parsed.hostname:
            raise CaptureError("CONFIG_INVALID", "hosted HTTPS hostname must be dotted")
        return
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and loopback and environment == "test":
        return
    raise CaptureError("AUTHORIZATION_DENIED", "hosted receiver must be HTTPS")


def atomic_write_json(path: Path, value: Any) -> None:
    """Atomically write mode-0600 JSON without following destination symlinks."""
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    if path.is_symlink():
        raise CaptureError("AUTHORIZATION_DENIED", "private output must not be a symlink")
    fd, name = tempfile.mkstemp(prefix=".email-capture-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirects so a local receiver cannot bounce a request off-host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CaptureError("AUTHORIZATION_DENIED", "receiver redirects are forbidden")


class HttpBackend:
    """Adapter for Mailpit, MailDev 2.x, and Inbucket v3 REST APIs."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.request_timeout = 10.0
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, path: str, method: str = "GET", payload: Any = None) -> Any:
        """Perform one bounded request and classify transport failures separately."""
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(
            self.profile.endpoint.rstrip("/") + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=_remaining_request_timeout(self)) as response:
                body = response.read()
        except CaptureError:
            raise
        except urllib.error.HTTPError as error:
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", f"receiver HTTP {error.code}") from None
        except (OSError, urllib.error.URLError) as error:
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", type(error).__name__) from None
        if not body:
            return None
        content_type = response.headers.get_content_type()
        if content_type == "application/json" or body[:1] in {b"{", b"[", b'"'}:
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver returned invalid JSON") from None
        return body.decode(errors="replace")

    def capabilities(self) -> dict[str, Any]:
        """Declare only behavior proven by the selected adapter."""
        return {
            "schema_version": VERSION,
            "backend": self.profile.backend,
            "allocation_scope": "recipient",
            "cursor": "received_at_id",
            "mime": True,
            "attachments": True,
            "attachment_size": self.profile.backend != "inbucket",
            "purge_scope": "allocation",
        }

    def health(self) -> bool:
        """Probe an adapter-specific non-destructive endpoint."""
        if self.profile.backend == "mailpit":
            self._request("/api/v1/info")
        elif self.profile.backend == "maildev":
            self._request("/healthz")
        else:
            self._request("/api/v1/mailbox/email-capture-healthcheck")
        return True

    def _matching_summaries(self, allocation: dict[str, Any]) -> list[dict[str, Any]]:
        """List only summaries addressed to the allocation recipient."""
        recipient = allocation["recipient"].lower()
        if self.profile.backend == "mailpit":
            query = urllib.parse.urlencode({"query": f"to:{recipient}"})
            data = self._request(f"/api/v1/messages?{query}")
            rows = data.get("messages", []) if isinstance(data, dict) else []
        elif self.profile.backend == "maildev":
            query = urllib.parse.urlencode({"to.address": recipient})
            rows = self._request(f"/email?{query}")
        else:
            mailbox = urllib.parse.quote(recipient.split("@", 1)[0], safe="")
            rows = self._request(f"/api/v1/mailbox/{mailbox}")
        if not isinstance(rows, list):
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver list has invalid shape")
        return [row for row in rows if _summary_contains_recipient(row, recipient)]

    def list(self, allocation: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch message details before normalizing content and attachment metadata."""
        summaries = self._matching_summaries(allocation)
        details: list[dict[str, Any]] = []
        for summary in summaries:
            identifier = _message_id(summary)
            if self.profile.backend == "mailpit":
                detail = self._request(f"/api/v1/message/{urllib.parse.quote(identifier, safe='')}")
                # Shape-check BEFORE mutating: a null / non-object detail body would
                # otherwise raise a bare TypeError here, which the CLI reports as
                # CONFIG_INVALID (non-retryable) instead of the retryable
                # CAPTURE_INFRA_UNAVAILABLE this receiver failure actually is.
                if not isinstance(detail, dict):
                    raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver detail has invalid shape")
                detail["headers"] = self._request(f"/api/v1/message/{urllib.parse.quote(identifier, safe='')}/headers")
            elif self.profile.backend == "maildev":
                detail = self._request(f"/email/{urllib.parse.quote(identifier, safe='')}")
            else:
                mailbox = urllib.parse.quote(allocation["recipient"].split("@", 1)[0], safe="")
                detail = self._request(f"/api/v1/mailbox/{mailbox}/{urllib.parse.quote(identifier, safe='')}")
            if not isinstance(detail, dict):
                raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver detail has invalid shape")
            receiver_time = summary.get("Created") or summary.get("created") or summary.get("time") or summary.get("date")
            if isinstance(receiver_time, str) and receiver_time:
                detail["received_at"] = receiver_time
            details.append(normalise_message(detail, self.profile.backend))
        return details

    def purge(self, allocation: dict[str, Any]) -> None:
        """Delete only messages selected by the allocation recipient."""
        summaries = self._matching_summaries(allocation)
        if self.profile.backend == "mailpit":
            identifiers = [_message_id(row) for row in summaries]
            if identifiers:
                self._request("/api/v1/messages", "DELETE", {"IDs": identifiers})
            return
        mailbox = urllib.parse.quote(allocation["recipient"].split("@", 1)[0], safe="")
        for row in summaries:
            identifier = urllib.parse.quote(_message_id(row), safe="")
            path = f"/email/{identifier}" if self.profile.backend == "maildev" else f"/api/v1/mailbox/{mailbox}/{identifier}"
            self._request(path, "DELETE")


class HostedHttpBackend:
    """HTTPS capture adapter for vendor or consumer-hosted message APIs."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.request_timeout = 10.0
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _token(self) -> str:
        token = os.environ.get("EMAIL_CAPTURE_HOSTED_TOKEN", "")
        if not token or token != token.strip() or any(ch.isspace() for ch in token):
            raise CaptureError("AUTHORIZATION_DENIED", "hosted token is missing or malformed")
        return token

    def _request(self, path: str, method: str = "GET") -> Any:
        """Perform one bounded authenticated request without following redirects."""
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token()}",
            "User-Agent": "email-capture/1.0",
        }
        request = urllib.request.Request(
            self.profile.endpoint.rstrip("/") + path,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=_remaining_request_timeout(self)) as response:
                body = _read_bounded_http_body(response, deadline=getattr(self, "_deadline", None))
        except CaptureError:
            raise
        except urllib.error.HTTPError as error:
            try:
                _read_bounded_http_body(error, deadline=getattr(self, "_deadline", None))
            except CaptureError:
                pass
            except OSError:
                pass
            if error.code in {401, 403}:
                raise CaptureError("AUTHORIZATION_DENIED", f"hosted HTTP {error.code}") from None
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", f"hosted HTTP {error.code}") from None
        except (OSError, urllib.error.URLError) as error:
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", type(error).__name__) from None
        if not body:
            if method == "DELETE":
                return None
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "hosted response was empty")
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver returned invalid JSON") from None

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": VERSION,
            "backend": "hosted",
            "allocation_scope": "recipient",
            "cursor": "received_at_id",
            "mime": True,
            "attachments": True,
            "attachment_size": True,
            "purge_scope": "allocation",
            "fidelity": self.profile.fidelity,
        }

    def health(self) -> bool:
        payload = self._request("/health")
        if isinstance(payload, dict) and payload.get("ok") is True:
            return True
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "hosted health payload was not ok:true")

    def _matching_summaries(self, allocation: dict[str, Any]) -> list[dict[str, Any]]:
        recipient = allocation["recipient"].lower()
        query = urllib.parse.urlencode({"to": recipient})
        data = self._request(f"/messages?{query}")
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver list has invalid shape")
        rows = data["messages"]
        matched: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver summary has invalid shape")
            _message_id(row)
            _require_well_formed_recipient_field(row)
            _require_consistent_to_aliases(row)
            if _summary_contains_recipient(row, recipient):
                matched.append(row)
        return matched

    def list(self, allocation: dict[str, Any]) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        recipient = allocation["recipient"].lower()
        for summary in self._matching_summaries(allocation):
            requested_id = _message_id(summary)
            identifier = urllib.parse.quote(requested_id, safe="")
            detail = self._request(f"/messages/{identifier}")
            if not isinstance(detail, dict):
                raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver detail has invalid shape")
            if _message_id(detail) != requested_id:
                raise CaptureError("AUTHORIZATION_DENIED", "detail id is outside allocation")
            _require_well_formed_recipient_field(detail)
            _require_consistent_to_aliases(detail)
            if not _summary_contains_recipient(detail, recipient):
                raise CaptureError("AUTHORIZATION_DENIED", "detail recipient is outside allocation")
            receiver_time = summary.get("received_at") or summary.get("Created") or summary.get("created") or summary.get("date")
            if isinstance(receiver_time, str) and receiver_time:
                detail["received_at"] = receiver_time
            _require_hosted_attachments_shape(detail)
            try:
                details.append(normalise_message(detail, "hosted"))
            except (TypeError, AttributeError, ValueError) as error:
                raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", type(error).__name__) from None
        return details

    def purge(self, allocation: dict[str, Any]) -> None:
        recipient = allocation["recipient"].lower()
        for row in self._matching_summaries(allocation):
            requested_id = _message_id(row)
            identifier = urllib.parse.quote(requested_id, safe="")
            detail = self._request(f"/messages/{identifier}")
            if not isinstance(detail, dict):
                raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver detail has invalid shape")
            if _message_id(detail) != requested_id:
                raise CaptureError("AUTHORIZATION_DENIED", "delete id is outside allocation")
            _require_well_formed_recipient_field(detail)
            _require_consistent_to_aliases(detail)
            _require_envelope_fields_well_formed(detail)
            if not _summary_contains_recipient(detail, recipient):
                raise CaptureError("AUTHORIZATION_DENIED", "delete recipient is outside allocation")
            if not _allocation_exclusively_owns(detail, recipient):
                continue
            self._request(f"/messages/{identifier}", "DELETE")


class MemoryBackend:
    """File-backed backend used for fast contract tests."""

    def __init__(self, profile: Profile):
        root = Path(os.environ.get("EMAIL_CAPTURE_PRIVATE_DIR", os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))) / "email-capture"
        self.path = Path(profile.endpoint) if profile.endpoint else root / "memory.json"

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        if self.path.is_symlink():
            raise CaptureError("AUTHORIZATION_DENIED", "memory state must not be a symlink")
        try:
            rows = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "invalid receiver state") from None
        if not isinstance(rows, list):
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "invalid receiver state shape")
        return rows

    def list(self, allocation: dict[str, Any]) -> list[dict[str, Any]]:
        recipient = allocation["recipient"].lower()
        return [normalise_message(row, "memory") for row in self._read() if _summary_contains_recipient(row, recipient)]

    def purge(self, allocation: dict[str, Any]) -> None:
        recipient = allocation["recipient"].lower()
        atomic_write_json(self.path, [row for row in self._read() if not _summary_contains_recipient(row, recipient)])

    def capabilities(self) -> dict[str, Any]:
        return {"schema_version": VERSION, "backend": "memory", "allocation_scope": "recipient", "cursor": "received_at_id", "mime": True, "attachments": True, "attachment_size": True, "purge_scope": "allocation"}

    def health(self) -> bool:
        return True


def _addresses(value: Any) -> list[Any]:
    """Normalize scalar and list address representations."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _required_string_field(row: dict[str, Any], *names: str) -> str:
    for name in names:
        if name in row:
            value = row[name]
            if not isinstance(value, str):
                raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", f"{name} must be a string")
            return value
    return ""


def _recipient_field_value(row: dict[str, Any]) -> tuple[bool, Any]:
    if "To" in row:
        return True, row["To"]
    if "to" in row:
        return True, row["to"]
    envelope = row.get("envelope")
    if isinstance(envelope, dict) and "to" in envelope:
        return True, envelope["to"]
    return False, None


def _recipient_values_are_well_formed(value: Any, *, require_address: bool = False) -> bool:
    """Reject null/blank/unparseable recipient values. Empty optional lists are allowed."""
    if isinstance(value, list):
        if not value:
            return not require_address
        if not all(_single_recipient_is_well_formed(item) for item in value):
            return False
        return all(bool(_parsed_addresses([item])) for item in value)
    if not _single_recipient_is_well_formed(value):
        return False
    return bool(_parsed_addresses(value))


def _single_recipient_is_well_formed(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        address = value.get("Address", value.get("address"))
        return isinstance(address, str) and bool(address.strip())
    return False


def _require_envelope_fields_well_formed(row: dict[str, Any]) -> None:
    """Fail closed if any present To/Cc/Bcc field is malformed before DELETE."""
    for key in ("To", "to", "Cc", "cc", "Bcc", "bcc"):
        if key in row and not _recipient_values_are_well_formed(row[key], require_address=key in {"To", "to"}):
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver envelope recipients are malformed")
    envelope = row.get("envelope")
    if isinstance(envelope, dict):
        for key in ("to", "cc", "bcc"):
            if key in envelope and not _recipient_values_are_well_formed(envelope[key], require_address=key == "to"):
                raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver envelope recipients are malformed")


def _require_hosted_attachments_shape(row: dict[str, Any]) -> None:
    """Hosted details must present attachments as a list of objects, or omit them."""
    for key in ("attachments", "Attachments"):
        if key not in row:
            continue
        value = row[key]
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "hosted attachments are malformed")


def _require_well_formed_recipient_field(row: dict[str, Any]) -> None:
    present, value = _recipient_field_value(row)
    if not present:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver summary is missing recipients")
    if not _recipient_values_are_well_formed(value, require_address=True):
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "receiver summary recipients are malformed")

def _require_consistent_to_aliases(row: dict[str, Any]) -> None:
    """Reject To/to/envelope.to aliases that name different recipients."""
    groups: list[tuple[str, ...]] = []
    for key in ("To", "to"):
        if key in row:
            groups.append(tuple(_parsed_addresses(row[key])))
    envelope = row.get("envelope")
    if isinstance(envelope, dict) and "to" in envelope:
        groups.append(tuple(_parsed_addresses(envelope["to"])))
    if len(groups) >= 2 and any(set(group) != set(groups[0]) for group in groups[1:]):
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "conflicting recipient aliases")



def _summary_has_recipient_field(row: dict[str, Any]) -> bool:
    present, value = _recipient_field_value(row)
    return present and _recipient_values_are_well_formed(value)


def _parsed_addresses(value: Any) -> list[str]:
    rendered: list[str] = []
    for item in _addresses(value):
        address = item.get("Address", item.get("address", "")) if isinstance(item, dict) else str(item)
        rendered.append(address)
    parsed: list[str] = []
    for _display_name, address in getaddresses(rendered):
        if address:
            parsed.append(address.lower())
    return parsed


def _envelope_addresses(row: dict[str, Any]) -> list[str]:
    """Collect To/Cc/Bcc envelope addresses used for exclusive-ownership deletes."""
    collected: list[str] = []
    for key in ("To", "to", "Cc", "cc", "Bcc", "bcc"):
        if key in row:
            collected.extend(_parsed_addresses(row[key]))
    envelope = row.get("envelope")
    if isinstance(envelope, dict):
        for key in ("to", "cc", "bcc"):
            if key in envelope:
                collected.extend(_parsed_addresses(envelope[key]))
    unique: list[str] = []
    seen: set[str] = set()
    for address in collected:
        if address not in seen:
            seen.add(address)
            unique.append(address)
    return unique


def _allocation_exclusively_owns(row: dict[str, Any], recipient: str) -> bool:
    addresses = _envelope_addresses(row)
    return bool(addresses) and all(address == recipient for address in addresses)


def _summary_contains_recipient(row: dict[str, Any], recipient: str) -> bool:
    """Match the envelope recipient without searching bodies or subjects."""
    present, values = _recipient_field_value(row)
    if not present:
        return False
    return recipient in _parsed_addresses(values)


def _message_id(row: dict[str, Any]) -> str:
    """Return a receiver message identifier or reject an undeletable row."""
    identifier = row.get("ID", row.get("id", row.get("key")))
    if not isinstance(identifier, str) or not identifier:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "message lacks stable id")
    return identifier


def _canonical_cursor(cursor: str) -> str:
    """Normalize persisted v0.1.1 second-precision cursors to the v0.2.0 stamp form."""
    if not cursor or cursor == "0:":
        return cursor or "0:"
    if ":" not in cursor:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "cursor is not sortable")
    stamp, rest = cursor.rsplit(":", 1)
    try:
        return f"{_canonical_received_at(stamp)}:{rest}"
    except CaptureError as error:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "cursor receive time is not sortable") from error


def _canonical_received_at(received: str) -> str:
    """Parse RFC3339 receive times into a single UTC form for cursor comparison."""
    matched = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})",
        received,
    )
    if not matched:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "message receive time is not sortable")
    head, fraction, zone = matched.groups()
    fraction = ((fraction or "") + "000000")[:6]
    iso = f"{head}.{fraction}{'+00:00' if zone == 'Z' else zone}"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as error:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "message receive time is not sortable") from error
    if parsed.tzinfo is None:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "message receive time is not sortable")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalise_message(row: dict[str, Any], backend_name: str) -> dict[str, Any]:
    """Map receiver-specific detail records into the v1 message shape."""
    identifier = _message_id(row)
    received = row.get("received_at") or row.get("Created") or row.get("created") or row.get("date") or row.get("Date") or row.get("time")
    if not isinstance(received, str) or not received:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "message lacks stable receive time")
    received = _canonical_received_at(received)
    body = row.get("body", {}) if isinstance(row.get("body", {}), dict) else {}
    text = row.get("text", row.get("Text", body.get("text", "")))
    html = row.get("html", row.get("HTML", body.get("html", "")))
    if text is None:
        text = ""
    if html is None:
        html = ""
    if not isinstance(text, str) or not isinstance(html, str):
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "message body fields have invalid types")
    headers = row.get("headers", row.get("header", {}))
    if not isinstance(headers, dict):
        headers = {}
    attachments = row.get("attachments", row.get("Attachments", []))
    if not isinstance(attachments, list):
        attachments = []
    if len(attachments) > MAX_ATTACHMENTS:
        raise CaptureError("CAPABILITY_UNSUPPORTED", "attachment count limit exceeded")
    if len((text or "").encode()) + len((html or "").encode()) > MAX_CONTENT_BYTES:
        raise CaptureError("CAPABILITY_UNSUPPORTED", "message content limit exceeded")
    normalized_attachments = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        raw_size = attachment.get("size", attachment.get("Size", 0))
        if raw_size in (None, ""):
            raw_size = 0
        if isinstance(raw_size, bool) or not isinstance(raw_size, (int, str)):
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "attachment size is malformed")
        try:
            size = int(raw_size)
        except (TypeError, ValueError) as error:
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "attachment size is malformed") from error
        if size < 0:
            raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "attachment size is malformed")
        if size > MAX_ATTACHMENT_BYTES:
            raise CaptureError("CAPABILITY_UNSUPPORTED", "attachment size limit exceeded")
        normalized_attachments.append({
            "filename": attachment.get("filename", attachment.get("FileName", "")),
            "content_type": attachment.get("content-type", attachment.get("contentType", attachment.get("ContentType", "application/octet-stream"))),
            "size": size,
        })
    opaque = hashlib.sha256(f"{backend_name}:{identifier}".encode()).hexdigest()[:24]
    present, recipient_value = _recipient_field_value(row)
    return {
        "schema_version": VERSION,
        "opaque_id": opaque,
        "received_at": received,
        "envelope": {"from": _addresses(row.get("from", row.get("From"))), "to": _addresses(recipient_value if present else row.get("to", row.get("To")))},
        "subject": _required_string_field(row, "subject", "Subject"),
        "headers": headers,
        "content": {"text_present": bool(text), "html_present": bool(html), "text": text or "", "html": html or ""},
        "attachments": normalized_attachments,
    }


def backend(profile: Profile) -> Backend:
    """Construct an enabled backend only."""
    if profile.mode == "off":
        raise CaptureError("CAPABILITY_UNSUPPORTED", "capture mode is off")
    if profile.backend == "memory":
        return MemoryBackend(profile)
    if profile.backend == "hosted":
        return HostedHttpBackend(profile)
    return HttpBackend(profile)


def _scope(request: dict[str, Any]) -> str:
    return json.dumps({key: request.get(key, "") for key in _SCOPE_KEYS}, sort_keys=True, separators=(",", ":"))


def _registry_path() -> Path:
    root = Path(os.environ.get("EMAIL_CAPTURE_PRIVATE_DIR", os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))) / "email-capture"
    return Path(os.environ.get("EMAIL_CAPTURE_ALLOCATION_REGISTRY", str(root / "allocations.json")))


@contextmanager
def _registry_lock():
    """Serialize registry read-modify-write operations across local processes."""
    path = _registry_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        return {}
    if path.is_symlink():
        raise CaptureError("AUTHORIZATION_DENIED", "allocation registry must not be a symlink")
    try:
        records = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "invalid allocation registry") from None
    if not isinstance(records, dict):
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "invalid allocation registry shape")
    return records


def allocate(request: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """Create or reuse a live allocation for an exact four-field scope."""
    for key in _SCOPE_KEYS:
        if not isinstance(request.get(key), str) or not request[key] or len(request[key]) > 200:
            raise CaptureError("CONFIG_INVALID", f"missing or invalid {key}")
    domain = request.get("domain", "capture.test")
    if not isinstance(domain, str) or len(domain) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain.lower()):
        raise CaptureError("CONFIG_INVALID", "invalid allocation domain")
    scope_hash = hashlib.sha256(_scope(request).encode()).hexdigest()
    with _registry_lock():
        records = _read_registry()
        existing = records.get(scope_hash)
        if isinstance(existing, dict) and float(existing.get("expires_at", 0)) > time.time():
            return existing
        now = time.time()
        records = {key: value for key, value in records.items() if float(value.get("expires_at", 0)) > now}
        digest = secrets.token_hex(16)
        result = {
            "schema_version": VERSION,
            "allocation_id": digest,
            "recipient": f"ec-{digest}@{domain.lower()}",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "expires_at": now + profile.ttl_seconds,
            "cursor": "0:",
            "scope": {key: request[key] for key in _SCOPE_KEYS},
        }
        records[scope_hash] = result
        atomic_write_json(_registry_path(), records)
        return result


def release_allocation(selected_backend: Backend, allocation: dict[str, Any]) -> None:
    """Idempotently purge messages and retire the allocation registry entry."""
    selected_backend.purge(allocation)
    with _registry_lock():
        records = _read_registry()
        retained = {key: value for key, value in records.items() if value.get("allocation_id") != allocation.get("allocation_id")}
        atomic_write_json(_registry_path(), retained)


def await_messages(selected_backend: Backend, allocation: dict[str, Any], timeout: float = 30, count: int = 1, not_before: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Wait for an exact fresh count, including a full bounded negative window."""
    if timeout < 0 or count < 0:
        raise CaptureError("CONFIG_INVALID", "timeout and count must be non-negative")
    if not_before:
        try:
            not_before = _canonical_received_at(not_before)
        except CaptureError as exc:
            raise CaptureError("CONFIG_INVALID", "invalid --not-before timestamp") from exc
    deadline = time.monotonic() + timeout
    cursor = _canonical_cursor(str(allocation.get("cursor", "0:")))
    supports_deadline = hasattr(selected_backend, "request_timeout")
    original_timeout = getattr(selected_backend, "request_timeout", None)
    first_poll = True
    try:
        while True:
            if supports_deadline:
                if first_poll and timeout == 0:
                    selected_backend._deadline = None
                    selected_backend.request_timeout = max(0.01, min(10.0, float(original_timeout or 10.0)))
                else:
                    selected_backend._deadline = deadline
                    selected_backend.request_timeout = max(0.01, min(10.0, deadline - time.monotonic()))
            rows = sorted(selected_backend.list(allocation), key=lambda message: (message["received_at"], message["opaque_id"]))
            first_poll = False
            rows = [message for message in rows if not not_before or message["received_at"] >= not_before]
            ids = [message["opaque_id"] for message in rows]
            if len(ids) != len(set(ids)):
                raise CaptureError("MESSAGE_REPLAYED", "duplicate opaque message id")
            fresh = [message for message in rows if f'{message["received_at"]}:{message["opaque_id"]}' > cursor]
            if count == 0:
                if fresh:
                    raise CaptureError("ASSERTION_FAILED", "bounded negative assertion failed")
                if time.monotonic() >= deadline:
                    return [], cursor
            else:
                if len(fresh) > count:
                    raise CaptureError("MESSAGE_AMBIGUOUS", "more messages than expected")
                if len(fresh) == count:
                    advanced = f'{fresh[-1]["received_at"]}:{fresh[-1]["opaque_id"]}'
                    allocation["cursor"] = advanced
                    return fresh, advanced
                if time.monotonic() >= deadline:
                    raise CaptureError("MESSAGE_TIMEOUT", "bounded wait expired")
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    finally:
        if supports_deadline:
            selected_backend._deadline = None
            if original_timeout is not None:
                selected_backend.request_timeout = original_timeout


def extract(message: dict[str, Any], kind: str, name: str | None = None) -> list[str]:
    """Deterministically extract links, numeric codes, or a named header."""
    content = message["content"].get("text", "") + "\n" + message["content"].get("html", "")
    if kind == "link":
        return sorted(set(re.findall(r'https?://[^\s<>"\']+', content)))
    if kind == "code":
        return sorted(set(re.findall(r"(?<!\d)\d{4,8}(?!\d)", content)))
    if kind == "header":
        values = [value for key, value in message.get("headers", {}).items() if key.lower() == str(name).lower()]
        return [str(item) for value in values for item in (value if isinstance(value, list) else [value])]
    raise CaptureError("CONFIG_INVALID", "unknown extraction kind")


def assert_messages(messages: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    """Apply exact-count, negative, and extraction-count assertions."""
    exact = rules.get("exact_count", 1)
    if not isinstance(exact, int) or isinstance(exact, bool) or exact < 0:
        raise CaptureError("CONFIG_INVALID", "exact_count must be a non-negative integer")
    if len(messages) != exact:
        raise CaptureError("ASSERTION_FAILED", "exact count mismatch")
    if rules.get("negative") and messages:
        raise CaptureError("ASSERTION_FAILED", "bounded negative assertion failed")
    extracted = []
    for rule in rules.get("extract", []):
        values = extract(messages[0], rule["kind"], rule.get("name")) if messages else []
        if rule.get("exact_count") is not None and len(values) != int(rule["exact_count"]):
            raise CaptureError("ASSERTION_FAILED", "extraction count mismatch")
        extracted.append({"kind": rule["kind"], "count": len(values)})
    return {"schema_version": VERSION, "result": "passed", "message_count": len(messages), "assertions": len(rules.get("extract", [])) + 1, "extractions": extracted}


def receipt(operation: str, result: str, started: float, **metadata: Any) -> dict[str, Any]:
    """Emit only allowlisted metadata."""
    allowlist = {"mode", "entity", "repository", "run_id", "error_code", "cleanup_state", "assertion_count", "message_count", "cursor", "fidelity"}
    return {"schema_version": VERSION, "operation": operation, "result": result, "duration_ms": round((time.monotonic() - started) * 1000), **{key: value for key, value in metadata.items() if key in allowlist}}
