#!/usr/bin/env python3
"""Finalise gate: run the repository's agent checks when work is handed off.

One provider-neutral gate at the seam where an agent finalises work — a
``git push`` or a pull-request creation — instead of on every turn. It runs
``scripts/agent-check.py`` (affected checks only, cached by input digest, so an
unchanged candidate costs about a second) and blocks the hand-off only on a
real check failure. Infrastructure gaps (missing integration ref, missing
toolchain, timeout) are reported as incomplete validation and never block:
CI remains the authority and a recurring environment fault must be fixed at
its owner, not routed around with repeated pushes.

Adapters (``--hook``):
  claude        Claude Code PreToolUse (matcher Bash|mcp__github__create_pull_request)
  codex         Codex PreToolUse (matcher Bash)
  cursor        Cursor beforeShellExecution (prints the permission JSON)
  git-pre-push  git pre-push hook (husky, .githooks or the installed shim)

Other modes:
  --install-git-hook   write a marked .git/hooks/pre-push shim (Devin, humans)
  --query replace-legacy-pre-push   exit 0 when config/qa.json opts in

Configuration is the optional ``config/qa.json`` ``finalise_gate`` section
(docs/qa-framework.md). A repository without ``config/agent-checks.json`` has
not adopted agent validation, so the gate is a no-op there.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

GATE_STATE = ".ai/agent-checks"
LOG_NAME = "finalise-gate.log"
LEDGER_NAME = "finalise-gate.jsonl"
SHIM_MARKER = "# managed-by: ai-starter-pack finalise-gate"
MODES = ("block", "warn", "off")
# Absent config means the repository never opted in explicitly. A plugin-only
# or silently upgraded consumer must not start blocking pushes without review.
DEFAULT_MODE_WITHOUT_CONFIG = "warn"
DEFAULT_TIMEOUT_SECONDS = 1100  # below the 1200s host hook timeout
TAIL_LINES = 30
TAIL_LINE_CHARS = 240
PR_TOOLS = {"mcp__github__create_pull_request"}
BYPASS_ENV = "AGENT_FINALISE_GATE"


# ── command classification ──────────────────────────────────────────────────

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FALLBACK = re.compile(r"\bgit\b[^;&|\n]*\bpush\b|\bgh\s+pr\s+create\b")


def classify_command(command: str) -> dict:
    """Return {"trigger": None|"push"|"pr", "repo_dir": str|None, "bypass": bool}.

    Only a real hand-off triggers the gate: dry runs and ref deletions do not.
    Unparseable shell falls back to a conservative regex so quoting tricks
    cannot silently skip validation.
    """
    result = {"trigger": None, "repo_dir": None, "bypass": False}
    for segment in _SEGMENT_SPLIT.split(command or ""):
        try:
            tokens = shlex.split(segment, comments=False, posix=True)
        except ValueError:
            if _FALLBACK.search(segment):
                result["trigger"] = result["trigger"] or "push"
            continue
        while tokens and _ASSIGNMENT.match(tokens[0]):
            name, _, value = tokens.pop(0).partition("=")
            if name == BYPASS_ENV and value.lower() == "off":
                result["bypass"] = True
        if not tokens:
            continue
        head = os.path.basename(tokens[0])
        if head == "gh" and tokens[1:3] == ["pr", "create"]:
            result["trigger"] = "pr"
            continue
        if head != "git":
            continue
        index, repo_dir = 1, None
        while index < len(tokens) and tokens[index].startswith("-"):
            option = tokens[index]
            if option in ("-C", "-c") and index + 1 < len(tokens):
                if option == "-C":
                    repo_dir = os.path.join(repo_dir or "", tokens[index + 1])
                index += 2
                continue
            index += 1
        if index >= len(tokens) or tokens[index] != "push":
            continue
        args = tokens[index + 1:]
        if any(a in ("--dry-run", "-n", "--delete", "-d") for a in args):
            continue
        refspecs = [a for a in args if not a.startswith("-")][1:]
        if refspecs and all(r.startswith(":") for r in refspecs):
            continue  # deleting remote refs is not a candidate hand-off
        result["trigger"] = result["trigger"] or "push"
        result["repo_dir"] = result["repo_dir"] or repo_dir
    return result


# ── repository + configuration ──────────────────────────────────────────────

def git_toplevel(start: Path) -> Path | None:
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None


def load_qa_config(root: Path) -> tuple[dict, bool]:
    """Return (finalise_gate section, config_present)."""
    path = root / "config" / "qa.json"
    if not path.is_file():
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"config/qa.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("config/qa.json requires schema_version=1")
    section = data.get("finalise_gate", {})
    if not isinstance(section, dict):
        raise ValueError("config/qa.json finalise_gate must be an object")
    return section, True


def resolve_mode(section: dict, present: bool, bypass_inline: bool) -> tuple[str, str]:
    """Return (mode, source). Environment and inline overrides win."""
    if bypass_inline:
        return "off", "inline"
    override = os.environ.get(BYPASS_ENV, "").strip().lower()
    if override in MODES:
        return override, "env"
    mode = section.get("mode", "block" if present else DEFAULT_MODE_WITHOUT_CONFIG)
    if mode not in MODES:
        raise ValueError(f"config/qa.json finalise_gate.mode must be one of {MODES}")
    return mode, "config" if present else "default"


def agent_check_path(root: Path) -> Path | None:
    for candidate in (root / "scripts" / "agent-check.py",
                      Path(__file__).resolve().parent.parent / "agent-check.py"):
        if candidate.is_file():
            return candidate
    return None


# ── execution ───────────────────────────────────────────────────────────────

def run_checks(root: Path, provider: str, timeout: int) -> dict:
    """Run agent-check and classify the outcome as pass|fail|incomplete."""
    runner = agent_check_path(root)
    if runner is None:
        return {"outcome": "incomplete", "summary": "scripts/agent-check.py is not installed"}
    state = root / GATE_STATE
    state.mkdir(parents=True, exist_ok=True)
    log_path = state / LOG_NAME
    start = time.monotonic()
    cmd = [sys.executable, str(runner), "--provision", "--provider", provider,
           "--repo-root", str(root)]
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            proc = subprocess.run(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = None
    duration_ms = round((time.monotonic() - start) * 1000)
    receipt = {}
    receipt_path = state / "receipt.json"
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = {}
    results = receipt.get("results", []) if isinstance(receipt, dict) else []
    failed = [r for r in results if isinstance(r, dict) and r.get("status") == "failed"]
    unavailable = [r for r in results if isinstance(r, dict) and r.get("status") == "unavailable"]
    base = {"duration_ms": duration_ms, "digest": str(receipt.get("digest", ""))[:12],
            "cached": bool(receipt.get("cached")), "log": str(log_path.relative_to(root))}
    if code == 0:
        return {**base, "outcome": "pass", "summary": "all affected checks passed"}
    if code is None:
        return {**base, "outcome": "incomplete", "summary": f"agent-check exceeded {timeout}s"}
    if code == 2:
        return {**base, "outcome": "incomplete",
                "summary": "agent-check blocked before running checks: " + blocked_reason(log_path)}
    if failed:
        names = ", ".join(f"{r.get('id')} ({r.get('reason')})" for r in failed)
        return {**base, "outcome": "fail", "summary": f"failed: {names}"}
    if unavailable:
        names = ", ".join(f"{r.get('id')} ({r.get('reason')})" for r in unavailable)
        return {**base, "outcome": "incomplete", "summary": f"unavailable: {names}"}
    return {**base, "outcome": "fail", "summary": f"agent-check exited {code}"}


def blocked_reason(log_path: Path) -> str:
    for line in reversed(tail(log_path, 50)):
        if "AGENT-CHECK-BLOCKED" in line:
            return line.split("AGENT-CHECK-BLOCKED:", 1)[-1].strip()[:TAIL_LINE_CHARS]
    return "see log"


def tail(path: Path, count: int = TAIL_LINES) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line[:TAIL_LINE_CHARS] for line in lines[-count:]]


def record(root: Path, entry: dict) -> None:
    """Append one cost/outcome row; never let telemetry break the hand-off."""
    try:
        path = root / GATE_STATE / LEDGER_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def record_bypass(root: Path, provider: str, source: str) -> None:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=5).stdout.strip()
        path = root / ".ai" / "bypass-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "scope": "finalise-gate", "head": head, "user": provider,
                "reason": f"{BYPASS_ENV}=off ({source})",
            }) + "\n")
    except (OSError, subprocess.SubprocessError):
        pass


def message_for(result: dict, provider: str, mode: str) -> str:
    rerun = f"python3 scripts/agent-check.py --provision --provider {provider}"
    lines = [f"FINALISE-GATE {result['outcome'].upper()}: {result['summary']}"]
    if result["outcome"] == "fail":
        lines.append(f"Fix the failing checks, then re-run: {rerun}")
        if mode == "warn":
            lines.append("(mode=warn: hand-off allowed; set finalise_gate.mode=block to enforce)")
    else:
        lines.append("Validation is incomplete, not passed — CI stays authoritative. "
                     f"Re-run when the environment is fixed: {rerun}")
    if result.get("log"):
        log_tail = tail(Path(result["root"]) / result["log"]) if result["outcome"] == "fail" else []
        if log_tail:
            lines.append(f"Last {len(log_tail)} lines of {result['log']}:")
            lines.extend(log_tail)
    return "\n".join(lines)


# ── adapters ────────────────────────────────────────────────────────────────

def read_payload() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def hook_target(adapter: str, payload: dict) -> tuple[str | None, str | None, bool, str]:
    """Return (trigger, start_dir, bypass, command) for a hook payload."""
    if adapter == "cursor":
        command = str(payload.get("command") or "")
        roots = payload.get("workspace_roots") or []
        start = payload.get("cwd") or (roots[0] if roots else None)
    else:
        tool = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        start = payload.get("cwd")
        if tool in PR_TOOLS:
            return "pr", start, False, tool
        if tool != "Bash":
            return None, start, False, ""
        command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    info = classify_command(command)
    if info["repo_dir"]:
        start = os.path.join(start or os.getcwd(), info["repo_dir"])
    return info["trigger"], start, info["bypass"], command


def emit(adapter: str, decision: str, text: str) -> int:
    """Translate allow|warn|deny into the host's hook protocol."""
    if adapter == "cursor":
        response = {"permission": "deny" if decision == "deny" else "allow"}
        if text:
            response["agent_message"] = text
            response["user_message"] = text.splitlines()[0]
        print(json.dumps(response))
        return 0
    if decision == "deny":
        print(text, file=sys.stderr)
        return 2 if adapter in ("claude", "codex") else 1
    if text:
        if adapter == "claude":
            print(json.dumps({"systemMessage": text.splitlines()[0]}))
        print(text, file=sys.stderr)
    return 0


