"""Durable append-only execution custody before model or publisher work.

An expiring artifact is not an execution lock. Native commit-status rows survive
artifact expiry, source-policy changes, cancellation and missing acknowledgements.
The default-source controller MUST serialize the check-and-claim transaction per
repository/PR/operation, then retain the canonical account lock during execution.
This helper is not a distributed CAS or a lease and never expires a claim.
"""

from __future__ import annotations

import re

from codex_review_profiles import AUTOMATION_ROLES

WORKFLOW = ".github/workflows/codex-automation-worker.yml"
SHA = re.compile(r"[a-f0-9]{40}\Z")
BOT_ID = 41898282


class CustodyHold(ValueError):
    """A fixed category; missing or unknown state never permits replay."""


def _integer(value):
    return type(value) is int and value > 0


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _identity(github, target):
    if (
        not isinstance(target, dict)
        or target.get("repository") != github.repository
        or not _integer(target.get("pull_request"))
        or not isinstance(target.get("operation"), str)
        or target["operation"] not in AUTOMATION_ROLES
        or not isinstance(target.get("head_sha"), str)
        or not SHA.fullmatch(target["head_sha"])
    ):
        raise CustodyHold("execution_identity_invalid")
    return (
        f"codex-automation-execution/{target['operation']}/pr-{target['pull_request']}"
    )


def _controller(github, producer_run_id, workflow_sha):
    if (
        not _integer(producer_run_id)
        or not isinstance(workflow_sha, str)
        or not SHA.fullmatch(workflow_sha)
    ):
        raise CustodyHold("execution_controller_invalid")
    repository = github.json("")
    run = github.json(f"/actions/runs/{producer_run_id}")
    workflow = github.json("/actions/workflows/codex-automation-worker.yml")
    repo_id = _mapping(repository).get("id")
    if (
        not _integer(repo_id)
        or _mapping(repository).get("full_name") != github.repository
        or not isinstance(run, dict)
        or not isinstance(workflow, dict)
        or type(run.get("id")) is not int
        or run["id"] != producer_run_id
        or not _integer(workflow.get("id"))
        or type(run.get("workflow_id")) is not int
        or run["workflow_id"] != workflow["id"]
        or run.get("path") != WORKFLOW
        or workflow.get("path") != WORKFLOW
        or run.get("event") != "workflow_run"
        or run.get("head_sha") != workflow_sha
        or run.get("status") != "in_progress"
        or run.get("conclusion") is not None
    ):
        raise CustodyHold("execution_controller_refused")
    for field in ("repository", "head_repository"):
        value = _mapping(run.get(field)).get("id")
        if type(value) is not int or value != repo_id:
            raise CustodyHold("execution_controller_refused")
    return f"https://github.com/{github.repository}/actions/runs/{producer_run_id}"


def claim(github, target, *, producer_run_id, workflow_sha, enabled: bool):
    """Check all native history, then append/verify one pending claim.

    Any prior row for this operation/head holds automatic re-execution, even a
    purported success, a different policy/base, or a row from another writer.
    Reusing a completed result requires separate authenticated result proof;
    this function never turns a status, expiry or transport rollback into that
    proof. It must return an acknowledged ID before model work may begin.
    """
    context = _identity(github, target)
    if type(enabled) is not bool:
        raise CustodyHold("execution_mode_invalid")
    rows = github.pages(f"/commits/{target['head_sha']}/statuses")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("context"), str)
        for row in rows
    ):
        raise CustodyHold("execution_history_unreadable")
    if any(row.get("context") == context for row in rows):
        raise CustodyHold("execution_already_claimed")
    if not enabled:
        return None
    target_url = _controller(github, producer_run_id, workflow_sha)
    payload = {
        "context": context,
        "state": "pending",
        "target_url": target_url,
        "description": "Execution claimed; outcome requires reconciliation.",
    }
    result = github.json(f"/statuses/{target['head_sha']}", method="POST", data=payload)
    if not isinstance(result, dict) or not _integer(result.get("id")):
        raise CustodyHold("execution_claim_ack_unknown")
    identity = result["id"]
    # Read back the durable row. A lost response or unreadable follow-up holds;
    # no in-memory flag or expiring artifact substitutes for native custody.
    recorded = github.pages(f"/commits/{target['head_sha']}/statuses")
    if not isinstance(recorded, list):
        raise CustodyHold("execution_claim_ack_unknown")
    relevant = [
        row
        for row in recorded
        if isinstance(row, dict) and row.get("context") == context
    ]
    found = [
        row
        for row in recorded
        if isinstance(row, dict)
        and type(row.get("id")) is int
        and row["id"] == identity
    ]
    if (
        len(found) != 1
        or len(relevant) != 1
        or any(found[0].get(k) != v for k, v in payload.items())
        or _mapping(found[0].get("creator")).get("id") != BOT_ID
    ):
        raise CustodyHold("execution_claim_ack_unknown")
    return {
        "status_id": identity,
        "context": context,
        "head_sha": target["head_sha"],
        "producer_run_id": producer_run_id,
        "workflow_sha": workflow_sha,
    }
