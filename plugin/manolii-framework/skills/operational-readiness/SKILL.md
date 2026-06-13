---
name: operational-readiness
version: 1.0.0
description: "Checks PRs adding scheduled workflows, cron jobs, or new infrastructure for missing alerting, dead man's switch, runbook reference, and explicit ownership."
type: skill
model: haiku
advisor_model: claude-sonnet-4-6
data_sensitivity: internal
max_tokens: 800
safety_tier: green
requires_mcp: []
required_entities: []
tools:
  - Read
  - Grep
  - Bash
tags:
  - pr-assessment
  - specialist
---

# Skill: Operational Readiness

Narrow specialist for `.github/workflows/*.yml` diffs and backup/export/restore script additions. Invoked by the `pr-classifier` when changed files include scheduled GitHub Actions workflows or `scripts/backup-*.sh` / `scripts/*-export.sh` / `scripts/*-restore.sh` / `scripts/*-posture-*.sh` files.

## Data Sensitivity Note

This skill receives diff snippets from the orchestration layer (`pr-assessment` orchestrator). The orchestrator is responsible for stripping client-identifiable content before passing diffs to this skill. `data_sensitivity: internal` assumes sanitised input.

## Input

- Diff of `.github/workflows/*.yml` files and/or `scripts/backup-*.sh`, `scripts/*-export.sh`, `scripts/*-restore.sh`, `scripts/*-posture-*.sh` files from the PR
- (Optional) Subset of `sast-findings.json` filtered to yaml/workflow-relevant rules

## Checks

1. **Missing failure notification** — any `.github/workflows/*.yml` with a `schedule:` trigger that has no Slack notification step (`SLACK_WEBHOOK_URL`, `slackapi/slack-github-action`, or `curl.*hooks.slack.com` pattern). Severity: ERROR. Rationale: failed scheduled runs need an explicit operator-facing notification channel.

2. **Missing dead man's switch** — scheduled workflows with no `ping-success`/`ping-failure` job structure or equivalent heartbeat ping (pattern: `HC_*_PING_URL`, `healthchecks.io`, `cronitor`, `betteruptime`, `sentry.io/api/0/organizations/`, `monitors/`, `SENTRY_AUTH_TOKEN`). A `sentry-checkin` job posting to the Sentry Crons API satisfies this check. Severity: WARNING in general; ERROR if the workflow name or path contains `backup`, `export`, or `restore` (critical data-safety workflows). Rationale: without a heartbeat, a skipped/stuck run is indistinguishable from a run that never fired.

3. **Missing runbook reference** — scripts added or renamed matching `backup-*.sh`, `export-*.sh`, `restore-*.sh`, or `*-posture-*.sh` that have no corresponding entry or mention in `docs/disaster-recovery-runbook.md`. Severity: WARNING. Check by scanning the runbook file for the script basename. If the runbook file is not present in the repo at all, flag WARNING with a note that no runbook file was found.

4. **Missing ownership** — workflows with a `schedule:` trigger and no `# Owner:` or `# Maintainer:` comment anywhere in the file. Severity: WARNING. Rationale: on-call escalation requires a clear owner.

5. **SLO gap** — scheduled workflows whose only failure signal is a GitHub Actions email (i.e. `on.schedule` is present, no heartbeat URL exists, and no Slack step exists). Severity: WARNING. Note: this check overlaps with checks 1 and 2 but captures the distinct case where both alerting mechanisms are absent simultaneously, signalling an SLO blind-spot rather than a single missing component.

## False-Positive Avoidance

- Do NOT flag a workflow that runs only on `push` or `pull_request` (no `schedule:` trigger) — those have PR notifications by default.
- Do NOT flag `workflow_dispatch`-only workflows for checks 1, 2, or 5 — manual workflows are operator-triggered and ephemeral.
- If `SLACK_WEBHOOK_URL`, `HC_*_PING_URL`, or `SENTRY_AUTH_TOKEN` (with a Sentry monitors endpoint) is referenced in an `env:` block at job level or a step `run:` block, treat the notification requirement as satisfied even if the pattern is split across lines.
- For check 3, a script that already existed before the PR (i.e., is only modified, not added) does NOT need a new runbook entry — only net-new files trigger this check.

