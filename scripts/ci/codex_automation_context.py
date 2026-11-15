"""Bounded inert guest metadata; never host authentication or execution proof."""

import json
import os
from pathlib import Path
import re
import stat

CONTEXT = Path("/input/codex-automation.json")
SCHEMA = "codex-automation-context/v1"
FIELDS = frozenset({"schema", "repository", "pull_request", "base_sha", "head_sha", "role",
                    "data_sensitivity", "reasoning_effort", "controller_run_id",
                    "controller_run_attempt", "selected_roles"})
ROLES = frozenset({"assessment", "triage", "critic", "judge", "implementation", "autofix"})


def _pairs(rows):
    value = {}
    for k, v in rows:
        if k in value:
            raise ValueError("subscription_context_unavailable")
        value[k] = v
    return value


def _constant(_value):
    raise ValueError("subscription_context_unavailable")


def read_context(path=CONTEXT, environ=None):
    env = os.environ if environ is None else environ
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_size > 16384:
            raise ValueError("invalid context input")
        raw = path.read_bytes()
        if len(raw) > 16384:
            raise ValueError("context exceeds limit")
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (OSError, ValueError, RecursionError):
        raise ValueError("subscription_context_unavailable") from None
    if (
        not isinstance(value, dict) or set(value) != FIELDS or value.get("schema") != SCHEMA
        or not isinstance(value.get("repository"), str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"])
        or any(type(value.get(k)) is not int or value[k] <= 0
               for k in ("pull_request", "controller_run_id", "controller_run_attempt"))
        or any(not isinstance(value.get(k), str) or not re.fullmatch(r"[a-f0-9]{40}", value[k])
               for k in ("base_sha", "head_sha"))
        or not isinstance(value.get("role"), str) or value["role"] not in ROLES
        or value.get("data_sensitivity") not in ("public", "internal")
        or value.get("reasoning_effort") not in ("medium", "xhigh")
    ):
        raise ValueError("subscription_context_invalid")
    roles = value["selected_roles"]
    if (not isinstance(roles, list) or len(roles) > 100
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", v) for v in roles)
            or len(set(roles)) != len(roles)):
        raise ValueError("subscription_context_invalid")
    expected = {"repository": "GITHUB_REPOSITORY", "pull_request": "PR_NUMBER",
                "base_sha": "PR_BASE_SHA", "head_sha": "PR_HEAD_SHA",
                "controller_run_id": "GITHUB_RUN_ID", "controller_run_attempt": "GITHUB_RUN_ATTEMPT"}
    if any(env.get(k) != str(value[field]) for field, k in expected.items()):
        raise ValueError("subscription_context_revision_mismatch")
    return value
