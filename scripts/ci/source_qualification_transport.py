#!/usr/bin/env python3
"""One-job native metadata transport canary; never source/merge/deploy authority."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

SOURCE = Path(__file__).with_name("source_qualification.py")
SPEC = importlib.util.spec_from_file_location("qualification_transport_reader", SOURCE)
Q = importlib.util.module_from_spec(SPEC)
exec(compile(SOURCE.read_bytes(), str(SOURCE), "exec"), Q.__dict__)
SCHEMA = "source-qualification-native-transport/v1"
WORKFLOW = ".github/workflows/source-qualification-producer-proof.yml"
BINDING = "config/source-qualification-producer.json"
COMPONENT = "Manolii-org/ai-starter-pack"
JOB = "producer"
JOB_NAME = "Source qualification transport producer"
ARTIFACT_KEY = "transport_proof"


def binding(value):
    keys = {"schema_version", "publication_status", "component_repository", "component_sha",
            "native_app_id", "workflow_path", "workflow_sha256", "job_key", "job_name", "artifact_key"}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["publication_status"] != "accepted_source_only"
            or value["component_repository"] != COMPONENT or not Q.sha(value["component_sha"])
            or type(value["native_app_id"]) is not int or value["native_app_id"] <= 0
            or value["workflow_path"] != WORKFLOW or value["job_key"] != JOB
            or value["job_name"] != JOB_NAME or value["artifact_key"] != ARTIFACT_KEY
            or not isinstance(value["workflow_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["workflow_sha256"]) is None):
        raise Q.QualificationError("compiled producer source publication/identity unavailable")
    return value


def native_context(api, repo, controller_sha, run_id, attempt, *, completed):
    """RUN attestation associates the producer only after complete job exclusivity.

    The expected controller SHA is an independent caller/owner pin. A
    workflow_run event's head_sha must never be substituted for workflow source.
    This seam admits only workflow_dispatch and still needs native canary proof.
    """
    if (not Q.owning_repository(repo) or not Q.sha(controller_sha)
            or type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0):
        raise Q.QualificationError("independent exact native controller context required")
    compiled = binding(Q.strict_json(Q.immutable_file(api, repo, BINDING, controller_sha)))
    workflow = Q.immutable_file(api, repo, WORKFLOW, controller_sha)
    if hashlib.sha256(workflow).hexdigest() != compiled["workflow_sha256"]:
        raise Q.QualificationError("pinned producer workflow bytes differ")
    status, conclusion = ("completed", "success") if completed else ("in_progress", None)
    run = api.get(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != run_id
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != attempt
            or run.get("event") != "workflow_dispatch" or run.get("path") != WORKFLOW
            or run.get("head_sha") != controller_sha
            or run.get("status") != status or run.get("conclusion") != conclusion
            or not isinstance(run.get("repository"), dict) or run["repository"].get("full_name") != repo):
        raise Q.QualificationError("native controller run/source identity unavailable")
    jobs = api.pages(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    if len(jobs) != 1:
        raise Q.QualificationError("producer run must contain exactly one native job")
    job = jobs[0]
    if (not isinstance(job, dict) or type(job.get("id")) is not int or job["id"] <= 0
            or job.get("name") != JOB_NAME or type(job.get("run_id")) is not int or job["run_id"] != run_id
            or type(job.get("run_attempt")) is not int or job["run_attempt"] != attempt
            or job.get("status") != status or job.get("conclusion") != conclusion):
        raise Q.QualificationError("exclusive native producer did not execute successfully")
    check_url = job.get("check_run_url", "")
    match = re.fullmatch(re.escape(f"https://api.github.com/repos/{repo}/check-runs/") + r"([1-9][0-9]*)", check_url)
    if not match:
        raise Q.QualificationError("native producer check identity unavailable")
    check_id = int(match[1])
    check = api.get(f"repos/{repo}/check-runs/{check_id}")
    if (not Q.native_identity(check, {"name": JOB_NAME, "app_id": compiled["native_app_id"]})
            or type(check.get("id")) is not int or check["id"] != check_id
            or check.get("head_sha") != controller_sha or check.get("status") != status
            or check.get("conclusion") != conclusion
            or check.get("details_url") != f"https://github.com/{repo}/actions/runs/{run_id}/job/{job['id']}"):
        raise Q.QualificationError("native producer check/run association unavailable")
    return {"schema": SCHEMA, "purpose": "transport_canary", "repository": repo,
            "controller_sha": controller_sha, "component_repository": COMPONENT,
            "component_sha": compiled["component_sha"], "binding_sha256": Q.digest(compiled),
            "workflow_path": WORKFLOW, "workflow_sha256": compiled["workflow_sha256"],
            "run_id": run_id, "run_attempt": attempt, "job_key": JOB,
            "native_job_id": job["id"], "native_check_id": check_id, "artifact_key": ARTIFACT_KEY,
            "source_qualified": False, "admission_authorized": False}


def prepared_record(context):
    return {**context, "subject_sha256": Q.digest(context)}


def read_transport(api, repo, controller_sha, run_id, attempt):
    context = native_context(api, repo, controller_sha, run_id, attempt, completed=True)
    record = api.artifact(repo, run_id, f"source-qualification-{ARTIFACT_KEY}-{attempt}",
                          expected_head=controller_sha)
    # Canonical hashing also distinguishes boolean aliases from actual integers.
    if Q.digest(record) != Q.digest(prepared_record(context)):
        raise Q.QualificationError("prepared transport subject/digest differs")
    return {"schema": SCHEMA, "native_transport_bound": True,
            "source_qualified": False, "admission_authorized": False,
            "controller_sha": controller_sha, "component_sha": context["component_sha"],
            "subject_sha256": record["subject_sha256"], "run_id": run_id, "run_attempt": attempt}


def produce_transport(api, env, compiled_path, output):
    with Q.receipt_output(output, env, ARTIFACT_KEY) as write:
        if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or env.get("GITHUB_JOB") != JOB):
            raise Q.QualificationError("isolated native metadata producer context required")
        controller = env.get("GITHUB_WORKFLOW_SHA")
        context = native_context(api, env.get("GITHUB_REPOSITORY"), controller,
                                 int(env.get("GITHUB_RUN_ID", "")), int(env.get("GITHUB_RUN_ATTEMPT", "")),
                                 completed=False)
        # The controller's compiled binding selected the immutable code before
        # token injection. Neither dispatch input nor artifact payload selects it.
        local = Path(compiled_path)
        if local.is_symlink() or not local.is_file():
            raise Q.QualificationError("regular compiled controller binding required")
        compiled = binding(Q.strict_json(local.read_bytes()))
        if Q.digest(compiled) != context["binding_sha256"]:
            raise Q.QualificationError("controller binding differs from its immutable source")
        component = subprocess.check_output(["git", "--no-replace-objects", "rev-parse", "HEAD"],
                                            cwd=SOURCE.parents[2], timeout=15).decode().strip()
        if component != context["component_sha"]:
            raise Q.QualificationError("executing component differs from reviewed source pin")
        write(prepared_record(context))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--produce", action="store_true")
    parser.add_argument("--repository")
    parser.add_argument("--controller-sha")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--binding", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        api = Q.NativeAPI(Q.review_module())
        if args.produce:
            produce_transport(api, os.environ, args.binding, args.output)
            return 0
        result = read_transport(api, args.repository, args.controller_sha, args.run_id, args.attempt)
    except (Q.QualificationError, OSError, ValueError, KeyError, TypeError, AttributeError,
            RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile):
        print(json.dumps({"schema": SCHEMA, "native_transport_bound": False,
                          "source_qualified": False, "admission_authorized": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
