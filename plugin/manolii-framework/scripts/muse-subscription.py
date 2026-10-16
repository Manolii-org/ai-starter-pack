#!/usr/bin/env python3
"""Run a native Muse subscription task without shared tenant infrastructure."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

MODEL = "muse-spark-1.3"
MAX_EVENT_RECORD_BYTES = 16 << 20
FORBIDDEN = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "META_API_KEY", "MODEL_API_KEY")
RUNTIME_ENV = {
    "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "RUNNER_TRACKING_ID",
}


class ProcessStartupError(RuntimeError):
    """The provider process could not be started; retain a terminal receipt."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def safe_text(text: str, key: str) -> str:
    if key:
        values = {key, key.encode().hex(), key[::-1], quote(key, safe=""), quote_plus(key),
                  json.dumps(key)[1:-1]}
        for encoded in (base64.b64encode(key.encode()).decode(), base64.urlsafe_b64encode(key.encode()).decode()):
            values.update((encoded, encoded.rstrip("=")))
        for value in sorted(values, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
    return text


def terminal_output(path: Path, key: str) -> tuple[str | None, str]:
    """Parse the v1 durable envelope; exit zero alone never proves completion."""
    terminal, text, partial = None, "", ""
    with path.open("rb") as stream:
        # Bound reader memory without silently skipping a large terminal.
        while line := stream.readline(MAX_EVENT_RECORD_BYTES + 1):
            if len(line) > MAX_EVENT_RECORD_BYTES:
                return "output_limit_exceeded", safe_text(partial, key)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            payload, kind = event.get("payload"), event.get("payload_type")
            if not isinstance(payload, dict) or not isinstance(kind, str):
                continue
            if kind == "run.output.delta" and isinstance(payload.get("text"), str):
                partial = (partial + payload["text"][-65536:])[-65536:]
            elif (event.get("record_type") == "event" and event.get("schema_version") == 1
                  and kind.startswith("run.terminal.") and payload.get("kind") == "run_terminal"):
                state = payload.get("terminal")
                terminal = state if (state in {"completed", "failed", "cancelled", "interrupted"}
                                     and kind == f"run.terminal.{state}") else "unknown"
                text = payload.get("text") if isinstance(payload.get("text"), str) else ""
    # Partial deltas are recovery evidence, never an eligible completed answer.
    if terminal == "completed" and not text.strip():
        terminal = "unknown"
    return terminal, safe_text(text or partial, key)


def protected_write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(text)


def stop_process(process: subprocess.Popen) -> None:
    """Stop the inference process and its tool children on POSIX hosts."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass  # It exited between the graceful wait and forced kill.
        process.wait(timeout=5)


def execute(command: list[str], workdir: Path, env: dict, stdout, stderr, timeout: int) -> tuple[int, str | None]:
    """Propagate cancellation to provider/tool processes and retain final evidence."""
    try:
        process = subprocess.Popen(command, cwd=workdir, env=env, stdout=stdout, stderr=stderr,
                                   start_new_session=os.name == "posix")
    except OSError as exc:
        raise ProcessStartupError(type(exc).__name__) from exc
    interrupted = False
    handlers = {}

    def cancel(_signum, _frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        stop_process(process)

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                handlers[signum] = signal.signal(signum, cancel)
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process(process)
            return 124, "timed_out"
        if interrupted:
            return 143, "interrupted"
        return result, "interrupted" if result in (-signal.SIGTERM, -signal.SIGINT) else None
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def run(args, env: dict[str, str]) -> int:
    workdir, output = args.workdir.resolve(), args.output_dir.resolve()
    prompt = args.prompt_file.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    identity = {"operation_id": args.operation_id, "prompt_sha256": digest(prompt),
                "workdir": str(workdir), "read_only": args.read_only, "model": MODEL,
                "reasoning_effort": args.reasoning_effort, "max_model_steps": args.max_model_steps,
                "timeout_seconds": args.timeout_seconds}
    if output.is_relative_to(workdir):
        raise ValueError("Protected receipts must be outside the workspace (for example RUNNER_TEMP)")
    if output.exists() and not args.dry_run:
        receipt_path, final_path = output / "receipt.json", output / "final.md"
        if receipt_path.is_file() and final_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("Existing receipt is invalid; reconciliation required")
            final = final_path.read_text(encoding="utf-8")
            if (all(receipt.get(k) == v for k, v in identity.items()) and receipt.get("status") == "completed"
                    and final.strip() and receipt.get("final_sha256") == digest(final)):
                print(json.dumps({**receipt, "reused_completed_result": True}))
                return 0
        raise ValueError("Existing operation needs reconciliation; paid inference was not repeated")
    if not workdir.is_dir():
        raise ValueError("Workdir must be an existing directory")
    if any(env.get(name) for name in FORBIDDEN):
        raise ValueError("Remove ambient usage-priced provider credentials before subscription execution")
    key = (env.get("MUSE_CODE_SUBSCRIPTION_KEY") or "").strip()
    if not key:
        raise ValueError("Tenant-owned MUSE_CODE_SUBSCRIPTION_KEY is not provisioned")
    executable = shutil.which("muse", path=env.get("PATH"))
    if not executable:
        raise ValueError("Install the official Muse CLI before running this task")
    executable = os.path.abspath(executable)  # Discovery uses the caller's PATH, execution uses workdir.
    clean = {k: v for k, v in env.items() if k in RUNTIME_ENV}
    receipt = {"version": 1, **identity, "runtime_provider": "muse_code", "transport": "native_cli",
               "billing_mode": "subscription", "allow_usage_priced_api": False, "model": MODEL,
               "started_at": datetime.now(UTC).isoformat(), "subscription_usage_available": False,
               "billing_evidence": "declared_credential_contract", "account_plan_verified": False}
    with tempfile.TemporaryDirectory(prefix="muse-home-") as home:
        clean["HOME"] = home
        version = subprocess.run([executable, "--version"], env=clean, capture_output=True,
                                 text=True, timeout=15, check=False)
        version_text = (version.stdout + version.stderr).strip()
        match = re.search(r"\bmuse(?:\s+code)?\s+(\d+)\.(\d+)\.(\d+)\b", version_text, re.IGNORECASE)
        if version.returncode or not match or tuple(map(int, match.groups())) < (1, 4, 9):
            raise ValueError("Muse CLI 1.4.9 or newer is required before credential injection")
        help_result = subprocess.run([executable, "exec", "--help"], env=clean, capture_output=True,
                                     text=True, timeout=15, check=False)
        help_text = help_result.stdout + help_result.stderr
        if help_result.returncode or "sandbox are ON by default" not in help_text:
            raise ValueError("Installed CLI does not attest the expected sandbox default")
        receipt["cli_version"] = version_text[:160]
        receipt["sandbox_default_verified"] = True
        receipt["exec_help_sha256"] = digest(help_text)
        if args.dry_run:
            print(json.dumps({**receipt, "status": "dry_run"}))
            return 0
        output.mkdir(parents=True, mode=0o700)
        output.chmod(0o700)
        receipt["status"] = "running"
        protected_write(output / "receipt.json", json.dumps(receipt, indent=2) + "\n")
        # Execute exactly the instructions bound to the receipt, even if the
        # caller's source file changes after it was read and hashed.
        prompt_snapshot = Path(home) / "prompt.md"
        protected_write(prompt_snapshot, prompt)
        # Parent secrets and personal configuration never enter the provider
        # process. Only the explicitly named CLI subscription key is injected.
        clean["META_API_KEY"] = key
        command = [executable, "exec", "--json", "--model", MODEL, "--provider", "meta",
                   "--reasoning-effort", args.reasoning_effort, "--contributor-mode", "off",
                   "--disable-approval", "--trust-workspace",
                   "--no-foreign-personal-context", "--disable-web-tools", "--disable-reminders",
                   "--disable-request-user-input", "-c", 'run.subagent_delegation_mode="off"',
                   "--max-model-steps", str(args.max_model_steps), "--prompt-file", str(prompt_snapshot)]
        if args.read_only:
            command.extend(["--disable-write", "--disable-shell"])
        trace, errors = output / "events.jsonl", output / "stderr.txt"
        with (
            os.fdopen(os.open(trace, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stdout,
            os.fdopen(os.open(errors, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stderr,
        ):
            try:
                exit_code, stopped = execute(command, workdir, clean, stdout, stderr, args.timeout_seconds)
            except ProcessStartupError as exc:
                exit_code, stopped = 127, "startup_failed"
                receipt["startup_error"] = str(exc)
        terminal, final = terminal_output(trace, key)
        if terminal == "output_limit_exceeded":
            receipt["output_parse_error"] = "event_record_exceeds_16_mib"
            terminal = None
        protected_write(output / "final.md", final)
        # Raw events/stderr are private diagnostics and must never be uploaded
        # wholesale. Workflow artifacts contain only the receipt and final.
        receipt.update(exit_code=exit_code, provider_terminal=terminal,
                       final_message_available=bool(final.strip()), final_sha256=digest(final),
                       completed_at=datetime.now(UTC).isoformat())
        receipt["status"] = "completed" if exit_code == 0 and terminal == "completed" and final.strip() else "failed"
        if stopped:
            receipt["status"] = stopped
        protected_write(output / "receipt.json", json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    return 0 if receipt["status"] == "completed" else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"), default="high")
    parser.add_argument("--max-model-steps", type=int, default=80)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--read-only", action="store_true", help="Disable shell and write tools for a provider canary.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.max_model_steps < 1 or args.timeout_seconds < 1 or not args.operation_id.strip():
        parser.error("Operation ID and positive step/time limits are required")
    try:
        return run(args, dict(os.environ))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
