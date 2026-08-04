# Renovate preset (`default.json`)

Consumers adopt the ecosystem's dependency policy by extending the pack:

```json
{
  "extends": ["github>Manolii-org/ai-starter-pack"]
}
```

Consumers that pin the preset to a release tag are also tracked:

```json
{
  "extends": ["github>Manolii-org/ai-starter-pack#v1.9.0"]
}
```

The preset's regex manager scans `renovate.json` for that form and opens the
next pack-bump PR. This is separate from the `.starter-pack-source` manager:
without the `renovate.json` manager, Renovate can resolve a pinned preset but
cannot discover that the pin itself is a dependency to update.

> **Bootstrap note:** a pinned preset is immutable. Consumers still on v1.9.0
> or earlier cannot load this manager from a newer pack commit. Ecosystem
> consumers are bootstrapped by the independently updated Manolii master or
> Buro org-local preset; a standalone consumer needs one final manual bump to
> the first release containing this manager. From that release onward, this
> manager discovers subsequent preset tags automatically.

## What it does

- **Automerges pin/digest rotations only** (GitHub-action digests, docker
  digests, `pin-dependencies`) via Renovate-managed automerge —
  `platformAutomerge` is deliberately **off** so Renovate itself waits for the
  branch's tests to pass before merging, and (with Renovate's default
  `ignoreTests: false`) refuses to automerge in a repo that has no tests at
  all. Nothing lands on a red or unverified build. These PRs have no semver
  surface; before this preset they accumulated for weeks across the ecosystem
  as pure review noise (see `manolii-org/master` `docs/ecosystem-audit.md`
  § fix-at-source).
- **Never automerges semver updates** — major/minor/patch stay
  manually-reviewed PRs, preserving each repo's existing "Automerge: Disabled"
  posture for anything with changeable behaviour.
- Keeps the ecosystem schedule convention (Monday before 9am, Europe/London)
  and the `dependencies` label. AI Starter Pack bump PRs additionally retain
  the `starter-pack` label and their existing commit-message topic; those
  metadata rules do not alter the semver manual-review policy.

## Precedence

Renovate merges configs with the consumer's own `renovate.json` last — but
note that `packageRules` are **concatenated**, not replaced, and a top-level
`"automerge": false` does NOT override a matching inherited packageRule. To
opt out of the preset's automerge entirely, a consumer must add a *later*
packageRule of its own:

```json
{
  "extends": ["github>Manolii-org/ai-starter-pack"],
  "packageRules": [
    { "matchPackageNames": ["*"], "automerge": false }
  ]
}
```

## Relationship to /ecosystem-audit

The audit's Amber tier merges green digest-pin PRs as a backstop and reports
each such merge under "Machinery drift" — once consumers extend this preset,
those backstop merges should trend to zero. A consumer whose digest PRs keep
appearing in the drift section hasn't adopted the preset yet.
