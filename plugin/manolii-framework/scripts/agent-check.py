#!/usr/bin/env python3
"""Run repository-owned CI commands before publishing an agent candidate.

No network API, credentials, reviewer or merge authority is provided here.
The ignored receipt proves local execution only. Configuration is trusted code,
just like package.json; never run a fork's configuration with privileged env.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

SCHEMA = "agent-check/v1"
STATE = ".ai/agent-checks"
TOOLS = {
    "python": [sys.executable, "--version"],
    "node": ["node", "--version"],
    "npm": ["npm", "--version"],
    "pnpm": ["pnpm", "--version"],
    "uv": ["uv", "--version"],
    "ruff": [sys.executable, "-m", "ruff", "--version"],
    "go": ["go", "version"],
    "shellcheck": ["shellcheck", "--version"],
    "actionlint": ["actionlint", "--version"],
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    return result.stdout


def inside(root: Path, value: str) -> Path:
    path = root / value
    if Path(value).is_absolute() or not path.resolve().is_relative_to(root):
        raise ValueError(f"path must stay inside the repository: {value}")
    return path


def snapshot(root: Path) -> dict:
    # Include staged AND unstaged edits, additions and deletions. Generated
    # ignored files are not evidence inputs; declare their source/config.
    untracked = {}
    for raw in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = root / name
        if path.is_symlink():
            untracked[name] = sha(os.fsencode(os.readlink(path)))
        elif path.is_file():
            untracked[name] = sha(path.read_bytes())
    return {
        "head": git(root, "rev-parse", "HEAD").decode().strip(),
        "tree": git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
        "diff": sha(git(root, "diff", "--binary", "HEAD")),
        "untracked": untracked,
    }


def config_for(root: Path) -> dict:
    config = json.loads(inside(root, "config/agent-checks.json").read_text())
    if config.get("schema_version") != 1 or not config.get("base"):
        raise ValueError("config/agent-checks.json requires schema_version=1 and base")
    identifiers = set()
    for kind in ("checks", "environments"):
        for item in config.get(kind, []):
            identifier = item.get("id", "")
            if not identifier or identifier in identifiers:
                raise ValueError("check and environment ids must be nonempty and unique")
            identifiers.add(identifier)
            inside(root, item.get("cwd", "."))
            if not item.get("commands") or not all(
                isinstance(cmd, list) and cmd and all(isinstance(s, str) and s for s in cmd)
                for cmd in item["commands"]
            ):
                raise ValueError(f"{identifier}: commands must be nonempty argv arrays")
            if not isinstance(item.get("paths", ["*"]), list):
                raise ValueError(f"{identifier}: paths must be an array")
    environments = {item["id"] for item in config.get("environments", [])}
    for check in config.get("checks", []):
        if check.get("environment") and check["environment"] not in environments:
            raise ValueError(f"unknown environment for {check['id']}")
        for source in check.get("ci_sources", []):
            text = inside(root, source["path"]).read_text()
            if source["contains"] not in text:
                raise ValueError(f"{check['id']}: CI command changed in {source['path']}; reconcile the local recipe")
    return config


def matches(paths: list[str], patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in patterns)


def plan_for(root: Path, config: dict, base: str | None = None) -> dict:
    ref = base or config["base"]
    base_sha = git(root, "merge-base", ref, "HEAD").decode().strip()
    changed = set()
    for command in (("diff", "--no-renames", "--name-only", "-z", base_sha),
                    ("ls-files", "--others", "--exclude-standard", "-z")):
        changed.update(os.fsdecode(p) for p in git(root, *command).split(b"\0") if p)
    paths = sorted(changed)
    # Owners explicitly declare global inputs in each recipe. Framework-only
    # changes must not implicitly install/build every application in the repo.
    checks = []
    for check in config.get("checks", []):
        patterns = check.get("paths", ["*"])
        if not matches(paths, patterns):
            continue
        check = dict(check)
        if check.get("append_changed_paths"):
            affected = [p for p in paths if matches([p], patterns)]
            selected = affected if all(inside(root, p).is_file() for p in affected) else []
            # Deleted workflows require checking remaining callers; no args
            # lets actionlint validate that remaining workflow graph.
            check["commands"] = [cmd + selected for cmd in check["commands"]]
        checks.append(check)
    needed = {c.get("environment") for c in checks}
    return {"base": base_sha, "paths": paths, "checks": checks,
            "environments": [e for e in config.get("environments", []) if e["id"] in needed],
            "review_paths": [p for p in paths if matches([p], config.get("review_paths", []))]}


def versions(plan: dict) -> dict:
    names = {"python"}
    for item in [*plan["checks"], *plan["environments"]]:
        names.update(item.get("tools", []))
    result = {"platform": platform.platform()}
    for name in sorted(names):
        if name not in TOOLS:
            raise ValueError(f"unknown tool fingerprint: {name}")
        try:
            proc = subprocess.run(TOOLS[name], capture_output=True, text=True, timeout=15)
            result[name] = (proc.stdout or proc.stderr).strip() if proc.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            result[name] = "unavailable"
    return result


def execute(root: Path, item: dict) -> dict:
    start = time.monotonic()
    status, reason = "passed", "completed"
    env = {**os.environ, "CI": "true", "PYTHONDONTWRITEBYTECODE": "1", **item.get("env", {})}
    for command in item["commands"]:
        print(f"AGENT-CHECK {item['id']}: {json.dumps(command)}", flush=True)
        try:
            proc = subprocess.run(command, cwd=inside(root, item.get("cwd", ".")), env=env,
                                  timeout=item.get("timeout_seconds", 900), check=False)
            if proc.returncode:
                status, reason = "failed", f"exit={proc.returncode}"
                break
        except FileNotFoundError:
            status, reason = "unavailable", "executable or working directory missing"
            break
        except subprocess.TimeoutExpired:
            status, reason = "failed", "timeout"
            break
    return {"id": item["id"], "status": status, "reason": reason,
            "duration_ms": round((time.monotonic() - start) * 1000)}


def environment_digest(root: Path, item: dict, tools: dict) -> str:
    inputs = {}
    for name in item.get("inputs", []):
        path = inside(root, name)
        inputs[name] = sha(path.read_bytes()) if path.is_file() else None
    return sha(encoded({"recipe": item, "inputs": inputs, "tools": tools}))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_bytes(encoded(value) + b"\n")
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--provider", choices=["claude", "codex", "cursor", "devin", "unknown"], default="unknown")
    parser.add_argument("--plan", "--dry-run", dest="plan", action="store_true")
    parser.add_argument("--provision", action="store_true", help="install selected development dependencies once per input/tool digest")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        config = config_for(root)
        plan = plan_for(root, config, args.base)
        tools = versions(plan)
        if args.plan:
            print(json.dumps({**plan, "tool_versions": tools}, indent=2))
            return 0
        state = inside(root, STATE)
        # Receipts must be ignored even before first use; never commit local
        # evidence or accidentally add it to its own cache fingerprint.
        if subprocess.run(["git", "check-ignore", "-q", f"{STATE}/receipt.json"], cwd=root).returncode:
            raise ValueError(f"ignore {STATE}/ in .gitignore before executing")
        before = snapshot(root)
        results = []
        results.append(execute(root, {"id": "diff-check", "commands": [["git", "diff", "--check", plan["base"]]]}))
        for environment in plan["environments"]:
            env_digest = environment_digest(root, environment, tools)
            env_receipt = state / "environments" / f"{env_digest}.json"
            ready = all(inside(root, p).exists() for p in environment.get("ready_paths", []))
            if env_receipt.is_file() and ready:
                continue
            if not args.provision:
                results.append({"id": environment["id"], "status": "unavailable",
                                "reason": "run again with --provision to validate frozen dependencies"})
                continue
            # An interrupted/failed reinstall must revoke the old success
            # before it can leave a partially populated dependency directory.
            env_receipt.unlink(missing_ok=True)
            result = execute(root, environment)
            results.append(result)
            if result["status"] == "passed" and environment_digest(root, environment, tools) == env_digest:
                write_json(env_receipt, {"digest": env_digest, "result": result})
        # Provisioning may install tools. Missing tools must not be mistaken
        # for a source failure, or cached as a successful old fingerprint.
        tools = versions(plan)
        identity = {"schema": SCHEMA, "source": sha(Path(__file__).read_bytes()),
                    "config": config, "plan": plan, "tools": tools, "snapshot": before}
        candidate_digest = sha(encoded(identity))
        cache = state / "cache" / f"{candidate_digest}.json"
        unavailable = [name for name, version in tools.items() if version == "unavailable"]
        if unavailable:
            results.append({"id": "toolchain", "status": "unavailable", "reason": ",".join(unavailable)})
        for name, prefix in config.get("tool_requirements", {}).items():
            if name in tools and not tools[name].startswith(prefix):
                results.append({"id": f"toolchain-{name}", "status": "unavailable",
                                "reason": f"requires {prefix}; provision the CI toolchain before validation"})
        # Setup scripts can change tracked inputs (for example lifecycle
        # generators). Never return an earlier PASS for that changed tree.
        if snapshot(root) != before:
            results.append({"id": "input-stability", "status": "failed",
                            "reason": "inputs changed during provisioning; inspect diff and rerun"})
        if all(r["status"] == "passed" for r in results) and cache.is_file() and not args.no_cache:
            previous = json.loads(cache.read_text())
            if previous.get("digest") == candidate_digest and previous.get("outcome") == "passed":
                previous = {**previous, "provider": args.provider, "cached": True}
                write_json(state / "receipt.json", previous)
                print(f"AGENT-CHECK-CACHED {candidate_digest[:12]}")
                return 0
        if all(r["status"] == "passed" for r in results):
            results.extend(execute(root, check) for check in plan["checks"])
        # Formatters, generators or concurrent edits invalidate the result.
        if snapshot(root) != before and not any(r["id"] == "input-stability" for r in results):
            results.append({"id": "input-stability", "status": "failed", "reason": "inputs changed while checks ran; inspect diff and rerun"})
        outcome = "passed" if all(r["status"] == "passed" for r in results) else "failed"
        receipt = {"schema": SCHEMA, "digest": candidate_digest, "provider": args.provider, "cached": False,
                   "created_at": time.time(), "outcome": outcome, "snapshot": before,
                   "base": plan["base"], "tool_versions": tools, "results": results,
                   "review_paths": plan["review_paths"], "merge_authority": False}
        write_json(state / "receipt.json", receipt)
        if outcome == "passed":
            write_json(cache, receipt)
        print(f"AGENT-CHECK-{outcome.upper()} {candidate_digest[:12]}")
        return 0 if outcome == "passed" else 1
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"AGENT-CHECK-BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
