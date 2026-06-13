#!/usr/bin/env python3
"""Pre-Tool Use Hook (Bash matcher) — 5s budget.

Only intercepts git commit commands. Warns about:
1. High edit count without a session report
2. Staged changes without a self-code-review
"""

import json
import sys
import time
from pathlib import Path

STATE_FILE = Path(".git/.session-state.json")
REPORT_DIR = Path(".claude/reports")
EDIT_THRESHOLD = 10


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def has_recent_report(max_age=3600):
    """Check if a session report was generated in the last hour."""
    if not REPORT_DIR.exists():
        return False
    reports = sorted(REPORT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return False
    age = time.time() - reports[0].stat().st_mtime
    return age < max_age


def main():
    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Only intercept Bash tool
    if tool_name != "Bash":
        return

    command = tool_input.get("command", "")

    # Only care about git commit commands
    if "git commit" not in command:
        return

    state = load_state()
    edit_count = state.get("edit_count", 0)

    messages = []

    if edit_count > EDIT_THRESHOLD and not has_recent_report():
        messages.append({
            "type": "systemMessage",
            "message": (
                f"Note: {edit_count} edits this session without a session report. "
                f"Consider running /session-report before committing to capture "
                f"decisions and patterns."
            )
        })

    # Remind about self-code-review skill for non-trivial changes
    if edit_count > 3 and not state.get("self_review_done"):
        messages.append({
            "type": "systemMessage",
            "message": (
                "Reminder: Run a self-code-review of staged changes before "
                "committing. Check for hardcoded secrets, missing auth markers, "
                "SQL injection, and error handling gaps."
            )
        })

    if messages:
        print(json.dumps(messages[:2]))


if __name__ == "__main__":
    main()
