# Capability Registry

One canonical, versioned store for shareable agent capabilities (skills, agents,
commands, hooks, policy prose), organised by **scope**. Consumers declare
dependencies in `ai-manifest.yaml`; `scripts/ai-resolve.py` materialises the
resolved set into the repo-local surfaces agents already read
(`.claude/skills/`, `.claude/agents/`, `.claude/commands/`).

See `docs/registry.md` for the model, privacy rules, and migration plan.

## Scopes

| Scope dir | Who can consume | IP owner |
|-----------|-----------------|----------|
| `platform/` | Every repo in every universe | Manolii Pty Ltd |
| `manolii/` | Manolii universe repos only | Manolii Pty Ltd |
| `buro/` | Buro Built universe repos only | Buro Built |
| `impaktful/` | Impaktful universe repos only | Impaktful Pty Ltd |
| `cpdcheck/` | CPDcheck universe repos only | Ensombl Pty Ltd |
| `repo/` | Reserved — consumer-repo-local assets, never published | owning repo |
| `personal/` | Reserved — per-user assets, never published | owning user |

Scope rules are enforced **fail-closed** by the resolver and CI lint:
a repo can only require `platform/*`, `<its-universe>/*`, and `repo`/`personal`
assets. Cross-universe promotion goes through `platform/` only, after sanitisation.

## Layout

```
registry/
  plugins.json              # machine index: scope → plugin → version
  CODEOWNERS                # per-scope review ownership
  <scope>/
    scope.yaml              # scope contract (ip_owner, promotion_policy, surfaces)
    <plugin>/
      .claude-plugin/plugin.json   # Claude Code plugin manifest
      .devin-plugin/plugin.json    # Devin plugin manifest
      skills/ agents/ commands/ hooks/ scripts/ data/
```

## Building

`registry/platform/*` is **generated** — do not hand-edit. Regenerate:

```bash
python3 scripts/build-registry.py            # writes registry/platform/{framework,om}
python3 scripts/registry-lint.py             # validates the whole registry
```

The canonical source for platform scope is the pack's `.claude/` Copier
template (same source that produces `plugin/manolii-framework`), rendered
unbranded. Universe scope plugins are hand-owned by their owning orgs.

## Status

P0 scaffold — see `docs/registry.md` § Migration. The legacy distribution
surfaces (`plugin/manolii-*`, `.claude/` template render, master sync rings)
remain the shipping path until P2 flips them to generated-from-registry.
