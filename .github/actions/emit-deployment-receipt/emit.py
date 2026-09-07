#!/usr/bin/env python3
"""Build and schema-validate a final deployment receipt from action inputs."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import jsonschema

RESULTS = frozenset({"success", "failure", "skipped"})
EMIT_KINDS = frozenset({"alias-confirmed", "deploy-succeeded"})
ROLLBACK_MODES = frozenset({
    "alias-promote", "image-revert", "provider-rollback", "manual", "not-applicable",
})


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def optional_url(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def optional_nonnegative_integer(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    if not value.isdecimal():
        raise ValueError("migration_pending must be a non-negative integer")
    return int(value)


def migration_evidence() -> dict | None:
    """Load caller-produced v2 evidence without re-querying the database."""
    value = os.environ.get("INPUT_MIGRATION_EVIDENCE_FILE", "").strip()
    if not value:
        return None
    path = Path(value)
    if path.stat().st_size > 1_000_000:
        raise ValueError("migration_evidence_file exceeds 1 MB")
    evidence = json.loads(path.read_text())
    if not isinstance(evidence, dict):
        raise ValueError("migration_evidence_file must contain a JSON object")
    for index, target in enumerate(evidence.get("targets", [])):
        if not isinstance(target, dict):
            continue
        if target.get("result") == "success" and set(target.get("expected_identifiers", [])) != set(target.get("applied_identifiers", [])):
            raise ValueError(
                f"migration target {index} reports success without identifier parity"
            )
        if target.get("result") == "inspection-unavailable" and target.get("applied_identifiers"):
            raise ValueError(
                f"migration target {index} cannot have applied identifiers when inspection is unavailable"
            )
    return evidence


def build_receipt() -> dict:
    emit_when = required("INPUT_EMIT_WHEN")
    result = required("INPUT_VERIFY_RESULT")
    rollback_mode = required("INPUT_ROLLBACK_MODE")
    target_sha = required("INPUT_TARGET_SHA")
    repo = required("INPUT_REPO")
    github_repo = required("GITHUB_REPOSITORY")
    canonical_url = required("INPUT_CANONICAL_URL")
    if emit_when not in EMIT_KINDS:
        raise ValueError(f"emit_when must be one of {sorted(EMIT_KINDS)}")
    if result not in RESULTS:
        raise ValueError(f"verify_result must be one of {sorted(RESULTS)}")
    if rollback_mode not in ROLLBACK_MODES:
        raise ValueError(f"rollback_mode must be one of {sorted(ROLLBACK_MODES)}")
    if not re.fullmatch(r"[0-9a-f]{40}", target_sha):
        raise ValueError("target_sha must be a full 40-character lowercase hex SHA")
    if repo.lower() != github_repo.lower():
        raise ValueError("repo must match GITHUB_REPOSITORY")
    canonical = urlparse(canonical_url)
    if not canonical.scheme or not canonical.netloc:
        raise ValueError("canonical_url must be an absolute URI")

    rollback = {
        "mode": rollback_mode,
        "previous_deployment_id": os.environ.get(
            "INPUT_PREVIOUS_DEPLOYMENT_ID", ""
        ).strip() or None,
    }
    migration = migration_evidence()
    migration_pending = optional_nonnegative_integer("INPUT_MIGRATION_PENDING")
    if migration is not None and migration_pending is not None:
        raise ValueError("migration_evidence_file and migration_pending are mutually exclusive")
    receipt = {
        "schema_version": 2 if migration is not None else 1,
        "repo": repo,
        "target_sha": target_sha,
        "promoted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "promote_run_url": required("INPUT_PROMOTE_RUN_URL"),
        "gate_run_url": optional_url("INPUT_GATE_RUN_URL"),
        "canonical_url": canonical_url,
        "post_promote_verify": {
            "workflow": required("INPUT_VERIFY_WORKFLOW"),
            "run_url": optional_url("INPUT_VERIFY_RUN_URL"),
            "result": result,
        },
        "rollback": rollback,
    }
    if migration is not None:
        receipt["migration"] = migration
    live_origin = optional_url("INPUT_LIVE_ORIGIN")
    if live_origin is not None:
        receipt["live_origin"] = live_origin
    if migration_pending is not None:
        receipt["migration_pending"] = migration_pending
    return receipt


def validate_receipt(receipt: dict, schema: dict) -> None:
    """Fail with paths and validators only; never echo receipt instance data."""
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    errors = sorted(validator.iter_errors(receipt), key=lambda error: list(error.absolute_path))
    if errors:
        details = ", ".join(
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}:{error.validator}"
            for error in errors
        )
        raise ValueError(f"receipt violates schema at {details}")


def main() -> int:
    try:
        receipt = build_receipt()
        schema_name = (
            "deployment-receipt.v2.schema.json"
            if receipt["schema_version"] == 2
            else "deployment-receipt.schema.json"
        )
        schema = json.loads((Path(__file__).parent / schema_name).read_text())
        validate_receipt(receipt, schema)
        output = Path(os.environ.get("RECEIPT_OUTPUT", "deployment-receipt.json"))
        output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"deployment receipt validates: {output}")
        return 0
    except (ValueError, jsonschema.ValidationError, OSError, json.JSONDecodeError) as exc:
        print(f"::error::deployment receipt emission failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
