# Learn — Extract Pattern from Session

Capture a reusable pattern from the current session's work.

## Prompt

Answer these questions:

1. **What was the problem?** (1-2 sentences)
2. **What was the solution?** (1-2 sentences)
3. **What's the generalised rule?** (The pattern others could follow)
4. **Confidence** (0.0 to 1.0 — how sure are you this pattern is broadly applicable?)
5. **Tags** (e.g., `debugging`, `performance`, `testing`, `security`, `architecture`)

## Format

Append to `.ai/memory/patterns.jsonl`:

```json
{"created": "{ISO_DATE}", "problem": "{...}", "solution": "{...}", "rule": "{...}", "confidence": 0.8, "tags": ["tag1", "tag2"], "reinforced": 1, "last_seen": "{ISO_DATE}", "source": "manual"}
```

## Tips

- High confidence (>0.8): Pattern worked multiple times, well-understood mechanism
- Medium confidence (0.5-0.8): Worked once, makes theoretical sense
- Low confidence (<0.5): Hypothesis based on limited evidence

## Optional: Remote Memory Backend

If a remote memory MCP tool is configured (e.g. a knowledge layer, Notion, Linear, or similar), also write there for cross-session persistence and semantic search. Format as: `Pattern: {rule}\n\nProblem: {problem}\nSolution: {solution}` with the same tags.

## Review

After saving, display the pattern and ask: "Does this look right? Should I adjust confidence or add tags?"
