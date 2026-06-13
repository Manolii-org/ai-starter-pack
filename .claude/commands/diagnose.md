# /diagnose

Search execution traces and memory for similar past issues.

Arguments: $ARGUMENTS (error message, symptom description, or error code)

1. Search `.ai/memory/patterns.jsonl` for related patterns (match against `problem`, `solution`, `rule`, `tags`)
2. Search `.ai/memory/sessions.jsonl` for sessions that encountered similar issues
3. Search `.ai/memory/facts.jsonl` for relevant facts or gotchas
4. If the repo has Sentry MCP configured: query recent errors matching the pattern
5. Report:
   - Similar past incidents (with dates, and resolutions when available)
   - Relevant patterns that may apply
   - Suggested diagnostic steps
   - Recommended fix approach based on past resolutions
