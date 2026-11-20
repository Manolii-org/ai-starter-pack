---
name: release-verification
version: 1.0.0
scope: platform
author: AI Starter Pack contributors
license: Apache-2.0
description: Verify actual deployment and product acceptance after a merge, promotion or cutover, including a green workflow with skipped jobs.
type: skill
safety_tier: green
requires_mcp: []
required_entities: []
tags: [deployment, operations, verification]
---

# Release verification

Use the owning release runbook to identify the target environment, expected candidate, required deployment jobs and acceptance checks.

1. Record the expected source SHA and image, artifact or plan identity. Read the trusted run ID and attempt, relevant jobs and actual outcomes. Classify each as applied, verified no change, skipped, failed, pending, or unknown. A successful workflow summary or approved plan cannot attest to apply. For a no-change plan, locate prior deployment evidence or independently read back the expected running state.
2. Read the deployed identity from the owning runtime when exposed and bind it to the expected candidate. Distinguish what the source declares, what was executed and what is live. Treat a generic health response as evidence only for that probe.
3. Verify the current phase through its normal entry points. Follow the runbook's route, data, compatibility and rollback checks. Use only an approved synthetic fixture and account for write-capable acceptance. If unavailable, finish read-only checks, name the missing fixture or decision and hold the dependent transition. Preserve production approvals and owning environment boundaries.
4. Record one concise result with repository, environment, release/source identity, run/attempt and job evidence, observed deployment identity, acceptance results and outstanding commitments. Close only after the required outcome is verified. Retry on a material event, an approved bounded health recheck or an explicit operator decision.
