# Checkpoint — Save Work State

Capture current work state for session resumption.

## Gather

1. Current branch: `git rev-parse --abbrev-ref HEAD`
2. Modified files: `git status --short`
3. Recent commits: `git log --oneline -5`
4. Session state from `.git/.session-state.json`

## Checkpoint Structure

```json
{
  "timestamp": "{ISO_DATE}",
  "branch": "{BRANCH}",
  "last_commit": "{SHA}",
  "modified_files": ["{FILES}"],
  "current_task": "{DESCRIPTION — agent-populated, not auto-tracked}",
  "key_decisions": ["{DECISIONS — agent-populated}"],
  "unresolved_issues": ["{ISSUES — agent-populated}"],
  "next_steps": ["{STEPS — agent-populated}"]
}
```

## Save

Save to `.claude/checkpoints/checkpoint-{YYYY-MM-DD-HHMMSS}.json`

## Usage

At session start, check for latest checkpoint:
```bash
ls -t .claude/checkpoints/ | head -1
```
