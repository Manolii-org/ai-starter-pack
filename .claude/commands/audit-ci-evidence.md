---
name: audit-ci-evidence
version: 1.0.0
description: "Build complete, head-bound GitHub MCP CI evidence for verify_sync_pr when CI REST reads are proxy-blocked."
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: [system, workflow, mcp]
eval_cases: null  # TODO: Prompt 16
supersedes: []
deprecation: null
---

# /audit-ci-evidence — MCP CI evidence envelope

Usage: `/audit-ci-evidence OWNER/REPO PR_NUMBER OUTPUT.json`

This read-only session helper converts GitHub MCP responses into the exact
fail-closed contract consumed by `scripts/audit/verify_sync_pr.py`. Do not
substitute raw REST when a Repository Scope notice excludes the repository.

1. Parse `OWNER/REPO`, the integer PR number, and output path. Refuse an output
   path inside the repository; evidence is ephemeral and must not be committed.
2. Call `mcp__github__pull_request_read` with `method=get`, then record the
   current PR `head.sha`. Stop if it is absent.
3. Call the same tool with `method=get_status`, then `method=get_check_runs`.
   Extract the combined-status object and the complete `check_runs` array from
   their MCP response envelopes; do not write the envelopes themselves into
   the evidence fields. Stop unless `combined_status.sha` and every
   `check_runs[].head_sha` equal the PR head. Paginate check runs if the response
   exposes pagination. Record the API's combined-status `total_count` and the
   check-run response's `total_count`; stop unless the accumulated check-run
   array length equals that total.
4. Call `mcp__github__actions_list` with `method=list_workflow_runs`, the same
   owner/repository, and a head-SHA filter when supported. Request the maximum
   page size and follow every page until the accumulated array length equals
   the full `total_count`. Keep only runs whose API `head_sha` equals the PR
   head. Retain the returned total only for a head-bound query; otherwise use
   the length of the filtered array. Stop if a retained run lacks `head_sha`.
5. Re-read the PR with `method=get`. Stop without writing if its head changed.
6. Write UTF-8 JSON atomically to the requested path, shaped exactly as:

```json
{
  "evidence_version": 1,
  "source": "github-mcp",
  "repo": "OWNER/REPO",
  "head_sha": "CURRENT_HEAD_SHA",
  "combined_status": {"sha": "CURRENT_HEAD_SHA", "state": "success", "total_count": 0},
  "check_runs_total_count": 0,
  "check_runs": [],
  "workflow_runs_total_count": 0,
  "workflow_runs": []
}
```

Every workflow-run object must contain its API `head_sha`; never synthesize
one. Preserve status, conclusion, name, and URL/ID fields returned by MCP. The
count must equal the final array length.
7. Run `python3 scripts/audit/verify_sync_pr.py --repo OWNER/REPO --pr PR_NUMBER
   --mode merge --ci-evidence OUTPUT.json`. Return its JSON unchanged plus the
   evidence path. A nonzero result is an audit decision, not a reason to weaken
   or hand-edit the evidence.
