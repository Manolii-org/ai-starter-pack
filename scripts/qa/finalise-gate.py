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
  claude        Claude Code PreToolUse (matcher Bash|mcp__.*__create_pull_request)
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
import signal
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
# Recipe exit code for "this check could not run here" (no browser, no dev
# server): incomplete validation, never a source failure (docs/qa-framework.md).
UNAVAILABLE_EXIT = 3
DEFAULT_TIMEOUT_SECONDS = 1100  # below the 1200s host hook timeout
TAIL_LINES = 30
TAIL_LINE_CHARS = 240
# Any MCP server name: mcp__github__…, mcp__plugin_github_github__…, etc.
PR_TOOL = re.compile(r"^mcp__.+__create_pull_request$")
BYPASS_ENV = "AGENT_FINALISE_GATE"


# ── command classification ──────────────────────────────────────────────────

_SEPARATORS = "();<>|&\n"
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Heredoc bodies are data (commit messages, fixtures), never commands.
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
# Last resort for unbalanced quoting: only a git/gh word at a command start.
_FALLBACK = re.compile(r"(?:^|[;&|(\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:\S*/)?"
                       r"(?:git\s(?:[^;&|\n'\"]*\s)?push\b|gh\s+pr\s+create\b)")
_WRAPPERS = {"env", "sudo", "doas", "command", "exec", "time", "nice", "nohup", "timeout", "xargs"}
_WRAPPER_VALUE_OPTS = {"sudo": {"-u", "-g", "-C", "-h", "-p"}, "env": {"-u", "-C", "-S"},
                       "timeout": {"-s", "-k", "--signal", "--kill-after"}, "nice": {"-n"}}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                   "--super-prefix", "--config-env"}


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def _simple_commands(tokens: list[str]):
    """Yield ("cmd", words) per simple command and ("open"|"close", None) at
    each subshell boundary, so a `cd` inside `( … )` stays inside it."""
    current: list[str] = []
    for token in tokens:
        if token and all(c in _SEPARATORS for c in token):
            if current:
                yield "cmd", current
            current = []
            for char in token:
                if char == "(":
                    yield "open", None
                elif char == ")":
                    yield "close", None
        else:
            current.append(token)
    if current:
        yield "cmd", current


def _strip_wrappers(words: list[str]) -> tuple[list[str], bool]:
    """Drop VAR=value prefixes and transparent wrappers (env, sudo, timeout, …).

    Returns the remaining words and whether this simple command itself
    carries AGENT_FINALISE_GATE=off (a prefix applies to that command only).
    """
    bypass = False
    while words:
        if _ASSIGNMENT.match(words[0]):
            name, _, value = words.pop(0).partition("=")
            if name == BYPASS_ENV:
                bypass = value.lower() == "off"
            continue
        head = os.path.basename(words[0])
        if head not in _WRAPPERS:
            return words, bypass
        words.pop(0)
        value_opts = _WRAPPER_VALUE_OPTS.get(head, set())
        while words and words[0].startswith("-"):
            option = words.pop(0)
            if option in value_opts and words:
                words.pop(0)
        if head == "timeout" and words:
            words.pop(0)  # the duration
    return words, bypass


def _classify(command: str, cwd: str | None, result: dict, depth: int) -> None:
    try:
        tokens = _tokens(_HEREDOC.sub("", command))
    except ValueError:
        if _FALLBACK.search(command):
            result["trigger"] = result["trigger"] or "push"
            result["repo_dir"] = result["repo_dir"] or cwd
        return
    stack: list[str | None] = []
    for kind, words in _simple_commands(tokens):
        if kind == "open":
            stack.append(cwd)  # a subshell's cd never escapes it
            continue
        if kind == "close":
            cwd = stack.pop() if stack else cwd
            continue
        # Brace groups run in the current shell, so their cd persists.
        while words and words[0] in ("{", "}"):
            words = words[1:]
        while words and words[-1] in ("{", "}"):
            words = words[:-1]
        words, local_bypass = _strip_wrappers(list(words))
        if not words:
            continue
        head = os.path.basename(words[0])
        # `export AGENT_FINALISE_GATE=off` really does reach later commands in
        # the same shell; `unset` withdraws it.
        if head == "export":
            for word in words[1:]:
                name, _, value = word.partition("=")
                if name == BYPASS_ENV:
                    result["_exported"] = value.lower() == "off"
            continue
        if head == "unset" and BYPASS_ENV in words[1:]:
            result["_exported"] = False
            continue
        bypassed = local_bypass or result.get("_exported", False)
        if head in ("cd", "pushd"):
            target = next((w for w in words[1:] if not w.startswith("-")), None)
            if target and target != "-":
                cwd = os.path.join(cwd or "", os.path.expanduser(target))
            continue
        if head in _SHELLS and "-c" in words[1:] and depth < 3:
            index = words.index("-c", 1)
            if index + 1 < len(words):
                inner = dict(result, _exported=bypassed)
                _classify(words[index + 1], cwd, inner, depth + 1)
                result.update({k: v for k, v in inner.items() if k != "_exported"})
            continue
        if head == "gh" and words[1:3] == ["pr", "create"]:
            result["trigger"] = result["trigger"] or "pr"
            result["repo_dir"] = result["repo_dir"] or cwd
            result["_handoffs"].append(bypassed)
            continue
        if head != "git":
            continue
        index, repo_dir = 1, cwd
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            if option in _GIT_VALUE_OPTS and index + 1 < len(words):
                if option == "-C":
                    repo_dir = os.path.join(repo_dir or "", os.path.expanduser(words[index + 1]))
                index += 2
                continue
            index += 1
        if index >= len(words) or words[index] != "push":
            continue
        args = words[index + 1:]
        if any(a in ("--dry-run", "-n", "--delete", "-d") for a in args):
            continue
        refspecs = [a for a in args if not a.startswith("-")][1:]
        if refspecs and all(r.startswith(":") for r in refspecs):
            continue  # deleting remote refs is not a candidate hand-off
        result["trigger"] = result["trigger"] or "push"
        result["repo_dir"] = result["repo_dir"] or repo_dir
        result["_handoffs"].append(bypassed)


