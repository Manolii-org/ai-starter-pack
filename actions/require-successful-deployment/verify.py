"""Fail closed on the latest branch run; never search backwards for a success."""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

API_ORIGIN = "https://api.github.com"
REQUEST_TIMEOUT = 15
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class VerificationError(Exception):
    """A static, safe-to-log rejection without API bodies or caller input."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def clean_text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= limit
        and value == value.strip()
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def validate_inputs(repository: str, workflow: str, branch: str, sha: str,
                    required_jobs: str) -> list[str]:
    require(bool(re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}",
        repository,
    )) and "--" not in repository.split("/")[0]
            and repository.split("/")[-1] not in (".", ".."), "Invalid owner/repository")
    require(len(workflow) <= 255 and bool(re.fullmatch(
        r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:yml|yaml)", workflow,
    )) and ".." not in workflow, "Invalid workflow filename")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", sha)), "Invalid exact SHA")
    require(
        clean_text(branch, 255) and branch != "@"
        and not any(c in branch for c in " ~^:?*[\\")
        and not any(s in branch for s in ("..", "//", "@{"))
        and not branch.endswith(".")
        and all(part and not part.startswith(".") and not part.endswith(".lock")
                for part in branch.split("/")),
        "Invalid branch name",
    )
    require(len(required_jobs) <= 65536, "Required jobs input is too large")
    try:
        names = json.loads(required_jobs)
    except (ValueError, RecursionError):
        raise VerificationError("Required jobs must be a JSON array") from None
    require(isinstance(names, list) and 1 <= len(names) <= 100,
            "Require between 1 and 100 job names")
    require(all(clean_text(name, 512) for name in names), "Invalid required job name")
    require(len(set(names)) == len(names), "Duplicate required job names")
    return names


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON metadata key")
        result[key] = value
    return result


def get_json(path: str, token: str) -> dict:
    # Fixed origin and no redirect handling: API metadata never chooses a URL.
    connection = http.client.HTTPSConnection("api.github.com", timeout=REQUEST_TIMEOUT)
    try:
        connection.request("GET", path, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "require-successful-deployment",
        })
        response = connection.getresponse()
        require(response.status == 200, "GitHub API did not return HTTP 200")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        require(len(body) <= MAX_RESPONSE_BYTES, "GitHub API response is too large")
        data = json.loads(body, object_pairs_hook=unique_object)
        require(isinstance(data, dict), "Invalid GitHub API object")
        return data
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        raise VerificationError("GitHub API request or JSON decoding failed") from None
    finally:
        connection.close()


def latest_run(data: dict, repository: str, workflow: str, branch: str, sha: str) -> dict:
    runs = data.get("workflow_runs")
    require(positive_int(data.get("total_count")) and isinstance(runs, list)
            and len(runs) == 1 and isinstance(runs[0], dict),
            "Expected exactly the single latest branch workflow run")
    run = runs[0]
    require(positive_int(run.get("id")) and positive_int(run.get("run_attempt"))
            and positive_int(run.get("workflow_id")), "Invalid workflow run identity")
    workflow_path = f".github/workflows/{workflow}"
    require(run.get("path") in (workflow_path, f"{workflow_path}@{branch}"),
            "Workflow path mismatch")
    require(run.get("head_branch") == branch and run.get("head_sha") == sha,
            "Latest run branch or exact SHA mismatch")
    require(run.get("event") == "push" and run.get("status") == "completed"
            and run.get("conclusion") == "success", "Latest push run is not successful")
    require("name" in run and (run["name"] is None or clean_text(run["name"], 512)),
            "Invalid workflow display name")
    for field in ("repository", "head_repository"):
        repo = run.get(field)
        require(isinstance(repo, dict) and positive_int(repo.get("id"))
                and isinstance(repo.get("full_name"), str)
                and repo["full_name"].casefold() == repository.casefold(),
                "Workflow run repository mismatch")
    require(run["repository"]["id"] == run["head_repository"]["id"],
            "Workflow run head repository mismatch")
    require(run.get("url") == f"{API_ORIGIN}/repos/{repository}/actions/runs/{run['id']}"
            and run.get("html_url") == f"https://github.com/{repository}/actions/runs/{run['id']}",
            "Workflow run URL mismatch")
    return run


def verify_jobs(data: dict, run: dict, names: list[str]) -> None:
    jobs, count = data.get("jobs"), data.get("total_count")
    require(type(count) is int and 1 <= count <= 100 and isinstance(jobs, list)
            and len(jobs) == count, "Incomplete or oversized jobs response")
    ids, matches = set(), {name: [] for name in names}
    for job in jobs:
        require(isinstance(job, dict) and positive_int(job.get("id")), "Invalid job identity")
        require(job["id"] not in ids, "Duplicate job identity")
        ids.add(job["id"])
        require(positive_int(job.get("run_id"))
                and ("run_attempt" not in job or (positive_int(job["run_attempt"])
                     and job["run_attempt"] == run["run_attempt"]))
                and all(key in job and job[key] == run[key] for key in
                        ("head_sha", "head_branch"))
                and job["run_id"] == run["id"] and job.get("run_url") == run["url"]
                and "workflow_name" in job and job["workflow_name"] == run["name"],
                "Job metadata does not match the current run attempt")
        require(clean_text(job.get("name"), 512), "Invalid job display name")
        if job["name"] in matches:
            matches[job["name"]].append(job)
    for matched in matches.values():
        require(len(matched) == 1, "Required job is missing or duplicated")
        require(matched[0].get("status") == "completed"
                and matched[0].get("conclusion") == "success", "Required job is not successful")


def verify(repository: str, workflow: str, branch: str, sha: str,
           required_jobs: str, token: str) -> dict[str, str]:
    names = validate_inputs(repository, workflow, branch, sha, required_jobs)
    require(clean_text(token, 16384), "Missing or invalid github.token")
    base = f"/repos/{repository}/actions"
    # No event, status or SHA filter: a newer non-success must hide older successes.
    path = f"{base}/workflows/{quote(workflow, safe='')}/runs?" + urlencode({
        "branch": branch, "per_page": 1,
    })
    run = latest_run(get_json(path, token), repository, workflow, branch, sha)
    jobs_path = f"{base}/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
    verify_jobs(get_json(jobs_path, token), run, names)
    final = latest_run(get_json(path, token), repository, workflow, branch, sha)
    fields = ("id", "run_attempt", "workflow_id", "path", "head_branch", "head_sha",
              "event", "status", "conclusion", "name", "url", "html_url")
    require(all(final[key] == run[key] for key in fields)
            and final["repository"]["id"] == run["repository"]["id"],
            "Latest workflow run changed during verification")
    return {"run_id": str(run["id"]), "html_url": run["html_url"]}


def main() -> int:
    try:
        output = os.environ.get("GITHUB_OUTPUT", "")
        require(bool(output), "Missing GitHub output file")
        result = verify(
            os.environ.get("DEPLOYMENT_REPOSITORY", ""), os.environ.get("INPUT_WORKFLOW", ""),
            os.environ.get("INPUT_BRANCH", ""), os.environ.get("INPUT_SHA", ""),
            os.environ.get("INPUT_REQUIRED_JOBS", ""), os.environ.get("DEPLOYMENT_TOKEN", ""),
        )
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in result.items()))
    except (VerificationError, OSError) as error:
        message = str(error) if isinstance(error, VerificationError) else "Cannot write GitHub outputs"
        print(f"Deployment verification failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
