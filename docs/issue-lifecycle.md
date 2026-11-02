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

## Pending state as a retry queue

A condition that is expected to clear on its own (a deployment deferred because the
target was busy, an owner-gated block) can use its issue as the queue for automatic
retries instead of a separate store:

1. The first failing attempt opens the issue (loud: the producing run fails).
2. A cheap scheduled poll lists the open issues for its key prefix and re-attempts only
   those targets. With nothing open the poll costs one short discovery job and starts
   no work job.
3. Each attempt **edits the issue in place** (state, last attempt, first-seen date)
   rather than commenting, so a condition that persists for days does not notify
   anyone repeatedly.
4. A verified recovery closes the issue, which ends the polling. A person closing it
   early stops the polling too.
5. Quiet is bounded: a poll that repeats an already-tracked, expected state may pass,
   but it must fail loudly once the state has been pending longer than a stated
   threshold, and whenever the tracking issue cannot be read or written. See
   `docs/workflow-outcome-convention.md` § Retry polls for `DEFERRED` work.

`scripts/issue_lifecycle.py` does not implement steps 2 and 3 today: `signal` selects one
exact key and comments on an existing issue, and there is no prefix-list or edit-in-place
command. A consumer that wants this pattern keeps its own thin wrapper (list open issues
by the stable key or marker embedded in the body, edit the body, close on recovery; a label
query only works if every producer applies the same label convention) and still follows the
helper's ownership rules:
close only machine-authored, unassigned issues, and only after a positive recovery check.
Adding the operations to the helper would need a pack release, so it is tracked as follow-up
work rather than assumed here.

## Ownership boundaries

This helper is ecosystem framework code. Consumer repositories configure their own condition keys, labels, impact text, health check, and recovery evidence. Product decisions, production mutations, and human-gated work remain entity-owned; the framework does not infer recovery from age or a merged PR.
