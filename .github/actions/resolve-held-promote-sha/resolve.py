#!/usr/bin/env python3
"""Fail closed unless an explicit SHA is on the repository default branch."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from urllib.parse import quote


def gh_json(path: str) -> object:
    process = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, timeout=30, check=False
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"gh api {path} failed")
    return json.loads(process.stdout)


def main() -> int:
    try:
        sha = os.environ.get("INPUT_SHA", "").strip()
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("sha is required and must be a full 40-character lowercase hex SHA")
        if not repo:
            raise ValueError("GITHUB_REPOSITORY is required")
        metadata = gh_json(f"repos/{repo}")
        default_branch = metadata.get("default_branch") if isinstance(metadata, dict) else None
        if not default_branch:
            raise ValueError("repository default branch could not be resolved")
        comparison = gh_json(
            f"repos/{repo}/compare/{quote(default_branch, safe='')}...{sha}"
        )
        status = comparison.get("status") if isinstance(comparison, dict) else None
        if status not in {"identical", "behind"}:
            raise ValueError(
                f"sha is not on {default_branch}: compare status={status!r}; "
                "expected identical or behind"
            )
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as stream:
                stream.write(f"sha={sha}\ndefault_branch={default_branch}\n")
        print(f"resolved held promote SHA {sha} on {default_branch} ({status})")
        return 0
    except (ValueError, RuntimeError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"::error::held promote SHA resolution failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
