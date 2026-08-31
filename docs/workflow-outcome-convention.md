# Workflow outcome convention

A workflow whose name promises an apply, promotion, synchronisation, or other
state change must not conclude `success` when that work was skipped.

## Terminal vocabulary

| State | Meaning | Success? |
|---|---|---|
| `EXECUTED` | The load-bearing operation ran and its end state was verified. | Yes |
| `NO_CHANGES` | The operation ran, measured the destination, and found no delta. | Yes |
| `DEFERRED` | Work was not attempted because a temporary owner held the resource. | No; publish a neutral check-run |
| `POLICY_HELD` | A deliberate approval or production policy prevented execution. | No; publish a neutral check-run |
| `FAILED` | Work was attempted and did not establish the required end state. | No; publish a failure check-run |

`DEFERRED` and `POLICY_HELD` must appear in the check name and summary. Colour is
not the interface. A policy hold is operationally healthy but is not evidence
that the held operation happened.

## Contract and lint

Repositories declare load-bearing jobs and an outcome reporter in
`config/workflow-outcomes.yaml`. Run:

```bash
python3 scripts/lint-workflow-outcomes.py
```

The reporter must use `if: always()`, depend on every load-bearing job, and name
`DEFERRED`, `POLICY_HELD`, and `FAILED`. The linter also rejects explicit
skip/defer/hold branches that exit zero inside load-bearing jobs.

A classifier step that intentionally selects `DEFERRED` may be listed under
`allowed_deferred_steps`. This is not a general suppression: the workflow still
needs the unconditional outcome reporter, and the configured value must exactly
match the step's human-readable `name`.

The reporter is diagnostic only. It must never bypass an environment approval,
production acknowledgement, advisory lock, backup, dry run, or reviewer gate.
For an apply workflow, record the destination state before and after execution;
a green shell exit alone is not an execution receipt.
