# Guarded paths — policy file taxonomy

`.ai/guards.json` marks repo paths where unattended AI merges are unsafe:
schema migrations, auth/session code, billing, deploy workflows, guard
config itself. Two enforcement layers, by design:

| Layer | Where | Mechanism |
|---|---|---|
| Session | `scripts/guard_check.py` (PreToolUse hook) | blocks the agent's Edit/Write mid-session unless the guard id sits in `session_unfreezes` |
| Merge | `.github/actions/check-guarded-paths` | diffs `base...head` in CI and fails when a block-mode guard was touched without bypass |

Session guards alone are insufficient for AI-first merging: hooks only run
where the agent runs. A PR authored by any agent on any harness must meet the
same invariant at merge time — that is what the CI action provides.

## File shape

```json
{
  "guards": [
    {
      "id": "db-migrations",
      "description": "Schema changes need an explicit human/agent sign-off",
      "paths": ["alembic/**", "migrations/**"],
      "mode": "block",
      "reason": "irreversible DDL can strand prod between app revisions",
      "default": true
    }
  ],
  "session_unfreezes": [],
  "bypass_log": ".ai/bypass-log.jsonl"
}
```

- `paths` — fnmatch globs, repo-relative.
- `mode` — `"block"` (default) or `"warn"` (CI annotation only).
- `regions` — optional JSON `json_path` sub-guards (session-hook granularity;
  the CI action treats them file-level).
- `session_unfreezes` — hook-layer thaw, never needed for CI bypass.

## CI bypass channels

The merge gate treats a guard as unfreezed when either holds:

1. PR label `guard-ok:<guard-id>` — caller maps label names into the action's
   `bypass_guards` input.
2. Commit trailer `Guarded-Path: <guard-id>` (comma-separate several).

The trailer is the audit channel for agents; the label is for humans.

## Caller wiring

```yaml
jobs:
  guarded-paths:
    runs-on: ubuntu-latest
    permissions: { contents: read, pull-requests: read }
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - id: labels
        run: |
          labels=$(jq -r '.pull_request.labels[].name // empty' "$GITHUB_EVENT_PATH" \
            | sed -n 's/^guard-ok://p' | paste -sd,)
          echo "bypass=$labels" >> "$GITHUB_OUTPUT"
      - uses: Manolii-org/ai-starter-pack/.github/actions/check-guarded-paths@v1
        with:
          base_ref: origin/${{ github.base_ref }}
          head_ref: ${{ github.sha }}
          bypass_guards: ${{ steps.labels.outputs.bypass }}
```

The action also auto-reads commit trailers — no caller wiring needed there.

## Suggested domain guards

See [`.ai/guards.domain.example.json`](../.ai/guards.domain.example.json) for a
catalogue of guard entries by domain (migrations, auth, billing, workflows,
guard config, secrets handling). Copy the ones your repo has; delete the rest.
