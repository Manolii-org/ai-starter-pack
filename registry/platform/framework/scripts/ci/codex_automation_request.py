"""Authenticate an inert automation request before any account/model admission.

The caller is the reviewed default-branch controller. Producer artifacts and PR
data are untrusted; an artifact cannot choose an operation, credential, source
revision, endpoint or executable. This module performs bounded read-only GitHub
operations through the existing trusted GitHub client and never starts work.
"""

from __future__ import annotations

import io
import json
import re
import stat
import zipfile

WORKFLOW_PATH = ".github/workflows/codex-automation-request.yml"
SCHEMA = "codex-automation-request/v1"
SHA = re.compile(r"[a-f0-9]{40}\Z")
MAX_ARCHIVE = 2 * 1024 * 1024
MAX_REQUEST = 16384
MAX_ARTIFACTS = 32
HARD_HOLDS = frozenset({"human-merge", "security-remediation", "skip-ai-review"})


class RequestRefused(ValueError):
    """Only a fixed reason reaches logs; never the request, PR body or API data."""


def _integer(value):
    return type(value) is int and value > 0


def _id_matches(value, expected):
    return type(value) is int and value == expected


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _same_repo(value, expected):
    return isinstance(value, str) and value.casefold() == expected.casefold()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestRefused("request_json_ambiguous")
        result[key] = value
    return result


def _constant(_value):
    raise RequestRefused("request_json_invalid")


def read_request(archive: bytes) -> dict:
    """One small regular JSON file; no extraction, nested paths or executables."""
    if not isinstance(archive, bytes) or not 0 < len(archive) <= MAX_ARCHIVE:
        raise RequestRefused("request_archive_budget")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            if len(entries) != 1:
                raise RequestRefused("request_archive_shape")
            entry = entries[0]
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if (
                entry.filename != "request.json"
                or entry.is_dir()
                or entry.flag_bits & 1
                or kind not in {0, stat.S_IFREG}
                or not 0 < entry.file_size <= MAX_REQUEST
            ):
                raise RequestRefused("request_archive_shape")
            raw = bundle.read(entry)
            if len(raw) != entry.file_size:
                raise RequestRefused("request_archive_shape")
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
    except (
        OSError,
        ValueError,
        RuntimeError,
        RecursionError,
        zipfile.BadZipFile,
        NotImplementedError,
    ) as exc:
        if isinstance(exc, RequestRefused):
            raise
        raise RequestRefused("request_archive_invalid") from None
    if not isinstance(value, dict):
        raise RequestRefused("request_json_invalid")
    return value


