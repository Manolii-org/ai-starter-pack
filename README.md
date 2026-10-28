# AI Starter Pack

Canonical home for the AI Starter Pack: a [Copier](https://copier.readthedocs.io) template, Claude Code plugin marketplace, and reusable CI workflows for production-grade AI-assisted development.

- **Render / update an instance:** `copier copy gh:manolii-org/ai-starter-pack <dest>` then `copier update`.
- **Full pack docs:** [README-STARTER-PACK.md](./README-STARTER-PACK.md)
- **License:** [Apache-2.0](./LICENSE) — free to deploy into your own and client environments.
- **Native Muse subscription tasks:** [isolated executor and read-only smoke check](docs/muse-subscription.md), using only the consumer's own CLI credential.
- **Shared development policy:** [native guidance](docs/native-guidance.md) delivers adaptable development, approved-design fidelity and triggered procedures to Claude Code, Codex, Cursor and Devin without replacing curated local instructions.

## Consumer pins (Prompt 11)

Go-forward release: **`v1.24.0`** (annotated tag on `main`).

```yaml
# Reusable workflows
uses: Manolii-org/ai-starter-pack/.github/workflows/ci-reusable.yml@v1.24.0
# Plugin
# /plugin install manolii-framework@v1.24.0
```

Prefer pinning the release commit SHA with a `# v1.24.0` comment
(`uses: …/ci-reusable.yml@<commit-sha> # v1.24.0`); the Renovate preset's
`manolii-ai-starter-pack` group proposes every pack bump as one reviewed PR.
That PR updates `uses:` pins but not `pack_ref` inputs: where a caller passes
`pack_ref` (for example `backup-kernel-validate-reusable`), change it to the
same release in that PR, or the workflow keeps loading the previous one.

Renovate preset: `extends: ["github>Manolii-org/ai-starter-pack"]` (optionally pin `#v1.24.0`).

Legacy `manolii-org/master@v1` still serves `routing-lint` / `shared-config` until those move to the pack — dual Renovate extends are expected during dual-run.

### Tag namespaces

- **Pack releases** are bare `vX.Y.Z` annotated tags, cut only through
  `.github/workflows/release-tag.yml` (workflow dispatch); that workflow also
  moves the floating `v1` alias. Never hand-create or move a release tag —
  consumers pin them, including by mutable tag checkout.
- **Component releases** use prefixed tags: `email-capture-vX.Y.Z` for the
  email-capture CLI and `feature-controls-vX.Y.Z` for the feature-control SDK
  (cut through the same workflow; they never move `v1`). Copier, Renovate and
  semver tooling ignore prefixed tags, so a component version never reads as a
  pack release.
- The bare `v0.1.x`–`v0.2.x` tags are legacy email-capture tags and stay in
  place (some lanes check out `ref: v0.2.3`). New email-capture releases use the
  prefixed form only. Since `v0.2.4`, `await` and `release` must find the
  allocation in the same local registry `allocate` wrote — keep those steps in
  one job (`docs/email-capture.md`). That document lists every legacy tag
  with its commit and component. Most legacy tags are lightweight, and
  `v0.2.4`–`v0.2.6` sit on Codex-review commits (`v0.2.6` is also
  `email-capture-v0.2.6`).

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

## Email deliverability

The opt-in `bin/email-quality` capability complements email capture with bounded
Unspam spam/placement checks, private run checkpoints and metadata-only receipts.
See [email-quality](docs/email-quality.md) for profiles, shared-account limits,
sender hooks, immutable CI consumption and rollout/rollback.
