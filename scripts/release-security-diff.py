#!/usr/bin/env python3
"""Summarise security-relevant workflow changes between two git refs.

Consumers run this pack's reusable workflows and actions with their own
secrets, and a pack update is usually approved by one person. Reading a large
diff by eye does not scale, so each release and each pack PR gets a short
report of what changed in the places that matter:

  * triggers (``on:``)                  * ``permissions:`` (workflow and job level)
  * secrets referenced                  * external ``uses:`` targets and pins
  * expressions inside ``run:`` scripts that reference ``github.event.*``,
    ``github.head_ref`` or ``inputs.*`` anywhere (including inside functions)
  * network hosts named in ``run:`` scripts

It is a report, not a gate: exit status is 0 unless git or YAML parsing fails.

Usage:
    python3 scripts/release-security-diff.py --base v1.25.1 --head HEAD
    python3 scripts/release-security-diff.py --base "$BASE_SHA" --summary "$GITHUB_STEP_SUMMARY"
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

SECRET_RE = re.compile(r"\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)")
ANY_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
# Contexts an outside actor can influence. Matched anywhere inside an expression,
# so function-wrapped uses (`toJSON(github.event.issue.title)`,
# `format('{0}', inputs.cmd)`) are reported too.
UNTRUSTED_REF_RE = re.compile(
    r"(?<![\w.])(?:github\.event\.[\w.\[\]'*-]+|github\.head_ref|inputs\.[\w-]+)")
WS_RE = re.compile(r"\s+")
HOST_RE = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
WATCHED = re.compile(r"^\.github/(workflows/[^/]+\.ya?ml|actions/.+/action\.ya?ml)$")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def changed_files(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    out = git(repo, "diff", "--name-status", "--no-renames", f"{base}", f"{head}")
    rows = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        if WATCHED.match(path):
            rows.append((status[:1], path))
    return rows


def show(repo: Path, ref: str, path: str) -> str | None:
    try:
        return git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


def _walk_steps(doc: dict):
    for job_id, job in (doc.get("jobs") or {}).items():
        if isinstance(job, dict):
            for step in job.get("steps") or []:
                if isinstance(step, dict):
                    yield job_id, step
    runs = doc.get("runs")  # composite action
    if isinstance(runs, dict):
        for step in runs.get("steps") or []:
            if isinstance(step, dict):
                yield "(action)", step


def facts(text: str | None) -> dict[str, set[str]]:
    empty = {k: set() for k in ("triggers", "permissions", "secrets", "uses",
                                "run_expressions", "hosts")}
    if text is None:
        return empty
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        return empty
    f = empty
    on = doc.get("on", doc.get(True))  # PyYAML parses a bare `on:` key as True
    if isinstance(on, dict):
        # Include each trigger's configuration: widening
        # `pull_request_target.branches` or `workflow_run.workflows` changes
        # when privileged code runs even though the event name is unchanged.
        f["triggers"] = {
            str(k) if v in (None, {}, []) else
            f"{k}: {json.dumps(v, sort_keys=True, default=str)}"
            for k, v in on.items()
        }
    elif isinstance(on, list):
        f["triggers"] = {str(k) for k in on}
    elif on:
        f["triggers"] = {str(on)}

    def perms(scope: str, value) -> None:
        if isinstance(value, dict):
            f["permissions"] |= {f"{scope}: {k}={v}" for k, v in value.items()}
        elif value is not None:
            f["permissions"].add(f"{scope}: {value}")

    perms("workflow", doc.get("permissions"))
    for job_id, job in (doc.get("jobs") or {}).items():
        if isinstance(job, dict):
            perms(f"job {job_id}", job.get("permissions"))
            if job.get("uses"):
                f["uses"].add(str(job["uses"]))
    # Only `secrets.X` inside `${{ }}` is a GitHub secret; `secrets.append(...)`
    # in an embedded script is not.
    f["secrets"] = {name for expr in ANY_EXPR_RE.findall(text)
                    for name in SECRET_RE.findall(expr)}
    for job_id, step in _walk_steps(doc):
        if step.get("uses"):
            f["uses"].add(str(step["uses"]))
        run = step.get("run")
        if isinstance(run, str):
            for expr in ANY_EXPR_RE.findall(run):
                if UNTRUSTED_REF_RE.search(expr):
                    f["run_expressions"].add(f"{job_id}: {WS_RE.sub(' ', expr).strip()}")
            f["hosts"] |= set(HOST_RE.findall(run))
    f["uses"] = {u for u in f["uses"] if not u.startswith("./")}
    return f


LABELS = {
    "triggers": "Triggers",
    "permissions": "Permissions",
    "secrets": "Secrets referenced",
    "uses": "External uses",
    "run_expressions": "Event/input expressions in run:",
    "hosts": "Hosts in run:",
}


def report(repo: Path, base: str, head: str) -> str:
    lines = [f"## Workflow security diff `{base}` → `{head}`", ""]
    rows = changed_files(repo, base, head)
    if not rows:
        return "\n".join(lines + ["No workflow or action files changed."]) + "\n"
    any_change = False
    for status, path in rows:
        before, after = facts(show(repo, base, path)), facts(show(repo, head, path))
        section = []
        for key, label in LABELS.items():
            added, removed = sorted(after[key] - before[key]), sorted(before[key] - after[key])
            if added or removed:
                section.append(f"- **{label}**")
                section += [f"  - ➕ `{x}`" for x in added]
                section += [f"  - ➖ `{x}`" for x in removed]
        if section:
            any_change = True
            kind = {"A": "added", "D": "deleted", "M": "modified"}.get(status, status)
            lines += [f"### `{path}` ({kind})", *section, ""]
    if not any_change:
        lines.append(f"{len(rows)} workflow/action file(s) changed; no security-relevant facts changed.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--summary", type=Path, help="Append the report to this file too")
    args = ap.parse_args(argv)
    text = report(args.repo, args.base, args.head)
    sys.stdout.write(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
