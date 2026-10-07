# Require successful deployment

Provider-neutral, fail-closed gate for a push deployment in the caller's
repository. Requires Python 3 and GNU `timeout` (available on GitHub-hosted
Linux runners); no package installation or checkout is needed.

```yaml
jobs:
  gate:
    runs-on: ubuntu-latest
    permissions:
      actions: read
    steps:
      - uses: Manolii-org/ai-starter-pack/actions/require-successful-deployment@<full-commit-sha>
        id: deployment
        with:
          workflow: deploy.yml
          branch: staging
          sha: ${{ github.sha }}
          required_jobs: '["Deploy", "Verify deployment"]'
```

Use a full lowercase 40-character SHA. `workflow` is a bare `.yml`/`.yaml`
filename under `.github/workflows`; `branch` is the exact short branch name.
`required_jobs` is a non-empty JSON array of unique, exact job **display names**,
not YAML job IDs. Include matrix suffixes and reusable-workflow prefixes exactly
as GitHub displays them. Names must have no surrounding whitespace or control
characters. The token is always `${{ github.token }}`; there is no token or
repository override input. The caller must grant `actions: read`.

The verifier queries the workflow's latest branch run with `per_page=1`,
**without** event, SHA, or status filters. That run must be a completed successful
`push` for the exact SHA. A newer failed, pending, dispatched, or different-SHA
run blocks the gate; an older matching success cannot satisfy it.

All jobs are fetched from the run's current numbered attempt. More than 100
jobs, incomplete responses, inconsistent job/run metadata, and missing,
duplicate, skipped, or otherwise non-successful required jobs are rejected.
The optional job `run_attempt` field must match when present; the request
endpoint selects the exact attempt when it is absent. API workflow paths
may be bare or qualified with the exact requested branch.
The latest branch run is re-read after checking jobs to detect a superseding
run or rerun. This is a bounded snapshot check, not a lock against future pushes.
Only `run_id` and `html_url` are written to action outputs after all checks pass.
Errors are static and never include API response bodies or token values.

Requests use only the fixed `https://api.github.com` origin and constructed
repository-scoped paths, never metadata-supplied URLs or redirects. Each request
has a 15-second socket timeout and a 4 MiB response limit; the action enforces a
90-second overall limit (with a 5-second forced-termination grace period).
Network, authorization, malformed metadata, and timeout failures block the gate;
there is no waiting loop or retry.

API contracts verified against the GitHub REST documentation:
- [List workflow runs for a workflow](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow)
- [List jobs for a workflow run attempt](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)
