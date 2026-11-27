#!/usr/bin/env python3
"""Check a real Fly wake from a completed consumer job and fleet scale run."""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime

RECONCILERS = {".github/workflows/fleet-scale.yml", ".github/workflows/fleet-monitor.yml"}
WAKE_WORKFLOW = ".github/workflows/fleet-scale-wake.yml"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DEMAND = re.compile(
    r"demand=\d+ \(busy=(\d+)\+queued=\d+; probed=(\d+), forwarded_floor=\d+, "
    r"applied_floor=\d+\) desired=(\d+) started=(\d+) cap=\d+ \[([^\]]+)\] queue_probe_complete=(true|false)"
)


def need(condition, detail):
    if not condition:
        raise ValueError(detail)


def timestamp(payload, field):
    value = datetime.fromisoformat(payload[field].replace("Z", "+00:00"))
    need(value.tzinfo is not None, f"{field} needs a timezone")
    return value


def relative_job(job):
    return f"actions/runs/{job['run_id']}/job/{job['id']}"


def dispatch_records(log):
    """Read exact run IDs logged by the trusted consumer dispatcher."""
    for line in log.splitlines():
        parts = line.split("\t", 2)
        message = parts[-1]
        message = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+Z\s+", "", message).strip()
        if not message.startswith("{"):
            continue
        try:
            value = json.loads(message)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("dispatched") is True and value.get("dry_run") is False:
            yield value.get("fleet_dispatch")


def validate(repo, fleet_repo, tenant, scenario, target, job, scale, scale_run, logs, wake=None):
    """Return a receipt only after matching queue pressure, Machine and GitHub job."""
    need(job.get("conclusion") == "success", "the consumer job must pass")
    need("fly" in {label.casefold() for label in job["labels"]}, "the consumer job must request the fly label")
    runner = re.fullmatch(r"fly-([0-9a-f]+)", job.get("runner_name") or "")
    need(runner, "GitHub did not assign the consumer job to a Fly Machine")
    need(scale.get("conclusion") == "success" and scale.get("name") in {f"Scale {tenant}", f"Probe {tenant}"},
         "the scale job must be successful and belong to this tenant")
    need(scale_run.get("path") in RECONCILERS and scale_run.get("head_branch") == "main",
         "the scale run must come from an owning fleet workflow on main")
    need(not re.search(r"^.*\[dry-run\] .*machine start ", logs, re.MULTILINE), "dry-run cannot qualify a wake")
    samples = DEMAND.findall(logs)
    need(len(samples) == 1, "expected one scaler queue observation")
    busy, queued, desired, started = map(int, samples[0][:4])
    action, complete = samples[0][4:]
    need(queued > 0 and complete == "true" and desired > started, "a complete read must show real queued work and room to scale")
    need((busy == started and started > 0) if scenario == "busy" else busy == started,
         "pre-wake capacity did not match the test scenario")
    if scenario == "cold":
        need(started == 0, "the cold test requires zero started Machines")
    machine = runner.group(1)
    created = timestamp(job, "created_at")
    assigned = timestamp(job, "started_at")
    scale_started = timestamp(scale, "started_at")
    scale_finished = timestamp(scale, "completed_at")
    # The start request, not the CLI confirmation, must precede assignment.
    # GitHub may assign work while Fly's start command is still returning.
    request = re.search(rf"(\d{{4}}-\d{{2}}-\d{{2}}T\S+Z)\s+Starting machine {machine} \(", logs)
    need(request and re.search(rf"\b{machine} has been started\b", logs),
         "the fleet log does not prove a start of the assigned Machine")
    start_requested = datetime.fromisoformat(request.group(1).replace("Z", "+00:00"))
    # REST job times have second precision and runner logs include fractions.
    need(created <= start_requested and start_requested.replace(microsecond=0) <= assigned,
         "queue, Machine start, and assignment timestamps are inconsistent")
    need(scale_started <= start_requested and start_requested.replace(microsecond=0) <= scale_finished,
         "Machine start timestamp is outside the scale job")
    wait = int((assigned - created).total_seconds())
    need(0 <= wait <= target, f"pickup took {wait}s against a {target}s target")

    wake_source = "manual"
    if scale_run.get("event") == "schedule" and scale.get("run_attempt") == 1:
        wake_source = "scheduled"
    if scenario == "missed-event":
        need(wake_source == "scheduled", "the fallback test needs the first attempt of a scheduled fleet run")
    if wake is not None:
        wake_repo, wake_job, wake_run, wake_logs = wake
        need(wake_source != "scheduled" and action == "up: start-only", "consumer wake must drive start-only scale")
        need(scale.get("run_attempt") == 1 and wake_job.get("run_attempt") == 1,
             "event qualification needs the first attempts of the consumer wake and fleet scale")
        need(wake_repo.split("/", 1)[0].casefold() == repo.split("/", 1)[0].casefold(),
             "consumer wake and demand must belong to the same organization")
        need(wake_job.get("conclusion") == "success" and wake_run.get("path") == WAKE_WORKFLOW
             and wake_run.get("event") not in {None, "schedule", "workflow_dispatch"},
             "expected an event-triggered consumer wake workflow")
        expected_dispatch = {"repository": fleet_repo, "tenant": tenant, "action": "up", "run_id": scale["run_id"]}
        need(scale_run.get("event") == "workflow_dispatch" and expected_dispatch in dispatch_records(wake_logs),
             "consumer wake did not record the selected fleet run and tenant")
        fleet_created = timestamp(scale_run, "created_at")
        wake_started = timestamp(wake_job, "started_at")
        wake_finished = timestamp(wake_job, "completed_at")
        need(wake_started <= fleet_created <= wake_finished, "scale dispatch did not occur during the consumer wake")
        wake_source = "event"

    receipt = {
        "tenant": tenant, "repository": repo, "scenario": scenario, "wake_source": wake_source,
        "job": f"https://github.com/{repo}/{relative_job(job)}",
        "scale_job": f"https://github.com/{fleet_repo}/{relative_job(scale)}",
        "started_before": started, "wait_seconds": wait,
    }
    if wake is not None:
        receipt["wake_job"] = f"https://github.com/{wake_repo}/{relative_job(wake_job)}"
        receipt["dispatch_run_id"] = scale["run_id"]
    return receipt


