#!/usr/bin/env python3
"""PostToolUse hook: detect advisor tool calls, log to .ai/metrics/advisor-usage.jsonl.

Additive hook — does NOT replace scripts/post-tool-use.py.
Wired as a separate entry in settings.json PostToolUse hooks array.
"""
import json
import os
import re
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_scripts_dir = os.path.join(_REPO_ROOT, "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

try:
    data = json.loads(sys.stdin.read())
except json.JSONDecodeError as exc:
    print(f"[advisor-metrics-hook] Invalid JSON from stdin: {exc}", file=sys.stderr)
    sys.exit(0)
except Exception as exc:
    print(f"[advisor-metrics-hook] Failed to read stdin: {exc}", file=sys.stderr)
    sys.exit(0)

if not isinstance(data, dict):
    sys.exit(0)

tool_name = data.get("tool_name", "")

if tool_name != "advisor":
    sys.exit(0)

_output_parts = []


def _emit(msg: str) -> None:
    _output_parts.append(msg)


def _flush() -> None:
    if _output_parts:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n\n".join(_output_parts),
            }
        }))


try:
    session_id = data.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")

    advisor_tokens_in = 0
    advisor_tokens_out = 0
    latency_ms = 0

    # Bounds applied to both dict-path and string-path (H3 + dict-path gap fix).
    _MAX_TOKENS = 10_000_000   # 10M — above any real advisor call
    _MAX_LATENCY_MS = 3_600_000  # 1 hour — above any real call

    raw_resp = data.get("tool_response")
    if isinstance(raw_resp, dict):
        usage = raw_resp.get("usage", {})
        if isinstance(usage, dict):
            advisor_tokens_in = min(int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0), _MAX_TOKENS)
            advisor_tokens_out = min(int(usage.get("output_tokens") or usage.get("completion_tokens") or 0), _MAX_TOKENS)
        for k in ("duration_ms", "latency_ms", "elapsed_ms"):
            if isinstance(raw_resp.get(k), (int, float)):
                latency_ms = min(int(raw_resp[k]), _MAX_LATENCY_MS)
                break
    elif isinstance(raw_resp, str) and raw_resp:
        _tail = raw_resp[-2000:] if len(raw_resp) > 2000 else raw_resp
        _SEP = r'[":=\s]+'  # tightened: require at least one separator char
        m = re.search(r"input_tokens" + _SEP + r"(\d{1,8})", _tail)
        if m:
            advisor_tokens_in = min(int(m.group(1)), _MAX_TOKENS)
        m = re.search(r"output_tokens" + _SEP + r"(\d{1,8})", _tail)
        if m:
            advisor_tokens_out = min(int(m.group(1)), _MAX_TOKENS)
        m = re.search(r"duration_ms" + _SEP + r"(\d{1,9})", _tail)
        if m:
            latency_ms = min(int(m.group(1)), _MAX_LATENCY_MS)

    advisor_model = os.environ.get("MAIN_THREAD_ADVISOR_MODEL", "claude-opus-4-7")
    executor_model = os.environ.get("MAIN_THREAD_EXECUTOR_MODEL", "claude-sonnet-4-6")

    try:
        from advisor_metrics import (
            CIRCUIT_BREAKER_THRESHOLD,
            circuit_breaker_check,
            log_advisor_invocation,
        )

        if circuit_breaker_check(session_id, threshold=CIRCUIT_BREAKER_THRESHOLD):
            _emit(
                f"[ADVISOR-CIRCUIT-BREAKER] Session `{session_id}` has invoked the advisor "
                f"≥50 times. This is far above normal operation (typical: 1-5). "
                f"Investigate for runaway escalation loop before continuing."
            )

        log_advisor_invocation(
            session_id=session_id,
            workload_class="general_main_thread",
            executor_model=executor_model,
            advisor_model=advisor_model,
            escalation_trigger="manual",
            executor_tokens_in=0,
            executor_tokens_out=0,
            advisor_tokens_in=advisor_tokens_in,
            advisor_tokens_out=advisor_tokens_out,
            latency_ms=latency_ms,
            outcome="plan_followed",
            actual_provider="Anthropic",
            action_type="advisor_invocation_main_thread",
        )
    except ImportError as exc:
        print(f"[advisor-metrics-hook] ImportError loading advisor_metrics: {exc}", file=sys.stderr)

except Exception as exc:
    print(f"[advisor-metrics-hook] Unexpected error: {exc}", file=sys.stderr)

_flush()
sys.exit(0)
