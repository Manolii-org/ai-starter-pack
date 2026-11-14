#!/usr/bin/env python3
"""Parallel source qualification; model assessments are optional audit, never authority.

The collector only reads authenticated native APIs. The producer records an existing
job's exact source/base/tested tree after its checks; it executes no test or deploy.
This candidate never authorizes merge or changes an installed required context.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import hashlib
import fnmatch
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
import zipfile

SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SCHEMA = "source-qualification/v1"
MAX_BYTES = 16 * 1024 * 1024
BOTS = {"coderabbitai[bot]", "chatgpt-codex-connector[bot]"}


class QualificationError(ValueError):
    pass


def strict_json(raw):
    if (not isinstance(raw, (str, bytes, bytearray))
            or len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_BYTES):
        raise QualificationError("native JSON input exceeds its bound")
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise QualificationError("duplicate qualification field")
            out[key] = value
        return out
    def constant(_):
        raise QualificationError("nonfinite native JSON value")
    def finite_float(raw_number):
        value = float(raw_number)
        if not math.isfinite(value):
            raise QualificationError("nonfinite native JSON value")
        return value
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None and value != "0" * 40


def owning_repository(value):
    return (isinstance(value, str) and REPO.fullmatch(value) is not None
            and all(part not in {".", ".."} for part in value.split("/")))


def policy(value):
    keys = {"schema_version", "model_assessment", "required_checks", "optional_model_checks", "review_publishers", "security_scope"}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["model_assessment"] != "optional"):
        raise QualificationError("unsupported source qualification policy")
    publishers = value["review_publishers"]
    if (not isinstance(publishers, list) or not publishers
            or any(not isinstance(p, dict) or set(p) != {"login", "user_id"}
                   or p["login"] not in BOTS or type(p["user_id"]) is not int or p["user_id"] <= 0
                   for p in publishers)
            or len({p["login"] for p in publishers}) != len(publishers)
            or len({p["user_id"] for p in publishers}) != len(publishers)):
        raise QualificationError("actual independent publisher identities must be compiled")
    validate_scope(value["security_scope"])
    seen, namespaces = set(), set()
    for kind in ("required_checks", "optional_model_checks"):
        if not isinstance(value[kind], list):
            raise QualificationError("native check inventory must be explicit")
        for item in value[kind]:
            if (not isinstance(item, dict)
                    or set(item) != {"name", "app_id", "workflow_path", "workflow_sha256", "job_key", "artifact_key", "role", "steps"}
                    or not isinstance(item["name"], str) or not item["name"].strip()
                    or type(item["app_id"]) is not int or item["app_id"] <= 0
                    or not isinstance(item["workflow_path"], str)
                    or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", item["workflow_path"])
                    or not isinstance(item["workflow_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["workflow_sha256"])
                    or not isinstance(item["job_key"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", item["job_key"])
                    or not isinstance(item["steps"], list) or not item["steps"]
                    or any(not isinstance(step, str) or not step.strip() for step in item["steps"])
                    or len(set(item["steps"])) != len(item["steps"])
                    or not isinstance(item["artifact_key"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["artifact_key"])
                    or item["role"] not in ({"classify", "deterministic", "security"} if kind == "required_checks" else {"model"})
                    or (item["name"], item["app_id"]) in seen):
                raise QualificationError("invalid or duplicate native check identity")
            seen.add((item["name"], item["app_id"]))
            if kind == "required_checks":
                if item["artifact_key"] in namespaces:
                    raise QualificationError("each native job/matrix check needs a unique artifact namespace")
                namespaces.add(item["artifact_key"])
    if not {"classify", "deterministic", "security"}.issubset({c["role"] for c in value["required_checks"]}):
        raise QualificationError("classification, deterministic and full security checks are required")
    return value



def relative_source(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or value.startswith("/") or any(c in {"", ".", ".."} for c in value.split("/"))):
        raise QualificationError("canonical owner source path required")
    return value


def validate_scope(value):
    if not isinstance(value, dict) or set(value) != {"semgrep", "bandit"}:
        raise QualificationError("reviewed scanner scope is required")
    for scanner, scope in value.items():
        if (not isinstance(scope, dict) or set(scope) != {"include", "exclude"}
                or not isinstance(scope["include"], list) or not scope["include"]
                or not isinstance(scope["exclude"], list)):
            raise QualificationError("explicit scanner includes and exclusions required")
        for pattern in scope["include"]:
            relative_source(pattern)
        for exclusion in scope["exclude"]:
            if (not isinstance(exclusion, dict) or set(exclusion) != {"path", "reason"}
                    or not isinstance(exclusion["reason"], str) or not exclusion["reason"].strip()):
                raise QualificationError("each reviewed scanner exclusion needs a reason")
            relative_source(exclusion["path"])
        if len(set(scope["include"])) != len(scope["include"]):
            raise QualificationError("duplicate scanner include")
        if scanner == "bandit" and scope["include"] != ["*.py"]:
            raise QualificationError("Bandit must inventory all tracked Python sources")
    return value


def source_inventory(entries, scope):
    validate_scope(scope)
    if not isinstance(entries, list) or not entries or len(entries) > 100000:
        raise QualificationError("complete bounded source tree inventory required")
    names, seen = [], set()
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get("mode") not in {"100644", "100755", "120000"}
                or entry.get("type") != "blob" or not sha(entry.get("sha"))):
            raise QualificationError("source inventory needs supported exact blob identities")
        name = relative_source(entry.get("path"))
        if name in seen:
            raise QualificationError("duplicate source tree path")
        seen.add(name)
        names.append(name)
    result = {}
    for scanner, rules in scope.items():
        selected = sorted(name for name in names
                          if any(fnmatch.fnmatchcase(name, pattern) for pattern in rules["include"])
                          and not any(fnmatch.fnmatchcase(name, exclusion["path"])
                                      for exclusion in rules["exclude"]))
        if not selected:
            raise QualificationError("reviewed scanner scope selected no sources")
        # Scanners cannot qualify a link merely by hashing its text. A separately
        # reviewed adapter is required before a link can enter their source scope.
        if any(entry["mode"] == "120000" and entry["path"] in selected for entry in entries):
            raise QualificationError("scanner scope contains a symlink without a raw-input adapter")
        result[scanner] = selected
    return result


def checked_source_tree(git_read, tested):
    """Observe clean physical checkout bytes without Git index/stat/filter reuse.

    No ignored dependency or external symlink targets are read. This is bounded
    repeated observation after the existing job, not an atomic filesystem snapshot.
    """
    raw = git_read("ls-tree", "-r", "-z", "--full-tree", tested)
    entries = []
    for row in raw.split("\0"):
        if not row:
            continue
        metadata, name = row.split("\t", 1)
        mode, kind, identity = metadata.split()
        entries.append({"mode": mode, "type": kind, "sha": identity, "path": name})
    if not entries or len(entries) > 100000:
        raise QualificationError("complete bounded checkout required")
    root = Path(git_read("rev-parse", "--show-toplevel"))
    if not root.is_absolute() or root.resolve() != root:
        raise QualificationError("canonical owning checkout required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    total = 0
    try:
        for item in entries:
            name = relative_source(item["path"])
            if item["mode"] not in {"100644", "100755", "120000"} or item["type"] != "blob" or not sha(item["sha"]):
                raise QualificationError("unsupported checkout entry requires a raw-input adapter")
            directory = os.dup(root_fd)
            try:
                parts = name.split("/")
                for parent in parts[:-1]:
                    following = os.open(parent, flags, dir_fd=directory)
                    os.close(directory)
                    directory = following
                details = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if item["mode"] == "120000":
                    if not stat.S_ISLNK(details.st_mode):
                        raise QualificationError("tracked source kind changed")
                    content = os.fsencode(os.readlink(parts[-1], dir_fd=directory))
                else:
                    if not stat.S_ISREG(details.st_mode) or details.st_size > 64 * 1024 * 1024:
                        raise QualificationError("unsupported or oversized tracked source")
                    mode = "100755" if details.st_mode & 0o100 else "100644"
                    if mode != item["mode"]:
                        raise QualificationError("tracked source mode changed")
                    descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    with os.fdopen(descriptor, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        content = stream.read(64 * 1024 * 1024 + 1)
                        after = os.fstat(stream.fileno())
                    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                            or (details.st_dev, details.st_ino) != (after.st_dev, after.st_ino)):
                        raise QualificationError("tracked source changed during observation")
                total += len(content)
                if total > 512 * 1024 * 1024:
                    raise QualificationError("tracked source observation exceeds bound")
                observed = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
                if observed != item["sha"]:
                    raise QualificationError("working source bytes differ from immutable tested tree")
            finally:
                os.close(directory)
    finally:
        os.close(root_fd)
    # Index changes/additions/removals and ordinary untracked nonignored inputs
    # also make the native checkout ambiguous. No diff/textconv/filter is run.
    if git_read("status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none"):
        raise QualificationError("native tested checkout is not clean")
    return entries


def report_paths(data, scanner):
    values = data["paths"]["scanned"] if scanner == "semgrep" else [p for p in data["metrics"] if p != "_totals"]
    result = []
    for path in values:
        if isinstance(path, str) and path.startswith("./"):
            path = path[2:]
        result.append(relative_source(path))
    if len(result) != len(set(result)):
        raise QualificationError("duplicate scanner source path")
    return sorted(result)


def producing_job(api, repo, head, run_id, attempt, required, *, tested_sha=None):
    """Read the actual in-progress native job/check, never invent IDs from env."""
    if (not owning_repository(repo) or not sha(head) or not sha(tested_sha)
            or type(run_id) is not int or run_id <= 0
            or type(attempt) is not int or attempt <= 0):
        raise QualificationError("trusted tested producer source required")
    run = api.get(f"repos/{repo}/actions/runs/{run_id}")
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != run_id
            or run.get("head_sha") != head or run.get("event") != "pull_request"
            or run.get("path") != required["workflow_path"]
            or hashlib.sha256(immutable_file(api, repo, required["workflow_path"], tested_sha)).hexdigest()
            != required["workflow_sha256"]):
        raise QualificationError("trusted native producer workflow source unavailable")
    checks = api.pages(f"repos/{repo}/commits/{head}/check-runs?filter=all", "check_runs")
    matches = []
    prefix = f"https://github.com/{repo}/actions/runs/{run_id}/job/"
    for check in checks:
        if (not native_identity(check, required) or check.get("head_sha") != head
                or type(check.get("id")) is not int or check["id"] <= 0
                or check.get("status") != "in_progress"):
            continue
        match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)", check.get("details_url", ""))
        if not match:
            continue
        job = api.get(f"repos/{repo}/actions/jobs/{match[1]}")
        if (isinstance(job, dict) and type(job.get("run_id")) is int and job["run_id"] == run_id
                and type(job.get("run_attempt")) is int and job["run_attempt"] == attempt
                and job.get("status") == "in_progress" and job.get("conclusion") is None
                and job.get("name") == required["name"]
                and type(job.get("id")) is int and job["id"] == int(match[1])
                and job.get("check_run_url") == f"https://api.github.com/repos/{repo}/check-runs/{check['id']}"):
            matches.append({"native_job_id": job["id"], "native_check_id": check["id"]})
    if len(matches) != 1:
        raise QualificationError("unique actual native producing job/check unavailable")
    return matches[0]


def scanner_report(data, code, scanner):
    # Retain the current PR Assessment report completeness rules, including
    # warnings, skipped rules and nonzero exits without findings.
    if (type(code) is not int or code not in (0, 1) or not isinstance(data, dict)
            or not isinstance(data.get("results"), list) or data.get("errors") != []
            or code == 1 and not data["results"]):
        raise QualificationError("missing, unsuccessful or incomplete scanner report")
    if scanner == "semgrep":
        scanned = data.get("paths", {}).get("scanned") if isinstance(data.get("paths"), dict) else None
        if (data.get("version") != "1.185.0" or not isinstance(scanned, list) or not scanned
                or any(not isinstance(p, str) or not p.strip() for p in scanned)
                or data.get("skipped_rules", []) != [] or data["paths"].get("skipped", []) != []):
            raise QualificationError("incomplete Semgrep version, rule or file scope")
        for row in data["results"]:
            try:
                fields = (row["check_id"], row["path"], row["extra"]["message"], row["extra"]["severity"])
                line = row["start"]["line"]
            except (TypeError, KeyError):
                raise QualificationError("malformed scanner finding") from None
            if not all(isinstance(v, str) and v for v in fields) or type(line) is not int or line < 1:
                raise QualificationError("malformed scanner finding")
    elif scanner == "bandit":
        metrics = data.get("metrics")
        if (not isinstance(metrics, dict) or "_totals" not in metrics
                or not any(k != "_totals" for k in metrics)
                or any(not isinstance(k, str) or not k.strip() or not isinstance(v, dict)
                       or type(v.get("loc")) is not int or v["loc"] < 0 for k, v in metrics.items())
                or metrics["_totals"]["loc"] != sum(v["loc"] for k, v in metrics.items() if k != "_totals")):
            raise QualificationError("incomplete Bandit file scope")
        for row in data["results"]:
            try:
                fields = (row["test_id"], row["filename"], row["issue_text"], row["issue_severity"])
                line = row["line_number"]
            except (TypeError, KeyError):
                raise QualificationError("malformed scanner finding") from None
            if not all(isinstance(v, str) and v for v in fields) or type(line) is not int or line < 1:
                raise QualificationError("malformed scanner finding")
    else:
        raise QualificationError("unsupported scanner")
    return {"status": "executed", "report_sha256": digest(data), "finding_count": len(data["results"])}


def validate_receipt(value, context, required, check):
    fields = {"schema", "repository", "head_sha", "base_sha", "tested_sha", "tested_tree", "run_id",
              "run_attempt", "workflow_sha", "job_key", "artifact_key", "role", "result", "security",
              "native_job_id", "native_check_id", "security_scope_sha256"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != SCHEMA:
        raise QualificationError("missing or unsupported source-bound job receipt")
    if value["workflow_sha"] != context["tested_sha"]:
        raise QualificationError("native workflow source differs from exact tested candidate")
    for key in ("repository", "head_sha", "base_sha", "tested_sha", "tested_tree"):
        if value[key] != context[key]:
            raise QualificationError("qualification source, current base or tested tree drift")
    if (value["run_id"] != check["workflow_run"]["id"]
            or type(value["run_id"]) is not int or type(value["run_attempt"]) is not int
            or value["run_attempt"] != check["workflow_run"]["run_attempt"]
            or value["job_key"] != required["job_key"] or value["artifact_key"] != required["artifact_key"]
            or value["role"] != required["role"]
            or value["result"] != "executed_pass"
            or type(value["native_job_id"]) is not int or value["native_job_id"] != check["native_job"]["id"]
            or type(value["native_check_id"]) is not int or value["native_check_id"] != check["id"]):
        raise QualificationError("qualification producer or execution identity drift")
    if required["role"] == "security":
        if (not isinstance(value["security_scope_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["security_scope_sha256"])
                or value["security_scope_sha256"] != context.get("security_scope_sha256")):
            raise QualificationError("reviewed security inventory or exclusions drift")
        security = value["security"]
        if not isinstance(security, dict) or set(security) != {"semgrep", "bandit"}:
            raise QualificationError("full security coverage missing")
        for item in security.values():
            if (not isinstance(item, dict) or set(item) != {"status", "report_sha256", "finding_count"}
                    or item["status"] != "executed" or type(item["finding_count"]) is not int
                    or item["finding_count"] != 0 or not isinstance(item["report_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["report_sha256"])):
                raise QualificationError("security scan missing, skipped, incomplete or has findings")
    elif value["security"] is not None or value["security_scope_sha256"] is not None:
        raise QualificationError("unexpected security evidence on another job")


def native_identity(check, required):
    return (isinstance(check, dict) and check.get("name") == required["name"]
            and isinstance(check.get("app"), dict)
            and type(check["app"].get("id")) is int and check["app"]["id"] == required["app_id"])


def select_native(checks, required, context):
    matches = [c for c in checks if native_identity(c, required)]
    if not matches or any(type(c.get("id")) is not int or c["id"] <= 0 for c in matches):
        raise QualificationError("required native check missing or malformed")
    check = max(matches, key=lambda c: c["id"])
    run = check.get("workflow_run", {})
    if not isinstance(run, dict):
        raise QualificationError("native workflow identity unavailable")
    if (check.get("head_sha") != context["head_sha"]
            or check.get("app", {}).get("id") != required["app_id"]
            or check.get("status") != "completed" or check.get("conclusion") != "success"
            or run.get("path") != required["workflow_path"]
            or run.get("observed_workflow_sha256") != required["workflow_sha256"]
            or run.get("head_sha") != context["head_sha"] or run.get("event") != "pull_request"
            or run.get("source_base_sha") != context["base_sha"]
            or type(run.get("id")) is not int or run["id"] <= 0
            or type(run.get("run_attempt")) is not int or run["run_attempt"] <= 0):
        raise QualificationError("required native check identity, current base or executed success missing")
    job = check.get("native_job", {})
    if (not isinstance(job, dict) or job.get("status") != "completed" or job.get("conclusion") != "success" or not isinstance(job.get("steps"), list)
            or type(job.get("id")) is not int or job["id"] <= 0
            or job.get("check_run_url") != f"https://api.github.com/repos/{context['repository']}/check-runs/{check['id']}"
            or type(job.get("run_id")) is not int or job["run_id"] != run["id"]
            or type(job.get("run_attempt")) is not int or job["run_attempt"] != run["run_attempt"]):
        raise QualificationError("native required job did not complete")
    for name in required["steps"]:
        selected = [step for step in job["steps"] if isinstance(step, dict) and step.get("name") == name]
        if len(selected) != 1 or selected[0].get("status") != "completed" or selected[0].get("conclusion") != "success":
            raise QualificationError("required native recipe or scanner step skipped, failed or missing")
    validate_receipt(check.get("qualification_receipt"), context, required, check)
    return check


def review_module():
    path = Path(__file__).with_name("review_evidence.py")
    spec = importlib.util.spec_from_file_location("source_qualification_review_evidence", path)
    result = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), "exec"), result.__dict__)
    return result



def review_submission(entry):
    """Require an actual timezone-bearing native submission, never infer from ID."""
    value = entry.get("submitted_at")
    if (not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
            r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value)
            or value.endswith("-00:00")):
        raise QualificationError("actual native review submission timestamp unavailable")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise QualificationError("invalid native review submission timestamp") from None


def effective_current_reviews(reviews, head):
    """Current native opinion per reviewer, ordered by submission rather than ID.

    An ID identifies a record created earlier, including a draft; it does not
    certify when the opinion was submitted. Unknown chronology and ambiguous
    latest decisions hold. A strictly later submitted decision supersedes older
    opinions without requiring their dismissal or creating another human task.
    """
    latest, seen = {}, {}
    for entry in reviews:
        if not isinstance(entry, dict):
            raise QualificationError("complete native review records required")
        if not sha(entry.get("commit_id")):
            raise QualificationError("actual native review source unavailable")
        if type(entry.get("id")) is not int or entry["id"] <= 0:
            raise QualificationError("actual native review record identity unavailable")
        # Validate one coherent snapshot before filtering ANY disposition or
        # source. Otherwise a nondispositive edit of this same native record
        # could conceal conflicting active facts. Canonical hashing also keeps
        # boolean/float aliases distinct from native integer fields.
        fingerprint = digest(entry)
        if entry["id"] in seen:
            if seen[entry["id"]] != fingerprint:
                raise QualificationError("conflicting duplicate native review records")
            continue
        seen[entry["id"]] = fingerprint
        if entry.get("commit_id") != head:
            continue
        state = entry.get("state")
        if (not isinstance(state, str)
                or state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED", "PENDING"}):
            raise QualificationError("actual native current-head review state unavailable")
        # A dismissed record contributes no current opinion. It cannot dismiss
        # a different still-active request from the same reviewer. When the
        # actual requesting record is dismissed, its state itself is DISMISSED.
        if state in {"COMMENTED", "PENDING", "DISMISSED"}:
            continue
        user = entry.get("user")
        if (not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0
                or not isinstance(user.get("login"), str) or not user["login"]
                or user.get("type") not in {"Bot", "User"}):
            raise QualificationError("complete native current-head review identity unavailable")
        instant = review_submission(entry)
        identity = user["id"]
        if identity not in latest or instant > latest[identity][0]:
            latest[identity] = (instant, [entry])
        elif instant == latest[identity][0]:
            latest[identity][1].append(entry)
    if any(len(entries) != 1 for _, entries in latest.values()):
        raise QualificationError("ambiguous native current-head review submission order")
    return [entries[0] for _, entries in latest.values()]


def independent_review(reviews, comments, context, reviewer, publishers):
    author = context["author"]
    identities = {p["login"]: p["user_id"] for p in publishers}
    def trusted(entry):
        user = entry.get("user") or {}
        return (user.get("login") in identities and user.get("type") == "Bot"
                and type(user.get("id")) is int and user["id"] == identities[user["login"]]
                and user["id"] != context["author_id"] and user["login"] != author)
    # Historical old-head opinions are audit; all unresolved threads are
    # independently checked by the native collector, across the whole PR.
    effective = effective_current_reviews(reviews, context["head_sha"])
    if any(r["state"] == "CHANGES_REQUESTED" for r in effective):
        return False
    candidates = [r for r in effective if trusted(r)]
    if reviewer.bot_review_verdict(candidates, context["head_sha"]) == "satisfy":
        return True
    if any((c.get("user") or {}).get("login") in identities and not trusted(c) for c in comments):
        return False
    independent = [c for c in comments if trusted(c)]
    return (reviewer.coderabbit_clean_summary(independent, context["head_sha"], context["repository"], context["pr"])
            or reviewer.codex_clean_verdict(independent, context["head_sha"]) == "satisfy")


def qualify(compiled, context, checks, reviews, comments, *, unresolved_threads, reviewer):
    policy(compiled)
    if (not isinstance(context, dict) or not owning_repository(context.get("repository"))
            or type(context.get("pr")) is not int or context["pr"] <= 0
            or not isinstance(context.get("author"), str) or not context["author"]
            or type(context.get("author_id")) is not int or context["author_id"] <= 0
            or any(not sha(context.get(k)) for k in ("head_sha", "base_sha", "tested_sha", "tested_tree"))
            or any(not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
                   for items in (checks, reviews, comments))
            or type(unresolved_threads) is not int or unresolved_threads != 0):
        raise QualificationError("complete exact native context and resolved review threads required")
    qualified = [select_native(checks, item, context)["id"] for item in compiled["required_checks"]]
    if not independent_review(reviews, comments, context, reviewer, compiled["review_publishers"]):
        raise QualificationError("independent completed exact-head hosted review unavailable")
    audits = []
    for item in compiled["optional_model_checks"]:
        found = [c for c in checks if native_identity(c, item) and type(c.get("id")) is int]
        latest = max(found, key=lambda c: c["id"], default=None)
        audits.append({"name": item["name"], "observed": latest is not None,
                       "status": latest.get("status") if latest else None,
                       "conclusion": latest.get("conclusion") if latest else None})
    return {"schema": SCHEMA, "source_qualified": True, "head_sha": context["head_sha"],
            "base_sha": context["base_sha"], "tested_sha": context["tested_sha"],
            "policy_sha256": digest(compiled), "native_check_ids": qualified,
            "independent_hosted_review": True, "optional_model_audit": audits,
            "admission_authorized": False, "cutover": "held_pending_native_owner_acceptance",
            "installed_required_checks_changed": False}


class NativeAPI:
    """Reuse the existing authenticated read route; no local evidence-file intake."""
    def __init__(self, reviewer):
        self.reviewer = reviewer

    def get(self, path):
        return strict_json(self.reviewer.gh_read([path]))

    def pages(self, path, key=None):
        items, seen, total = [], set(), None
        for page in range(1, 31):
            separator = "&" if "?" in path else "?"
            value = self.get(path + separator + f"per_page=100&page={page}")
            if key is not None:
                count = value.get("total_count") if isinstance(value, dict) else None
                if type(count) is not int or count < 0 or total is not None and count != total:
                    raise QualificationError("native pagination count unavailable or changed")
                total = count
            batch = value if key is None else value.get(key)
            if not isinstance(batch, list) or len(batch) > 100:
                raise QualificationError("native pagination unavailable")
            for row in batch:
                if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
                    raise QualificationError("native page record identity unavailable")
                if row["id"] in seen:
                    raise QualificationError("duplicate native page record")
                seen.add(row["id"])
            items.extend(batch)
            if total is not None and len(items) > total:
                raise QualificationError("native pagination exceeds its declared count")
            if len(batch) < 100:
                if total is not None and len(items) != total:
                    raise QualificationError("native pagination incomplete")
                return items
        raise QualificationError("native pagination bound exceeded")

    def artifact(self, repo, run_id, name, *, expected_head=None):
        if (not owning_repository(repo) or type(run_id) is not int or run_id <= 0
                or not sha(expected_head)):
            raise QualificationError("exact native artifact run and source required")
        matches = [a for a in self.pages(f"repos/{repo}/actions/runs/{run_id}/artifacts", "artifacts")
                   if a.get("name") == name and a.get("expired") is False]
        if len(matches) != 1:
            raise QualificationError("exact source-bound native artifact missing or ambiguous")
        ident = matches[0]["id"]
        if (type(ident) is not int or ident <= 0 or type(matches[0].get("size_in_bytes")) is not int
                or not 0 < matches[0]["size_in_bytes"] <= MAX_BYTES):
            raise QualificationError("invalid native artifact identity or size")
        metadata = self.get(f"repos/{repo}/actions/artifacts/{ident}")
        # GitHub attests this RUN association, never the producing JOB. The
        # legacy live collector holds source authority even when these IDs agree.
        run = metadata.get("workflow_run") if isinstance(metadata, dict) else None
        if (not isinstance(metadata, dict) or type(metadata.get("id")) is not int
                or metadata["id"] != ident or metadata.get("name") != name
                or metadata.get("expired") is not False
                or type(metadata.get("size_in_bytes")) is not int
                or metadata["size_in_bytes"] != matches[0]["size_in_bytes"]
                or not isinstance(run, dict) or type(run.get("id")) is not int
                or run["id"] != run_id or run.get("head_sha") != expected_head):
            raise QualificationError("native artifact run/source binding unavailable")
        result = subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{ident}/zip"],
                                capture_output=True, timeout=30, check=True)
        if len(result.stdout) > MAX_BYTES:
            raise QualificationError("native artifact exceeds bound")
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
            if archive.namelist() != ["source-qualification.json"]:
                raise QualificationError("unexpected native artifact members")
            entry = archive.infolist()[0]
            kind = stat.S_IFMT(entry.external_attr >> 16)
            # Some native ZIP writers omit POSIX kind bits. Read those only as
            # JSON bytes; never extract. Explicit nonregular or DOS directory
            # members cannot claim to be the plain JSON receipt.
            if (entry.file_size > MAX_BYTES or kind not in {0, stat.S_IFREG}
                    or entry.is_dir() or entry.external_attr & 0x10):
                raise QualificationError("unsafe native artifact member")
            return strict_json(archive.read(entry))


def immutable_file(api, repo, path, revision):
    metadata = api.get(f"repos/{repo}/contents/{path}?ref={revision}")
    raw = base64.b64decode(metadata["content"], validate=False)
    if (len(raw) > MAX_BYTES or metadata.get("encoding") != "base64" or metadata.get("type") != "file"
            or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest() != metadata.get("sha")):
        raise QualificationError("immutable native source identity unavailable")
    return raw


def collect(api, reviewer, repo, pr, head, base, policy_path):
    if (not owning_repository(repo) or not sha(head) or not sha(base) or type(pr) is not int or pr <= 0
            or not re.fullmatch(r"config/[A-Za-z0-9_.-]+\.json", policy_path)):
        raise QualificationError("exact owning source inputs required")
    current = api.get(f"repos/{repo}/pulls/{pr}")
    if (current["head"]["sha"] != head or current["base"]["sha"] != base
            or current["base"]["repo"]["full_name"] != repo or not sha(current.get("merge_commit_sha"))):
        raise QualificationError("current native PR head/base or combined candidate drift")
    compiled = policy(strict_json(immutable_file(api, repo, policy_path, base)))
    tested = current["merge_commit_sha"]
    tree = api.get(f"repos/{repo}/git/commits/{tested}")["tree"]["sha"]
    inventory = api.get(f"repos/{repo}/git/trees/{tree}?recursive=1")
    if inventory.get("truncated") is not False:
        raise QualificationError("complete native source inventory unavailable")
    blobs = [row for row in inventory.get("tree", []) if row.get("type") != "tree"]
    scope = source_inventory(blobs, compiled["security_scope"])
    context = {"security_scope_sha256": digest({"scope": compiled["security_scope"], "inventory": scope}),
               "repository": repo, "pr": pr, "author": current["user"]["login"],
               "author_id": current["user"]["id"], "head_sha": head,
               "base_sha": base, "tested_sha": tested, "tested_tree": tree}
    checks = api.pages(f"repos/{repo}/commits/{head}/check-runs?filter=all", "check_runs")
    for item in compiled["required_checks"]:
        matches = [c for c in checks if native_identity(c, item)]
        if not matches:
            raise QualificationError("required native check unavailable")
        check = max(matches, key=lambda c: c["id"])
        prefix = f"https://github.com/{repo}/actions/runs/"
        match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)/job/([1-9][0-9]*)", check.get("details_url", ""))
        if not match:
            raise QualificationError("native check run/job binding unavailable")
        run_id, job_id = map(int, match.groups())
        run = api.get(f"repos/{repo}/actions/runs/{run_id}")
        job = api.get(f"repos/{repo}/actions/jobs/{job_id}")
        if (job.get("check_run_url") != f"https://api.github.com/repos/{repo}/check-runs/{check['id']}"
                or job.get("conclusion") != "success" or run.get("id") != run_id or job.get("id") != job_id):
            raise QualificationError("native job/check identity unavailable")
        if (job.get("run_id") != run_id or type(job.get("run_attempt")) is not int
                or job["run_attempt"] <= 0):
            raise QualificationError("exact native job attempt unavailable")
        # A partial rerun does not invalidate another job's original executed
        # result. Bind this native job's observed attempt, not the run's latest.
        run["run_attempt"] = job["run_attempt"]
        receipt = api.artifact(repo, run_id, "source-qualification-" + item["artifact_key"] + "-" + str(job["run_attempt"]),
                               expected_head=head)
        run["observed_workflow_sha256"] = hashlib.sha256(immutable_file(api, repo, item["workflow_path"], tested)).hexdigest()
        run["source_base_sha"] = receipt.get("base_sha")
        check["workflow_run"], check["qualification_receipt"], check["native_job"] = run, receipt, job
    reviews = api.pages(f"repos/{repo}/pulls/{pr}/reviews")
    comments = reviewer.fetch_comments(repo, pr, head)
    # Existing findings remain blockers, including old-head unresolved threads.
    query = """query($owner:String!,$name:String!,$pr:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$pr){reviewThreads(first:100,after:$cursor){nodes{isResolved} pageInfo{hasNextPage endCursor}}}}}"""
    owner, name = repo.split("/")
    unresolved, cursor, cursors = 0, None, set()
    for _ in range(30):
        value = strict_json(reviewer.gh_read(["graphql", "--input", "-"], json.dumps(
            {"query": query, "variables": {"owner": owner, "name": name, "pr": pr, "cursor": cursor}})))
        if value.get("errors"):
            raise QualificationError("review thread facts unavailable")
        page = value["data"]["repository"]["pullRequest"]["reviewThreads"]
        if (not isinstance(page["nodes"], list)
                or any(not isinstance(t, dict) or type(t.get("isResolved")) is not bool for t in page["nodes"])):
            raise QualificationError("incomplete review thread facts")
        unresolved += sum(not t["isResolved"] for t in page["nodes"])
        if page["pageInfo"]["hasNextPage"] is False:
            break
        cursor = page["pageInfo"]["endCursor"]
        if (page["pageInfo"]["hasNextPage"] is not True or not isinstance(cursor, str)
                or not cursor or cursor in cursors):
            raise QualificationError("review thread pagination unavailable")
        cursors.add(cursor)
    else:
        raise QualificationError("review thread pagination bound exceeded")
    # A second independent native read invalidates a head/base change during collection.
    after = api.get(f"repos/{repo}/pulls/{pr}")
    if after["head"]["sha"] != head or after["base"]["sha"] != base or after.get("merge_commit_sha") != tested:
        raise QualificationError("native source changed during qualification")
    result = qualify(compiled, context, checks, reviews, comments,
                     unresolved_threads=unresolved, reviewer=reviewer)
    # Native artifact APIs attest a RUN, not the producing JOB. Other jobs in
    # the same run can upload that namespace; receipt IDs are payload claims.
    # Keep this legacy intake useful as audit coherence while the independent
    # owner preparation/transport binding is qualified. Never expose it as an
    # authentic native source pass merely because its fields agree.
    return {**result, "candidate_coherent": True, "source_qualified": False,
            "native_transport_bound": False,
            "cutover": "held_native_component_artifact_origin_unqualified"}


def produce_receipt(env, event, role, git_read, *, artifact_key=None, semgrep=None, bandit=None,
                    semgrep_exit=None, bandit_exit=None, compiled=None, producer=None):
    """Capture native source topology and complete reports without models/secrets."""
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_EVENT_NAME") != "pull_request"
            or role not in {"classify", "deterministic", "security"}
            or not owning_repository(env.get("GITHUB_REPOSITORY"))
            or not isinstance(event, dict)):
        raise QualificationError("native owning PR context required")
    pr = event["pull_request"]
    head, base, tested = pr["head"]["sha"], pr["base"]["sha"], env.get("GITHUB_SHA")
    if (pr["base"]["repo"]["full_name"] != env["GITHUB_REPOSITORY"]
            or any(not sha(v) for v in (head, base, tested))
            or git_read("rev-parse", "HEAD") != tested or env.get("GITHUB_WORKFLOW_SHA") != tested):
        raise QualificationError("native checkout source identity mismatch")
    parents = git_read("show", "-s", "--format=%P", tested).split()
    if parents != [base, head]:
        # A source checkout is combined-state only when it already includes this base.
        if tested != head or git_read("merge-base", head, base) != base:
            raise QualificationError("checkout does not combine the exact source and current base")
    tree = git_read("rev-parse", tested + "^{tree}")
    if not sha(tree) or not re.fullmatch(r"[1-9][0-9]*", env.get("GITHUB_RUN_ID", "")) or not re.fullmatch(
            r"[1-9][0-9]*", env.get("GITHUB_RUN_ATTEMPT", "")) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", env.get("GITHUB_JOB", "")):
        raise QualificationError("native execution identity unavailable")
    artifact_key = env["GITHUB_JOB"] if artifact_key is None else artifact_key
    if not isinstance(artifact_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", artifact_key):
        raise QualificationError("reviewed artifact namespace required")
    entries = checked_source_tree(git_read, tested)
    compiled = policy(compiled)
    required = [item for item in compiled["required_checks"] if item["job_key"] == env["GITHUB_JOB"]
                and item["artifact_key"] == artifact_key and item["role"] == role]
    if len(required) != 1:
        raise QualificationError("reviewed native producer namespace unavailable")
    if (not isinstance(producer, dict) or set(producer) != {"native_job_id", "native_check_id"}
            or any(type(value) is not int or value <= 0 for value in producer.values())):
        raise QualificationError("actual native producing job/check identity required")
    security, scope_identity = None, None
    if role == "security":
        security = {"semgrep": scanner_report(semgrep, semgrep_exit, "semgrep"),
                    "bandit": scanner_report(bandit, bandit_exit, "bandit")}
        if semgrep_exit != 0 or bandit_exit != 0 or any(item["finding_count"] for item in security.values()):
            raise QualificationError("scanner findings or unsuccessful exit cannot produce passing evidence")
        inventory = source_inventory(entries, compiled["security_scope"])
        for scanner, report in (("semgrep", semgrep), ("bandit", bandit)):
            if report_paths(report, scanner) != inventory[scanner]:
                raise QualificationError("scanner did not execute the complete reviewed relevant source inventory")
        scope_identity = digest({"scope": compiled["security_scope"], "inventory": inventory})
    # Reobserve after report processing; observable changes invalidate publication.
    if checked_source_tree(git_read, tested) != entries or git_read("rev-parse", "HEAD") != tested:
        raise QualificationError("tested source changed during receipt preparation")
    return {"schema": SCHEMA, "repository": env["GITHUB_REPOSITORY"], "head_sha": head,
            "base_sha": base, "tested_sha": tested, "tested_tree": tree,
            "run_id": int(env["GITHUB_RUN_ID"]), "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
            "workflow_sha": env["GITHUB_WORKFLOW_SHA"],
            **producer, "security_scope_sha256": scope_identity,
            "job_key": env["GITHUB_JOB"], "artifact_key": artifact_key, "role": role, "result": "executed_pass", "security": security}


@contextmanager
def receipt_output(path, env, artifact_key):
    """One fixed native temporary namespace, retained no-follow descriptors."""
    temporary = Path(env["RUNNER_TEMP"])
    job, run, attempt = (env.get(k, "") for k in ("GITHUB_JOB", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
    if (not isinstance(artifact_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", artifact_key)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", job)
            or any(not re.fullmatch(r"[1-9][0-9]*", v) for v in (run, attempt))
            or not temporary.is_absolute() or temporary.resolve() != temporary
            or any(p.is_symlink() for p in (temporary, *temporary.parents))):
        raise QualificationError("native output namespace unavailable")
    name = "source-qualification-" + run + "-" + attempt + "-" + job + "-" + artifact_key
    if (path != temporary / name / "source-qualification.json"
            or path.is_symlink() or path.parent.is_symlink()):
        raise QualificationError("output must be the exact native temporary namespace")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(temporary, flags)
    directory = None
    try:
        try:
            os.mkdir(name, mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        directory = os.open(name, flags, dir_fd=root)
        expected = os.fstat(directory)
        def unchanged():
            actual = os.stat(name, dir_fd=root, follow_symlinks=False)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise QualificationError("native output directory changed")
        unchanged()
        try:
            os.unlink("source-qualification.json", dir_fd=directory)
        except FileNotFoundError:
            pass
        def write(value):
            unchanged()
            descriptor = os.open("source-qualification.json",
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 mode=0o600, dir_fd=directory)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps(value, sort_keys=True) + "\n")
            unchanged()
        yield write
    finally:
        if directory is not None:
            os.close(directory)
        os.close(root)


def receipt_main(argv):
    parser = argparse.ArgumentParser(description="Native job evidence producer; no execution authority.")
    parser.add_argument("--role", required=True, choices=["classify", "deterministic", "security"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifact-key")
    parser.add_argument("--policy-path", default="config/source-qualification.json")
    args = parser.parse_args(argv)
    try:
        artifact_key = args.artifact_key or os.environ.get("GITHUB_JOB", "")
        with receipt_output(args.output, os.environ, artifact_key) as write:
            inherited = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            def git_read(*parts):
                return subprocess.check_output(["git", "--no-replace-objects", *parts], env=inherited,
                                               timeout=15).decode().strip()
            event = strict_json(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
            reports = {}
            if args.role == "security":
                for scanner in ("semgrep", "bandit"):
                    raw = Path("/tmp/" + scanner + "-results.json").read_bytes()
                    if len(raw) > MAX_BYTES:
                        raise QualificationError("scanner report exceeds bound")
                    status = os.environ.get(scanner.upper() + "_EXIT", "")
                    if status not in {"0", "1"}:
                        raise QualificationError("missing successful native scanner exit")
                    reports[scanner] = strict_json(raw)
                    reports[scanner + "_exit"] = int(status)
            if not re.fullmatch(r"config/[A-Za-z0-9_.-]+\.json", args.policy_path):
                raise QualificationError("reviewed owner policy path required")
            base = event["pull_request"]["base"]["sha"]
            if not sha(base):
                raise QualificationError("immutable native base required")
            compiled = policy(strict_json(git_read("show", base + ":" + args.policy_path)))
            matches = [item for item in compiled["required_checks"]
                       if item["job_key"] == os.environ.get("GITHUB_JOB")
                       and item["artifact_key"] == artifact_key and item["role"] == args.role]
            if len(matches) != 1:
                raise QualificationError("reviewed native job namespace required")
            # Existing authenticated read route, native read-only token only.
            producer = producing_job(NativeAPI(review_module()), os.environ["GITHUB_REPOSITORY"],
                                     event["pull_request"]["head"]["sha"], int(os.environ["GITHUB_RUN_ID"]),
                                      int(os.environ["GITHUB_RUN_ATTEMPT"]), matches[0],
                                      tested_sha=os.environ.get("GITHUB_SHA"))
            result = produce_receipt(os.environ, event, args.role, git_read, artifact_key=artifact_key,
                                     compiled=compiled, producer=producer, **reports)
            write(result)
    except (QualificationError, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError):
        print("Source qualification receipt unavailable; no successful evidence emitted.", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["receipt"]:
        return receipt_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--policy-path", default="config/source-qualification.json")
    args = parser.parse_args(argv)
    try:
        reviewer = review_module()
        result = collect(NativeAPI(reviewer), reviewer, args.repository, args.pr,
                         args.head, args.base, args.policy_path)
    except (QualificationError, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError,
            subprocess.SubprocessError, zipfile.BadZipFile) as error:
        reason = str(error) if isinstance(error, QualificationError) else "native_" + type(error).__name__
        print(json.dumps({"schema": SCHEMA, "source_qualified": False, "admission_authorized": False,
                          "cutover": "blocked_native_evidence_unavailable_or_failed", "reason": reason}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("source_qualified") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
