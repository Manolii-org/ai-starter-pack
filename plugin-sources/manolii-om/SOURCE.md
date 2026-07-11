# Source provenance — manolii-om plugin

## Skills

Sourced from `manolii-org/master` at commit
`d390d6aebaf9343a4a2e94a4852b58f6cb66d6e7` (2026-07-11):

- `.claude/skills/om-fact-capture/SKILL.md`
- `.claude/skills/om-readiness/SKILL.md`
- `.claude/skills/om-staff-answer/SKILL.md`
- `.claude/skills/om-handover/SKILL.md`

Rehomed in this repo at `.claude/skills/{% raw %}{% if kl_integration %}<name>{% endif %}{% endraw %}/SKILL.md`
so they render into consumer instances when `kl_integration=true` and are
picked up by `scripts/build-plugin.py --plugin manolii-om` for the plugin
artifact.

## Eval pack

Sourced from `manolii-org/master` at the same commit:
`.ai/evals/operational-memory/` (14 cases + README) → `evals/operational-memory/`.

## Contract schemas

**Not vendored.** Canonical source of truth:
`manolii-org/manolii-knowledge-layer` at commit
`2fdfe2e860ce89b0d540f98cf2614b869d31300b` (2026-07-11),
path `contracts/operational-memory/`. See `README.md` for the referenced
files.

## Refresh procedure

When bumping any of the above:

1. `git -C ../master rev-parse HEAD` and update the commit above.
2. Recopy the source files verbatim.
3. Run `python3 scripts/build-plugin.py --plugin manolii-om` and commit the
   resulting `plugin/manolii-om/` artifact alongside this SOURCE.md bump.
4. Bump `plugin/manolii-om/.claude-plugin/plugin.json` `version`.
