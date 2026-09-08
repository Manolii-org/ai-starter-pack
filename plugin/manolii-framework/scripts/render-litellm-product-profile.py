#!/usr/bin/env python3
"""Merge an owner profile scaffold into a downloaded LiteLLM product contract."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys

PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def render(contract: dict, scaffold: dict) -> dict:
    profile = scaffold.get("profile")
    definition = scaffold.get("profile_definition")
    if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile) or profile.startswith("replace-"):
        raise ValueError("profile must be a concrete lowercase owner profile name")
    if not isinstance(definition, dict):
        raise ValueError("profile_definition must be an object")
    profiles = contract.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("contract profiles must be an object")
    if profile in profiles:
        raise ValueError(f"profile already exists in contract: {profile}")
    rendered = copy.deepcopy(contract)
    rendered["profiles"][profile] = copy.deepcopy(definition)
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        scaffold = json.loads(args.profile.read_text(encoding="utf-8"))
        result = render(contract, scaffold)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
