---
description: "Capture a summary of the current session for persistent memory"
---

# /session-summary

Capture a summary of the current session for persistent memory. Run `/extract-insights` first (to capture implicit knowledge), then run this command to finalise the session record.

## Steps

1. **Gather session context** by reviewing your conversation history:
   - What branch are you on? (`git branch --show-current`)
   - What files were modified? (`git diff --name-only HEAD~5..HEAD 2>/dev/null || git diff --name-only`)
   - What key decisions were made during this session?
   - What patterns were discovered?
   - What issues remain unresolved?

2. **Draft a session summary** with these fields:
   - Before saving: **redact** secrets, tokens, account numbers, personal identifiers, and sensitive references — use generalised descriptions instead
   - `session_date`: today's date (ISO format, date only)
   - `branch`: current git branch
   - `files_modified`: array of modified file paths (max 20)
   - `decisions`: array of 1-2 sentence decision summaries (max 5)
   - `patterns_discovered`: array of pattern descriptions (max 3)
   - `issues_unresolved`: array of open blockers/questions (max 3)
   - `tags`: relevant domain tags

3. **Append** a JSON line to `.ai/memory/sessions.jsonl`:

```json
{"session_date": "YYYY-MM-DD", "branch": "...", "files_modified": [...], "decisions": [...], "patterns_discovered": [...], "issues_unresolved": [...], "tags": [...]}
```

4. **Confirm** what was saved and display the summary.

5. **Regenerate knowledge index** — update the human-browsable index with this session's data:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/generate-knowledge-index.sh"
   ```

## Auto-trigger guidance

Run `/extract-insights` then `/session-summary` at the end of significant sessions. Also triggered by asking "summarize this session" or "save session notes."
