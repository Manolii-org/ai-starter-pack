#!/usr/bin/env python3
"""Validate immutable release proposals. This module grants no execution authority."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

import jsonschema

SCHEMA = Path(__file__).resolve().parents[3] / "schemas/release-manifest.v1.schema.json"
MAX_BYTES = 1_000_000


class ManifestError(ValueError):
    """A release proposal cannot be used; messages never echo input values."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ManifestError("non-finite JSON number")


def load_json(path: Path) -> dict:
    if path.stat().st_size > MAX_BYTES:
        raise ManifestError("release input exceeds 1 MB")
    with path.open(encoding="utf-8") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data.encode("utf-8")) > MAX_BYTES:
        raise ManifestError("release input exceeds 1 MB")
    try:
        result = json.loads(data, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ManifestError("invalid release JSON") from exc
    if not isinstance(result, dict):
        raise ManifestError("release input must be a JSON object")
    return result


def canonical_bytes(value: dict) -> bytes:
    """v1 identity: sorted keys, compact JSON, literal Unicode, UTF-8, no NaN."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ManifestError("subject cannot be canonically encoded") from exc


def digest(value: dict) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() != timezone.utc.utcoffset(result):
        raise ManifestError("release times must be UTC")
    return result


def validate(manifest: dict, *, expected_release_id: str | None = None) -> dict:
    schema = json.loads(SCHEMA.read_text())
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    if next(validator.iter_errors(manifest), None) is not None:
        # Schema error messages may contain arbitrary caller values or secrets.
        raise ManifestError("release manifest does not match schema v1")
    subject = manifest["subject"]
    identity = digest(subject)
    if identity != manifest["release_id"]:
        raise ManifestError("release subject digest differs from release_id")
    if expected_release_id is not None and identity != expected_release_id:
        raise ManifestError("release subject differs from trusted expected identity")
    qualification = subject["qualification"]
    if qualification["source_sha"] != subject["source_sha"]:
        raise ManifestError("qualification source differs from candidate")
    artifacts = subject["artifacts"]
    databases = subject["databases"]
    targets = [item["target"] for item in artifacts]
    database_ids = [item["id"] for item in databases]
    if len(set(targets)) != len(targets) or len(set(database_ids)) != len(database_ids):
        raise ManifestError("duplicate release destination")
    referenced = {db for item in artifacts for db in item["databases"]}
    if referenced != set(database_ids):
        raise ManifestError("artifact database dependencies differ from declared targets")
    for db in databases:
        baseline, pending, after = (set(db[key]) for key in ("baseline", "pending", "expected_after"))
        if baseline & pending or after != baseline | pending:
            raise ManifestError("migration baseline, pending and expected after-state disagree")
    if databases:
        if qualification["schema_source_sha"] != subject["source_sha"]:
            raise ManifestError("qualification schema source differs from candidate")
        if not qualification["migration_sha256"] or not qualification["fixture_version"]:
            raise ManifestError("database qualification lacks schema or fixture identity")
        if any(db["migration_sha256"] != qualification["migration_sha256"] for db in databases):
            raise ManifestError("v1 qualification requires the same tested migration graph for every database")
    timing = subject["timing"]
    if not (_instant(timing["selected_at"]) < _instant(timing["approval_deadline"])
            < _instant(timing["execute_before"])):
        raise ManifestError("release deadlines are not strictly ordered")
    continuation = subject["continuation"]
    if subject["kind"] == "activation-only":
        if continuation is None or any(db["pending"] for db in databases):
            raise ManifestError("activation-only requires linked schema evidence and empty SQL")
        if subject["decision_sequence"] not in (["activation"], ["release"]):
            raise ManifestError("activation-only requires a new protected decision")
    elif continuation is not None:
        raise ManifestError("normal release cannot claim continuation evidence")
    return manifest


def prepare(subject: dict) -> dict:
    result = {"schema_version": 1, "release_id": digest(subject), "subject": copy.deepcopy(subject)}
    return validate(result)


def activation_only(parent: dict, *, schema_evidence: dict, current: dict,
                    timing: dict, preparation: dict, expected_evidence_sha256: str,
                    admission_revision: str, admitted_continuation: str | None) -> dict:
    """Prepare a NEW proposal; caller must CAS its admission and obtain approval.

    Evidence/current state must come from the owning trusted observer, never a
    user assertion. Identity validation is not provenance attestation. The
    admission revision is returned for a real shared-store compare-and-swap;
    producing this proposal neither reserves it nor authorizes activation.
    """
    validate(parent)
    if not admission_revision or admitted_continuation is not None:
        raise ManifestError("continuation admission revision absent or predecessor already claimed")
    if digest(schema_evidence) != expected_evidence_sha256:
        raise ManifestError("schema evidence differs from trusted retained digest")
    if (schema_evidence.get("release_id") != parent["release_id"]
            or schema_evidence.get("stage") != "schema"
            or schema_evidence.get("status") != "success"):
        raise ManifestError("predecessor lacks successful schema evidence")
    try:
        completed = _instant(schema_evidence["completed_at"])
        limit = completed + timedelta(seconds=parent["subject"]["timing"]["intermediate_seconds"])
        if not (completed <= _instant(timing["selected_at"])
                < _instant(timing["execute_before"]) <= limit):
            raise ManifestError("continuation exceeds the approved intermediate window")
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise ManifestError("continuation timing or schema completion evidence is invalid") from exc
    if parent["subject"]["kind"] != "release":
        raise ManifestError("continuation cannot chain to another continuation")
    subject = copy.deepcopy(parent["subject"])
    if current.get("policy_sha256") != subject["policy_sha256"]:
        raise ManifestError("current policy differs; an owner policy decision is required")
    expected_artifacts = {a["target"]: a["before_artifact_id"] for a in subject["artifacts"]}
    if current.get("artifacts") != expected_artifacts:
        raise ManifestError("current live artifact differs from predecessor before-state")
    expected_databases = {db["id"]: db["expected_after"] for db in subject["databases"]}
    if (schema_evidence.get("databases") != expected_databases
            or current.get("databases") != expected_databases):
        raise ManifestError("schema evidence/current ledger differs from approved after-state")
    for db in subject["databases"]:
        db["baseline"] = list(db["expected_after"])
        db["pending"] = []
    subject.update(kind="activation-only", decision_sequence=["activation"],
                   timing=copy.deepcopy(timing), preparation=copy.deepcopy(preparation),
                   continuation={"parent_release_id": parent["release_id"],
                                 "schema_evidence_sha256": expected_evidence_sha256})
    return {"manifest": prepare(subject), "expected_admission_revision": admission_revision,
            "continuation_key": parent["release_id"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-release-id")
    parser.add_argument("--dry-run", action="store_true", help="Do not append GitHub step outputs")
    args = parser.parse_args(argv)
    try:
        manifest = validate(load_json(args.manifest), expected_release_id=args.expected_release_id)
        print("release_id=" + manifest["release_id"])
        print("authority=none; manifest validation is not approval or live-state proof")
        if not args.dry_run and os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
                stream.write("release_id=" + manifest["release_id"] + "\n")
        return 0
    except (ManifestError, OSError, UnicodeError) as exc:
        # Do not print paths, raw input, provider bodies or credential values.
        print("RELEASE_MANIFEST_INVALID: " + (str(exc) if isinstance(exc, ManifestError)
                                            else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
