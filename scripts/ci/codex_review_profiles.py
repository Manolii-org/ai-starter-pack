"""Owner-authorised personal sessions on one trusted, persistent private host.

This module never accepts a credential path, account override, or consent from
a PR. The host administrator installs the registry and owner consent outside
checkouts. Each account has one session and one OS lock across all repositories.
The ordinary CLI owns refresh; this module does not implement OAuth refresh.
"""

from __future__ import annotations

from contextlib import contextmanager
import base64
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

REGISTRY = Path("/etc/codex-review/profiles.json")
STATE = Path("/var/lib/codex-review")
CODEX = "/opt/codex-review/bin/codex"
SLUG = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
REPO = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
AUTOMATION_ROLES = frozenset({"assessment", "triage", "critic", "judge", "implementation", "autofix"})


class ProfileError(ValueError):
    """Only fixed categories, never provider diagnostics or credentials."""


def valid(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def protected_json(path):
    # Refuse symlinks at every boundary, including a replaced parent directory.
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ProfileError("profile_policy_unprotected")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 131072:
        raise ProfileError("profile_policy_invalid")
    return json.loads(path.read_text())


def registry():
    data = protected_json(REGISTRY)
    machine = hashlib.sha256(Path("/etc/machine-id").read_bytes().strip()).hexdigest()
    if (
        not isinstance(data, dict)
        or data.get("schema") != "codex-review-profiles/v1"
        or data.get("host_sha256") != machine
        or not isinstance(data.get("profiles"), dict)
        or not isinstance(data.get("repositories"), dict)
        or not isinstance(data.get("organisations"), dict)
    ):
        raise ProfileError("profile_registry_invalid")
    accounts = set()
    for name, profile in data["profiles"].items():
        if (
            not valid(SLUG, name)
            or not isinstance(profile, dict)
            or not valid(SLUG, profile.get("owner"))
            or not valid(HASH, profile.get("account_sha256"))
            or type(profile.get("generation")) is not int
            or profile["generation"] < 1
        ):
            raise ProfileError("profile_registry_invalid")
        # Aliases must not create independent sessions or capacity for one account.
        if profile["account_sha256"] in accounts:
            raise ProfileError("profile_duplicate_account")
        accounts.add(profile["account_sha256"])
    for repo, name in data["repositories"].items():
        if (
            not valid(REPO, repo)
            or not isinstance(name, str)
            or name not in data["profiles"]
        ):
            raise ProfileError("profile_assignment_invalid")
    for org, name in data["organisations"].items():
        if (
            not valid(SLUG, org)
            or not isinstance(name, str)
            or name not in data["profiles"]
        ):
            raise ProfileError("profile_assignment_invalid")
    return data


def select(repository, lane, *, reserved=None):
    return _select(repository, lane, reserved=reserved, automation=False)


def select_automation(repository, role, *, reserved=None):
    """New roles require their own protected grant and exact controller access.

    They reuse the registry's one account/session; review-only consent cannot
    grant automation privileges and a queued role cannot become another role.
    """
    if not isinstance(repository, str) or not isinstance(role, str):
        raise ProfileError("profile_scope_invalid")
    return _select(repository, role, reserved=reserved, automation=True)


def _select(repository, lane, *, reserved, automation):
    data = registry()
    repository = repository.lower()
    allowed_lanes = AUTOMATION_ROLES if automation else {"code", "security"}
    if not REPO.fullmatch(repository) or lane not in allowed_lanes:
        raise ProfileError("profile_scope_invalid")
    access = data.get("runner_access")
    if not isinstance(access, dict):
        raise ProfileError("profile_host_access_unverified")
    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "")
    pieces = workflow.split("/", 2)
    if len(pieces) == 3:
        workflow = pieces[0].lower() + "/" + pieces[1].lower() + "/" + pieces[2]
    controller = "codex-automation-worker.yml" if automation else "codex-review-worker.yml"
    if (
        access.get("mode") != "selected_trusted_workflows"
        or not valid(HASH, access.get("verification_sha256"))
        or type(access.get("verified_until")) is not int
        or access["verified_until"] <= int(time.time())
        or not isinstance(access.get("workflows"), list)
        or workflow not in access["workflows"]
        or not workflow.startswith(
            repository + "/.github/workflows/" + controller + "@refs/heads/"
        )
    ):
        raise ProfileError("profile_host_access_unverified")
    # Explicit repo pin wins. There is no cross-organisation/global default.
    name = data["repositories"].get(repository)
    if name is None:
        name = data["organisations"].get(repository.split("/")[0])
    if name not in data["profiles"]:
        raise ProfileError("profile_not_nominated")
    profile = data["profiles"][name]
    directory = "automation-consents" if automation else "consents"
    grant_field = "roles" if automation else "lanes"
    schema = "codex-automation-consent/v1" if automation else "codex-review-consent/v1"
    consent = protected_json(REGISTRY.parent / directory / (name + ".json"))
    if not isinstance(consent, dict) or not isinstance(consent.get(grant_field), list):
        raise ProfileError("profile_consent_unavailable")
    now = int(time.time())
    allowed = consent.get("repositories")
    if (
        consent.get("schema") != schema
        or consent.get("profile") != name
        or consent.get("owner") != profile["owner"]
        or consent.get("account_sha256") != profile["account_sha256"]
        or consent.get("generation") != profile["generation"]
        or consent.get("revoked") is not False
        or type(consent.get("expires_at")) is not int
        or consent["expires_at"] <= now
        or not isinstance(allowed, list)
        or repository not in allowed
        or lane not in consent.get(grant_field, [])
        or consent.get("purchased_credits_disabled") is not True
        or consent.get("automatic_purchases_disabled") is not True
    ):
        raise ProfileError("profile_consent_unavailable")
    assignment = {
        "repository": repository,
        "profile": name,
        "host_sha256": data["host_sha256"],
        "host_access_sha256": access["verification_sha256"],
        "account_sha256": profile["account_sha256"],
        "owner": profile["owner"],
        "generation": profile["generation"],
        "consent_sha256": digest(consent),
    }
    if automation:
        assignment.update(role=lane, controller_workflow=workflow)
    assignment["assignment_sha256"] = digest(assignment)
    if reserved is not None and assignment != reserved:
        raise ProfileError("profile_assignment_changed")
    return assignment


