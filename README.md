# AI Starter Pack

Canonical home for the AI Starter Pack: a [Copier](https://copier.readthedocs.io) template, Claude Code plugin marketplace, and reusable CI workflows for production-grade AI-assisted development.

- **Render / update an instance:** `copier copy gh:manolii-org/ai-starter-pack <dest>` then `copier update`.
- **Full pack docs:** [README-STARTER-PACK.md](./README-STARTER-PACK.md)
- **License:** [Apache-2.0](./LICENSE) — free to deploy into your own and client environments.

## Consumer pins (Prompt 11)

Go-forward release: **`v1.9.6`** (annotated tag on `main`).

```yaml
# Reusable workflows
uses: Manolii-org/ai-starter-pack/.github/workflows/ci-reusable.yml@v1.9.6
# Plugin
# /plugin install manolii-framework@v1.9.6
```

Renovate preset: `extends: ["github>Manolii-org/ai-starter-pack"]` (optionally pin `#v1.9.1`).

Legacy `manolii-org/master@v1` still serves `routing-lint` / `shared-config` until those move to the pack — dual Renovate extends are expected during dual-run.

Standalone by default: all external integrations (OSS routing, remote memory, browser automation, telemetry, cross-provider review) are feature-flagged off.

## Hermetic virtual inbox

`bin/email-capture` is the single portable, backend-neutral executable for Mailpit (default), MailDev, Inbucket, an in-memory conformance backend, and an optional hosted HTTPS capture API. It provides `allocate`, `await`, `assert`, `release`, `capabilities`, `doctor`, and `canary`; all input can be inline JSON or `@mode-0600-file`. See `schemas/email-capture/` for the Draft 2020-12 v1 contract, `docs/adr/ADR-0010-hermetic-virtual-inbox.md` for the hermetic lock, and `docs/adr/ADR-0010b-hosted-capture-lane.md` for hosted bounds.

```bash
export EMAIL_CAPTURE_MODE=hermetic EMAIL_CAPTURE_BACKEND=mailpit \
  EMAIL_CAPTURE_ENDPOINT=http://127.0.0.1:8025 EMAIL_CAPTURE_ENVIRONMENT=ci
bin/email-capture doctor
umask 077
bin/email-capture allocate --request '{"schema_version":"1.0","entity":"example","repository":"app","environment":"ci","run_id":"local-1"}' --output allocation.json
# Mask the recipient immediately; never print allocation.json.
bin/email-capture await --allocation @allocation.json --output messages.json
bin/email-capture release --allocation @allocation.json
rm -f allocation.json messages.json
```

Hermetic capture is fail-closed in staging/production. Hosted capture is allowlisted for `test|ci|preview|preprod|staging` over HTTPS, with the bearer token only in `EMAIL_CAPTURE_HOSTED_TOKEN`. Disable either with `EMAIL_CAPTURE_MODE=off`; this never changes business-provider configuration.
