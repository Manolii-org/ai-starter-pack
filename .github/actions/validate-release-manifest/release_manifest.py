#!/usr/bin/env python3
"""Validate immutable release proposals. This module grants no execution authority."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
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
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", value
    ):
        raise ManifestError("release times must be RFC 3339 UTC instants")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ManifestError("release times must be RFC 3339 UTC instants") from None
    if result.utcoffset() != timezone.utc.utcoffset(result):
        raise ManifestError("release times must be UTC")
    return result


def validate(manifest: dict, *, expected_release_id: str | None = None) -> dict:
    version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if type(version) is not int or version not in (1, 2):
        raise ManifestError("unsupported release manifest version")
    schema = json.loads(SCHEMA.with_name(f"release-manifest.v{version}.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    if next(validator.iter_errors(manifest), None) is not None:
        # Schema error messages may contain arbitrary caller values or secrets.
        raise ManifestError("release manifest does not match schema")
    subject = manifest["subject"]
    identity = digest(subject)
    if identity != manifest["release_id"]:
        raise ManifestError("release subject digest differs from release_id")
    if expected_release_id is not None and identity != expected_release_id:
        raise ManifestError("release subject differs from trusted expected identity")
    if version == 2:
        _validate_vector(subject)
        return manifest
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


def prepare(subject: dict, *, schema_version: int = 1) -> dict:
    result = {"schema_version": schema_version, "release_id": digest(subject), "subject": copy.deepcopy(subject)}
    return validate(result)


def qualification_subject(subject: dict) -> dict:
    """Exact combined inputs; evidence does not recursively hash itself."""
    return {key: copy.deepcopy(subject[key]) for key in (
        "entity", "environment", "control_sha", "capability_sha", "policy_sha256",
        "config_sha256", "components", "databases", "unchanged_components")}


def validate_native_binding(manifest: dict, *, expected_release_id: str,
                            expected_controller_sha: str, execution_context: dict,
                            now: datetime) -> dict:
    """Revalidate immutable inputs INSIDE a separately qualified native job.

    Context must be supplied by the reviewed owner adapter from the native
    executor/preparation dependency, never candidate JSON. This function cannot
    observe protection settings, credential gating or human review and grants
    no authority. Approval history and its optional timestamp are not inputs.
    """
    if (not isinstance(expected_release_id, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_release_id)
            or not isinstance(expected_controller_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_controller_sha)):
        raise ManifestError("native binding requires independent exact expected identities")
    validate(manifest, expected_release_id=expected_release_id)
    subject = manifest["subject"]
    if subject["control_sha"] != expected_controller_sha:
        raise ManifestError("controller differs from reviewed expected revision")
    expected = {"repo": subject["repo"], "environment": subject["environment"],
                "controller_sha": expected_controller_sha, "release_id": expected_release_id,
                "preparation_run_id": subject["preparation"]["run_id"],
                "preparation_run_attempt": subject["preparation"]["run_attempt"]}
    if manifest["schema_version"] == 2:
        if subject["execution"]["workflow_sha"] != expected_controller_sha:
            raise ManifestError("protected execution workflow differs from reviewed controller")
        expected.update(execution_run_id=subject["execution"]["run_id"],
                        execution_run_attempt=subject["execution"]["run_attempt"],
                        execution_job=subject["execution"]["job"])
    if canonical_bytes(execution_context) != canonical_bytes(expected):
        raise ManifestError("native executor binding differs from prepared subject")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ManifestError("execution clock must be a timezone-aware instant")
    if not _instant(subject["timing"]["selected_at"]) <= now < _instant(subject["timing"]["execute_before"]):
        raise ManifestError("execution is outside fixed release window")
    return manifest


def _validate_vector(subject: dict) -> None:
    components = subject["components"]
    databases = subject["databases"]
    ids = [item["id"] for item in components]
    db_ids = [item["id"] for item in databases]
    unchanged = [item["id"] for item in subject["unchanged_components"]]
    targets = [item["target"] for item in components]
    for values in (ids, db_ids, unchanged, targets):
        if len(values) != len(set(values)):
            raise ManifestError("duplicate release vector identity")
    if set(ids) & set(unchanged):
        raise ManifestError("selected component also declared unchanged")
    # Same organization is a conservative portable floor. The trusted owner
    # adapter must enforce its compiled entity/target allowlist as well.
    owner = subject["repo"].split("/")[0].casefold()
    sources = [item["source"] for item in components + databases + subject["unchanged_components"]]
    if any(source["repo"].split("/")[0].casefold() != owner for source in sources):
        raise ManifestError("release source crosses owning organization")
    if {db for item in components for db in item["databases"]} != set(db_ids):
        raise ManifestError("component database dependencies differ from declared graphs")
    visited = set(unchanged)
    for component in components:
        if (component["recovery_artifact_id"] is None) != (component["recovery_artifact_sha256"] is None):
            raise ManifestError("recovery artifact identity and digest must be paired")
        features = component["feature_states"]
        if component["features_sha256"] != digest(features):
            raise ManifestError("feature state differs from component feature identity")
        feature_ids = [feature["id"] for feature in features]
        if len(feature_ids) != len(set(feature_ids)):
            raise ManifestError("duplicate component feature identity")
        if any(feature["readiness"] == "development" and feature["default_enabled"] for feature in features):
            raise ManifestError("development feature cannot default enabled")
        if not set(component["depends_on"]) <= visited:
            raise ManifestError("component order contains unknown or forward dependency")
        visited.add(component["id"])
    for db in databases:
        baseline, pending, after = (set(db[key]) for key in ("baseline", "pending", "expected_after"))
        if baseline & pending or after != baseline | pending:
            raise ManifestError("independent migration ledger sets disagree")
        consumers = {c["id"] for c in components if db["id"] in c["databases"]}
        if not consumers <= set(db["compatible_components"]) <= set(ids + unchanged):
            raise ManifestError("migration compatibility omits consumer or names unknown component")
    timing = subject["timing"]
    if not (_instant(timing["selected_at"]) < _instant(timing["approval_deadline"])
            < _instant(timing["execute_before"])):
        raise ManifestError("release deadlines are not strictly ordered")
    tested = digest(qualification_subject(subject))
    for evidence in subject["qualifications"]:
        if evidence["tested_subject_sha256"] != tested:
            raise ManifestError("qualification differs from selected combined runtime")
        if not (_instant(evidence["completed_at"]) <= _instant(timing["selected_at"])
                < _instant(timing["execute_before"]) <= _instant(evidence["retain_until"])):
            raise ManifestError("qualification is unavailable for release window")
        if evidence["first_attempt"] == "failure" and evidence["retry_count"] == 0:
            raise ManifestError("recovered qualification lacks retry outcome")
    if subject["kind"] == "activation-only" and any(db["pending"] for db in databases):
        raise ManifestError("activation-only cannot contain pending SQL")


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
    if parent["schema_version"] != 1:
        raise ManifestError("v2 continuation requires fresh combined-runtime qualification")
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
    continuation_decision = ["release"] if subject["decision_sequence"] == ["release"] else ["activation"]
    subject.update(kind="activation-only", decision_sequence=continuation_decision,
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
