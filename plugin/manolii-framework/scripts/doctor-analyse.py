#!/usr/bin/env python3
"""
doctor-analyse.py — Detect session dysfunction signals from Claude Code transcripts.

Called by the /doctor command. Reads the most recent session transcript from
.ai/session-logs/ (or stdin with --stdin) and flags dysfunction patterns:

  A. Edit-thrashing    — same file edited ≥5 times in one session
  B. Error-loop        — ≥3 consecutive tool errors
  C. Repeated prompt   — near-duplicate user messages (Jaccard ≥0.6)
  D. Correction-heavy  — ≥20% of turns contain user corrections
  E. Rapid corrections — ≥15% of consecutive turn pairs are correction→fix

Outputs a JSON report to stdout (or --output path):
  {"signals": [...], "rules": [...], "session_file": "..."}

Each signal: {"type": "A|B|C|D|E", "label": str, "evidence": str, "rule": str}
Each rule:   proposed CLAUDE.md rule text to add.

Exit codes:
  0 = no signals detected
  2 = one or more signals detected (non-blocking, informational)
"""
import argparse
import json
import pathlib
import re
import sys


def _latest_session_log(logs_dir: pathlib.Path) -> pathlib.Path | None:
    logs = sorted(logs_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _load_events(path: pathlib.Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"[doctor] skipping malformed line: {exc} — {line[:80]!r}", file=sys.stderr)
    return events


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _content_text(content) -> str:
    """Normalize a transcript message 'content' to plain text.

    Claude Code stores content as either a string or a list of content blocks
    (e.g. [{"type": "text", "text": "..."}]); extract text from both forms.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _analyse(events: list[dict]) -> list[dict]:
    signals: list[dict] = []

    # --- A: Edit-thrashing ---
    edit_counts: dict[str, int] = {}
    for ev in events:
        if ev.get("type") == "tool_use" and ev.get("name") in ("Edit", "Write"):
            path = (ev.get("input") or {}).get("file_path", "")
            if path:
                edit_counts[path] = edit_counts.get(path, 0) + 1
    thrashed = [(p, n) for p, n in edit_counts.items() if n >= 5]
    for p, n in thrashed:
        signals.append({
            "type": "A",
            "label": "edit-thrashing",
            "evidence": f"{p} edited {n}× in this session",
            "rule": "Before editing a file for the 3rd time in one session, stop and restate the goal. Root cause must precede any further edits.",
        })

    # --- B: Error-loop ---
    consecutive_errors = 0
    max_consecutive = 0
    for ev in events:
        if ev.get("type") == "tool_result" and ev.get("is_error"):
            consecutive_errors += 1
            max_consecutive = max(max_consecutive, consecutive_errors)
        else:
            consecutive_errors = 0
    if max_consecutive >= 3:
        signals.append({
            "type": "B",
            "label": "error-loop",
            "evidence": f"{max_consecutive} consecutive tool errors detected",
            "rule": "After 2 consecutive tool errors on the same approach, stop and call advisor() before retrying.",
        })

    # --- C/D/E: User message analysis ---
    user_messages = [
        _content_text(ev.get("content"))
        for ev in events
        if ev.get("role") == "user"
    ]
    user_messages = [m for m in user_messages if m]

    correction_keywords = re.compile(
        r"\b(no[,.]?|wrong|incorrect|that'?s not|don'?t|stop|wait|actually|re-?do|undo|revert|you missed|not what)\b",
        re.I,
    )

    # C: Repeated prompt
    for i, msg in enumerate(user_messages):
        for j, other in enumerate(user_messages):
            if i >= j:
                continue
            if _jaccard(msg, other) >= 0.6 and len(msg.split()) >= 5:
                signals.append({
                    "type": "C",
                    "label": "repeated-instructions",
                    "evidence": f"User messages {i+1} and {j+1} are near-duplicates (Jaccard ≥0.6)",
                    "rule": "When the user repeats an instruction, acknowledge the original was not followed and state explicitly what was missed.",
                })
                break
        else:
            continue
        break

    # D: Correction-heavy
    if user_messages:
        corrections = sum(1 for m in user_messages if correction_keywords.search(m))
        ratio = corrections / len(user_messages)
        if ratio >= 0.20:
            signals.append({
                "type": "D",
                "label": "correction-heavy",
                "evidence": f"{corrections}/{len(user_messages)} user turns ({ratio:.0%}) contain corrections",
                "rule": "After every tool action that modifies state, verify the outcome matches the stated goal before proceeding.",
            })

    # E: Rapid corrections
    if len(user_messages) >= 4:
        correction_pairs = sum(
            1 for i in range(len(user_messages) - 1)
            if correction_keywords.search(user_messages[i + 1])
        )
        pair_ratio = correction_pairs / (len(user_messages) - 1)
        if pair_ratio >= 0.15:
            signals.append({
                "type": "E",
                "label": "rapid-corrections",
                "evidence": f"{correction_pairs}/{len(user_messages)-1} consecutive turn pairs ({pair_ratio:.0%}) are correction→fix",
                "rule": "Slow down: confirm understanding with a one-sentence restatement before executing any multi-step change.",
            })

    return signals


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect session dysfunction signals.")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--session", default="", help="Path to session .jsonl file")
    source_group.add_argument("--stdin", action="store_true", help="Read events from stdin")
    parser.add_argument("--output", default="", help="Write JSON report to this path")
    args = parser.parse_args()

    if args.stdin:
        lines = sys.stdin.read().splitlines()
        events: list[dict] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[doctor] skipping malformed stdin line: {exc} — {line[:80]!r}", file=sys.stderr)
        session_file = "<stdin>"
    elif args.session:
        p = pathlib.Path(args.session)
        events = _load_events(p)
        session_file = str(p)
    else:
        logs_dir = pathlib.Path(".ai/session-logs")
        latest = _latest_session_log(logs_dir)
        if not latest:
            print("[doctor] No session log found in .ai/session-logs/ — nothing to analyse.")
            sys.exit(0)
        events = _load_events(latest)
        session_file = str(latest)

    signals = _analyse(events)
    rules = list({s["rule"] for s in signals})

    report = {
        "session_file": session_file,
        "signals": signals,
        "rules": rules,
    }

    if args.output:
        pathlib.Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    else:
        print(json.dumps(report, indent=2))

    sys.exit(2 if signals else 0)


if __name__ == "__main__":
    main()
