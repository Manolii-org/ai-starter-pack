#!/usr/bin/env python3
"""Gate a SkillSpector JSON report: fail on HIGH/CRITICAL skill risk.

    skillspector scan <dir> --recursive --no-llm --format json --output report.json
    python3 scripts/skill-scan-gate.py report.json [--fail-on HIGH]

Static mode only (``--no-llm``): no model spend. Exit codes:
  0  every skill below the threshold
  1  at least one skill at or above the threshold
  2  unreadable report, or the scan itself did not complete (fail closed)

Prints one line per skill at MEDIUM or above so reviewers see the trend
without opening the artifact. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys

ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def _rank(severity: str) -> int:
    try:
        return ORDER.index(str(severity).upper())
    except ValueError:
        return len(ORDER)  # unknown severity: treat as worst (fail closed)


def _skills(report: dict) -> list[dict]:
    if isinstance(report.get("skills"), list):
        return report["skills"]
    return [report]  # single-skill report shape


def evaluate(report: dict, fail_on: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    if report.get("execution_successful") is False:
        return 2, ["::error::SkillSpector reported an incomplete scan"]
    threshold = _rank(fail_on)
    worst = 0
    for skill in _skills(report):
        if skill.get("execution_successful") is False:
            return 2, [f"::error::SkillSpector could not analyse {skill.get('name') or skill.get('path')}"]
        assessment = skill.get("risk_assessment") or {}
        severity = assessment.get("severity") or skill.get("risk_severity") or "UNKNOWN"
        score = assessment.get("score", skill.get("risk_score"))
        rank = _rank(severity)
        worst = max(worst, rank)
        if rank >= _rank("MEDIUM"):
            level = "error" if rank >= threshold else "notice"
            lines.append(f"::{level}::skill {skill.get('name') or skill.get('path')}: {severity} (score {score})")
    return (1 if worst >= threshold else 0), lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report")
    parser.add_argument("--fail-on", default="HIGH", choices=ORDER)
    args = parser.parse_args(argv)
    try:
        with open(args.report, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"::error::cannot read SkillSpector report: {exc}")
        return 2
    if not isinstance(report, dict):
        print("::error::unexpected SkillSpector report shape")
        return 2
    code, lines = evaluate(report, args.fail_on)
    for line in lines:
        print(line)
    print(f"skill-scan-gate: {'FAIL' if code == 1 else 'ERROR' if code == 2 else 'PASS'} (fail-on {args.fail_on})")
    return code


if __name__ == "__main__":
    sys.exit(main())
