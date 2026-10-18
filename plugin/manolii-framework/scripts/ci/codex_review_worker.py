"""Trusted default-branch controller and revision-proof reader.

No PR executable/configuration runs on this host. Source is a bare Git input to
an external, credential-free container. Failures are explicit incomplete states.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Only the explicitly checked-out trusted controller directory is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_review_broker import (
    BrokerError,
    NoRedirect,
    REVIEW_MODEL,
    Subscription,
    serve,
)
from codex_review_receipt import build_receipt, final_code_turn, security_required

HERE = Path(__file__).resolve().parent
WORKFLOW = ".github/workflows/codex-review-worker.yml"
LANES = {
    ".github/workflows/codex-pr-review.yml": "code",
    ".github/workflows/codex-security-diff-scan.yml": "security",
}
JOBS = {"code": "Codex review coverage", "security": "Codex security coverage"}
MAX_JSON = 4 * 1024 * 1024
MAX_ZIP = 2 * 1024 * 1024
MAX_REVIEW = 1024 * 1024
MAX_GIT_PACK = 256 * 1024 * 1024
MAX_REQUESTS = {"code": 32, "security": 96}
SAFE_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
TOOLS = {
    "model": REVIEW_MODEL,
    "code": "0.151.0",
    "security": "0.2.0",
    "security_codex": "0.162.0-alpha.15",
    "security_sdk": "0.162.0-alpha.15",
}


class Incomplete(Exception):
    """Only a fixed, non-sensitive category may reach a receipt or a log."""


def policy_hash():
    digest = hashlib.sha256()
    for name in (
        "codex_review_worker.py",
        "codex_review_broker.py",
        "codex_review_receipt.py",
        "codex-review-worker.json",
        "codex-review-runtime/Dockerfile",
        "codex-review-runtime/container.py",
        "codex-review-runtime/seccomp.json",
        "codex-review-runtime/apparmor",
    ):
        digest.update(name.encode() + b"\0" + (HERE / name).read_bytes() + b"\0")
    return digest.hexdigest()


def bounded_json(response, limit=MAX_JSON):
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise Incomplete("response_size_exceeded")
    return json.loads(raw)


class GitHub:
    def __init__(self, repository, token):
        if not SAFE_SLUG.fullmatch(repository) or not token:
            raise Incomplete("github_identity_unavailable")
        self.repository = repository
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, route, *, method="GET", data=None):
        # No caller-supplied absolute URLs or redirects can carry this token.
        if route and (not route.startswith("/") or route.startswith("//")):
            raise Incomplete("invalid_github_route")
        request = urllib.request.Request(
            "https://api.github.com/repos/" + self.repository + route,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            return self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise Incomplete(
                "github_resource_missing"
                if exc.code == 404
                else "github_capability_refused"
            ) from exc

    def json(self, route, **kwargs):
        with self.request(route, **kwargs) as response:
            if response.status == 204:
                return {}
            return bounded_json(response)

    def pages(self, route, key=None):
        result = []
        for page in range(1, 21):
            data = self.json(
                route + ("&" if "?" in route else "?") + f"per_page=100&page={page}"
            )
            items = data[key] if key else data
            if not isinstance(items, list):
                raise Incomplete("invalid_github_result")
            result.extend(items)
            if len(items) < 100:
                return result
        raise Incomplete("github_pagination_incomplete")

    def archive(self, artifact_id):
        # Obtain the signed URL without forwarding Authorization on its hop.
        route = f"/actions/artifacts/{int(artifact_id)}/zip"
        url = "https://api.github.com/repos/" + self.repository + route
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location", "")
            status = exc.code
            exc.close()
            target = urllib.parse.urlsplit(location)
            if (
                status != 302
                or target.scheme != "https"
                or target.username
                or target.password
                or target.port not in (None, 443)
                or not target.hostname
                or not (
                    target.hostname.endswith(".blob.core.windows.net")
                    or target.hostname.endswith(".githubusercontent.com")
                )
            ):
                raise Incomplete("artifact_download_refused") from exc
            response = self.opener.open(urllib.request.Request(location), timeout=30)
        with response:
            raw = response.read(MAX_ZIP + 1)
        if len(raw) > MAX_ZIP:
            raise Incomplete("artifact_too_large")
        return raw


def identity(pr, repository, lane):
    base = (pr.get("base") or {}).get("sha", "")
    head = (pr.get("head") or {}).get("sha", "")
    number = pr.get("number")
    if (
        lane not in JOBS
        or not isinstance(number, int)
        or number <= 0
        or not SHA.fullmatch(base)
        or not SHA.fullmatch(head)
    ):
        raise Incomplete("invalid_revision")
    if any(
        (pr.get(side, {}).get("repo") or {}).get("full_name", "").lower()
        != repository.lower()
        for side in ("head", "base")
    ):
        raise Incomplete("native_required_for_fork")
    return {
        "repository": repository,
        "pull_request": number,
        "base_sha": base,
        "head_sha": head,
        "lane": lane,
    }


def admit(github, event):
    repository = github.json("")
    request = github.json(f"/actions/runs/{int(event['workflow_run']['id'])}")
    lane = LANES.get(request.get("path"))
    if (
        not lane
        or request.get("event") != "pull_request"
        or request.get("status") != "completed"
    ):
        raise Incomplete("request_producer_refused")
    expected = github.json("/actions/workflows/" + request["path"].rsplit("/", 1)[1])
    if (
        request.get("workflow_id") != expected.get("id")
        or expected.get("path") != request["path"]
        or (request.get("repository") or {}).get("id") != repository.get("id")
        or (request.get("head_repository") or {}).get("id") != repository.get("id")
    ):
        raise Incomplete("request_producer_refused")
    pulls = request.get("pull_requests") or []
    if len(pulls) != 1:
        raise Incomplete("request_pull_association_missing")
    pr = github.json(f"/pulls/{int(pulls[0]['number'])}")
    target = identity(pr, github.repository, lane)
    if (
        request.get("head_sha") != target["head_sha"]
        or (pulls[0].get("head") or {}).get("sha") != target["head_sha"]
    ):
        raise Incomplete("superseded_revision")
    if pr.get("state") != "open" or pr.get("draft"):
        raise Incomplete("pull_not_ready")
    users = {
        "devin-ai-integration[bot]",
        "renovate[bot]",
        "dependabot[bot]",
        "mend-bolt-for-github[bot]",
        "claude[bot]",
        "cursor[bot]",
    }
    if (
        pr.get("author_association")
        not in {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}
        and (pr.get("user") or {}).get("login") not in users
    ):
        raise Incomplete("author_not_admitted")
    return target, request, repository


def validate_producer(run, workflow, repository):
    return bool(
        run.get("workflow_id") == workflow.get("id")
        and run.get("path") == WORKFLOW
        and workflow.get("path") == WORKFLOW
        and run.get("event") == "workflow_run"
        and run.get("head_branch") == repository.get("default_branch")
        and (run.get("repository") or {}).get("id") == repository.get("id")
        and (run.get("head_repository") or {}).get("id") == repository.get("id")
        and run.get("status") == "completed"
        and SHA.fullmatch(run.get("head_sha", ""))
    )


def read_receipt(raw, target, policy, *, run_id, workflow_sha):
    output = ""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            files = archive.infolist()
            names = [item.filename for item in files]
            if len(names) != len(set(names)) or set(names) not in (
                {"receipt.json"},
                {"receipt.json", "review.txt"},
            ):
                raise Incomplete("invalid_receipt_archive")
            for item in files:
                limit = 32768 if item.filename == "receipt.json" else MAX_REVIEW
                if (
                    item.is_dir()
                    or item.file_size > limit
                    or item.compress_size > MAX_ZIP
                    or item.flag_bits & 1
                    or ((item.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise Incomplete("invalid_receipt_archive")
            with archive.open("receipt.json") as member:
                receipt = bounded_json(member, 32768)
            if "review.txt" in names:
                with archive.open("review.txt") as member:
                    data = member.read(MAX_REVIEW + 1)
                if len(data) > MAX_REVIEW:
                    raise Incomplete("invalid_receipt_archive")
                output = data.decode("utf-8")
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise Incomplete("invalid_receipt_archive") from exc
    states = {
        "completed",
        "exempt",
        "blocked_credentials",
        "blocked_quota",
        "failed_scan",
        "failed_range",
        "failed_install",
        "failed_output",
        "blocked_previous_attempt",
    }
    if (
        not isinstance(receipt, dict)
        or any(receipt.get(key) != value for key, value in target.items())
        or receipt.get("schema") != "codex-trusted-review/v2"
        or receipt.get("policy_sha256") != policy
        or receipt.get("tools") != TOOLS
        or receipt.get("producer_run_id") != run_id
        or receipt.get("workflow_sha") != workflow_sha
        or receipt.get("billing_mode") != "subscription"
        or receipt.get("auth_boundary") != "external_broker"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt.get("image_id", ""))
        or receipt.get("state") not in states
    ):
        raise Incomplete("invalid_receipt_identity")
    if receipt["state"] == "completed" and receipt.get("reason") not in {
        "verdict_produced",
        "findings_validated",
    }:
        raise Incomplete("invalid_receipt_result")
    if receipt["state"] == "exempt" and receipt.get("reason") not in {
        "empty",
        "security_exempt",
    }:
        raise Incomplete("invalid_receipt_result")
    if receipt["state"] == "completed":
        if (
            not output
            or hashlib.sha256(output.encode()).hexdigest()
            != receipt.get("output_sha256")
            or not isinstance(receipt.get("successful_provider_turns"), int)
            or receipt["successful_provider_turns"] < 1
        ):
            raise Incomplete("completed_output_unavailable")
        if target["lane"] == "code" and not final_code_turn(output):
            raise Incomplete("completed_output_invalid")
        if target["lane"] == "security":
            try:
                findings = json.loads(output)
                findings = (
                    findings.get("findings") if isinstance(findings, dict) else findings
                )
                if not isinstance(findings, list) or any(
                    not isinstance(item, dict) for item in findings
                ):
                    raise ValueError("invalid findings")
            except (TypeError, ValueError) as exc:
                raise Incomplete("completed_output_invalid") from exc
    return {**receipt, "_output": output}


def prior_receipt(github, target, repository, *, ignore_run=None):
    try:
        workflow = github.json("/actions/workflows/codex-review-worker.yml")
    except Incomplete as exc:
        if str(exc) == "github_resource_missing":
            return None
        raise
    # Query exact artifact names, not thousands of unrelated workflow runs.
    # An attempt reservation prevents automatic retries after cancellation or
    # publication failure. It can never satisfy coverage.
    for prefix in ("codex-trusted", "codex-attempt"):
        name = (
            f"{prefix}-{target['pull_request']}-{target['lane']}-{target['head_sha']}"
        )
        artifacts = github.pages(
            "/actions/artifacts?" + urllib.parse.urlencode({"name": name}), "artifacts"
        )
        for artifact in artifacts:
            owner = artifact.get("workflow_run") or {}
            if (
                artifact.get("name") != name
                or artifact.get("expired")
                or artifact.get("size_in_bytes", MAX_ZIP + 1) > MAX_ZIP
                or not owner.get("id")
                or owner["id"] == ignore_run
            ):
                continue
            run = github.json(f"/actions/runs/{int(owner['id'])}")
            if (
                not validate_producer(run, workflow, repository)
                or owner.get("head_sha") != run["head_sha"]
            ):
                continue
            try:
                receipt = read_receipt(
                    github.archive(artifact["id"]),
                    target,
                    policy_hash(),
                    run_id=run["id"],
                    workflow_sha=run["head_sha"],
                )
                if prefix == "codex-attempt" and (
                    receipt["state"] != "blocked_previous_attempt"
                    or receipt["reason"] != "attempt_reserved"
                ):
                    continue
                return receipt
            except Incomplete:
                continue
    return None


def verify_reservation(github, target, repository):
    artifact_id = int(os.environ.get("ATTEMPT_ARTIFACT_ID", "0"))
    artifact = github.json(f"/actions/artifacts/{artifact_id}")
    run_id = int(os.environ["GITHUB_RUN_ID"])
    run = github.json(f"/actions/runs/{run_id}")
    workflow = github.json("/actions/workflows/codex-review-worker.yml")
    # The current producer is still running; all other provenance checks apply.
    producer = {**run, "status": "completed"}
    owner = artifact.get("workflow_run") or {}
    name = (
        f"codex-attempt-{target['pull_request']}-{target['lane']}-{target['head_sha']}"
    )
    if (
        not validate_producer(producer, workflow, repository)
        or run.get("status") != "in_progress"
        or run.get("head_sha") != os.environ["GITHUB_WORKFLOW_SHA"]
        or artifact.get("name") != name
        or artifact.get("expired")
        or owner.get("id") != run_id
        or owner.get("head_sha") != run["head_sha"]
    ):
        raise Incomplete("attempt_reservation_unverified")
    receipt = read_receipt(
        github.archive(artifact_id),
        target,
        policy_hash(),
        run_id=run_id,
        workflow_sha=run["head_sha"],
    )
    if (
        receipt["state"] != "blocked_previous_attempt"
        or receipt["reason"] != "attempt_reserved"
    ):
        raise Incomplete("attempt_reservation_unverified")


def native_proof(github, target):
    """Native code/security protocols remain separate; no comment is worker proof."""

    def bot(item):
        return (item.get("user") or {}).get("login") == "chatgpt-codex-connector[bot]"

    head = target["head_sha"]
    pr = target["pull_request"]

    def bad(item):
        return bool(
            re.search(
                r"(?:^|\n)(?:you have reached your Codex usage limits|Codex usage limits have been reached)|connect to github|create a Codex account",
                item.get("body") or "",
                re.I,
            )
        )

    if target["lane"] == "code":
        reviews = github.pages(f"/pulls/{pr}/reviews")
        reviews = [
            item
            for item in reviews
            if bot(item)
            and not bad(item)
            and item.get("commit_id") == head
            and str(item.get("state", "")).upper()
            in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED"}
        ]
        if any(
            re.search("codex review", item.get("body") or "", re.I)
            and not re.search(
                "codex-security-review|codex security review",
                item.get("body") or "",
                re.I,
            )
            for item in reviews
        ):
            return True
        ids = {item.get("id") for item in reviews if item.get("id")}
        inline = github.pages(f"/pulls/{pr}/comments")
        if any(
            bot(item)
            and not bad(item)
            and item.get("commit_id") == head
            and item.get("original_commit_id") == head
            and item.get("path")
            and not item.get("in_reply_to_id")
            and item.get("pull_request_review_id") in ids
            and re.search(
                r"<!-- codex-code-review-finding:v1 -->\s*$", item.get("body") or ""
            )
            for item in inline
        ):
            return True
    comments = github.pages(f"/issues/{pr}/comments")
    for item in comments:
        if not bot(item) or bad(item):
            continue
        body = item.get("body") or ""
        prefixes = []
        if target["lane"] == "code" and re.search(
            r"codex review:[^\n]*didn.?t find any major issues", body, re.I
        ):
            match = re.search(r"reviewed commit:[^0-9a-f]*([0-9a-f]{7,40})", body, re.I)
            if match:
                prefixes.append(match[1])
        for line in body.splitlines():
            cells = [cell.strip() for cell in line.split("|")]
            if (
                len(cells) < 5
                or not re.search(
                    r"\*\*"
                    + ("Code" if target["lane"] == "code" else "Security")
                    + r" Review\*\*",
                    cells[1],
                    re.I,
                )
                or not re.fullmatch(
                    r"✅\s*\*\*Completed\*\*(?:\s*<relative-time[^>]*>[^<]*</relative-time>)?",
                    cells[2],
                    re.I,
                )
            ):
                continue
            match = re.fullmatch(r"`([0-9a-f]{7,40})`", cells[3], re.I)
            if match:
                prefixes.append(match[1])
        for prefix in prefixes:
            if not head.lower().startswith(prefix.lower()):
                continue
            if len(prefix) == 40:
                return True
            try:
                if github.json("/commits/" + prefix).get("sha") == head:
                    return True
            except Incomplete:
                continue
    return False


def recover_worker(github, event):
    repository = github.json("")
    workflow = github.json("/actions/workflows/codex-review-worker.yml")
    run = github.json(f"/actions/runs/{int(event['workflow_run']['id'])}")
    if not validate_producer(run, workflow, repository):
        raise Incomplete("recovery_producer_refused")
    artifacts = github.pages(f"/actions/runs/{int(run['id'])}/artifacts", "artifacts")
    for artifact in artifacts:
        match = re.fullmatch(
            r"codex-trusted-([1-9][0-9]*)-(code|security)-([0-9a-f]{40})",
            artifact.get("name", ""),
        )
        owner = artifact.get("workflow_run") or {}
        if (
            not match
            or artifact.get("expired")
            or artifact.get("size_in_bytes", MAX_ZIP + 1) > MAX_ZIP
            or owner.get("id") != run["id"]
            or owner.get("head_sha") != run["head_sha"]
        ):
            continue
        pr = github.json(f"/pulls/{int(match[1])}")
        target = identity(pr, github.repository, match[2])
        if target["head_sha"] != match[3] or not fresh(github, target):
            continue
        receipt = read_receipt(
            github.archive(artifact["id"]),
            target,
            policy_hash(),
            run_id=run["id"],
            workflow_sha=run["head_sha"],
        )
        if receipt["state"] not in {"completed", "exempt"}:
            continue
        original_target, request, _ = admit(
            github, {"workflow_run": {"id": receipt["request_run_id"]}}
        )
        if original_target == target:
            rerun_completion(github, request, target["lane"], target)


def fresh(github, target):
    pr = github.json(f"/pulls/{target['pull_request']}")
    return (
        pr.get("state") == "open"
        and not pr.get("draft")
        and identity(pr, github.repository, target["lane"]) == target
    )


def scoped_subscription(config):
    if not config.get("identity") or config.get("enabled") is not True:
        raise Incomplete("blocked_credentials")
    audience = config["audience"]
    oidc_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    oidc_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    target = urllib.parse.urlsplit(oidc_url)
    if (
        target.scheme != "https"
        or not target.hostname
        or not target.hostname.endswith(".actions.githubusercontent.com")
        or not oidc_token
    ):
        raise Incomplete("blocked_credentials")
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(
        oidc_url
        + ("&" if "?" in oidc_url else "?")
        + urllib.parse.urlencode({"audience": audience}),
        headers={"Authorization": "Bearer " + oidc_token},
    )
    try:
        with opener.open(request, timeout=30) as response:
            jwt = bounded_json(response)["value"]
        request = urllib.request.Request(
            "https://api.doppler.com/v3/auth/oidc",
            data=json.dumps({"identity": config["identity"], "token": jwt}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=30) as response:
            token = bounded_json(response)["token"]
        query = urllib.parse.urlencode(
            {
                "project": config["project"],
                "config": config["config"],
                "name": "CODEX_SUBSCRIPTION_AUTH_JSON",
            }
        )
        request = urllib.request.Request(
            "https://api.doppler.com/v3/configs/config/secret?" + query,
            headers={"Authorization": "Bearer " + token},
        )
        with opener.open(request, timeout=30) as response:
            secret = bounded_json(response)["value"]["computed"]
        return Subscription(json.loads(secret))
    except (OSError, ValueError, KeyError, BrokerError, urllib.error.URLError) as exc:
        raise Incomplete("blocked_credentials") from exc


def git_input(directory, target, token):
    # Bare repository: no attacker-controlled working tree is materialized on host.
    source = directory / "source.git"
    source.mkdir()
    common = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.fsmonitor",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*args, auth=False):
        env = dict(common)
        if auth:
            import base64

            env.update(
                GIT_CONFIG_COUNT="3",
                GIT_CONFIG_KEY_2="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_2="Authorization: Basic "
                + base64.b64encode(("x-access-token:" + token).encode()).decode(),
            )
        import resource
        import signal

        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_GIT_PACK, MAX_GIT_PACK))
            resource.setrlimit(
                resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024)
            )
            resource.setrlimit(resource.RLIMIT_CPU, (180, 180))

        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            with subprocess.Popen(
                [
                    "git",
                    "-c",
                    "fetch.unpackLimit=1",
                    "-c",
                    "transfer.unpackLimit=1",
                    "-c",
                    "pack.threads=1",
                    "-c",
                    "gc.auto=0",
                    "-c",
                    "maintenance.auto=false",
                    *args,
                ],
                cwd=source,
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                preexec_fn=limits,
            ) as process:
                try:
                    process.wait(timeout=240)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        process.poll()
                if process.returncode:
                    raise Incomplete("failed_range")
            stdout.seek(0)
            output = stdout.read(2 * 1024 * 1024 + 1)
            if len(output) > 2 * 1024 * 1024:
                raise Incomplete("failed_range")
            return output

    git("init", "--bare", "-q")
    git(
        "remote", "add", "origin", "https://github.com/" + target["repository"] + ".git"
    )
    git(
        "fetch",
        "--no-tags",
        "--depth=2000",
        "origin",
        target["base_sha"] + ":refs/heads/base",
        target["head_sha"] + ":refs/heads/review",
        auth=True,
    )
    stats = dict(
        line.split(": ", 1) for line in git("count-objects", "-v").decode().splitlines()
    )
    if (
        int(stats.get("in-pack", "0")) > 500000
        or int(stats.get("packs", "0")) > 4
        or sum(file.stat().st_size for file in source.rglob("*") if file.is_file())
        > 640 * 1024 * 1024
    ):
        raise Incomplete("failed_range")
    git("symbolic-ref", "HEAD", "refs/heads/review")
    if git("rev-parse", "refs/heads/review").decode().strip() != target["head_sha"]:
        raise Incomplete("failed_range")
    paths = (
        git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            target["base_sha"] + "..." + target["head_sha"],
        )
        .decode()
        .split("\0")
    )
    paths = [path for path in paths if path]
    if len(paths) > 20000:
        raise Incomplete("failed_range")
    return source, paths


def docker(*args):
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    env["BUILDX_CONFIG"] = str(
        Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
        / "codex-review-buildx"
    )
    return ["docker", "--host=unix:///var/run/docker.sock", *args], env


def build_image(directory):
    command, env = docker(
        "build",
        "--quiet",
        "--iidfile",
        str(directory / "image.id"),
        "--secret",
        "id=proxy_ca,src="
        + os.environ.get("CODEX_PROXY_CERT", "/etc/ssl/certs/ca-certificates.crt"),
        str(HERE / "codex-review-runtime"),
    )
    result = subprocess.run(
        command, env=env, cwd=directory, capture_output=True, timeout=600
    )
    if result.returncode:
        raise Incomplete("failed_install")
    image = (directory / "image.id").read_text().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise Incomplete("failed_install")
    return image


def container_args(image, source, broker_directory, target, name, *, profile=None):
    args = [
        "run",
        "--name",
        name,
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--security-opt",
        "seccomp=" + str(HERE / "codex-review-runtime/seccomp.json"),
        "--pids-limit",
        "256",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--tmpfs",
        f"/state:rw,nosuid,nodev,size=1536m,uid={os.getuid()},gid={os.getgid()},mode=700",
        "--mount",
        f"type=bind,src={source},dst=/input/repo,readonly",
        "--mount",
        f"type=bind,src={broker_directory},dst=/broker,readonly",
    ]
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        args += ["--env", key + "="]
    if profile:
        args += ["--security-opt", "apparmor=" + profile]
    args += [image, target["lane"], target["base_sha"], target["head_sha"]]
    return args


def qualify_sandbox(image, source, directory, target):
    """Actual inner executables must launch tools before any auth is fetched."""
    profile = None
    for attempt in range(2):
        name = "codex-probe-" + os.urandom(8).hex()
        args = container_args(image, source, directory, target, name, profile=profile)[
            :-4
        ]
        args += [
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            "set -e; mkdir -p /state/home; git init --bare -q /state/git-probe; git -C /state/git-probe cat-file --batch -Z </dev/null; printf GIT_BATCH_TOOL_OK; /opt/code/node_modules/.bin/codex sandbox -- /bin/sh -c 'printf CODEX_TOOL_OK'; /opt/security/node_modules/.bin/codex sandbox -- /bin/sh -c 'printf SECURITY_TOOL_OK'",
        ]
        command, env = docker(*args)
        try:
            result = subprocess.run(command, env=env, capture_output=True, timeout=45)
        finally:
            cleanup, cleanup_env = docker("rm", "--force", name)
            subprocess.run(cleanup, env=cleanup_env, capture_output=True, timeout=20)
        if (
            result.returncode == 0
            and b"GIT_BATCH_TOOL_OK" in result.stdout
            and b"CODEX_TOOL_OK" in result.stdout
            and b"SECURITY_TOOL_OK" in result.stdout
        ):
            return profile
        # Only a GitHub-hosted ephemeral runner may load this named profile.
        # No sysctl/unconfined/capability fallback or shared-runner mutation.
        if attempt or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            break
        status = subprocess.run(
            [
                "sudo",
                "-n",
                "apparmor_parser",
                "-r",
                "-W",
                str(HERE / "codex-review-runtime/apparmor"),
            ],
            capture_output=True,
            timeout=20,
        )
        if status.returncode:
            break
        profile = "codex-review-container"
    raise Incomplete("failed_install")


def run_container(image, source, directory, target, *, profile=None):
    name = "codex-review-" + os.urandom(8).hex()
    command, env = docker(
        *container_args(image, source, directory, target, name, profile=profile)
    )
    try:
        with subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as process:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            output = bytearray()
            deadline = time.monotonic() + 1150
            try:
                while time.monotonic() < deadline:
                    if selector.select(timeout=0.2):
                        block = os.read(process.stdout.fileno(), 16384)
                        if not block:
                            process.wait(timeout=3)
                            if process.returncode:
                                raise Incomplete("failed_scan")
                            return json.loads(output)
                        output.extend(block)
                        if len(output) > 2 * 1024 * 1024:
                            raise Incomplete("failed_output")
                raise Incomplete("failed_scan")
            finally:
                selector.close()
                cleanup, clean_env = docker("rm", "--force", name)
                subprocess.run(cleanup, env=clean_env, capture_output=True, timeout=20)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Incomplete("failed_scan") from exc


def receipt_for(target, state, reason, image, *, output="", successful_turns=0):
    return {
        "output_sha256": hashlib.sha256(output.encode()).hexdigest()
        if state == "completed"
        else None,
        "successful_provider_turns": successful_turns,
        **target,
        "schema": "codex-trusted-review/v2",
        "state": state,
        "reason": reason,
        "billing_mode": "subscription",
        "auth_boundary": "external_broker",
        "policy_sha256": policy_hash(),
        "tools": TOOLS,
        "image_id": image,
        "request_run_id": int(os.environ["CODEX_REQUEST_RUN_ID"]),
        "producer_run_id": int(os.environ["GITHUB_RUN_ID"]),
        "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
    }


def publish(github, target, receipt, output):
    if not fresh(github, target):
        return
    lane = target["lane"]
    marker = (
        "<!-- codex-pr-review:v1 -->"
        if lane == "code"
        else "<!-- codex-security-diff-scan:v1 -->"
    )
    text = final_code_turn(output) if lane == "code" else output
    # Markdown inside model text cannot terminate this plain-code fence.
    truncated = len(text) > 45000
    text = text.replace("```", "` ` `").replace("<!--", "&lt;!--")[:45000]
    body = (
        marker
        + "\n### Codex "
        + lane
        + " review\n\nRevision `"
        + target["head_sha"]
        + "`\n\nExecution: **"
        + receipt["state"]
        + "** ("
        + receipt["reason"]
        + ").\n\n"
    )
    if receipt["state"] == "completed":
        body += "```text\n" + text + "\n```\n"
    elif receipt["state"] != "exempt":
        body += "No completed review is available for this revision.\n"
    if receipt["state"] == "completed":
        body += (
            ("\nComment shortened. " if truncated else "\n")
            + "Complete review.txt: [trusted run artifacts](https://github.com/"
            + github.repository
            + "/actions/runs/"
            + str(receipt["producer_run_id"])
            + "#artifacts).\n"
        )
    body += "\nFindings are advisory model output. Coverage accepts authenticated worker artifacts, never this comment.\n"
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(body)
    comments = github.pages(f"/issues/{target['pull_request']}/comments")
    prior = next(
        (
            item
            for item in comments
            if (item.get("body") or "").startswith(marker)
            and (item.get("user") or {}).get("login") == "github-actions[bot]"
            and "\nRevision `" + target["head_sha"] + "`" in (item.get("body") or "")
        ),
        None,
    )
    if (not prior or prior.get("body") != body) and fresh(github, target):
        github.json(
            f"/issues/comments/{prior['id']}"
            if prior
            else f"/issues/{target['pull_request']}/comments",
            method="PATCH" if prior else "POST",
            data={"body": body},
        )


def execute(github, target, repository, directory):
    image = "sha256:" + "0" * 64
    output = ""
    try:
        previous = prior_receipt(
            github, target, repository, ignore_run=int(os.environ["GITHUB_RUN_ID"])
        )
        if previous:
            state = (
                previous["state"]
                if previous["state"] in {"completed", "exempt"}
                else "blocked_previous_attempt"
            )
            reason = (
                previous["reason"]
                if state in {"completed", "exempt"}
                else "previous_attempt_incomplete"
            )
            return (
                receipt_for(
                    target,
                    state,
                    reason,
                    previous["image_id"],
                    output=previous.get("_output", ""),
                    successful_turns=previous.get("successful_provider_turns", 0),
                ),
                previous.get("_output", ""),
                True,
            )
        source, paths = git_input(directory, target, github.token)
        if not paths or (target["lane"] == "security" and not security_required(paths)):
            return (
                receipt_for(
                    target, "exempt", "empty" if not paths else "security_exempt", image
                ),
                "",
                True,
            )
        config = json.loads((HERE / "codex-review-worker.json").read_text())
        if config.get("enabled") is not True or not config.get("identity"):
            raise Incomplete("blocked_credentials")
        image = build_image(directory)
        if not fresh(github, target):
            raise Incomplete("superseded_revision")
        socket_directory = directory / "broker"
        socket_directory.mkdir(mode=0o700)
        profile = qualify_sandbox(image, source, socket_directory, target)
        if not fresh(github, target):
            raise Incomplete("superseded_revision")
        subscription = scoped_subscription(config)
        with serve(
            socket_directory / "model.sock",
            subscription,
            max_requests=MAX_REQUESTS[target["lane"]],
        ) as broker:
            result = run_container(
                image, source, socket_directory, target, profile=profile
            )
        if broker.terminal_error:
            raise Incomplete(
                "blocked_quota"
                if broker.terminal_error == "subscription_quota_exhausted"
                else "failed_scan"
            )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("output"), str)
            or not isinstance(result.get("returncode"), int)
        ):
            raise Incomplete("failed_output")
        output = result["output"]
        if (
            result["returncode"] == 0
            and broker.audit()["successful_provider_turns"] < 1
        ):
            raise Incomplete("failed_output")
        if target["lane"] == "code" and result["returncode"] == 0:
            if not broker.last_verdict:
                raise Incomplete("failed_output")
            output = "codex\n" + broker.last_verdict
        # Defense in depth: the model result never becomes an executable command.
        if not subscription.response_safe(output.encode()):
            raise Incomplete("failed_output")
        env = {
            "GITHUB_REPOSITORY": target["repository"],
            "PR_NUM": str(target["pull_request"]),
            "BASE_SHA": target["base_sha"],
            "HEAD_SHA": target["head_sha"],
            "RANGE_STATE": "ready",
            "AUTH_PRESENT": "true",
            "INSTALL_OUTCOME": "success",
            "SCAN_OUTCOME": "success" if result["returncode"] == 0 else "failure",
            "SCAN_RC": str(result["returncode"]),
        }
        result_receipt = build_receipt(target["lane"], env, output)
        return (
            receipt_for(
                target,
                result_receipt["state"],
                result_receipt["reason"],
                image,
                output=output,
                successful_turns=broker.audit()["successful_provider_turns"],
            ),
            output,
            True,
        )
    except (Incomplete, OSError, ValueError, subprocess.SubprocessError) as exc:
        state = str(exc) if isinstance(exc, Incomplete) else "failed_scan"
        if state not in {
            "blocked_credentials",
            "blocked_quota",
            "failed_scan",
            "failed_range",
            "failed_install",
            "failed_output",
        }:
            state = "failed_scan"
        return receipt_for(target, state, state, image), "", True


def rerun_completion(github, request, lane, target):
    jobs = github.pages(
        f"/actions/runs/{int(request['id'])}/jobs?filter=latest", "jobs"
    )
    for job in jobs:
        if (
            job.get("name") == JOBS[lane]
            and job.get("conclusion") == "failure"
            and fresh(github, target)
        ):
            github.json(f"/actions/jobs/{int(job['id'])}/rerun", method="POST")
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=["prepare", "run", "coverage", "recover", "publish"]
    )
    parser.add_argument("--lane", choices=list(JOBS))
    args = parser.parse_args()
    github = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if args.operation == "coverage":
        target = identity(event["pull_request"], github.repository, args.lane)
        if not fresh(github, target):
            raise Incomplete("superseded_revision")
        proof = prior_receipt(github, target, github.json(""))
        if not proof or proof["state"] not in {"completed", "exempt"}:
            raise Incomplete("trusted_review_incomplete")
        print("Authenticated trusted-worker coverage: " + proof["state"])
        return
    if args.operation == "recover":
        recover_worker(github, event)
        return
    target, request, repository = admit(github, event)
    default_ref = "refs/heads/" + repository["default_branch"]
    if (
        os.environ.get("GITHUB_REF") != default_ref
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_run"
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != github.repository + "/" + WORKFLOW + "@" + default_ref
    ):
        raise Incomplete("untrusted_controller_revision")
    if native_proof(github, target):
        rerun_completion(github, request, target["lane"], target)
        print("Exact native proof already satisfies this revision; no model execution.")
        return
    os.environ["CODEX_REQUEST_RUN_ID"] = str(request["id"])
    if args.operation == "publish":
        destination = Path(os.environ["RUNNER_TEMP"]) / "codex-trusted-result"
        receipt = json.loads((destination / "receipt.json").read_text())
        if any(receipt.get(key) != value for key, value in target.items()):
            raise Incomplete("publication_revision_mismatch")
        output = (
            (destination / "review.txt").read_text()
            if receipt["state"] == "completed"
            else ""
        )
        publish(github, target, receipt, output)
        return
    if args.operation == "prepare":
        if os.environ.get("CODEX_REVIEW_ENABLED") == "false" or (
            target["lane"] == "security"
            and os.environ.get("CODEX_SECURITY_ENABLED") == "false"
        ):
            raise Incomplete("operator_disabled")
        config = json.loads((HERE / "codex-review-worker.json").read_text())
        if config.get("enabled") is not True or not config.get("identity"):
            raise Incomplete("blocked_credentials")
        destination = Path(os.environ["RUNNER_TEMP"]) / "codex-trusted-attempt"
        destination.mkdir(mode=0o700, exist_ok=True)
        receipt = receipt_for(
            target, "blocked_previous_attempt", "attempt_reserved", "sha256:" + "0" * 64
        )
        (destination / "receipt.json").write_text(
            json.dumps(receipt, sort_keys=True) + "\n"
        )
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(
                f"artifact=codex-attempt-{target['pull_request']}-{target['lane']}-{target['head_sha']}\nneeded=true\n"
            )
        return
    verify_reservation(github, target, repository)
    if os.environ.get("CODEX_REVIEW_ENABLED") == "false" or (
        target["lane"] == "security"
        and os.environ.get("CODEX_SECURITY_ENABLED") == "false"
    ):
        raise Incomplete("operator_disabled")
    with tempfile.TemporaryDirectory(
        prefix="codex-trusted-", dir=os.environ["RUNNER_TEMP"]
    ) as temporary:
        receipt, output, created = execute(github, target, repository, Path(temporary))
    if created:
        destination = Path(os.environ["RUNNER_TEMP"]) / "codex-trusted-result"
        destination.mkdir(mode=0o700, exist_ok=True)
        (destination / "receipt.json").write_text(
            json.dumps(receipt, sort_keys=True) + "\n"
        )
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(
                f"artifact=codex-trusted-{target['pull_request']}-{target['lane']}-{target['head_sha']}\n"
            )
        if receipt["state"] == "completed":
            (destination / "review.txt").write_text(output, encoding="utf-8")
    print("Trusted review execution: " + receipt["state"])


if __name__ == "__main__":
    try:
        main()
    except (Incomplete, KeyError, ValueError, OSError) as error:
        print(
            "Review incomplete: "
            + (str(error) if isinstance(error, Incomplete) else "controller_failed")
        )
        raise SystemExit(1)
