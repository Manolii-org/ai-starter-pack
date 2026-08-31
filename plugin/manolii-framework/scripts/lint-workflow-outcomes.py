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
OUTCOMES = {"EXECUTED", "NO_CHANGES", "DEFERRED", "POLICY_HELD", "FAILED"}


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
        if inline and not re.fullmatch(r"[|>][+-]?", inline):
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
    if re.search(r"\b(?:exit|return)\s+0\b", executable):
        return True
    announcements = re.findall(r"(?im)^\s*(?:echo|printf)\b[^\n]*", executable)
    return any(SKIP_WORDS.search(line) for line in announcements)


def parse_needs(block: str) -> set[str]:
    """Parse GitHub Actions scalar, inline-list, and block-list needs syntax."""
    scalar = re.search(r"(?m)^    needs:[ \t]*([^\n#]+?)[ \t]*$", block)
    if scalar:
        value = scalar.group(1).strip()
        if value.startswith("[") and value.endswith("]"):
            return {item.strip() for item in value[1:-1].split(",") if item.strip()}
        return {value}
    list_match = re.search(
        r"(?ms)^    needs:\s*$\n(?P<items>(?:      - [A-Za-z0-9_-]+\s*$\n?)+)", block
    )
    if not list_match:
        return set()
    return set(re.findall(r"(?m)^      - ([A-Za-z0-9_-]+)\s*$", list_match.group("items")))


def step_block(job: str, step_name: str) -> str | None:
    match = re.search(rf"(?m)^      - name:\s*{re.escape(step_name)}\s*$", job)
    if not match:
        return None
    next_step = re.search(r"(?m)^      - (?:name:|uses:)", job[match.end() :])
    end = match.end() + next_step.start() if next_step else len(job)
    return job[match.start() : end]


def lint_contract(repo: Path, relative: str, contract: dict) -> list[str]:
    errors: list[str] = []
    path = repo / relative
    if not path.is_file():
        return [f"{relative}: contracted workflow is missing"]
    text = path.read_text(encoding="utf-8")
    load_bearing = contract.get("load_bearing_jobs", [])
    reporter_name = contract.get("outcome_reporter_job")
    outcomes = contract.get("outcomes", [])
    load_bearing_steps = contract.get("load_bearing_steps", {})
    if (
        not isinstance(load_bearing, list)
        or not load_bearing
        or not all(isinstance(x, str) and x for x in load_bearing)
    ):
        errors.append(f"{relative}: load_bearing_jobs must be a non-empty string list")
        return errors
    if not isinstance(load_bearing_steps, dict):
        errors.append(f"{relative}: load_bearing_steps must be an object")
        return errors
    if not isinstance(reporter_name, str) or not reporter_name:
        errors.append(f"{relative}: outcome_reporter_job must be a non-empty string")
        return errors
    if (
        not isinstance(outcomes, list)
        or not outcomes
        or not all(isinstance(item, str) and item in OUTCOMES for item in outcomes)
        or "FAILED" not in outcomes
    ):
        errors.append(
            f"{relative}: outcomes must be a non-empty supported list containing FAILED"
        )
        return errors
    for name in load_bearing:
        block = job_block(text, name)
        if block is None:
            errors.append(f"{relative}: load-bearing job {name!r} is missing")
        elif announces_green_skip(block):
            errors.append(
                f"{relative}: load-bearing job {name!r} contains skip/defer/hold semantics; classify outside it"
            )
        for step_name in load_bearing_steps.get(name, []):
            step = step_block(block or "", step_name)
            if step is None:
                errors.append(
                    f"{relative}: load-bearing step {step_name!r} is missing from job {name!r}"
                )
            elif re.search(r"(?m)^        if:\s*", step):
                errors.append(
                    f"{relative}: load-bearing step {step_name!r} must not be conditional"
                )
    reporter = job_block(text, reporter_name)
    if reporter is None:
        errors.append(f"{relative}: outcome_reporter_job {reporter_name!r} is missing")
        return errors
    active_reporter = "\n".join(
        re.sub(r"(^|\s)//.*$", r"\1", line)
        for line in reporter.splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )
    conditions = re.findall(r"(?m)^    if:\s*(.+?)\s*$", reporter)
    if conditions != ["${{ always() }}"]:
        errors.append(
            f"{relative}: outcome reporter must use exactly if: ${{{{ always() }}}}"
        )
    needs = parse_needs(reporter)
    missing = sorted(set(load_bearing) - needs)
    if missing:
        errors.append(
            f"{relative}: outcome reporter does not need load-bearing job(s): {', '.join(missing)}"
        )
    if "github.rest.checks.create" not in active_reporter:
        errors.append(f"{relative}: outcome reporter must publish a GitHub check-run")
    if "checks: write" not in text:
        errors.append(f"{relative}: outcome reporter requires checks: write permission")
    if "name: `" not in active_reporter or "${outcome}" not in active_reporter:
        errors.append(f"{relative}: check-run name must contain the selected outcome")
    if not re.search(r"output:[\s\S]{0,200}\bsummary\b", active_reporter):
        errors.append(f"{relative}: check-run must publish an outcome summary")
    for outcome in outcomes:
        if outcome not in active_reporter:
            errors.append(f"{relative}: outcome reporter does not publish {outcome}")
        selects_outcome = re.search(
            rf"\boutcome\s*=[^;\n]*['\"]{outcome}['\"]", active_reporter
        ) or re.search(r"\boutcome\s*=\s*process\.env\.OUTCOME\b", active_reporter)
        if not selects_outcome:
            errors.append(f"{relative}: no executable branch selects outcome {outcome}")
        has_dynamic_summary = re.search(
            r"(?:const|let)\s+summary\s*=[^;\n]*\$\{(?:process\.env\.)?OUTCOME|(?:const|let)\s+summary\s*=[^;\n]*\$\{outcome",
            active_reporter,
            re.I,
        )
        has_literal_summary = re.search(
            rf"summary\s*=\s*['\"`][^\n]*{outcome}", active_reporter, re.I
        )
        if not (has_dynamic_summary or has_literal_summary):
            errors.append(f"{relative}: summary cannot identify selected outcome {outcome}")
    if "FAILED" in outcomes and not (
        re.search(r"FAILED[^\n]{0,120}failure", active_reporter, re.I)
        or re.search(r"outcome\s*===\s*['\"]FAILED['\"][^\n]{0,120}['\"]failure['\"]", active_reporter)
    ):
        errors.append(f"{relative}: FAILED must map to a failure conclusion")
    for held in ("DEFERRED", "POLICY_HELD"):
        has_explicit_neutral = re.search(
            rf"{held}[^\n]{{0,120}}neutral", active_reporter, re.I
        )
        has_neutral_fallback = re.search(
            r"const\s+conclusion\s*=.*:\s*['\"]neutral['\"]\s*;", active_reporter
        )
        if held in outcomes and not (has_explicit_neutral or has_neutral_fallback):
            errors.append(f"{relative}: {held} must map to a neutral diagnostic conclusion")
    for successful in ("EXECUTED", "NO_CHANGES"):
        if successful in outcomes and not (
            re.search(rf"{successful}[^\n]{{0,120}}success", active_reporter, re.I)
            or re.search(
                r"outcome\s*===\s*['\"]FAILED['\"]\s*\?\s*['\"]failure['\"]\s*:\s*['\"]success['\"]",
                active_reporter,
            )
        ):
            errors.append(f"{relative}: {successful} must map to a success conclusion")
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
