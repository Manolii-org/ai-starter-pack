# Integration Admission

Integration Admission is a deterministic, reusable merge-candidate planning capability. It
reduces duplicate validation without treating a cache hit, prior status name, branch, or
ancestor as proof.

## Assurance identity

Exact reuse requires the same repository installation, source-tree OID, test-contract
digest, dependency/input closure, accepted producer version, surface, and an original
`executed_pass`. Composed reuse permits a different whole-tree OID only when the surface's
complete `input_closure_digest` is unchanged. Reused evidence can never cite other reused
evidence as its producer.

## Consumer contract

Call `.github/workflows/integration-admission-reusable.yml` from an unfiltered
`pull_request`/`merge_group` caller and pass trusted event-derived base/head SHAs. Supply a
repository-local `config/integration-admission.json` derived from the schema and example.
Pin the reusable and `pack_ref` to the same immutable tag.

```yaml
name: Integration Admission

on:
  pull_request:
    branches: [main]
  merge_group:
    types: [checks_requested]

permissions:
  contents: read

jobs:
  admission:
    uses: Manolii-org/ai-starter-pack/.github/workflows/integration-admission-reusable.yml@v1.x.y
    with:
      config_path: config/integration-admission.json
      base_sha: ${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha }}
      head_sha: ${{ github.event.pull_request.head.sha || github.event.merge_group.head_sha }}
      installation_id: ${{ github.event.installation.id || 'github-actions' }}
      accepted_producer: v1.x.y
      pack_ref: v1.x.y
      shadow: true
```

The reusable reads command authority from `base_sha`, not from candidate-edited config. It
runs no deployment, staging, browser, LLM, or production checks. Its lane runner receives no
secrets. A consumer may select an isolated self-hosted product-compute runner only when its
fork/trust and cleanup controls are appropriate for candidate code.

## Adoption order

1. Establish hosted and self-hosted compute baselines.
2. Add the caller in `shadow: true` without changing required checks.
3. Map every product path, dependency edge, generated contract, and global invalidator.
4. Compare assurance and minutes with existing validation.
5. Make `Integration Admission` required while legacy contexts remain required.
6. Verify a controlled PR and phase-appropriate publisher integration ID.
7. Retire redundant legacy contexts only after replacement enforcement is proven.

Rollback reverses that sequence: restore and verify legacy contexts first, then remove the
replacement requirement. Never allow an interval with neither gate.

## Cost rules

- One planner, one job per required runtime lane, and one aggregate; never one matrix leg per
  individual check by default.
- Share checkout, runtime setup, dependency restore, and install within a lane.
- Exact/composed reuse starts no product lane for that surface.
- `not_applicable` is not a pass; `reused_*` is evidence-backed assurance.
- Cancel only obsolete cancel-safe PR/queue work. Never cancel durable migration, staging,
  deployment, or promotion work.
- Enforcement requires a measured non-positive GitHub-hosted-minutes delta or an explicit,
  bounded owner exception. Report total self-hosted compute separately.

## Current scope

The reusable implements configuration validation, affected-surface expansion, exact and
input-closure reuse planning, shared runtime lanes, and a stable aggregate. Cross-run
evidence discovery, Check Run publication, revocation storage, ruleset provisioning, and
tenant-partitioned webhook replay belong to the external Integration Admission broker and
must remain in shadow mode until those controls are deployed and validated.
