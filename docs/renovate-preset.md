# Renovate preset (`default.json`)

Consumers adopt the ecosystem's dependency policy by extending the pack:

```json
{
  "extends": ["github>Manolii-org/ai-starter-pack"]
}
```

## What it does

- **Automerges pin/digest rotations only** (GitHub-action digests, docker
  digests, `pin-dependencies`) via platform automerge — the PR still waits for
  every required status check, so nothing lands on a red build. These PRs have
  no semver surface; before this preset they accumulated for weeks across the
  ecosystem as pure review noise (see `manolii-org/master`
  `docs/ecosystem-audit.md` § fix-at-source).
- **Never automerges semver updates** — major/minor/patch stay
  manually-reviewed PRs, preserving each repo's existing "Automerge: Disabled"
  posture for anything with changeable behaviour.
- Keeps the ecosystem schedule convention (Monday before 9am, Europe/London)
  and the `dependencies` label.

## Precedence

Renovate merges configs with the consumer's own `renovate.json` last, so a
repo can override any rule locally (e.g. disable automerge entirely) without
forking the preset.

## Relationship to /ecosystem-audit

The audit's Amber tier merges green digest-pin PRs as a backstop and reports
each such merge under "Machinery drift" — once consumers extend this preset,
those backstop merges should trend to zero. A consumer whose digest PRs keep
appearing in the drift section hasn't adopted the preset yet.
