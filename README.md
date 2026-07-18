# AI Starter Pack

Canonical home for the AI Starter Pack: a [Copier](https://copier.readthedocs.io) template, Claude Code plugin marketplace, and reusable CI workflows for production-grade AI-assisted development.

- **Render / update an instance:** `copier copy gh:manolii-org/ai-starter-pack <dest>` then `copier update`.
- **Full pack docs:** [README-STARTER-PACK.md](./README-STARTER-PACK.md)
- **License:** [Apache-2.0](./LICENSE) — free to deploy into your own and client environments.

## Consumer pins (Prompt 11)

Go-forward release: **`v1.7.1`** (annotated tag on `main`).

```yaml
# Reusable workflows
uses: Manolii-org/ai-starter-pack/.github/workflows/ci-reusable.yml@v1.7.1
# Plugin
# /plugin install manolii-framework@v1.7.1
```

Renovate preset: `extends: ["github>Manolii-org/ai-starter-pack"]` (optionally pin `#v1.7.1`).

Legacy `manolii-org/master@v1` still serves `routing-lint` / `shared-config` until those move to the pack — dual Renovate extends are expected during dual-run.

Standalone by default: all external integrations (OSS routing, remote memory, browser automation, telemetry, cross-provider review) are feature-flagged off.