def admit(
    github, event: dict, *, allowed_bases: tuple[str, ...], hold_labels: tuple[str, ...]
) -> dict:
    """Bind one assessment request to actual API identities and full current SHAs.

    ``allowed_bases`` and ``hold_labels`` come from the trusted controller's
    reviewed policy, never the producer artifact or PR branch. Funding consent,
    sensitivity, deduplication, custody and execution admission remain separate.
    """
    if (
        not isinstance(allowed_bases, tuple)
        or not allowed_bases
        or not isinstance(hold_labels, tuple)
        or any(not isinstance(v, str) or not v for v in (*allowed_bases, *hold_labels))
    ):
        raise RequestRefused("controller_policy_invalid")
    incoming = event.get("workflow_run") if isinstance(event, dict) else None
    if not isinstance(incoming, dict) or not _integer(incoming.get("id")):
        raise RequestRefused("request_event_invalid")
    run_id = incoming["id"]
    repository = github.json("")
    run = github.json(f"/actions/runs/{run_id}")
    workflow = github.json("/actions/workflows/" + WORKFLOW_PATH.rsplit("/", 1)[1])
    if (
        not isinstance(repository, dict)
        or not _integer(repository.get("id"))
        or not _same_repo(repository.get("full_name"), github.repository)
    ):
        raise RequestRefused("request_repository_invalid")
    repo_id = repository["id"]
    if (
        not isinstance(run, dict)
        or not isinstance(workflow, dict)
        or not _id_matches(run.get("id"), run_id)
        or run.get("path") != WORKFLOW_PATH
        or workflow.get("path") != WORKFLOW_PATH
        or not _integer(workflow.get("id"))
        or not _id_matches(run.get("workflow_id"), workflow["id"])
        or run.get("event") != "pull_request"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not _integer(run.get("run_attempt"))
        or not _id_matches(_mapping(run.get("repository")).get("id"), repo_id)
        or not _id_matches(_mapping(run.get("head_repository")).get("id"), repo_id)
    ):
        raise RequestRefused("request_producer_refused")
    pulls = run.get("pull_requests")
    if not isinstance(pulls, list) or len(pulls) != 1 or not isinstance(pulls[0], dict):
        raise RequestRefused("request_pull_association_missing")
    number = pulls[0].get("number")
    if not _integer(number):
        raise RequestRefused("request_pull_association_missing")
    pr = github.json(f"/pulls/{number}")
    if (
        not isinstance(pr, dict)
        or not _id_matches(pr.get("number"), number)
        or pr.get("state") != "open"
        or pr.get("draft") is not False
    ):
        raise RequestRefused("request_pull_not_active")
    for side in ("base", "head"):
        value = pr.get(side)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("sha"), str)
            or not SHA.fullmatch(value["sha"])
            or not _id_matches(_mapping(value.get("repo")).get("id"), repo_id)
            or not _same_repo(
                _mapping(value.get("repo")).get("full_name"), github.repository
            )
        ):
            raise RequestRefused("request_fork_or_revision_refused")
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    if pr["base"].get("ref") not in allowed_bases:
        raise RequestRefused("request_base_not_admitted")
    if run.get("head_sha") != head or _mapping(pulls[0].get("head")).get("sha") != head:
        raise RequestRefused("request_superseded_revision")
    labels = pr.get("labels")
    if not isinstance(labels, list) or any(
        not isinstance(v, dict) or not isinstance(v.get("name"), str) for v in labels
    ):
        raise RequestRefused("request_hold_state_unreadable")
    if (HARD_HOLDS | {v.casefold() for v in hold_labels}) & {
        v["name"].casefold() for v in labels
    }:
        raise RequestRefused("request_held")
    head_ref = pr["head"].get("ref")
    if (
        not isinstance(head_ref, str)
        or not 0 < len(head_ref) <= 256
        or head_ref.startswith("-")
        or any(ord(c) < 32 for c in head_ref)
    ):
        raise RequestRefused("request_branch_invalid")
    listing = github.json(f"/actions/runs/{run_id}/artifacts?per_page=100")
    if not isinstance(listing, dict):
        raise RequestRefused("request_artifact_list_invalid")
    artifacts, count = listing.get("artifacts"), listing.get("total_count")
    if (
        type(count) is not int
        or not 0 <= count <= MAX_ARTIFACTS
        or not isinstance(artifacts, list)
        or len(artifacts) != count
        or any(not isinstance(a, dict) for a in artifacts)
    ):
        raise RequestRefused("request_artifact_list_incomplete")
    name = f"codex-automation-request-{number}-{head}"
    matches = [a for a in artifacts if a.get("name") == name]
    if len(matches) != 1:
        raise RequestRefused("request_artifact_ambiguous")
    artifact = matches[0]
    owner = _mapping(artifact.get("workflow_run"))
    if (
        not _integer(artifact.get("id"))
        or artifact.get("expired") is not False
        or type(artifact.get("size_in_bytes")) is not int
        or not 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE
        or not _id_matches(owner.get("id"), run_id)
        or not _id_matches(owner.get("repository_id"), repo_id)
        or not _id_matches(owner.get("head_repository_id"), repo_id)
        or owner.get("head_sha") != head
    ):
        raise RequestRefused("request_artifact_owner_refused")
    value = read_request(github.archive(artifact["id"]))
    expected = {
        "schema": SCHEMA,
        "repository": github.repository,
        "pull_request": number,
        "base_sha": base,
        "head_sha": head,
        "operation": "assessment",
        "request_run_id": run_id,
        "request_run_attempt": run["run_attempt"],
    }
    if value != expected or any(
        type(value.get(k)) is not int
        for k in ("pull_request", "request_run_id", "request_run_attempt")
    ):
        raise RequestRefused("request_payload_not_current")
    return {
        **expected,
        "request_workflow_id": workflow["id"],
        "request_artifact_id": artifact["id"],
        "head_ref": head_ref,
    }