def gate(adapter: str, start: str | None, bypass_inline: bool, trigger: str) -> int:
    root = git_toplevel(Path(start or os.getcwd()))
    if root is None or not (root / "config" / "agent-checks.json").is_file():
        return emit(adapter, "allow", "")
    provider = adapter if adapter in ("claude", "codex", "cursor") else (
        os.environ.get("AGENT_PROVIDER", "unknown").strip() or "unknown")
    if provider not in ("claude", "codex", "cursor", "devin", "unknown"):
        provider = "unknown"
    try:
        section, present = load_qa_config(root)
        mode, source = resolve_mode(section, present, bypass_inline)
    except ValueError as exc:
        return emit(adapter, "allow", f"FINALISE-GATE INCOMPLETE: {exc}")
    if mode == "off":
        if source in ("env", "inline"):
            record_bypass(root, provider, source)
        record(root, {"ts": time.time(), "provider": provider, "trigger": trigger,
                      "mode": mode, "outcome": "off", "source": source})
        return emit(adapter, "allow", "")
    timeout = section.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS
    result = run_checks(root, provider, timeout)
    result["root"] = str(root)
    record(root, {"ts": time.time(), "provider": provider, "trigger": trigger, "mode": mode,
                  "outcome": result["outcome"], "duration_ms": result.get("duration_ms"),
                  "cached": result.get("cached"), "digest": result.get("digest")})
    if result["outcome"] == "pass":
        return emit(adapter, "allow", "")
    text = message_for(result, provider, mode)
    if result["outcome"] == "fail" and mode == "block":
        return emit(adapter, "deny", text)
    return emit(adapter, "warn", text)