def private_home(account):
    home = STATE / "accounts" / account
    # State belongs to the dedicated trusted controller user, never PR jobs.
    for path in (STATE, STATE / "accounts", home):
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o077
        ):
            raise ProfileError("profile_session_unprotected")
    return home


def read_auth(home, assignment):
    path = home / "auth.json"
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or not 0 < info.st_size <= 65536
    ):
        raise ProfileError("profile_session_unprotected")
    auth = json.loads(path.read_text())
    if not isinstance(auth, dict) or not isinstance(auth.get("tokens"), dict):
        raise ProfileError("profile_account_mismatch")
    tokens = auth.get("tokens") or {}
    if (
        auth.get("auth_mode") != "chatgpt"
        or auth.get("OPENAI_API_KEY")
        or not all(
            isinstance(tokens.get(key), str) and tokens[key]
            for key in ("account_id", "access_token", "refresh_token", "id_token")
        )
        or hashlib.sha256(tokens["account_id"].encode()).hexdigest()
        != assignment["account_sha256"]
    ):
        raise ProfileError("profile_account_mismatch")
    return auth


def cli_environment(home):
    # Explicit allowlist: no GitHub/Doppler/other-provider secrets, user config,
    # project hooks or inherited CODEX_HOME can enter the trusted auth probe.
    return {
        **{
            key: os.environ[key]
            for key in (
                "PATH",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "NO_PROXY",
                "SSL_CERT_FILE",
                "NODE_EXTRA_CA_CERTS",
            )
            if key in os.environ
        },
        "HOME": str(home),
        "CODEX_HOME": str(home),
    }


