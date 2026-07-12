# manolii-om plugin — source

This directory is the **build input** for the `manolii-om` Claude Code plugin.
`scripts/build-plugin.py --plugin manolii-om` reads from here and from the
copier-rendered `.claude/skills/om-*` directories, and writes the plugin
artifact to `plugin/manolii-om/`.

## What ships in the plugin

- **Skills** (4): `om-fact-capture`, `om-readiness`, `om-staff-answer`,
  `om-handover`. Sourced from `.claude/skills/{% if kl_integration %}<name>{% endif %}/SKILL.md`
  (rendered with `kl_integration=true`) so a consumer instance with the copier
  flag on and a plugin install both see the same skill definitions.
- **Eval pack**: `evals/operational-memory/` — 14 synthetic behaviour pins for
  the OM skills. Cases follow `.ai/evals/schema.md` v1 in the master repo and
  are run via `scripts/run-evals.py` with `EVAL_ALLOW_MCP=1` (they exercise
  live Knowledge Layer retrieval, so the PR `eval-gate.yml` skips them).
- **This README** — the plugin's own top-level README, describing where to
  find the canonical contract schemas.

## Contract validators — source of truth

**The Operational Memory contract schemas are not forked into this plugin.**
The canonical, versioned schemas live in the Knowledge Layer repo at
`manolii-knowledge-layer/contracts/operational-memory/`:

- `module-contract.v1.schema.json`
- `reviewed-knowledge-item.v1.schema.json`
- `extensions/hospitality.v1.schema.json`
- `source-registry-starters.json`
- `examples/generic.module.json`, `examples/hiha-holiday-rentals.module.json`

The skills validate manifests via the KL repo's
`scripts/validate-om-contracts.ts` (invoked as `npx tsx …` from a checkout of
the KL repo). Consumers that need the raw JSON Schemas should pin the KL repo
at the commit recorded in `contracts-mirror/SOURCE.md` and reference the
schemas from that checkout — never copy them into the consumer repo.

If a future build needs the schema files on disk to run offline (e.g. for the
`om-handover` skill's contract-copy step), vendor a **mirror** into
`contracts-mirror/` and record the source commit in `contracts-mirror/SOURCE.md`.
The mirror is a snapshot, not a fork; drift is a build failure.

## Provenance

Skill and eval-pack sources were seeded from the master repo. `SOURCE.md`
records the commits used.
