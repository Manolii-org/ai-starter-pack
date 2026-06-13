---
name: drift-check
version: 1.0.0
description: "Run project drift detection — checks agent routing, hook scripts, memory health, and stale checkpoints."
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags:
  - system
  - workflow
  - claude-code
---

# /drift-check — Project Drift Detector

Detects silent configuration drift before it compounds across sessions.

## Checks Performed

| Check | What It Finds |
|-------|--------------|
| **Agent routing lint** | Violations in data_sensitivity, model assignment, missing frontmatter |
| **Hook scripts** | Missing or non-executable scripts referenced in `.claude/settings.json` |
| **Memory health** | Malformed entries in local JSONL memory files |
| **Stale checkpoint** | Active task checkpoint older than 24h with an in-flight step |

## Usage

```
/drift-check
```

Or run directly:
```bash
python3 scripts/lint-agent-routing.py
python3 scripts/system-self-check.py
```

## Protocol

1. Run the agent routing linter:
```bash
python3 scripts/lint-agent-routing.py
```

2. Run the system self-check:
```bash
python3 scripts/system-self-check.py
```

3. Check for stale checkpoints:
```bash
if [ -f .ai/sessions/active-task.json ]; then
  python3 -c "
import json, datetime
from pathlib import Path
data = json.loads(Path('.ai/sessions/active-task.json').read_text())
updated = datetime.datetime.fromisoformat(data.get('last_updated','').replace('Z','+00:00'))
age = datetime.datetime.now(datetime.timezone.utc) - updated
if age.total_seconds() > 86400:
    print(f'STALE CHECKPOINT: {age.days}d {age.seconds//3600}h old — active_step_id: {data.get(\"active_step_id\")}')
else:
    print(f'Checkpoint is current ({int(age.total_seconds()/60)} min old)')
"
fi
```

4. Review output:
   - **Errors** (exit 2): Must be fixed before closing the session
   - **Warnings** (exit 1): Flag for attention
   - **Info**: Include summary in session report

5. Re-run after fixes to confirm clean.

6. Include the summary in the session report:
   `"Drift check: {N} errors, {M} warnings, clean"`

## Integration with /wrap-up

`/drift-check` is called as part of `/wrap-up`. If errors are found during wrap-up, resolve them before the session closes.
