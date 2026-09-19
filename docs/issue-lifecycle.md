# Closed-loop issue lifecycle

Automated monitors must use one stable issue key per condition and close their own issue only after a positive recovery check. Use `scripts/issue_lifecycle.py`; do not build append-only incident ledgers in individual workflows.

## Producer contract

On failure:

```bash
python3 scripts/issue_lifecycle.py signal \
  --key production-build --title "Production build failed" \
  --body "Impact, owner, and recovery criterion." --label incident \
  --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
```

On a later successful health check:

```bash
python3 scripts/issue_lifecycle.py recover \
  --key production-build --evidence "Production smoke passed for the intended SHA." \
  --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
```

The helper embeds `issue-lifecycle:v1` plus the stable key, comments on the existing canonical issue, and closes only machine-authored, unassigned issues. A human-authored or human-assigned issue is never auto-closed.

## Ownership boundaries

This helper is ecosystem framework code. Consumer repositories configure their own condition keys, labels, impact text, health check, and recovery evidence. Product decisions, production mutations, and human-gated work remain entity-owned; the framework does not infer recovery from age or a merged PR.