def account_probe(home, *, seconds=30):
    """Official CLI handles account read/refresh; no model turn is started."""
    installed = Path(CODEX).resolve(strict=True)
    for path in (Path(CODEX), *Path(CODEX).parents, installed, *installed.parents):
        info = path.lstat()
        if info.st_uid != 0 or (
            not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022
        ):
            raise ProfileError("profile_cli_unprotected")
    # npm's Node launcher spawns Rust and does not forward SIGKILL. Only a
    # native process can be owned by this parent-death/lock lifetime contract.
    with installed.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ProfileError("profile_cli_native_required")
    version = subprocess.run(
        [str(installed), "--version"],
        env=cli_environment(home),
        capture_output=True,
        timeout=10,
        check=False,
    )
    if version.returncode or version.stdout.strip() != b"codex-cli 0.163.0":
        raise ProfileError("profile_cli_unqualified")
    requests = [
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "trusted_review", "version": "1"}},
        },
        {"method": "initialized"},
        {"id": 2, "method": "account/read", "params": {"refreshToken": True}},
        {"id": 3, "method": "account/rateLimits/read"},
    ]
    replies = {}
    parent = os.getpid()

    def protect_child():
        # A killed controller cannot leave a refresh writer past its account
        # lock. This host mode is explicitly Linux-only (flock + machine-id).
        if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
            os._exit(1)

    with subprocess.Popen(
        [str(installed), "app-server"],
        cwd=home,
        env=cli_environment(home),
        preexec_fn=protect_child,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as proc:
        try:
            # Initialise before issuing authenticated RPCs. The server may
            # process requests concurrently; a rate-limit read must follow refresh.
            pending = b""
            total = 0
            deadline = time.monotonic() + seconds
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                for request in requests:
                    proc.stdin.write((json.dumps(request) + "\n").encode())
                    proc.stdin.flush()
                    if "id" not in request:
                        continue
                    while request["id"] not in replies:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise ProfileError("profile_probe_unavailable")
                        block = os.read(proc.stdout.fileno(), 65536)
                        total += len(block)
                        if not block or total > 1048576:
                            raise ProfileError("profile_probe_unavailable")
                        pending += block
                        *lines, pending = pending.split(b"\n")
                        for line in lines:
                            message = json.loads(line)
                            if not isinstance(message, dict):
                                raise ProfileError("profile_probe_unavailable")
                            if "id" in message:
                                if "error" in message:
                                    raise ProfileError("profile_probe_unavailable")
                                replies[message["id"]] = message.get("result")
            return replies[2], replies[3]
        finally:
            proc.kill()
            proc.wait(timeout=5)


def capacity(account, limits, assignment):
    if not isinstance(account, dict) or not isinstance(limits, dict):
        raise ProfileError("profile_capacity_unknown")
    value = (account or {}).get("account") or {}
    raw = (limits or {}).get("rateLimits") or {}
    if not isinstance(value, dict) or not isinstance(raw, dict):
        raise ProfileError("profile_capacity_unknown")
    # account/read returns type/email/planType, not an account ID. The CLI
    # validates the account remotely; compare the authoritative auth file after
    # this probe in session(), rather than inventing an RPC identity field.
    if value.get("type") != "chatgpt":
        raise ProfileError("profile_account_unverified")
    credits = raw.get("credits") or {}
    if not isinstance(credits, dict):
        raise ProfileError("profile_funding_unverified")
    observed_account = limits.get("accountId")
    if observed_account is not None and (
        not isinstance(observed_account, str)
        or hashlib.sha256(observed_account.encode()).hexdigest()
        != assignment["account_sha256"]
    ):
        raise ProfileError("profile_account_mismatch")
    if limits.get("ordinaryUsageAllowed") is not True:
        raise ProfileError("profile_capacity_unknown")
    # Unknown credit state is not evidence of included-only funding. Even a
    # zero balance does not verify auto-purchase settings: owner controls above
    # remain required, and receipts do not claim provider-side spend enforcement.
    if credits.get("hasCredits") is not False or credits.get("unlimited") is not False:
        raise ProfileError("profile_funding_unverified")
    snapshot = {"observed_at": int(time.time()), "windows": {}}
    for key in ("primary", "secondary"):
        window = raw.get(key)
        if not isinstance(window, dict):
            raise ProfileError("profile_capacity_unknown")
        used = window.get("usedPercent")
        reset = window.get("resetsAt")
        if (
            type(used) not in {int, float}
            or not math.isfinite(used)
            or not 0 <= used < 80
            or type(reset) is not int
            or reset <= int(time.time())
        ):
            raise ProfileError("profile_capacity_exhausted")
        snapshot["windows"][key] = {"used_percent": used, "resets_at": reset}
    return snapshot


@contextmanager
def _session(assignment, repository, lane, *, wait_seconds, selector):
    """One quota owner, one refresh writer, all admitted roles/repositories.

    Jobs queue in GitHub's dedicated runner queue. A busy account yields an
    explicit incomplete result rather than jumping to another person's account.
    No cancellation, account rotation or inferred extra quota is authorised here.
    """
    selector(repository, lane, reserved=assignment)
    home = private_home(assignment["account_sha256"])
    path = home / "review.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise ProfileError("profile_session_unprotected")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProfileError("profile_session_busy") from exc
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        selector(repository, lane, reserved=assignment)
        read_auth(home, assignment)
        account, limits = account_probe(home)
        snapshot = capacity(account, limits, assignment)
        # Read the refreshed file; never restore the initial seed or write to
        # the managed-executor/canary secret. Its session is independent.
        auth = read_auth(home, assignment)
        try:
            payload = auth["tokens"]["access_token"].split(".")[1]
            expiry = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            ).get("exp")
            if type(expiry) is not int or expiry < int(time.time()) + 1500:
                raise ValueError("short token")
        except (ValueError, IndexError, KeyError) as exc:
            raise ProfileError("profile_session_expiry_unverified") from exc
        selector(repository, lane, reserved=assignment)
        yield auth, snapshot
    finally:
        os.close(fd)


def session(assignment, repository, lane, *, wait_seconds=1250):
    return _session(assignment, repository, lane, wait_seconds=wait_seconds, selector=select)


def automation_session(assignment, repository, role, *, wait_seconds=1250):
    return _session(assignment, repository, role, wait_seconds=wait_seconds, selector=select_automation)
