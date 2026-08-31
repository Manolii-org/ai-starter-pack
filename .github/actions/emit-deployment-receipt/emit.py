#!/usr/bin/env python3
"""Build and schema-validate a final deployment receipt from action inputs."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

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


def build_receipt() -> dict:
    emit_when = required("INPUT_EMIT_WHEN")
    result = required("INPUT_VERIFY_RESULT")
    rollback_mode = required("INPUT_ROLLBACK_MODE")
    target_sha = required("INPUT_TARGET_SHA")
    repo = required("INPUT_REPO")
    github_repo = required("GITHUB_REPOSITORY")
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

    rollback = {
        "mode": rollback_mode,
        "previous_deployment_id": os.environ.get(
            "INPUT_PREVIOUS_DEPLOYMENT_ID", ""
        ).strip() or None,
    }
    receipt = {
        "schema_version": 1,
        "repo": repo,
        "target_sha": target_sha,
        "promoted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "promote_run_url": required("INPUT_PROMOTE_RUN_URL"),
        "gate_run_url": optional_url("INPUT_GATE_RUN_URL"),
        "canonical_url": required("INPUT_CANONICAL_URL"),
        "post_promote_verify": {
            "workflow": required("INPUT_VERIFY_WORKFLOW"),
            "run_url": optional_url("INPUT_VERIFY_RUN_URL"),
            "result": result,
        },
        "rollback": rollback,
    }
    live_origin = optional_url("INPUT_LIVE_ORIGIN")
    if live_origin is not None:
        receipt["live_origin"] = live_origin
    return receipt


def main() -> int:
    try:
        receipt = build_receipt()
        schema = json.loads(
            (Path(__file__).parent / "deployment-receipt.schema.json").read_text()
        )
        jsonschema.validate(receipt, schema, format_checker=jsonschema.FormatChecker())
        output = Path(os.environ.get("RECEIPT_OUTPUT", "deployment-receipt.json"))
        output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"deployment receipt validates: {output}")
        return 0
    except (ValueError, jsonschema.ValidationError, OSError, json.JSONDecodeError) as exc:
        print(f"::error::deployment receipt emission failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
