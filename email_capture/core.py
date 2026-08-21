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


@dataclass(frozen=True)
class Profile:
    """Validated capture configuration."""

    mode: str
    backend: str
    endpoint: str
    environment: str
    ttl_seconds: int = 900

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
        version = raw.get("schema_version", VERSION)
        mode = raw.get("mode", "off")
        backend_name = raw.get("backend", "memory")
        environment = raw.get("environment", os.environ.get("NODE_ENV", "test"))
        ttl = raw.get("ttl_seconds", 900)
        endpoint = raw.get("endpoint", "")
        values = (version, mode, backend_name, environment, endpoint)
        if not all(isinstance(value, str) for value in values):
            raise CaptureError("CONFIG_INVALID", "profile fields have invalid types")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 86400:
            raise CaptureError("CONFIG_INVALID", "TTL must be between 1 and 86400 seconds")
        if version.split(".")[0] != "1":
            raise CaptureError("CONFIG_INVALID", "unsupported schema major version")
        if mode not in {"off", "hermetic"} or backend_name not in {"memory", "mailpit", "maildev", "inbucket"}:
            raise CaptureError("CONFIG_INVALID", "unknown mode or backend")
        if mode != "off" and environment.lower() in {"prod", "production", "staging"}:
            raise CaptureError("CONFIG_INVALID", "capture forbidden in deployed environment")
        if mode == "hermetic" and backend_name != "memory":
            _validate_local_endpoint(endpoint)
        return cls(mode, backend_name, endpoint, environment, ttl)


class Backend(Protocol):
    """Receiver service-provider interface."""

    def capabilities(self) -> dict[str, Any]: ...
    def list(self, allocation: dict[str, Any]) -> list[dict[str, Any]]: ...
    def purge(self, allocation: dict[str, Any]) -> None: ...
    def health(self) -> bool: ...


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
            with self._opener.open(request, timeout=self.request_timeout) as response:
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


def _summary_contains_recipient(row: dict[str, Any], recipient: str) -> bool:
    """Match the envelope recipient without searching bodies or subjects."""
    values = row.get("To", row.get("to", row.get("envelope", {}).get("to", [])))
    rendered: list[str] = []
    for value in _addresses(values):
        address = value.get("Address", value.get("address", "")) if isinstance(value, dict) else str(value)
        rendered.append(address)
    for _display_name, address in getaddresses(rendered):
        if address.lower() == recipient:
            return True
    return False


def _message_id(row: dict[str, Any]) -> str:
    """Return a receiver message identifier or reject an undeletable row."""
    identifier = row.get("ID", row.get("id", row.get("key")))
    if not isinstance(identifier, str) or not identifier:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "message lacks stable id")
    return identifier


def normalise_message(row: dict[str, Any], backend_name: str) -> dict[str, Any]:
    """Map receiver-specific detail records into the v1 message shape."""
    identifier = _message_id(row)
    received = row.get("received_at") or row.get("Created") or row.get("created") or row.get("date") or row.get("Date") or row.get("time")
    if not isinstance(received, str) or not received:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE", "message lacks stable receive time")
    body = row.get("body", {}) if isinstance(row.get("body", {}), dict) else {}
    text = row.get("text", row.get("Text", body.get("text", "")))
    html = row.get("html", row.get("HTML", body.get("html", "")))
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
        size = int(attachment.get("size", attachment.get("Size", 0)) or 0)
        if size > MAX_ATTACHMENT_BYTES:
            raise CaptureError("CAPABILITY_UNSUPPORTED", "attachment size limit exceeded")
        normalized_attachments.append({
            "filename": attachment.get("filename", attachment.get("FileName", "")),
            "content_type": attachment.get("content-type", attachment.get("contentType", attachment.get("ContentType", "application/octet-stream"))),
            "size": size,
        })
    opaque = hashlib.sha256(f"{backend_name}:{identifier}".encode()).hexdigest()[:24]
    return {
        "schema_version": VERSION,
        "opaque_id": opaque,
        "received_at": received,
        "envelope": {"from": _addresses(row.get("from", row.get("From"))), "to": _addresses(row.get("to", row.get("To")))},
        "subject": row.get("subject", row.get("Subject", "")),
        "headers": headers,
        "content": {"text_present": bool(text), "html_present": bool(html), "text": text or "", "html": html or ""},
        "attachments": normalized_attachments,
    }


def backend(profile: Profile) -> Backend:
    """Construct an enabled backend only."""
    if profile.mode == "off":
        raise CaptureError("CAPABILITY_UNSUPPORTED", "capture mode is off")
    return MemoryBackend(profile) if profile.backend == "memory" else HttpBackend(profile)


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
    deadline = time.monotonic() + timeout
    cursor = allocation.get("cursor", "0:")
    while True:
        if isinstance(selected_backend, HttpBackend):
            selected_backend.request_timeout = max(0.01, min(10.0, deadline - time.monotonic()))
        rows = sorted(selected_backend.list(allocation), key=lambda message: (message["received_at"], message["opaque_id"]))
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
    allowlist = {"mode", "entity", "repository", "run_id", "error_code", "cleanup_state", "assertion_count", "message_count", "cursor"}
    return {"schema_version": VERSION, "operation": operation, "result": result, "duration_ms": round((time.monotonic() - started) * 1000), **{key: value for key, value in metadata.items() if key in allowlist}}
