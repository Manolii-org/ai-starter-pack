---
name: om-handover
version: 1.0.0
description: "Compile a client handover pack for an Operational Memory deployment: module manifest, source registry export, object schemas, capability profile, evaluation results, deployment assumptions, export manifest, recovery instructions, ownership boundaries, known limitations, unresolved decisions."
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
  - handover
  - client-deployment
  - export
---

# /om-handover — client handover pack

Read-only compilation. The pack's purpose is client independence: after handover the client operates without Adrian, without Manolii-hosted infrastructure, and without a proprietary runtime.

## Inputs

- `$1` — module manifest path. Optional `--out <dir>` (default `reports/om-handover-<client>-<date>/`).

## Procedure

1. Validate the manifest (`validate-om-contracts.ts`). A handover pack for a `production` manifest with non-empty `manolii_infrastructure_dependencies` is refused — that is the definition of not-handover-ready.
2. Assemble into the output dir:
   - `module.json` — the overlay manifest (verbatim).
   - `contracts/` — module-contract.v1, reviewed-knowledge-item.v1, applicable extension schemas (copied from the KL repo; versions pinned).
   - `capability-profile.json` — the profile the deployment runs under.
   - `source-registry.json` — `kl_list_source_registry` export for umbrella + workstream (metadata only; no raw content).
   - `evaluation-results.md` — latest run of the deployment's eval pack (`.ai/evals/operational-memory`), verbatim outcomes; do not editorialise failures away.
   - `export-manifest.md` — what `kl_export_entity` produces, where it lands, and integrity checks.
   - `recovery.md` — restore-from-export steps (from `manolii-knowledge-layer/docs/operational-memory-module.md#export-and-recovery`), written for the client's operator.
   - `ownership.md` — client-owns / manolii-owns split from the manifest, plus deletion classes and DSR path.
   - `limitations.md` — `unsupported_capabilities` (verbatim), known gaps from the latest `/om-readiness` report, and every open decision from the manifest/maturity evidence labelled **unresolved**.
3. Leakage gate before writing: run the sanitisation denylist (`om-validator.ts findLeakage`) over every generated file; credentials, access codes, PII, money values must not appear. Failures abort the pack.
4. Do NOT publish, upload or send the pack anywhere. Report the local path.

## Output

Chat reply ≤100 words: pack path, file list, handover-readiness verdict (ready / blocked, with the blocking items), unresolved decisions count.
