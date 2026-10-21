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
import hmac
import json
import os
from pathlib import Path
import platform
import secrets
import stat
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
    "swift": ["swiftc", "--version"],
    "shellcheck": ["shellcheck", "--version"],
    "actionlint": ["actionlint", "--version"],
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def git_environment(environ: dict | None = None) -> dict:
    # Hooks can export the enclosing repository's locations. An explicit cwd
    # must select this repository, while transport/proxy configuration stays.
    locations = {
        "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_QUARANTINE_PATH", "GIT_CONFIG",
    }
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if key not in locations}


def git(root: Path, *args: str) -> bytes:
    if args and args[0] == "diff":
        # Evidence must describe bytes, independently of display-only drivers.
        args = (args[0], "--no-ext-diff", "--no-textconv", "--no-color", *args[1:])
    result = subprocess.run(["git", *args], cwd=root, env=git_environment(), capture_output=True, check=True)
    return result.stdout


def execution_environment(item: dict, inherited: dict | None = None) -> dict:
    """Use the same effective environment for execution, probes and identity."""
    return {**git_environment(inherited), "CI": "true", "PYTHONDONTWRITEBYTECODE": "1", **item.get("env", {})}


def private_cache_key(root: Path) -> bytes:
    """Create one ignored private key without exposing inherited secret values."""
    path = inside(root, f"{STATE}/environment.key")
    if subprocess.run(["git", "check-ignore", "-q", str(path.relative_to(root))],
                      cwd=root, env=git_environment()).returncode:
        raise ValueError(f"ignore {STATE}/environment.key before executing")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Link a complete private temporary file into place atomically. A competing
    # invocation uses the existing key; an unsafe existing path is never replaced.
    if not path.exists() and not path.is_symlink():
        temporary = path.parent / f".environment-key-{os.getpid()}-{secrets.token_hex(8)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                print("AGENT-CHECK: reusing concurrently created private cache key")
        finally:
            temporary.unlink(missing_ok=True)
    if path.is_symlink():
        raise ValueError("environment cache key must be a private regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        for attempt in range(20):
            details = os.fstat(stream.fileno())
            if (not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600
                    or details.st_uid != os.getuid()):
                raise ValueError("environment cache key must be an owned private regular file with mode 0600")
            if details.st_nlink == 1:
                break
            # A concurrent publisher briefly owns both the temporary and final names.
            # Keep the same descriptor and require one link before reading any material.
            # Persistent/extra links still fail closed; never replace or repair the key.
            if details.st_nlink != 2 or attempt == 19:
                raise ValueError("environment cache key must be an owned private regular file with mode 0600")
            time.sleep(0.05)
        key = stream.read(33)
    if len(key) != 32:
        raise ValueError("environment cache key is invalid")
    return key


def environment_fingerprint(key: bytes, environ: dict) -> str:
    """Only an aggregate keyed digest may appear in local evidence."""
    return hmac.new(key, encoded(environ), hashlib.sha256).hexdigest()


def inside(root: Path, value: str) -> Path:
    path = root / value
    if Path(value).is_absolute() or not path.resolve().is_relative_to(root):
        raise ValueError(f"path must stay inside the repository: {value}")
    return path


def require_unfiltered_tracked_inputs(root: Path) -> None:
    """Git clean/smudge filters can conceal raw working-file changes."""
    names = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                           env=git_environment(), capture_output=True, check=True).stdout
    attributes = subprocess.run(["git", "check-attr", "--all", "-z", "--stdin"],
                                input=names, cwd=root, env=git_environment(),
                                capture_output=True, check=True).stdout.split(b"\0")
    if attributes[-1:] == [b""]:
        attributes.pop()
    if len(attributes) % 3:
        raise ValueError("cannot determine tracked Git filter attributes")
    if any(attributes[i + 1] == b"filter" for i in range(0, len(attributes), 3)):
        raise ValueError("Git filter attributes are unsupported for local validation; "
                         "filtered working files need a raw-input validation adapter")


