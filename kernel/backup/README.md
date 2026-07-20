# Backup Kernel (PR-H6 scaffold — Phase-2 start of the Manolii Resilience Platform)

> **Status: SCAFFOLD.** No consumer runs on this kernel yet. `manolii-org/master`
> remains the operational source of truth for backup behaviour until the WS-2
> cutover (gated on the Phase-1 exit: 3 consecutive green drills + fail-closed
> encryption live ≥2 weeks + `/phase-gate` PASS). This directory exists so the
> kernel contract, manifest schema, and plumbing can be reviewed and versioned
> ahead of that flip. Design: `manolii-org/master`
> `reports/plans/manolii-resilience-platform-end-to-end-execution-plan-2026-07-20.md`
> (WS-2) and the investigation report §46 decisions.

## What the kernel is

One versioned implementation of dump → encrypt → upload → verify → drill,
consumed by tenants via tag-pinned `ai-starter-pack` reusable workflows
(`@v*`, org pin policy). Tenants differ **only** by a declarative manifest —
no copy-pasted workflow bodies. Modeled on the `actions-runner-fleet`
`tenants.yaml` discipline (ADR-0024: centralize code, isolate runtime and bill).

Layout:

- `lib/backup-db-lib.sh` — shared dump/encrypt/upload helpers (verbatim from master; see `PROVENANCE.md`)
- `bin/backup-pg-dump.sh` — single-database dump entrypoint
- `bin/restore-drill.sh`, `bin/restore-drill-neon-app.sh` — drill logic
- `manifest/backup-tenant.schema.json` — tenant manifest draft (JSON Schema)
- `manifest/examples/manolii.yaml` — example manifest mirroring master's live matrix (names only, no secrets)
- `bin/validate-backup-manifest.py` — schema + cross-field validation
- `.github/workflows/backup-kernel-validate-reusable.yml` — validate-only reusable workflow (the dump/drill reusable workflows are ported in WS-2 proper, not in this scaffold)

## Rules while this is a scaffold

1. **Master is upstream.** Any change to the source scripts in
   `manolii-org/master:scripts/` must be re-synced here (`PROVENANCE.md`
   records the source SHA + per-file SHA-256; refresh both in the same PR).
   Do not fork behaviour in this copy before cutover.
2. **Names are wiring.** Secret names (`BACKUP_*`, `RESTIC_PASSWORD`), bucket
   names, and Sentry slugs in manifests are load-bearing identifiers — the
   never-rename list in the execution plan §6 applies here verbatim.
3. **Cutover requires behavioural parity**: dump outputs and drill receipts
   diffed before/after master flips to kernel-consuming workflows; master runs
   a full month of green drills on the kernel before any second tenant.
4. **Manifests carry names and metadata only** — never secret values. All
   credentials resolve at runtime from the tenant's own Doppler project.

## Versioning

Ships with the pack's normal release tags. Consumers pin
`manolii-org/ai-starter-pack/.github/workflows/backup-kernel-validate-reusable.yml@v*`
per the org pin policy (org-internal reusables: `@v*` tag pin).
