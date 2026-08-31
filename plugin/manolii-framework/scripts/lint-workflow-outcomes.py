#!/usr/bin/env python3
"""Validate explicit outcome contracts for load-bearing GitHub workflows.

The contract is intentionally repository-owned: central tooling defines the
semantics while each consumer identifies which jobs actually perform work.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml


SKIP_WORDS = re.compile(r"\b(skip(?:ped|ping)?|defer(?:red|ring)?|hold|no[- ]?op|absent|missing|not set)\b", re.I)
EXIT_ZERO = re.compile(r"\bexit\s+0\b")


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def jobs_for(workflow: dict) -> dict:
    jobs = workflow.get("jobs", {})
    return jobs if isinstance(jobs, dict) else {}


def lint_contract(repo: Path, relative: str, contract: dict) -> list[str]:
    errors: list[str] = []
    path = repo / relative
    if not path.is_file():
        return [f"{relative}: contracted workflow is missing"]

    workflow = load_yaml(path)
    jobs = jobs_for(workflow)
    load_bearing = contract.get("load_bearing_jobs", [])
    reporter_name = contract.get("outcome_reporter_job")
    allowed_deferred_steps = set(contract.get("allowed_deferred_steps", []))
    if not load_bearing:
        errors.append(f"{relative}: load_bearing_jobs must not be empty")

    for job_name in load_bearing:
        job = jobs.get(job_name)
        if not isinstance(job, dict):
            errors.append(f"{relative}: load-bearing job {job_name!r} is missing")
            continue
        for step in job.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            command = step.get("run")
            step_name = str(step.get("name", "<unnamed>"))
            if (
                isinstance(command, str)
                and EXIT_ZERO.search(command)
                and SKIP_WORDS.search(command)
                and step_name not in allowed_deferred_steps
            ):
                errors.append(
                    f"{relative}: load-bearing job {job_name!r}, step "
                    f"{step_name!r} can report success after skipping work"
                )

    reporter = jobs.get(reporter_name) if reporter_name else None
    if not isinstance(reporter, dict):
        errors.append(f"{relative}: outcome_reporter_job {reporter_name!r} is missing")
        return errors

    condition = str(reporter.get("if", ""))
    if "always()" not in condition:
        errors.append(f"{relative}: outcome reporter must use if: always()")

    needs = reporter.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    missing_needs = sorted(set(load_bearing) - set(needs or []))
    if missing_needs:
        errors.append(
            f"{relative}: outcome reporter does not need load-bearing job(s): "
            + ", ".join(missing_needs)
        )

    reporter_text = yaml.safe_dump(reporter, sort_keys=False)
    for outcome in ("DEFERRED", "POLICY_HELD", "FAILED"):
        if outcome not in reporter_text:
            errors.append(f"{relative}: outcome reporter does not name {outcome}")
    return errors


def lint(repo: Path, config_path: Path) -> list[str]:
    config = load_yaml(config_path)
    workflows = config.get("workflows", {})
    if not isinstance(workflows, dict):
        return [f"{config_path}: workflows must be a mapping"]
    errors: list[str] = []
    for relative, contract in workflows.items():
        if not isinstance(contract, dict):
            errors.append(f"{relative}: contract must be a mapping")
            continue
        errors.extend(lint_contract(repo, relative, contract))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("config/workflow-outcomes.yaml"))
    args = parser.parse_args()
    repo = args.repo.resolve()
    config = args.config if args.config.is_absolute() else repo / args.config
    if not config.is_file():
        print(f"ERROR: workflow outcome contract missing: {config}", file=sys.stderr)
        return 1
    try:
        errors = lint(repo, config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    print(f"OK: {len(load_yaml(config).get('workflows', {}))} workflow outcome contract(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
