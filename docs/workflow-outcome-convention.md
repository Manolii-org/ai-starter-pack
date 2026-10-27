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
| `INERT` | The check ran but could not detect the failure it exists to catch (for example no baseline, or no test executed). | No; publish a neutral check-run |
| `FAILED` | Work was attempted and did not establish the required end state. | No; publish a failure check-run |

`DEFERRED`, `POLICY_HELD` and `INERT` must appear in the check name and summary. Colour is
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

## Activation contract (reusable gates)

A required check that cannot fail is not a gate. Reusables whose green result
can mean "nothing was checked" expose an `activation` output, so callers and
outcome reporters can tell the two apart:

| Reusable | `activation` values | Inert when | Opt-in failure |
|---|---|---|---|
| `coverage-ratchet-reusable` | `active`, `inert` | no committed baseline (`baseline_path`) | `fail_on_inert` or `require_baseline` |
| `e2e-playwright-reusable` | `active`, `inert`, `unknown` | Playwright executed no test (all skipped, or none found); `unknown` when no reporter summary is readable | `fail_on_inert` |
| `pr-autofix-loop-reusable` | `active`, `skipped` | never: missing credentials and an agent step that produced no execution already fail the job | none |

Rules:

- An inert run always emits a `::warning::` naming the workflow and the word
  `INERT`, and sets `activation=inert`.
- `fail_on_inert` defaults to `false` in v1, so no existing caller turns red.
  It is planned to default to `true` in v2.
- `skipped` (autofix) is a legitimate decline of one event (loop-break,
  budget, actor, stale head), not an inert gate.
- A caller that publishes an outcome check maps `activation=inert` to the
  `INERT` state of `outcome-check-reusable` (neutral check-run, never
  success).

```yaml
  report:
    needs: coverage
    if: ${{ always() }}
    uses: Manolii-org/ai-starter-pack/.github/workflows/outcome-check-reusable.yml@<sha> # vX.Y.Z
    with:
      operation: coverage ratchet
      outcome: ${{ needs.coverage.outputs.activation == 'inert' && 'INERT' || (needs.coverage.result == 'success' && 'EXECUTED' || 'FAILED') }}
      summary: activation=${{ needs.coverage.outputs.activation }}
```
