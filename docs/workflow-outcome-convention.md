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
`config/workflow-outcomes.json`. Run:

```bash
python3 scripts/lint-workflow-outcomes.py
```

Each contract declares its reachable outcomes and must include `FAILED`. Its
reporter must use exactly `if: ${{ always() }}`, depend on every load-bearing
job, and publish the selected outcome in both the GitHub check-run name and
summary. Classification belongs in a separate prerequisite job: the linter
rejects skip/defer/hold semantics anywhere in a load-bearing job, including
implicit green branches without an explicit `exit 0`.

The reporter is diagnostic only. It must never bypass an environment approval,
production acknowledgement, advisory lock, backup, dry run, or reviewer gate.
For an apply workflow, record the destination state before and after execution;
a green shell exit alone is not an execution receipt.
