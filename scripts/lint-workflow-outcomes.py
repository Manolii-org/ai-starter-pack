#!/usr/bin/env python3
"""Validate explicit outcome contracts for load-bearing GitHub workflows."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKIP_WORDS = re.compile(
    r"\b(skip(?:ped|ping)?|defer(?:red|ring)?|hold|no[- ]?op|absent|missing|not set)\b",
    re.I,
)


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def job_block(text: str, job_name: str) -> str | None:
    match = re.search(rf"(?m)^  {re.escape(job_name)}:\s*$", text)
    if not match:
        return None
    next_job = re.search(r"(?m)^  [A-Za-z0-9_-]+:\s*$", text[match.end() :])
    end = match.end() + next_job.start() if next_job else len(text)
    return text[match.start() : end]


def announces_green_skip(block: str) -> bool:
    """Return true when executable shell announces a skip/defer/hold path."""
    lines = block.splitlines()
    commands: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^\s{8}run:\s*(.*)$", line)
        if not match:
            continue
        inline = match.group(1)
        if inline and inline != "|":
            commands.append(inline)
            continue
        following: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate and len(candidate) - len(candidate.lstrip()) <= 8:
                break
            following.append(candidate)
        commands.append("\n".join(following))
    executable = "\n".join(
        line
        for command in commands
        for line in command.splitlines()
        if not line.lstrip().startswith("#")
    )
    announcements = re.findall(r"(?im)^\s*(?:echo|printf)\b[^\n]*", executable)
    return any(SKIP_WORDS.search(line) for line in announcements)


def lint_contract(repo: Path, relative: str, contract: dict) -> list[str]:
    errors: list[str] = []
    path = repo / relative
    if not path.is_file():
        return [f"{relative}: contracted workflow is missing"]
    text = path.read_text(encoding="utf-8")
    load_bearing = contract.get("load_bearing_jobs", [])
    reporter_name = contract.get("outcome_reporter_job")
    if (
        not isinstance(load_bearing, list)
        or not load_bearing
        or not all(isinstance(x, str) and x for x in load_bearing)
    ):
        errors.append(f"{relative}: load_bearing_jobs must be a non-empty string list")
        return errors
    if not isinstance(reporter_name, str) or not reporter_name:
        errors.append(f"{relative}: outcome_reporter_job must be a non-empty string")
        return errors
    for name in load_bearing:
        block = job_block(text, name)
        if block is None:
            errors.append(f"{relative}: load-bearing job {name!r} is missing")
        elif announces_green_skip(block):
            errors.append(
                f"{relative}: load-bearing job {name!r} contains skip/defer/hold semantics; classify outside it"
            )
    reporter = job_block(text, reporter_name)
    if reporter is None:
        errors.append(f"{relative}: outcome_reporter_job {reporter_name!r} is missing")
        return errors
    conditions = re.findall(r"(?m)^    if:\s*(.+?)\s*$", reporter)
    if conditions != ["${{ always() }}"]:
        errors.append(
            f"{relative}: outcome reporter must use exactly if: ${{{{ always() }}}}"
        )
    needs_match = re.search(r"(?m)^    needs:\s*\[([^]]*)\]\s*$", reporter)
    needs = (
        {x.strip() for x in needs_match.group(1).split(",")} if needs_match else set()
    )
    missing = sorted(set(load_bearing) - needs)
    if missing:
        errors.append(
            f"{relative}: outcome reporter does not need load-bearing job(s): {', '.join(missing)}"
        )
    if "github.rest.checks.create" not in reporter:
        errors.append(f"{relative}: outcome reporter must publish a GitHub check-run")
    for outcome in ("DEFERRED", "POLICY_HELD", "FAILED"):
        if outcome not in reporter:
            errors.append(f"{relative}: outcome reporter does not publish {outcome}")
    return errors


def lint(repo: Path, config_path: Path) -> list[str]:
    config = load_json(config_path)
    workflows = config.get("workflows", {})
    if not isinstance(workflows, dict):
        return [f"{config_path}: workflows must be an object"]
    errors: list[str] = []
    for relative, contract in workflows.items():
        if not isinstance(contract, dict):
            errors.append(f"{relative}: contract must be an object")
        else:
            errors.extend(lint_contract(repo, relative, contract))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config", type=Path, default=Path("config/workflow-outcomes.json")
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    config = args.config if args.config.is_absolute() else repo / args.config
    if not config.is_file():
        print(f"ERROR: workflow outcome contract missing: {config}", file=sys.stderr)
        return 1
    try:
        errors = lint(repo, config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    print(
        f"OK: {len(load_json(config).get('workflows', {}))} workflow outcome contract(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