def snapshot(root: Path) -> dict:
    require_unfiltered_tracked_inputs(root)
    # Include staged AND unstaged edits, additions and deletions. Generated
    # ignored files are not evidence inputs; declare their source/config.
    untracked = {}
    for raw in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = root / name
        if path.is_symlink():
            untracked[name] = sha(b"symlink\0" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            untracked[name] = sha(b"regular\0" + path.read_bytes())
    # Git records link text, not mutable target contents. Keep normal check
    # execution for linked candidates, but never reuse or save a cached PASS.
    names = set(untracked)
    names.update(os.fsdecode(p) for p in git(root, "ls-files", "-z").split(b"\0") if p)
    symlinks = sorted(name for name in names if (root / name).is_symlink())
    return {
        "head": git(root, "rev-parse", "HEAD").decode().strip(),
        "tree": git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
        "diff": sha(git(root, "diff", "--binary", "HEAD")),
        "untracked": untracked,
        "symlinks": symlinks,
    }


def config_for(root: Path) -> dict:
    config = json.loads(inside(root, "config/agent-checks.json").read_text(encoding="utf-8"))
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
            text = inside(root, source["path"]).read_text(encoding="utf-8")
            if source["contains"] not in text:
                raise ValueError(f"{check['id']}: CI command changed in {source['path']}; reconcile the local recipe")
    return config


def matches(paths: list[str], patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in patterns)


def plan_for(root: Path, config: dict, base: str | None = None) -> dict:
    require_unfiltered_tracked_inputs(root)
    ref = base or config["base"]
    base_sha = git(root, "merge-base", ref, "HEAD").decode().strip()
    changed: set[str] = set()
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


def versions(root: Path, plan: dict, inherited: dict | None = None) -> dict:
    # Corepack/pyenv and similar shims select versions from their working
    # directory. Probe each distinct execution context, never the caller's cwd.
    contexts = {("python", ".", "{}")}
    for item in [*plan["checks"], *plan["environments"]]:
        for name in item.get("tools", []):
            contexts.add((name, str(Path(item.get("cwd", "."))), encoded(item.get("env", {})).decode()))
    result = {"platform": platform.platform()}
    for name, cwd, env_json in sorted(contexts):
        if name not in TOOLS:
            raise ValueError(f"unknown tool fingerprint: {name}")
        declared_env = json.loads(env_json)
        key = name if cwd == "." and not declared_env else f"{name}@{cwd}"
        if declared_env:
            key += "#" + sha(env_json.encode())[:12]
        env = execution_environment({"env": declared_env}, inherited)
        try:
            proc = subprocess.run(TOOLS[name], cwd=inside(root, cwd), env=env,
                                  capture_output=True, text=True, timeout=15)
            result[key] = (proc.stdout or proc.stderr).strip() if proc.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            result[key] = "unavailable"
    return result


def execute(root: Path, item: dict, inherited: dict | None = None) -> dict:
    start = time.monotonic()
    status, reason = "passed", "completed"
    env = execution_environment(item, inherited)
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


def environment_digest(root: Path, item: dict, tools: dict, key: bytes, inherited: dict) -> str:
    inputs = {}
    for name in item.get("inputs", []):
        path = inside(root, name)
        inputs[name] = sha(path.read_bytes()) if path.is_file() else None
    return sha(encoded({"recipe": item, "inputs": inputs, "tools": tools,
                        "execution_environment": environment_fingerprint(key, execution_environment(item, inherited))}))


def missing_ready_paths(root: Path, item: dict) -> list[str]:
    return [p for p in item.get("ready_paths", []) if not inside(root, p).exists()]


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
    inherited = git_environment()
    receipt_path = None
    try:
        if not args.plan:
            state = inside(root, STATE)
            receipt_path = state / "receipt.json"
            # Revoke old evidence before config, Git or setup can block this
            # execution. Planning remains a read-only operation.
            receipt_path.unlink(missing_ok=True)
        config = config_for(root)
        plan = plan_for(root, config, args.base)
        tools = versions(root, plan, inherited)
        if args.plan:
            print(json.dumps({**plan, "tool_versions": tools}, indent=2))
            return 0
        # Receipts must be ignored even before first use; never commit local
        # evidence or accidentally add it to its own cache fingerprint.
        if subprocess.run(["git", "check-ignore", "-q", f"{STATE}/receipt.json"],
                          cwd=root, env=git_environment()).returncode:
            raise ValueError(f"ignore {STATE}/ in .gitignore before executing")
        key = private_cache_key(root)
        before = snapshot(root)
        cache_reusable = not before["symlinks"]
        results = []
        provisioned = []
        reused_environments = []
        results.append(execute(root, {"id": "diff-check", "commands": [["git", "diff", "--check", plan["base"]]]}, inherited))
        for environment in plan["environments"]:
            env_digest = environment_digest(root, environment, tools, key, inherited)
            # One slot per environment prevents an earlier tool fingerprint
            # from resurrecting success after a failed reinstall changes tools.
            env_receipt = state / "environments" / f"environment-{sha(encoded(environment['id']))}.json"
            ready = not missing_ready_paths(root, environment)
            previous_environment = {}
            if env_receipt.is_file():
                try:
                    previous_environment = json.loads(env_receipt.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    print(f"AGENT-CHECK {environment['id']}: invalid environment receipt; frozen setup needs validation")
            if (isinstance(previous_environment, dict) and previous_environment.get("digest") == env_digest
                    and isinstance(previous_environment.get("result"), dict)
                    and previous_environment["result"].get("status") == "passed" and ready):
                reused_environments.append((environment, env_receipt, env_digest))
                continue
            if not args.provision:
                results.append({"id": environment["id"], "status": "unavailable",
                                "reason": "run again with --provision to validate frozen dependencies"})
                continue
            # An interrupted/failed reinstall must revoke the old success
            # before it can leave a partially populated dependency directory.
            env_receipt.unlink(missing_ok=True)
            input_digest = environment_digest(root, environment, {}, key, inherited)
            result = execute(root, environment, inherited)
            missing = missing_ready_paths(root, environment)
            if result["status"] == "passed" and missing:
                result.update(status="failed", reason="declared ready paths missing: " + ",".join(missing))
            results.append(result)
            if result["status"] == "passed":
                # Bind setup to the tools it actually left ready, before a
                # later selected environment can replace any of those tools.
                tools = versions(root, plan, inherited)
                validated_digest = environment_digest(root, environment, tools, key, inherited)
                provisioned.append((environment, env_receipt, input_digest, validated_digest, result))
        # Provisioning may install tools. Missing tools must not be mistaken
        # for a source failure, or cached as a successful old fingerprint.
        tools = versions(root, plan, inherited)
        # A later setup may remove an earlier environment's readiness output.
        # Recheck all selected environments before recording setup or candidate PASS.
        for environment in plan["environments"]:
            missing = missing_ready_paths(root, environment)
            if missing:
                matching = next((r for r in results if r["id"] == environment["id"]), None)
                if matching is None:
                    results.append({"id": environment["id"], "status": "failed",
                                    "reason": "declared ready paths missing: " + ",".join(missing)})
                elif matching["status"] == "passed":
                    matching.update(status="failed", reason="declared ready paths missing: " + ",".join(missing))
                (state / "environments" / f"environment-{sha(encoded(environment['id']))}.json").unlink(missing_ok=True)
        # A cache hit was proved against the initial tools. Later setup can
        # change a shared tool, so it must still match before checks can run.
        # Revoke stale setup evidence and require a bounded fresh invocation.
        for environment, env_receipt, reused_digest in reused_environments:
            if environment_digest(root, environment, tools, key, inherited) != reused_digest:
                env_receipt.unlink(missing_ok=True)
                results.append({"id": environment["id"], "status": "failed",
                                "reason": "cached environment invalidated by later provisioning; rerun with --provision"})
        # New setups also keep their actual validated context. Never re-key an
        # earlier setup as compatible with a tool installed by a later one.
        for environment, env_receipt, input_digest, validated_digest, result in provisioned:
            if result["status"] != "passed":
                continue
            if (environment_digest(root, environment, {}, key, inherited) != input_digest
                    or environment_digest(root, environment, tools, key, inherited) != validated_digest):
                env_receipt.unlink(missing_ok=True)
                result.update(status="failed",
                              reason="provisioned environment invalidated by later provisioning or changed inputs; rerun with --provision")
            else:
                write_json(env_receipt, {"digest": validated_digest, "result": result})
        identity = {"schema": SCHEMA, "source": sha(Path(__file__).read_bytes()),
                    "config": config, "plan": plan, "tools": tools, "snapshot": before,
                    "execution_environment": environment_fingerprint(key, {
                        "default": execution_environment({}, inherited),
                        "checks": {item["id"]: execution_environment(item, inherited) for item in plan["checks"]},
                        "environments": {item["id"]: execution_environment(item, inherited) for item in plan["environments"]}})}
        candidate_digest = sha(encoded(identity))
        cache = state / "cache" / f"{candidate_digest}.json"
        unavailable = [name for name, version in tools.items() if version == "unavailable"]
        if unavailable:
            results.append({"id": "toolchain", "status": "unavailable", "reason": ",".join(unavailable)})
        for name, prefix in config.get("tool_requirements", {}).items():
            for context, version in tools.items():
                if context.partition("@")[0] == name and not version.startswith(prefix):
                    results.append({"id": f"toolchain-{context}", "status": "unavailable",
                                    "reason": f"requires {prefix}; provision the CI toolchain before validation"})
        # Setup scripts can change tracked inputs (for example lifecycle
        # generators). Never return an earlier PASS for that changed tree.
        if snapshot(root) != before:
            results.append({"id": "input-stability", "status": "failed",
                            "reason": "inputs changed during provisioning; inspect diff and rerun"})
        if cache_reusable and all(r["status"] == "passed" for r in results) and cache.is_file() and not args.no_cache:
            previous = json.loads(cache.read_text(encoding="utf-8"))
            if previous.get("digest") == candidate_digest and previous.get("outcome") == "passed":
                previous = {**previous, "provider": args.provider, "cached": True}
                write_json(state / "receipt.json", previous)
                print(f"AGENT-CHECK-CACHED {candidate_digest[:12]}")
                return 0
        if all(r["status"] == "passed" for r in results):
            results.extend(execute(root, check, inherited) for check in plan["checks"])
        # Formatters, generators or concurrent edits invalidate the result.
        if snapshot(root) != before and not any(r["id"] == "input-stability" for r in results):
            results.append({"id": "input-stability", "status": "failed", "reason": "inputs changed while checks ran; inspect diff and rerun"})
        outcome = "passed" if all(r["status"] == "passed" for r in results) else "failed"
        receipt = {"schema": SCHEMA, "digest": candidate_digest, "provider": args.provider, "cached": False,
                   "cache_reusable": cache_reusable,
                   "created_at": time.time(), "outcome": outcome, "snapshot": before,
                   "base": plan["base"], "tool_versions": tools, "results": results,
                   "review_paths": plan["review_paths"], "merge_authority": False}
        write_json(state / "receipt.json", receipt)
        if outcome == "passed" and cache_reusable:
            write_json(cache, receipt)
        print(f"AGENT-CHECK-{outcome.upper()} {candidate_digest[:12]}")
        return 0 if outcome == "passed" else 1
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if receipt_path is not None:
            try:
                receipt_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                print(f"AGENT-CHECK-BLOCKED: cannot revoke receipt: {cleanup_error}", file=sys.stderr)
        print(f"AGENT-CHECK-BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