def github_json(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True, timeout=30))


def github_job(repo, job_id):
    return github_json(f"repos/{repo}/actions/jobs/{job_id}")


def run_and_log(repo, job):
    run = github_json(f"repos/{repo}/actions/runs/{job['run_id']}")
    log = subprocess.check_output(["gh", "run", "view", "-R", repo, "--job", str(job["id"]), "--log"],
                                  text=True, timeout=60)
    return run, ANSI.sub("", log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--fleet-repository", required=True, help="owning fleet controller repository (OWNER/REPO)")
    parser.add_argument("--scenario", choices=["busy", "cold", "missed-event"], required=True)
    parser.add_argument("--job", type=int, required=True, help="completed consumer job database ID")
    parser.add_argument("--scale-job", type=int, required=True, help="fleet scale/monitor job database ID")
    parser.add_argument("--wake-job", type=int, help="consumer wake job database ID for event-triggered evidence")
    parser.add_argument("--wake-repository", help="consumer wake repository when a tenant broker dispatched the run")
    parser.add_argument("--target-seconds", type=int, required=True)
    args = parser.parse_args()
    try:
        need(re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.tenant), "invalid tenant name")
        wake_repo = args.wake_repository or args.repository
        for repo in (args.repository, args.fleet_repository, wake_repo):
            need(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo), "expected owner/repository")
        need(args.job > 0 and args.scale_job > 0 and args.target_seconds > 0, "positive job IDs and target required")
        need(args.wake_job is not None or args.wake_repository is None, "--wake-repository requires --wake-job")
        job = github_job(args.repository, args.job)
        scale = github_job(args.fleet_repository, args.scale_job)
        scale_run, logs = run_and_log(args.fleet_repository, scale)
        wake = None
        if args.wake_job is not None:
            need(args.wake_job > 0, "positive wake job ID required")
            wake_job = github_job(wake_repo, args.wake_job)
            wake_run, wake_logs = run_and_log(wake_repo, wake_job)
            wake = (wake_repo, wake_job, wake_run, wake_logs)
        print(json.dumps(validate(args.repository, args.fleet_repository, args.tenant, args.scenario, args.target_seconds,
                                  job, scale, scale_run, logs, wake), indent=2))
    except (KeyError, ValueError, TypeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Wake acceptance: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
