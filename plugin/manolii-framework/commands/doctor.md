---
name: doctor
version: 1.0.0
description: Scan session transcripts for agent dysfunction signals (edit-thrashing, error-loops, repeated instructions, correction-heavy turns) and propose CLAUDE.md/persistent-instructions rules
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: [session, memory, reflection, claude-code]
---

# /doctor — Session Dysfunction Detector

Post-hoc analysis of Claude Code session transcripts. Detects behavioural anti-patterns and proposes rules for `.claude/persistent-instructions.md` or `CLAUDE.md`. **Proposes only — never auto-applies.**

## When to run

- End of a session that felt frustrating or slow (before `/wrap-up`)
- Weekly as part of project health review
- After onboarding a new workflow where you're unsure which CLAUDE.md rules it stressed

## Steps

### 1. Run the detector

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor-analyse.py"
```

Flags:
- `--session <path>` — analyse a specific `.jsonl` session log file
- `--stdin` — read session events from stdin (mutually exclusive with `--session`)
- `--output <path>` — write JSON report to file (default: stdout)

Exit codes: `0` = no signals · `2` = one or more signals detected.

### 2. Review findings

Signals detected:

| Signal | Trigger | Maps to |
|---|---|---|
| `edit-thrashing` | Same file edited ≥5× in one session | Plan-then-edit discipline |
| `error-loop` | ≥3 consecutive tool errors | Change approach after failures |
| `repeated-instructions` | Near-duplicate user messages (Jaccard ≥0.6, ≥5 words) | Re-read user messages |
| `correction-heavy` | ≥20% of user turns contain correction keywords | Pause on correction |
| `rapid-corrections` | ≥15% of consecutive user→assistant pairs are corrections | Slow down between steps |

### 3. Triage proposed rules

For each proposed rule:
- **Adopt** — genuinely recurring pattern. Add the rule to `.claude/persistent-instructions.md`.
- **Defer** — one-off; watch for repeats.
- **Dismiss** — false positive (e.g. deliberate iteration on a file).

**Do not blindly paste all proposed rules.** The detectors see symptoms, not intent.

### 4. Output

```json
{
  "session_file": ".ai/session-logs/2026-05-06.jsonl",
  "signals": [
    {
      "type": "A",
      "label": "edit-thrashing",
      "evidence": "src/api.ts edited 6× in this session",
      "rule": "Before editing a file for the 3rd time in one session, stop and restate the goal."
    }
  ],
  "rules": ["...proposed CLAUDE.md rule text..."]
}
```

## Notes

- The script prints a JSON report to stdout or writes to `--output <path>`. There is no automatic persistence to `.ai/memory/` — save the output manually if you want to track trends.
- Detection thresholds (e.g. ≥5 edits, ≥3 errors, Jaccard 0.6) are embedded in `_analyse()` in `scripts/doctor-analyse.py`.
- Reads the most recent `.jsonl` file in `.ai/session-logs/` by default; use `--session <path>` to specify a different file.