def git_pre_push() -> int:
    # stdin: "<local ref> <local sha> <remote ref> <remote sha>" per ref.
    lines = [line.split() for line in sys.stdin.read().splitlines() if line.strip()]
    if lines and all(len(p) >= 2 and set(p[1]) == {"0"} for p in lines):
        return 0  # every ref is a deletion
    return gate("git-pre-push", os.getcwd(), False, "push")


# ── git shim ────────────────────────────────────────────────────────────────

SHIM = """#!/bin/sh
{marker}
# Runs the repository's finalise gate before every push. Remove this file to
# uninstall. Re-run `python3 scripts/qa/finalise-gate.py --install-git-hook`
# to refresh it.
ROOT="$(git rev-parse --show-toplevel)" || exit 0
GATE="$ROOT/scripts/qa/finalise-gate.py"
[ -f "$GATE" ] || GATE={fallback}
[ -f "$GATE" ] || exit 0
exec python3 "$GATE" --hook git-pre-push "$@"
"""


def install_git_hook(root: Path) -> int:
    """Install a marked pre-push shim without displacing any hook manager."""
    configured = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=root,
                                capture_output=True, text=True).stdout.strip()
    if configured:
        print(f"finalise-gate: core.hooksPath={configured} is managed elsewhere; "
              "call `scripts/qa/finalise-gate.py --hook git-pre-push` from that manager's pre-push")
        return 0
    hooks_dir = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=root,
                               capture_output=True, text=True).stdout.strip()
    if not hooks_dir:
        print("finalise-gate: not a git repository", file=sys.stderr)
        return 1
    target = (root / hooks_dir) / "pre-push"
    if target.exists() and SHIM_MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        print(f"finalise-gate: {target} exists and is not managed by this gate; leaving it unchanged")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    fallback = shlex.quote(str(Path(__file__).resolve()))
    target.write_text(SHIM.format(marker=SHIM_MARKER, fallback=fallback), encoding="utf-8")
    target.chmod(0o755)
    print(f"finalise-gate: installed {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--hook", choices=["claude", "codex", "cursor", "git-pre-push"])
    group.add_argument("--install-git-hook", action="store_true")
    group.add_argument("--query", choices=["replace-legacy-pre-push"])
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("remainder", nargs="*", help=argparse.SUPPRESS)  # git passes remote name/url
    args = parser.parse_args(argv)
    if args.install_git_hook:
        root = git_toplevel(args.repo_root or Path.cwd())
        if root is None:
            print("finalise-gate: not a git repository", file=sys.stderr)
            return 1
        return install_git_hook(root)
    if args.query:
        root = git_toplevel(args.repo_root or Path.cwd())
        try:
            section, _ = load_qa_config(root) if root else ({}, False)
        except ValueError:
            return 1
        return 0 if section.get("replace_legacy_pre_push") is True else 1
    if args.hook == "git-pre-push":
        return git_pre_push()
    payload = read_payload()
    trigger, start, bypass, _command = hook_target(args.hook, payload)
    if trigger is None:
        return emit(args.hook, "allow", "")
    return gate(args.hook, start, bypass, trigger)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
