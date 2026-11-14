#!/usr/bin/env python3
"""Run the Pack's existing owner recipe in a read-only native job.

This is execution evidence only. It never grants review, admission, mutation
credentials or production authority. Private readiness/cache state stays local.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

OWNER = "Manolii-org/ai-starter-pack"
SCHEMA = "native-owner-recipe/v1"


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("an exact nonzero source revision is required")
    return value


def positive(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("an actual native run identity is required")
    return int(value)


def subject(event, env):
    # Refuse other owners before reading their source, recipe or private state.
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != OWNER
            or event.get("repository", {}).get("full_name") != OWNER):
        raise ValueError("this Pack-local qualification cannot execute another owner")
    kind = env.get("GITHUB_EVENT_NAME")
    if kind == "pull_request":
        pr = event["pull_request"]
        if pr["base"]["repo"]["full_name"] != OWNER:
            raise ValueError("foreign qualification base")
        head, base = sha(pr["head"]["sha"]), sha(pr["base"]["sha"])
    elif kind == "push":
        if env.get("GITHUB_REF") != "refs/heads/main" or event.get("ref") != "refs/heads/main":
            raise ValueError("only the owning main push is supported")
        head, base = sha(event["after"]), sha(event["before"])
        if env.get("GITHUB_SHA") != head:
            raise ValueError("push source differs from native identity")
    elif kind == "merge_group":
        group = event["merge_group"]
        if group["base_ref"] != "refs/heads/main":
            raise ValueError("foreign merge-group base")
        head, base = sha(group["head_sha"]), sha(group["base_sha"])
    else:
        raise ValueError("unsupported native event")
    return {"repository": OWNER, "head_sha": head, "base_sha": base,
            "native_run_id": positive(env.get("GITHUB_RUN_ID")),
            "native_run_attempt": positive(env.get("GITHUB_RUN_ATTEMPT")),
            "native_workflow_sha": sha(env.get("GITHUB_WORKFLOW_SHA")),
            "native_job": env.get("GITHUB_JOB")}


def git(root, *args, input=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0",
               GIT_GRAFT_FILE=os.devnull, GIT_SHALLOW_FILE=os.devnull, GIT_ATTR_NOSYSTEM="1")
    # Metadata queries must not execute a local index-monitor hook or update
    # its caches. Command-line config wins over repository/include settings.
    return subprocess.check_output(["git", "--no-replace-objects",
                                   "-c", "core.fsmonitor=false",
                                   "-c", "core.untrackedCache=false",
                                   "-c", "core.commitGraph=false",
                                   "-c", "advice.graftFileDeprecated=false",
                                   "-c", "core.attributesFile=" + os.devnull,
                                   "-c", "protocol.allow=never", *args], cwd=root,
                                   env=env, input=input, timeout=30)


def require_unfiltered_source(root):
    # Attribute metadata can be inspected without converting worktree bytes.
    # Refuse filter drivers before diff has an opportunity to execute one.
    inventory = git(root, "ls-files", "--cached", "-v", "-z")
    if not inventory.endswith(b"\0"):
        raise ValueError("source index inventory is incomplete")
    entries = inventory.split(b"\0")[:-1]
    if any(not entry.startswith(b"H ") or len(entry) <= 2 for entry in entries):
        raise ValueError("special or ambiguous source index flags cannot be qualified")
    paths = b"\0".join(entry[2:] for entry in entries) + b"\0"
    # --all distinguishes absent attributes from literal driver names such as
    # "unset"/"unspecified", which the single-attribute text form conflates.
    attributes = git(root, "check-attr", "-z", "--stdin", "--all", input=paths)
    if not paths.endswith(b"\0") or (attributes and not attributes.endswith(b"\0")):
        raise ValueError("source filter attributes are incomplete")
    names = paths.split(b"\0")[:-1]
    values = attributes.split(b"\0")[:-1] if attributes else []
    if len(set(names)) != len(names) or len(values) % 3:
        raise ValueError("source filter attributes are incomplete")
    known, seen = set(names), set()
    for index in range(0, len(values), 3):
        entry, attribute, _value = values[index:index + 3]
        key = (entry, attribute)
        if entry not in known or not attribute or key in seen or attribute == b"filter":
            raise ValueError("filtered or ambiguous source cannot be qualified")
        seen.add(key)


def checkout_matches_head(root):
    """Compare physical tracked bytes and modes with HEAD's expected checkout.

    Normalized diff applies clean conversion (eol/ident) before comparing, so
    converted-away bytes look unchanged. The reference is the HEAD blob after
    Git's built-in checkout conversion (``cat-file --filters``), never the raw
    blob, so a supported eol/ident checkout still matches. Callers have already
    refused external filter drivers. Unreadable, special, missing or
    symlink-mismatched entries return False (fail closed).
    """
    listing = git(root, "ls-tree", "-r", "--full-tree", "-z", "HEAD")
    if listing and not listing.endswith(b"\0"):
        return False
    file_mode = git(root, "config", "--type=bool", "--default=true", "--get", "core.filemode").strip()
    symlinks = git(root, "config", "--type=bool", "--default=true", "--get", "core.symlinks").strip()
    directories = {}
    for entry in listing.split(b"\0")[:-1] if listing else []:
        metadata, separator, name = entry.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or not name or len(fields) != 3 or fields[1] != b"blob":
            return False  # gitlinks/submodules have no checkout bytes to bind
        mode, identity = fields[0], fields[2].decode()
        relative = os.fsdecode(name)
        # A symlinked parent directory would redirect the read elsewhere.
        parent = os.path.dirname(relative)
        while parent:
            if parent not in directories:
                try:
                    directories[parent] = stat.S_ISDIR(os.lstat(root / parent).st_mode)
                except OSError:
                    directories[parent] = False
            if not directories[parent]:
                return False
            parent = os.path.dirname(parent)
        path = root / relative
        try:
            if mode == b"120000" and symlinks == b"true":
                if not stat.S_ISLNK(os.lstat(path).st_mode):
                    return False
                if os.fsencode(os.readlink(path)) != git(root, "cat-file", "blob", identity):
                    return False
                continue
            if mode not in (b"100644", b"100755", b"120000"):
                return False
            expected = (git(root, "cat-file", "blob", identity) if mode == b"120000" else
                        git(root, "cat-file", "--filters", b"--path=" + name, identity))
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != len(expected):
                    return False
                if (mode != b"120000" and file_mode == b"true"
                        and bool(info.st_mode & 0o100) != (mode == b"100755")):
                    return False
                if stream.read(len(expected) + 1) != expected:
                    return False
        except OSError:
            return False
    return True


def identifiers(items):
    if not isinstance(items, list):
        raise ValueError("recipe inventory must be an array")
    result = []
    for item in items:
        identifier = item.get("id") if isinstance(item, dict) else None
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier)
                or identifier in result):
            raise ValueError("recipe inventory is missing or ambiguous")
        result.append(identifier)
    return result


def passing_receipt(value, current, selected, base):
    if (not isinstance(value, dict) or value.get("schema") != "agent-check/v1"
            or value.get("outcome") != "passed" or value.get("cached") is not False
            or value.get("merge_authority") is not False or value.get("base") != base):
        raise ValueError("fresh exact recipe execution is required")
    snapshot = value.get("snapshot")
    if (not isinstance(snapshot, dict) or snapshot.get("head") != current["head_sha"]
            or snapshot.get("tree") != current["tree_sha"]):
        raise ValueError("recipe source subject differs")
    results = value.get("results")
    if not selected or not isinstance(results, list) or not results:
        raise ValueError("unselected or missing work is not passing evidence")
    ids = identifiers(results)
    if (any(item.get("status") != "passed" for item in results)
            or any(item not in ids for item in selected)):
        raise ValueError("selected recipe work is incomplete")
    return True


def execute(root, event, env, *, plan_only=False):
    current = subject(event, env)
    if current["native_job"] != "owner_recipe":
        raise ValueError("unexpected native producing job")
    if git(root, "rev-parse", "HEAD").decode().strip() != current["head_sha"]:
        raise ValueError("checkout differs from immutable event head")
    current["tree_sha"] = sha(git(root, "rev-parse", "HEAD^{tree}").decode().strip())
    try:
        git(root, "merge-base", "--is-ancestor", current["base_sha"], current["head_sha"])
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        raise ValueError("the selected current base must be integrated before qualification") from error
    require_unfiltered_source(root)
    if (git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "HEAD")
            or not checkout_matches_head(root)):
        raise ValueError("tracked source is modified before qualification")
    if git(root, "ls-files", "--others", "--exclude-standard", "-z"):
        raise ValueError("untracked source is present before qualification")
    summary = {"schema": SCHEMA, **current, "recipe_passed": False,
               "whole_configured_recipe_passed": False, "merge_authority": False,
               "admission_authorized": False, "production_authority": False}
    # Verify the fixed helper bytes before invoking the unchanged owner CLI.
    path = root / "scripts/agent-check.py"
    raw = path.read_bytes()
    if raw != git(root, "show", current["head_sha"] + ":scripts/agent-check.py"):
        raise ValueError("owner helper differs from reviewed source")
    config = json.loads((root / "config/agent-checks.json").read_text())
    # Classification uses the existing selector without provisioning or tool
    # probes. It cannot create/read a cached PASS or skip checks during execution.
    command = [sys.executable, str(path), "--repo-root", str(root),
               "--base", current["base_sha"]]
    planned = subprocess.run([*command, "--plan", "--select-only"], cwd=root,
                             capture_output=True, text=True, timeout=120)
    if planned.returncode != 0:
        summary["status"] = "classification-failed"
        return summary, planned.returncode
    # Classification is code execution too. Recheck before it can select an
    # execution or publish even an unselected/plan-only successful result.
    require_unfiltered_source(root)
    if (git(root, "rev-parse", "HEAD").decode().strip() != current["head_sha"]
            or git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "HEAD")
            or git(root, "ls-files", "--others", "--exclude-standard", "-z")
            or not checkout_matches_head(root)):
        raise ValueError("source changed during qualification")
    if len(planned.stdout) > 2_000_000:
        raise ValueError("recipe plan exceeds bounded metadata")
    plan = json.loads(planned.stdout)
    selected, configured = identifiers(plan["checks"]), identifiers(config["checks"])
    summary.update(selected_check_ids=selected, configured_check_ids=configured)
    if not selected:
        summary["status"] = "not-selected-no-qualification-evidence"
        return summary, 0
    if plan_only:
        summary["status"] = "selected-pending-execution"
        return summary, 0
    result = subprocess.run([*command, "--provision", "--provider", "codex", "--no-cache"], cwd=root)
    if result.returncode != 0:
        summary["status"] = "failed"
        return summary, result.returncode
    receipt_path = root / ".ai/agent-checks/receipt.json"
    receipt_raw = receipt_path.read_bytes()
    if len(receipt_raw) > 2_000_000:
        raise ValueError("owner receipt exceeds bounded metadata")
    passing_receipt(json.loads(receipt_raw), current, selected, plan["base"])
    require_unfiltered_source(root)
    if (git(root, "rev-parse", "HEAD").decode().strip() != current["head_sha"]
            or git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "HEAD")
            or git(root, "ls-files", "--others", "--exclude-standard", "-z")
            or not checkout_matches_head(root)):
        raise ValueError("source changed during qualification")
    summary.update(status="passed", recipe_passed=True,
                   whole_configured_recipe_passed=set(selected) == set(configured),
                   receipt_sha256=hashlib.sha256(receipt_raw).hexdigest())
    return summary, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    # Foreign owners must not read source or mutate an existing summary.
    if (os.environ.get("GITHUB_REPOSITORY") != OWNER
            or os.environ.get("GITHUB_ACTIONS") != "true"):
        print("Pack-local native qualification only", file=sys.stderr)
        return 2
    temporary = Path(os.environ.get("RUNNER_TEMP", ""))
    if (not temporary.is_absolute() or args.output.parent != temporary
            or args.output.name != "native-owner-recipe.json"):
        print("qualification summary must use the fixed native temporary path", file=sys.stderr)
        return 2
    # Revoke stale summary before preparation; no private cache is uploaded.
    args.output.unlink(missing_ok=True)
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        summary, code = execute(Path.cwd(), event, os.environ, plan_only=args.plan)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        summary, code = {"schema": SCHEMA, "status": "blocked",
                         "recipe_passed": False, "whole_configured_recipe_passed": False,
                         "merge_authority": False, "admission_authorized": False,
                         "production_authority": False}, 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")
    if args.plan and code == 0:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("selected=" + str(bool(summary.get("selected_check_ids"))).lower() + "\n")
    print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
