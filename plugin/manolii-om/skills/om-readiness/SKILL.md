---
name: om-readiness
version: 1.0.0
description: "Read-only Operational Memory readiness report for a deployment: source-registry coverage, review/sensitivity gaps, available reviewed knowledge, conflicts, freshness risks, blockers to safe staff use, and the recommended next change set. Includes the stale/conflicting-knowledge review queue."
type: skill
model: claude-sonnet-4-6
data_sensitivity: restricted_us_oss_ok
max_tokens: 4000
safety_tier: green
requires_mcp:
  - knowledge-layer
required_entities:
  - manolii
allowed-tools:
  - Read
  - Bash
tags:
  - operational-memory
  - readiness
  - read-only
  - client-deployment
---

# /om-readiness — Operational Memory readiness report

Read-only. Never mutates KL records, never applies change sets, never touches client systems.

## Inputs

- `$1` — path to a module manifest (module-contract.v1), e.g. `manolii-knowledge-layer/contracts/operational-memory/examples/hiha-holiday-rentals.module.json`. Default: ask which deployment.
- Optional `--project <slug>` to override the target workstream.

## Tool scope

Use ONLY the `read_only` mode of `config/capability-profiles/operational-memory-readonly.json` (KL repo). Load non-anchored tools (`kl_project_continuation_pack`, `kl_list_source_registry`) via `kl_search_tools` → `kl_load_tool`. Always pass `entity` from the manifest's `deployment.kl_entity`.

## Procedure

1. Validate the manifest: `npx tsx scripts/validate-om-contracts.ts <manifest>` (KL repo). Invalid manifest → stop, report violations.
2. Identity: `kl_get_projects` + `kl_project_continuation_pack(project_slug=<workstream>)`. Confirm the canonical/workstream slugs exist and are related via project_relationships. Never merge/rename/alias.
3. Sources: `kl_list_source_registry` for umbrella + workstream. Compare against the manifest's `starter_pack_refs`: expected-but-missing sources; present-but-unreviewed (`review_status != reviewed`); `sensitivity = unknown`; any `safe_to_*` true without review (constraint violation — flag loudly).
4. Knowledge: `kl_get_facts` / `kl_get_notes` / `kl_get_open_questions` / `kl_get_risks` for the workstream. Count reviewed vs candidate items; list conflicts and facts past `stale_after`; note missing reviewers.
5. Diagnostics: `kl_audit_suite` + `kl_health_check` + `kl_get_pending_actions`.
6. Recommended next change set: derive from gaps (e.g. "review + classify N bootstrap sources", "resolve conflict X"). Propose only — do NOT create it unless the operator explicitly asks; then use `kl_prepare_change_set` and stop at preview.

## Output (write to `reports/om-readiness-<deployment>-<date>.md`, ≤2 pages)

Sections: Identity | Source coverage (table: source, review_status, sensitivity, safe_to_* flags) | Reviewed knowledge available | Stale & conflicting queue (prioritised: expired → conflicting → low-confidence → unreviewed-source-newer-than-fact → missing reviewer) | Open research gaps | Blockers to a staff-answer pilot | Recommended next change set. Every factual claim cites a record ID, slug or tool result. Label each conclusion **verified** / **inferred** / **proposed** / **unresolved**. Final chat reply: ≤120 words summary + report path.
