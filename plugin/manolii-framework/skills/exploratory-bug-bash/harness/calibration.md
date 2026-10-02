# Calibration set recipe

A committed set of planted-bug patches proves the lane still sees what it
claims to see. Without it, "the explorer found nothing" is unmeasurable.

## What a set needs

**At least 3 patches**, covering at minimum:

1. **A count/total bug** — a displayed number that diverges from the truth it
   claims to show (increment/decrement off-by-one, doubled tally, count that
   ignores one member of the set). The lane's value concentrates here:
   aggregates are where nobody writes assertions.
2. **A validation bug** — a guard clause removed or loosened so invalid input
   persists silently (max length unenforced, format check dropped, required
   field made optional on the save path).
3. **A state/persistence bug** — a field dropped from the save payload, a
   reverted optimistic update, stale data after navigation — real behavior
   that only shows up across a save+reload round trip.

Each patch must be:

- **Small and realistic** — a plausible human mistake on a charter path, not a
  theatrical breakage. If the judge can't plausibly attribute it to real code,
  recall overestimates.
- **A git-apply-able `.patch` against the app's current main** — regenerate
  hunks when the target lines move; a stale patch is a broken calibration, not
  a passing one.
- **On the charter paths** — a planted bug in code no charter exercises
  contributes 0 to measured recall.
- **Applied in a disposable worktree on its own port**, never merged, never
  aimed at shared staging. Revert by removing the worktree.

## Recall

`recall = planted bugs the run reports as candidates / planted total`.
Below 50% (and below 2/3 for a 3-patch set) → fix charters or budgets before
trusting real runs. Re-run calibration when the actor or judge model changes,
when the `e2e` pin bumps, or after ~90 days — whichever is first.

A candidate that appears in BOTH calibration and real runs is pre-existing:
triage it as a real finding, not as recall evidence.
