#!/usr/bin/env python3
"""Portable Delivery OS F1/F2/F8 lints for GitHub Actions workflows.

Rules (no product IDs):
- If ``on.pull_request.types`` is an explicit list, it must include
  ``ready_for_review``.
- A pull_request workflow that runs ``pnpm dev`` (or ``npm run dev``) must
  also wait on Preview-Ready / ``deployment_status`` / a ``preview-ready`` job.
- A job whose id or name looks readonly must not ``needs`` a lock-holding
  job (id/name contains ``e2e-preview`` or ``mutating``).

Usage:
  python3 scripts/lint_pull_request_types.py [--root ROOT] [--paths FILE ...]

Exit 0 clean; 1 on findings; 2 on parse/environment error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def _on_block(doc: dict):
    if not isinstance(doc, dict):
        return None
    on_trigger = doc.get("on")
    if on_trigger is None:
        on_trigger = doc.get(True)
    return on_trigger


def _pr_types(on_trigger) -> list[str] | None:
    """Return explicit types list, or None if types are implicit."""
    if on_trigger == "pull_request":
        return None
    if isinstance(on_trigger, list):
        return None
    if not isinstance(on_trigger, dict):
        return None
    pr = on_trigger.get("pull_request")
    if pr is None or pr is True:
        return None
    if isinstance(pr, dict) and "types" in pr:
        types = pr.get("types") or []
        if isinstance(types, str):
            return [types]
        if isinstance(types, list):
            return [str(t) for t in types]
    return None


def _workflow_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def lint_file(path: Path, root: Path) -> list[str]:
    findings: list[str] = []
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    text = _workflow_text(path)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"{rel}: YAML parse error: {exc}"]
    if not isinstance(doc, dict):
        return findings

    types = _pr_types(_on_block(doc))
    if types is not None and "ready_for_review" not in types:
        findings.append(
            f"{rel}: on.pull_request.types is explicit but missing ready_for_review"
        )

    on_trigger = _on_block(doc)
    is_pr = False
    has_deployment_status = False
    if on_trigger == "pull_request" or (
        isinstance(on_trigger, list) and "pull_request" in on_trigger
    ):
        is_pr = True
    if isinstance(on_trigger, dict):
        if "pull_request" in on_trigger:
            is_pr = True
        if "deployment_status" in on_trigger:
            has_deployment_status = True

    jobs = doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}
    job_ids = list(jobs)
    lock_jobs = {
        jid
        for jid, job in jobs.items()
        if isinstance(job, dict)
        and (
            "e2e-preview" in jid
            or "mutating" in jid
            or "e2e-preview" in str(job.get("name") or "")
            or "mutating" in str(job.get("name") or "").lower()
        )
    }
    has_preview_ready_job = any(
        "preview-ready" in jid or "preview_ready" in jid or "preview-ready" in str((job or {}).get("name") or "").lower()
        for jid, job in jobs.items()
        if isinstance(job, dict)
    )

    if is_pr and ("pnpm dev" in text or "npm run dev" in text):
        if not (has_deployment_status or has_preview_ready_job or "Preview-Ready" in text):
            findings.append(
                f"{rel}: PR workflow installs a local stack (`pnpm dev`/`npm run dev`) "
                "without Preview-Ready / deployment_status / a preview-ready job"
            )

    for jid, job in jobs.items():
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "")
        readonly_like = "readonly" in jid.lower() or "readonly" in name.lower() or "read-only" in name.lower()
        if not readonly_like:
            continue
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list):
            continue
        for dep in needs:
            dep_s = str(dep)
            if dep_s in lock_jobs or "e2e-preview" in dep_s or "mutating" in dep_s:
                findings.append(
                    f"{rel}: readonly job `{jid}` must not needs lock-holding job `{dep_s}`"
                )
    _ = job_ids
    return findings


def iter_workflows(root: Path, paths: list[str] | None) -> list[Path]:
    if paths:
        out = []
        for p in paths:
            fp = Path(p)
            if not fp.is_absolute():
                fp = root / p
            if fp.exists():
                out.append(fp)
        return out
    wf = root / ".github" / "workflows"
    if not wf.is_dir():
        return []
    return sorted(wf.glob("*.yml")) + sorted(wf.glob("*.yaml"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--paths", nargs="*", default=None)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        files = iter_workflows(root, args.paths)
    except OSError as exc:
        print(f"lint_pull_request_types: {exc}", file=sys.stderr)
        return 2
    findings: list[str] = []
    for path in files:
        findings.extend(lint_file(path, root))
    for line in findings:
        print(line, file=sys.stderr)
    if findings:
        print(f"lint_pull_request_types: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
