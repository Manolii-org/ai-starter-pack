---
name: analytics
version: 1.0.0
description: "Aggregates telemetry into a weekly analytics report: tool usage, error rates, compaction patterns, and day-of-week activity."
type: skill
model: haiku
advisor_model: claude-sonnet-4-6
data_sensitivity: internal
max_tokens: 400
safety_tier: green
requires_mcp: []
required_entities: []
tools:
  - Bash
tags:
  - observability
  - analytics
intent_phrases:
  - "show analytics"
  - "usage report"
  - "tool usage stats"
  - "session analytics"
  - "how much have we spent"
  - "which tools are failing"
  - "generate analytics report"
---

# Skill: Analytics

Runs `scripts/session-analytics.py` to generate a weekly analytics report from existing telemetry files.

## Usage

Invoke with `/analytics` — optionally specify days: "show analytics for last 7 days"

## Execution

```bash
python3 scripts/session-analytics.py --days {N} 2>&1
```

Default: last 30 days.

## Output

Print the 5-line stdout summary plus the report path. Under 150 words. No preamble.
