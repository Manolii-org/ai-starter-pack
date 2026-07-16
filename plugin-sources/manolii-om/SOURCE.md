# Source provenance — manolii-om plugin

## Skills

Sourced from the maintainer's internal orchestration repo:

- `.claude/skills/om-fact-capture/SKILL.md`
- `.claude/skills/om-readiness/SKILL.md`
- `.claude/skills/om-staff-answer/SKILL.md`
- `.claude/skills/om-handover/SKILL.md`

Rehomed in this repo at `.claude/skills/{% raw %}{% if kl_integration %}<name>{% endif %}{% endraw %}/SKILL.md`
so they render into consumer instances when `kl_integration=true` and are
picked up by `scripts/build-plugin.py --plugin manolii-om` for the plugin
artifact.

## Eval pack

Sourced from the same internal orchestration repo:
`.ai/evals/operational-memory/` (14 cases + README) → `evals/operational-memory/`.

## Contract schemas

**Not vendored.** Canonical source of truth lives in the maintainer's
internal knowledge-layer repo under `contracts/operational-memory/`. See
`README.md` for the referenced files.

## Refresh procedure

When bumping any of the above:

1. Recopy the source files verbatim from the internal source.
2. Run `python3 scripts/build-plugin.py --plugin manolii-om` and commit the
   resulting `plugin/manolii-om/` artifact alongside this SOURCE.md bump.
3. Bump `plugin/manolii-om/.claude-plugin/plugin.json` `version`.
