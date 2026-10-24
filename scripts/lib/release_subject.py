"""Portable exact-candidate identities. These values never confer authority.

The owning trusted controller must authenticate approvals and confine every
production writer. A matching local JSON value is never production approval.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

SCHEMA = "agent-release-subject/v1"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}")


def _require(value, message):
    if not value:
        raise ValueError(message)


def _fields(value, required, name):
    _require(isinstance(value, dict) and set(value) == set(required), f"invalid {name} fields")


def _identifier(value, name):
    _require(isinstance(value, str) and IDENTIFIER.fullmatch(value), f"invalid {name}")


def _digest(value, name):
    _require(isinstance(value, str) and DIGEST.fullmatch(value), f"invalid {name} digest")


def _migrations(value):
    _require(isinstance(value, list) and len(value) <= 1000, "invalid migration set")
    ids = set()
    for row in value:
        _fields(row, {"id", "digest"}, "migration")
        _identifier(row["id"], "migration id")
        _digest(row["digest"], "migration")
        _require(row["id"] not in ids, "duplicate migration id")
        ids.add(row["id"])


def validate(subject):
    """Validate a complete subject including verification and exact recovery."""
    _fields(subject, {"schema", "repository", "target", "source_sha", "artifact_digest",
                      "migrations", "compatibility_order", "verification", "recovery",
                      "policy_sha", "workflow_sha"}, "release subject")
    _require(subject["schema"] == SCHEMA, "unsupported release subject")
    _require(isinstance(subject["repository"], str)
             and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", subject["repository"]), "invalid repository")
    _identifier(subject["target"], "target")
    for field in ("source_sha", "policy_sha", "workflow_sha"):
        _require(isinstance(subject[field], str) and SHA.fullmatch(subject[field]), f"invalid {field}")
    _digest(subject["artifact_digest"], "artifact")
    _migrations(subject["migrations"])
    order = subject["compatibility_order"]
    _require(order in (["migrations", "artifact", "verification"],
                       ["artifact", "migrations", "verification"]), "invalid compatibility order")
    verification = subject["verification"]
    _require(isinstance(verification, list) and 1 <= len(verification) <= 100, "verification must be declared")
    for check in verification:
        _identifier(check, "verification check")
    _require(len(set(verification)) == len(verification), "duplicate verification check")
    recovery = subject["recovery"]
    _fields(recovery, {"action", "artifact_digest", "migrations", "verification"}, "recovery")
    _require(recovery["action"] in {"none", "restore-artifact", "roll-forward"}, "invalid recovery action")
    _migrations(recovery["migrations"])
    _require(isinstance(recovery["verification"], list), "invalid recovery verification")
    if recovery["action"] == "none":
        _require(recovery["artifact_digest"] is None and recovery["migrations"] == []
                 and recovery["verification"] == [], "undeclared recovery cannot authorize effects")
    else:
        _digest(recovery["artifact_digest"], "recovery artifact")
        _require(1 <= len(recovery["verification"]) <= 100, "recovery verification must be declared")
        for check in recovery["verification"]:
            _identifier(check, "recovery verification check")
        _require(len(set(recovery["verification"])) == len(recovery["verification"]), "duplicate recovery verification")
    return subject


def release_id(subject):
    """Hash complete canonical values; preserve migration/compatibility order."""
    validate(subject)
    encoded = json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _time(value):
    _require(isinstance(value, str), "invalid approval timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None, "approval timestamp needs timezone")
    return parsed.astimezone(timezone.utc)


def compare_approval(subject, approval, *, now):
    """Compare scope only; caller must supply freshly authenticated native proof.

    All decisions return native_authority_required=True. The module deliberately
    has no API that grants execution from a file, actor string or dispatch input.
    """
    identity = release_id(subject)
    _fields(approval, {"instance_id", "release_id", "decision", "actor", "issued_at", "expires_at"}, "approval")
    _identifier(approval["instance_id"], "approval instance")
    _identifier(approval["actor"], "approval actor")
    _digest(approval["release_id"], "approved release")
    _require(approval["decision"] in {"approved", "rejected", "cancelled"}, "invalid approval decision")
    _require(isinstance(now, datetime) and now.tzinfo is not None, "current time needs timezone")
    issued, expires = _time(approval["issued_at"]), _time(approval["expires_at"])
    _require(expires > issued, "invalid approval lifetime")
    reason = ("decision-held" if approval["decision"] != "approved"
              else "candidate-changed" if approval["release_id"] != identity
              else "not-yet-valid" if now < issued
              else "expired" if now >= expires else "scope-matches")
    return {"status": reason, "release_id": identity, "approval_instance": approval["instance_id"],
            "native_authority_required": True, "execution_authorized": False}


def stage_key(subject, approval_instance, stage):
    """Stable stage identity for the owner's existing durable idempotency store."""
    _identifier(approval_instance, "approval instance")
    _require(stage in {"migrations", "artifact", "verification", "recovery", "recovery-verification"}, "invalid stage")
    return hashlib.sha256(json.dumps([release_id(subject), approval_instance, stage], separators=(",", ":")).encode()).hexdigest()