def classify_command(command: str) -> dict:
    """Return {"trigger": None|"push"|"pr", "repo_dir": str|None, "bypass": bool}.

    Only a real hand-off triggers the gate: dry runs and ref deletions do not.
    Quotes, heredoc bodies and command substitutions inside quotes are data,
    so a commit message or grep pattern that mentions `git push` never
    triggers. `cd`, `git -C`, wrappers (env, sudo, timeout …) and `sh -c`
    strings are followed. The inline bypass counts only when it reaches the
    hand-off itself (a prefix on that command, or an earlier `export`). Unbalanced quoting falls back to a regex anchored
    at a command start.
    """
    result = {"trigger": None, "repo_dir": None, "_handoffs": []}
    _classify(command or "", None, result, 0)
    handoffs = result.pop("_handoffs")
    result.pop("_exported", None)
    # Bypassed only when every hand-off in the command carries the bypass;
    # an assignment on some other command never reaches the push.
    result["bypass"] = bool(handoffs) and all(handoffs)
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
    mode = section.get("mode", DEFAULT_MODE_WITHOUT_CONFIG)
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
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        # Own process group: a timeout must also stop the recipes' children
        # (dev servers, browsers), not just agent-check itself.
        proc = subprocess.Popen(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
    duration_ms = round((time.monotonic() - start) * 1000)
    receipt = {}
    receipt_path = state / "receipt.json"
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = {}
    results = receipt.get("results", []) if isinstance(receipt, dict) else []
    environments = environment_ids(root)
    failed, unavailable = [], []
    for item in results:
        if not isinstance(item, dict) or item.get("status") not in ("failed", "unavailable"):
            continue
        # Setup failures (npm ci on a flaky network), per-check timeouts and
        # self-declared unavailability are environment faults, not defects.
        environmental = (item.get("status") == "unavailable" or item.get("id") in environments
                         or item.get("reason") in ("timeout", f"exit={UNAVAILABLE_EXIT}"))
        (unavailable if environmental else failed).append(item)
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
        incomplete = "; could not run: " + ", ".join(
            f"{r.get('id')} ({r.get('reason')})" for r in unavailable) if unavailable else ""
        return {**base, "outcome": "fail", "summary": f"failed: {names}{incomplete}"}
    if unavailable:
        names = ", ".join(f"{r.get('id')} ({r.get('reason')})" for r in unavailable)
        return {**base, "outcome": "incomplete", "summary": f"unavailable: {names}"}
    return {**base, "outcome": "fail", "summary": f"agent-check exited {code}"}


def environment_ids(root: Path) -> set[str]:
    try:
        config = json.loads((root / "config" / "agent-checks.json").read_text(encoding="utf-8"))
        return {str(e.get("id")) for e in config.get("environments", []) if isinstance(e, dict)}
    except (OSError, ValueError, AttributeError):
        return set()


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
        count = TAIL_LINES if result["outcome"] == "fail" else 10
        log_tail = tail(Path(result["root"]) / result["log"], count)
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


def workspaces_for(adapter: str, payload: dict) -> list[Path]:
    """The trust boundary the user opened; the gate never runs code outside it."""
    if adapter == "cursor":
        roots = [r for r in payload.get("workspace_roots") or [] if isinstance(r, str) and r]
    elif adapter == "claude":
        roots = [os.environ.get("CLAUDE_PROJECT_DIR", "")]
    else:
        roots = []
    return [Path(r).resolve() for r in roots if r and Path(r).is_dir()]


def within(root: Path, workspaces: list[Path]) -> bool:
    if not workspaces:
        return True
    resolved = root.resolve()
    return any(resolved == ws or ws in resolved.parents for ws in workspaces)


def hook_target(adapter: str, payload: dict) -> tuple[str | None, str | None, bool, str]:
    """Return (trigger, start_dir, bypass, command) for a hook payload."""
    if adapter == "cursor":
        command = str(payload.get("command") or "")
        roots = [r for r in payload.get("workspace_roots") or [] if isinstance(r, str) and r]
        start = payload.get("cwd") or None
        # Cursor may report cwd as "" or "/"; fall back to the first workspace
        # root that is a repository rather than silently allowing the push.
        if roots and (not start or git_toplevel(Path(start)) is None):
            start = next((r for r in roots if git_toplevel(Path(r))), roots[0])
    else:
        tool = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        start = payload.get("cwd")
        if PR_TOOL.match(tool):
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
        if adapter in ("claude", "codex"):
            # Model-visible without blocking: the agent learns what failed
            # or could not run, and the hand-off proceeds.
            response = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "additionalContext": text}}
            if adapter == "claude":
                response["systemMessage"] = text.splitlines()[0]
            print(json.dumps(response))
        else:
            print(text, file=sys.stderr)
    return 0


def gate(adapter: str, start: str | None, bypass_inline: bool, trigger: str,
         workspaces: list[Path] | None = None) -> int:
    root = git_toplevel(Path(start or os.getcwd()))
    if root is None or not (root / "config" / "agent-checks.json").is_file():
        return emit(adapter, "allow", "")
    if not within(root, workspaces or []):
        # A repository outside the opened workspace (a nested third-party
        # clone elsewhere) is not trusted to run its recipes from a hook.
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
    return gate(args.hook, start, bypass, trigger, workspaces_for(args.hook, payload))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
