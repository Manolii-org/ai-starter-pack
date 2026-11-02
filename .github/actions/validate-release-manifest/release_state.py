"""CAS release-admission metadata in a custom Git ref; never grants approval.

The ref is outside refs/heads, so admission writes are not application pushes.
The owner's ordinary writer inventory must qualify this transport before use.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from release_manifest import _instant, _invalid_constant, _pairs, validate

MAX_STATE_BYTES = 1_000_000
# Git's blob JSON includes base64 and line wrapping as well as the raw state.
MAX_API_BYTES = 2_000_000


class StateConflict(RuntimeError):
    """Another admission/stage advanced the shared revision."""


class StateUnavailable(RuntimeError):
    """The trusted shared state could not be observed or retained."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise StateUnavailable("state API returned an invalid object identity")
    return value


class GitHubStateStore:
    """Git fast-forward-only updates compare the exact retained parent revision."""

    def __init__(self, repo: str, environment: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("invalid repository")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", environment):
            raise ValueError("environment needs a stable normalized key")
        if not token:
            raise ValueError("state transport requires its owning scoped token")
        self.repo = repo
        self.ref = "release-state/" + environment
        self.token = token

    def request(self, method: str, path: str, payload=None, *, missing=False):
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request = Request("https://api.github.com/repos/" + self.repo + "/" + path,
                          data=body, method=method,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Accept": "application/vnd.github+json",
                                   "Content-Type": "application/json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with build_opener(_NoRedirect()).open(request, timeout=30) as response:
                raw = response.read(MAX_API_BYTES + 1)
            if len(raw) > MAX_API_BYTES:
                raise StateUnavailable("state response exceeds bound")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise StateUnavailable("state response is not an object")
            return data
        except HTTPError as exc:
            exc.close()
            if missing and exc.code == 404:
                return None
            if method in {"PATCH", "POST"} and exc.code in {409, 422}:
                raise StateConflict("shared release revision changed or write refused") from exc
            raise StateUnavailable("release state API refused HTTP " + str(exc.code)) from exc
        except (OSError, ValueError) as exc:
            raise StateUnavailable("release state transport or representation unavailable") from exc

    def read(self) -> tuple[str, dict]:
        # A 404 can conceal denied access. Runtime admission never fabricates
        # empty state from absence; initialization is an explicit adoption act.
        ref = self.request("GET", "git/ref/" + self.ref)
        try:
            revision = _sha(ref["object"]["sha"])
            commit = self.request("GET", "git/commits/" + revision)
            tree = self.request("GET", "git/trees/" + _sha(commit["tree"]["sha"]))
            entries = tree["tree"]
            if len(entries) != 1 or entries[0].get("path") != "release-state.json" \
                    or entries[0].get("mode") != "100644" or entries[0].get("type") != "blob":
                raise StateUnavailable("unexpected release state tree")
            blob_sha = _sha(entries[0]["sha"])
            blob = self.request("GET", "git/blobs/" + blob_sha)
            size = blob.get("size")
            if (blob.get("encoding") != "base64" or type(size) is not int
                    or not 0 <= size <= MAX_STATE_BYTES):
                raise StateUnavailable("unexpected release state blob")
            raw = base64.b64decode("".join(blob["content"].split()), validate=True)
            if len(raw) != size or len(raw) > MAX_STATE_BYTES:
                raise StateUnavailable("release state blob size differs")
            if hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != blob_sha:  # nosec B324 -- native Git object identity
                raise StateUnavailable("release state blob identity differs")
            state = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
            validate_state(state)
            return revision, state
        except (KeyError, TypeError, ValueError) as exc:
            raise StateUnavailable("release state could not be verified") from exc

    def compare_and_swap(self, expected: str | None, state: dict) -> str:
        if expected is None:
            raise StateUnavailable("runtime admission requires an initialized retained revision")
        return self._compare_and_swap(expected, state)

    def initialize_empty_for_adoption(self) -> str:
        """Explicit bootstrap after qualified source adoption and legacy drain.

        Creates only an empty custom ref; an existing/concealed ref cannot be
        overwritten because the provider must accept a CREATE, never an update.
        This operation grants no stage or production execution authority.
        """
        return self._compare_and_swap(None, {"schema_version": 1, "active": None, "continuations": {}})

    def _compare_and_swap(self, expected: str | None, state: dict) -> str:
        validate_state(state)
        if expected is not None and not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise ValueError("state revision must be an exact commit SHA")
        current = self.request("GET", "git/ref/" + self.ref, missing=True)
        try:
            observed = _sha(current["object"]["sha"]) if current is not None else None
        except (KeyError, TypeError) as exc:
            raise StateUnavailable("state reference representation unavailable") from exc
        if observed != expected:
            raise StateConflict("shared release revision differs from expected parent")
        content = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(content.encode()) > MAX_STATE_BYTES:
            raise StateUnavailable("release state requires owner retention maintenance")
        try:
            blob = self.request("POST", "git/blobs", {"content": content, "encoding": "utf-8"})
            tree = self.request("POST", "git/trees", {"tree": [{"path": "release-state.json",
                                "mode": "100644", "type": "blob", "sha": _sha(blob["sha"])}]})
            # Identical concurrent proposals must still have different child
            # commits. Otherwise both same-SHA ref updates could report success.
            commit = self.request("POST", "git/commits", {"message": "Record release admission state "
                                  + secrets.token_hex(16), "tree": _sha(tree["sha"]),
                                  "parents": [expected] if expected else []})
            revision = _sha(commit["sha"])
            if expected is None:
                confirmation = self.request("POST", "git/refs", {"ref": "refs/" + self.ref, "sha": revision})
            else:
                # No force: two children of one parent diverge, so only one wins.
                confirmation = self.request("PATCH", "git/refs/" + self.ref, {"sha": revision, "force": False})
            if confirmation.get("ref") != "refs/" + self.ref or confirmation.get("object", {}).get("sha") != revision:
                raise StateUnavailable("state reference write acknowledgement differs; reconcile before proceeding")
            return revision
        except (KeyError, TypeError) as exc:
            raise StateUnavailable("release state write response is incomplete") from exc


def validate_state(state: dict) -> None:
    if (not isinstance(state, dict) or set(state) != {"schema_version", "active", "continuations"}
            or type(state["schema_version"]) is not int or state["schema_version"] != 1
            or not isinstance(state["continuations"], dict)):
        raise StateUnavailable("invalid release state envelope")
    for parent, child in state["continuations"].items():
        if not isinstance(parent, str) or not isinstance(child, str) \
                or not re.fullmatch(r"[0-9a-f]{64}", parent) or not re.fullmatch(r"[0-9a-f]{64}", child):
            raise StateUnavailable("invalid continuation identity")
    active = state["active"]
    if active is not None and (not isinstance(active, dict) or set(active) != {"release_id", "phase"}
                              or not isinstance(active["release_id"], str)
                              or not re.fullmatch(r"[0-9a-f]{64}", active["release_id"])
                              or not isinstance(active["phase"], str)
                              or active["phase"] not in {"selected", "schema-running", "schema-partial",
                                                        "schema-applied", "activation-held", "activation-running",
                                                        "activation-unverified", "failed-unverified", "success",
                                                        "failed-before-writes", "recovered"}):
        raise StateUnavailable("invalid active release state")


def reserve(state: dict, manifest: dict, *, now: datetime | None = None) -> dict:
    """Construct next admission state; only a successful store CAS reserves it."""
    validate_state(state)
    validate(manifest)
    active = state["active"]
    subject = manifest["subject"]
    admission_time = datetime.now(timezone.utc) if now is None else now
    if not isinstance(admission_time, datetime) or admission_time.utcoffset() is None:
        raise StateUnavailable("admission clock must be a timezone-aware instant")
    if _instant(subject["timing"]["execute_before"]) <= admission_time:
        raise StateConflict("expired release scope cannot be admitted")
    if subject["kind"] == "activation-only":
        parent = subject["continuation"]["parent_release_id"]
        if (not active or active["release_id"] != parent
                or active["phase"] not in {"schema-applied", "activation-held"}
                or parent in state["continuations"]):
            raise StateConflict("predecessor unavailable or continuation already admitted")
    elif active and active["phase"] not in {"success", "failed-before-writes", "recovered"}:
        raise StateConflict("an active or unresolved release must complete before new admission")
    result = copy.deepcopy(state)
    result["active"] = {"release_id": manifest["release_id"], "phase": "selected"}
    if subject["kind"] == "activation-only":
        result["continuations"][parent] = manifest["release_id"]
    validate_state(result)
    return result