## Output Schema

```json
{
  "source": "operational-readiness",
  "findings": [
    {
      "file": ".github/workflows/backup-nightly.yml",
      "line": 1,
      "severity": "ERROR|WARNING",
      "message": "...",
      "fix": "..."
    }
  ]
}
```

The `source` field is required — the judge uses it for attribution and Gate 3 deduplication. Return `{"source": "operational-readiness", "findings": []}` if no issues found. Never return prose outside the JSON object.

## Severity Guide

| Severity | When |
|---|---|
| ERROR | Missing alerting on a critical scheduled workflow (backup, export, restore); will cause silent data-safety failures |
| WARNING | Missing ownership, missing runbook entry, SLO gap on non-critical workflows |

## Phase 1: Executor (Haiku)

Run all 5 checks from the Checks section above against each changed workflow file.

For each scheduled workflow change:
- Identify all `schedule:` triggers (flag if any `cron:` entry exists)
- Check for presence of Slack notification step (SLACK_WEBHOOK_URL, slackapi/slack-github-action, curl.*hooks.slack.com)
- Check for presence of dead man's switch (HC_*_PING_URL, healthchecks.io, SENTRY_AUTH_TOKEN with monitors endpoint, cronitor, betteruptime)
- Check for `ping-success`/`ping-failure` or `sentry-checkin` job names or heartbeat-equivalent patterns
- Check for `# Owner:` or `# Maintainer:` comment anywhere in the file
- For each newly added script file matching the runbook patterns, search for its basename in docs/disaster-recovery-runbook.md

Draft a finding entry per issue (file, line, severity, message, fix). Rate self-confidence: `high` | `medium` | `low`.

## Phase 2: Advisor (Sonnet)

Review each draft finding from Phase 1:
1. **Validity check:** Is this actually a scheduled workflow with no alerting, or does it already satisfy the check in a non-obvious way?
2. **Severity check:** Is the workflow critical (backup/export/restore)? If so, elevate WARNING to ERROR for checks 1 and 2.
3. **Context check:** Are there mitigating factors — e.g. a parent workflow that handles alerting, or an environment-gated step that only runs on failure?
4. **Escalation check:** A scheduled backup/export/restore workflow with no heartbeat AND no Slack notification is the highest-severity finding this skill produces.

Escalation trigger: scheduled backup/export/restore workflow with no heartbeat AND no Slack notification.

Output: confirmed findings only. Suppress findings where Phase 1 confidence was `low` and the workflow is not a critical backup/export/restore type.

## Phase 3: Final Output

Merge Phase 1 and Phase 2 results:
- Keep only findings confirmed by advisor
- Sort: escalation findings first, then by severity (ERROR before WARNING), then by file path (alphabetical)
- Return the final JSON output schema defined above

## Examples

### Well-formed invocation
**Task:** Review a diff that adds `.github/workflows/backup-nightly.yml` with a `schedule:` trigger but no Slack notification and no dead man's switch.
**Expected output:** JSON with two findings:
  1. File: `.github/workflows/backup-nightly.yml`, severity ERROR, message about missing failure notification (no Slack step), fix specifies adding a `sentry-checkin` job with `SLACK_WEBHOOK_URL` on failure or equivalent
  2. File: `.github/workflows/backup-nightly.yml`, severity ERROR, message about missing dead man's switch on a critical backup workflow, fix specifies adding a `sentry-checkin` job posting to Sentry Crons API (`SENTRY_AUTH_TOKEN` + `SENTRY_ORG`) or equivalent heartbeat

### Malformed invocation (do not do this)
**Task:** "Is this workflow safe?"
**What's missing:** No diff provided, no file path, no context about which workflow changed.
**Result:** Skill cannot determine which schedules exist or which alerting mechanisms are present.
