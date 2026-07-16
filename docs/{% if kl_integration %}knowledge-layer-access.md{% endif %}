# Knowledge Layer access (rendered — `kl_integration: true`)

This file is emitted into an instance repo when the AI Starter Pack is rendered
with `kl_integration=true`. It is a **stub** — customise it for your deployment
before you rely on it.

## What ships with `kl_integration`

- The four Operational Memory skills (`.claude/skills/om-fact-capture`,
  `om-readiness`, `om-staff-answer`, `om-handover`). These are propose-only or
  read-only against a Knowledge Layer (KL) MCP backend.
- A required secret: `MCP_API_KEY` in the instance's Doppler config (see
  `pack-components.yml` → `required_secrets.doppler.keys`). Without it the
  `kl_*` MCP tools cannot authenticate.

The **Operational Memory eval pack** and **contract validators** do **not**
render into instances — they ship via the standalone `manolii-om` plugin from
the Manolii marketplace (`.claude-plugin/marketplace.json`). Install the plugin
alongside `manolii-framework` when your instance uses these skills seriously.

## Before you use the OM skills

Every OM skill assumes a running Knowledge Layer with:

1. **A registered MCP app.** Add a row to the KL `mcp_apps` table with a
   namespace + capability manifest that covers the `kl_*` tools the skills
   call. The read-only skills (`om-readiness`, `om-staff-answer`) work under
   the `read_only` mode of `config/capability-profiles/operational-memory-readonly.json`
   in the KL repo. `om-fact-capture` needs the same profile's `propose_only`
   mode. `om-handover` is read-only.
2. **A module manifest** conforming to `module-contract.v1`. The canonical
   schema lives in the Knowledge Layer repo at
   `contracts/operational-memory/module-contract.v1.schema.json`. Do not fork
   the schema; reference it. A generic example is in the same directory.
3. **A source registry populated with reviewed sources.** The
   `om-fact-capture` skill refuses free-floating raw text — capture it first
   (`kl_capture` → `kl_create_source_packet`) so provenance exists.

## Configuration checklist

- [ ] `MCP_API_KEY` set in the instance Doppler config.
- [ ] `mcp_apps` row created; capability profile pinned to the deployment.
- [ ] Module manifest committed at a stable repo path (skills take it as `$1`).
- [ ] Source registry seeded (`om-readiness` will report gaps loudly if not).
- [ ] For handover: `manolii_infrastructure_dependencies` empty (a
      `production` manifest with dependencies refuses handover by design).

## Related documentation

- Contracts source of truth: `<your-om-contracts-repo>/contracts/operational-memory/`
- Architecture and lifecycle: `<your-om-contracts-repo>/docs/operational-memory-module.md`
- Plugin: `.claude-plugin/marketplace.json` → `manolii-om`

## What this file is not

It is not a substitute for the KL setup guide, not a secrets manifest, and not
a client-facing handover doc. Treat it as a placeholder your team edits into
whatever runbook you actually rely on.
