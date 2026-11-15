#!/usr/bin/env python3
"""Observe GitHub review-history capability without inventing approval evidence."""
from __future__ import annotations

import argparse
import json
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from release_state import _NoRedirect


def classify_history(data) -> dict:
    """The documented history endpoint cannot attest a release manifest digest.

    Even an apparent extra client field cannot upgrade this API contract into
    retained subject/job/attempt/time evidence. A future provider mechanism
    requires a reviewed adapter, real transport evidence and new tests.
    """
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("approval history representation is invalid")
    return {"schema_version": 1, "provider": "github", "status": "unsupported",
            "execution_authorized": False, "history_records": len(data),
            "reason_codes": ["history-does-not-bind-manifest-digest",
                             "history-does-not-bind-exact-job-and-attempt",
                             "history-does-not-record-authenticated-decision-time"]}


def observe(repo: str, run_id: int, token: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository")
    if type(run_id) is not int or run_id < 1 or not token:
        raise ValueError("owning token and positive run ID are required")
    request = Request(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/approvals",
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("approval history exceeds bound")
        return classify_history(json.loads(raw))
    except HTTPError as exc:
        exc.close()
        return {"schema_version": 1, "provider": "github", "status": "blocked",
                "execution_authorized": False, "http_status": exc.code,
                "reason_codes": ["approval-history-read-refused"]}
    except (OSError, ValueError):
        return {"schema_version": 1, "provider": "github", "status": "unavailable",
                "execution_authorized": False, "reason_codes": ["approval-history-unavailable"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    try:
        result = observe(args.repo, args.run_id, os.environ.get("GH_TOKEN", ""))
    except ValueError:
        parser.error("valid repository, run ID and owning GH_TOKEN are required")
    print(json.dumps(result, sort_keys=True))
    # The exit status is an enablement gate, not a green approval observation.
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
