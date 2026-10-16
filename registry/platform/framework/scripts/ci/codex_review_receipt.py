#!/usr/bin/env python3
"""Revision-bound Codex execution receipts; findings are not execution success."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
from pathlib import Path

QUOTA = re.compile(r"(?:usage limits?|quota).{0,100}(?:reached|exceeded|exhausted)|"
                   r"(?:reached|exceeded|exhausted).{0,100}(?:usage limits?|quota)|"
                   r"insufficient_quota|rate_limit_exceeded", re.I | re.S)
SENSITIVE = re.compile(
    r"(?:^|/)(?:\.github|\.gitlab|\.ai|\.claude|\.codex|\.cursor|\.githooks|auth(?:entication|orization)?|security|secrets?|"
    r"migrations?|supabase|policies|permissions|deploy|infrastructure|terraform|"
    r"crypto(?:graphy)?|network|webhooks?|audit|tenants?)(?:/|[_.-]|$)|"
    r"(?:^|/)(?:middleware|proxy|schema|Dockerfile|docker-compose|CODEOWNERS)(?:[./_-]|$)|"
    r"(?:^|[/_.-])(?:rls|oauth|jwt|ssrf|csrf|acl|rbac|encryption|signing|"
    r"credentials?|secrets?|tokens?|session|payment|billing|boundaries|isolation|safety|authorization|authentication)(?:/|[_.-]|$)|"
    r"(?:^|/)(?:package(?:-lock)?\.json|composer\.(?:json|lock)|Gemfile(?:\.lock)?|Cargo\.toml|pyproject\.toml|Pipfile(?:\.lock)?|pnpm-lock\.yaml|yarn\.lock|"
    r"requirements[^/]*\.txt|uv\.lock|poetry\.lock|go\.(?:mod|sum)|Cargo\.lock)$",
    re.I,
)


def security_required(paths: list[str]) -> bool:
    """Unknown input is not an exemption; callers must prove a resolved range."""
    return any(SENSITIVE.search(path) for path in paths)


def final_code_turn(raw: str) -> str:
    """Never publish command traces as a verdict if the model turn is missing."""
    parts = re.split(r"(?m)^codex\r?$", raw)
    if len(parts) < 2:
        return ""
    turn = parts[-1].strip()
    turn = re.split(r"(?m)^tokens used\s*$", turn)[0].strip()
    return turn


def classify(lane: str, env: dict[str, str], output: str | None) -> tuple[str, str, int | None]:
    """Pure, fail-closed execution classification from step outcomes and output."""
    range_state = env.get("RANGE_STATE", "unresolved")
    if range_state == "empty" or (lane == "security" and range_state == "security_exempt"):
        return "exempt", range_state, None
    if range_state != "ready":
        return "failed_range", "unresolved_diff", None
    if env.get("AUTH_PRESENT") != "true":
        return "blocked_credentials", "fetch_failed" if env.get("AUTH_PRESENT") == "failed" else "not_provisioned", None
    if env.get("INSTALL_OUTCOME") != "success":
        return "failed_install", "cli_install_failed", None
    if env.get("SCAN_OUTCOME") == "cancelled":
        return "cancelled", "scan_cancelled", None
    if env.get("SCAN_OUTCOME") != "success" or env.get("SCAN_RC", "0") != "0":
        state = "blocked_quota" if QUOTA.search((output or "")[-4000:]) else "failed_scan"
        return state, "quota_exhausted" if state == "blocked_quota" else "scan_incomplete", None
    if env.get("SCAN_WITHHELD") == "true":
        return "failed_output", "output_withheld", None
    if lane == "code":
        verdict = final_code_turn(output or "")
        if QUOTA.search(verdict) and re.match(r"(?:you have|codex usage|quota|error:|insufficient_quota|rate_limit_exceeded)", verdict, re.I):
            return "blocked_quota", "quota_exhausted", None
        if not verdict:
            return "failed_output", "missing_model_verdict", None
        return "completed", "verdict_produced", None
    try:
        data = json.loads(output or "")
    except (ValueError, TypeError):
        return "failed_output", "missing_or_invalid_findings", None
    findings = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(findings, list) or any(not isinstance(f, dict) for f in findings):
        return "failed_output", "invalid_findings_shape", None
    return "completed", "findings_validated", len(findings)


def redact_directory(directory: Path, auth_file: Path) -> bool:
    """Redact a file/directory; withhold artifacts if credential fragments survive."""
    auth = json.loads(auth_file.read_text(encoding="utf-8"))
    values: list[str] = []

    def collect(value):
        if isinstance(value, str) and len(value) >= 20:
            values.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)

    collect(auth.get("tokens", {}))
    if not values:
        raise ValueError("credential redaction has no token values")
    import codecs
    variants = {v for s in values for v in (s, s[::-1], s.encode().hex(),
                base64.b64encode(s.encode()).decode(), base64.urlsafe_b64encode(s.encode()).decode(),
                codecs.encode(s, "rot_13"))}
    withheld = False
    for path in ([directory] if directory.is_file() else directory.rglob("*")):
        if path.is_symlink():
            withheld = True
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for value in variants:
            text = text.replace(value, "[REDACTED-CREDENTIAL]")
        normalized = re.sub(r"[^A-Za-z0-9]", "", text)
        fragmented = any(normal[i:i + 40] in normalized
                         for value in variants
                         for normal in [re.sub(r"[^A-Za-z0-9]", "", value)]
                         for i in range(0, max(0, len(normal) - 39), 8))
        if fragmented or re.search(r"[A-Za-z0-9_+\-.=/]{300,}", text):
            withheld = True
            text = "[output withheld: possible credential material]"
        path.write_text(text, encoding="utf-8")
    return withheld


def build_receipt(lane: str, env: dict[str, str], output: str | None) -> dict:
    """Only runner-generated metadata belongs in this receipt, never model text."""
    state, reason, findings = classify(lane, env, output)
    repo = env.get("GITHUB_REPOSITORY", "")
    head, base = env.get("HEAD_SHA", ""), env.get("BASE_SHA", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository identity")
    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", sha) for sha in (head, base)):
        raise ValueError("full base/head SHAs are required")
    pr = int(env.get("PR_NUM", "0"))
    if pr <= 0:
        raise ValueError("positive PR number is required")
    return {"schema_version": 1, "repository": repo, "pull_request": pr,
            "base_sha": base.lower(), "head_sha": head.lower(), "lane": lane,
            "state": state, "reason": reason, "findings": findings,
            "billing_mode": "subscription", "run_url": env.get("RUN_URL", "")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", choices=["code", "security"])
    ap.add_argument("--input", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--classify-paths", type=Path, help="NUL-delimited git paths")
    ap.add_argument("--redact-directory", "--redact-path", type=Path)
    ap.add_argument("--auth-file", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.redact_directory:
        if args.dry_run:
            print("Would redact review artifacts before credential cleanup")
            return 0
        if not args.auth_file:
            ap.error("--auth-file is required for redaction")
        withheld = redact_directory(args.redact_directory, args.auth_file)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write("withheld=" + str(withheld).lower() + "\n")
        return 0
    if args.classify_paths:
        paths = [p for p in args.classify_paths.read_text().split("\0") if p]
        print("security_required=" + str(security_required(paths)).lower())
        return 0
    if not args.lane or not args.output:
        ap.error("--lane and --output are required")
    raw = args.input.read_text(encoding="utf-8", errors="replace") if args.input and args.input.exists() else None
    receipt = build_receipt(args.lane, dict(os.environ), raw)
    if not args.dry_run:
        args.output.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write(f"review_state={receipt['state']}\nreview_reason={receipt['reason']}\nreview_head={receipt['head_sha']}\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
