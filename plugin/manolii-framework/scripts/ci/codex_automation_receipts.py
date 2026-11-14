"""Emit `automation.inference/v2` receipts for subscription broker completions.

The broker records one nonsecret receipt per validated text completion
(provider-reported model and usage, actual admitted input size, observation
time). The trusted controller joins those with its own native coordinates.
Nothing here grants execution, consent, funding or publication authority, and
the cost stays unknown (`None`): a subscription completion is never zero cost.

The contract mirrors the owning reader (`scripts/lib/inference_measurement.py`
in the orchestrating repository, `validate_receipt_version`). Keep both in step.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

SCHEMA = "automation.inference/v2"
FIELDS = frozenset({
    "schema", "observed_at", "status", "transport", "subscription_eligible",
    "model_verification", "cost_usd", "fallback_reason", "runtime_provider",
    "billing_mode", "model", "role", "data_sensitivity", "input_chars", "usage",
    "repository", "run_id", "run_attempt", "base_sha", "head_sha", "workflow_sha",
    "correlation_id", "logical_request_id", "attempt_id",
})
ROLES = frozenset({"assessment", "triage", "critic", "judge"})
SENSITIVITY = frozenset({"public", "internal"})
ELIGIBLE_MAX_INPUT_CHARS = 180000
SHA = re.compile(r"[a-f0-9]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ReceiptError(ValueError):
    """A completion cannot become a measurement receipt; never guess a field."""


def _identity(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ReceiptError("invalid receipt identity")
    return value


def _timestamp(value):
    if not isinstance(value, str):
        raise ReceiptError("timestamp must be an explicit UTC string")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReceiptError("invalid timestamp") from None
    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ReceiptError("timestamp must use UTC")


def validate(row: dict) -> dict:
    """Refuse anything the owning reader would refuse."""
    if not isinstance(row, dict) or set(row) != FIELDS or row.get("schema") != SCHEMA:
        raise ReceiptError("broker receipt fields must be complete and nonsecret")
    if (row["transport"] != "subscription_broker" or row["runtime_provider"] != "codex"
            or row["billing_mode"] != "subscription"
            or row["model_verification"] != "provider_reported"
            or row["cost_usd"] is not None or row["fallback_reason"] is not None
            or row["status"] not in ("completed", "truncated")
            or type(row["subscription_eligible"]) is not bool
            or row["role"] not in ROLES or row["data_sensitivity"] not in SENSITIVITY):
        raise ReceiptError("invalid broker receipt contract")
    _timestamp(row["observed_at"])
    for name in ("model", "correlation_id", "logical_request_id", "attempt_id"):
        _identity(row[name])
    if (not isinstance(row["repository"], str) or not REPOSITORY.fullmatch(row["repository"])
            or any(type(row[n]) is not int or row[n] <= 0 for n in ("run_id", "run_attempt"))
            or any(not isinstance(row[n], str) or not SHA.fullmatch(row[n])
                   for n in ("base_sha", "head_sha", "workflow_sha"))):
        raise ReceiptError("invalid broker native attribution")
    size = row["input_chars"]
    if size is not None and (type(size) is not int or size < 0):
        raise ReceiptError("invalid broker input size")
    if row["subscription_eligible"] and (size is None or size > ELIGIBLE_MAX_INPUT_CHARS):
        raise ReceiptError("broker eligibility requires actual bounded input size")
    usage = row["usage"]
    if (not isinstance(usage, dict)
            or set(usage) != {"input_tokens", "output_tokens", "cached_input_tokens"}
            or any(type(v) is not int or v < 0 for v in usage.values())
            or usage["cached_input_tokens"] > usage["input_tokens"]):
        raise ReceiptError("invalid broker usage")
    return row


def build(completions, *, repository, run_id, run_attempt, base_sha, head_sha,
          workflow_sha, role, data_sensitivity, correlation_id) -> list[dict]:
    """One receipt per validated broker completion, in broker order.

    `logical_request_id` and `attempt_id` are derived from the trusted native
    run/attempt and the broker's completion order, so a rerun attempt is a
    distinct physical attempt and a duplicate delivery is detectable.
    """
    if not isinstance(completions, list):
        raise ReceiptError("broker completions unavailable")
    rows = []
    for index, completion in enumerate(completions, 1):
        if not isinstance(completion, dict) or completion.get("model_verification") != "provider_reported":
            raise ReceiptError("unverified broker completion")
        size = completion.get("input_chars")
        row = {
            "schema": SCHEMA,
            "observed_at": completion.get("observed_at"),
            "status": "completed",
            "transport": "subscription_broker",
            "subscription_eligible": type(size) is int and 0 <= size <= ELIGIBLE_MAX_INPUT_CHARS,
            "model_verification": "provider_reported",
            "cost_usd": None,
            "fallback_reason": None,
            "runtime_provider": "codex",
            "billing_mode": "subscription",
            "model": completion.get("model"),
            "role": role,
            "data_sensitivity": data_sensitivity,
            "input_chars": size,
            "usage": completion.get("usage"),
            "repository": repository,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "workflow_sha": workflow_sha,
            "correlation_id": correlation_id,
            "logical_request_id": f"{correlation_id}:{index}",
            "attempt_id": f"{run_id}.{run_attempt}:{index}",
        }
        rows.append(validate(row))
    return rows


def dumps(rows) -> str:
    """JSONL with sorted keys; no prompt, response, credential or path data."""
    return "".join(json.dumps(validate(row), sort_keys=True) + "\n" for row in rows)
