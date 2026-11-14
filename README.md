# AI Starter Pack

Canonical home for the AI Starter Pack: a [Copier](https://copier.readthedocs.io) template, Claude Code plugin marketplace, and reusable CI workflows for production-grade AI-assisted development.

- **Render / update an instance:** `copier copy --vcs-ref=<release-commit-sha> gh:manolii-org/ai-starter-pack <dest>` then `copier update`.
- **Full pack docs:** [README-STARTER-PACK.md](./README-STARTER-PACK.md)
- **License:** [Apache-2.0](./LICENSE) — free to deploy into your own and client environments.
- **Native Muse subscription tasks:** [isolated executor and read-only smoke check](docs/muse-subscription.md), using only the consumer's own CLI credential.
- **Shared development policy:** [native guidance](docs/native-guidance.md) delivers adaptable development, approved-design fidelity and triggered procedures to Claude Code, Codex, Cursor and Devin without replacing curated local instructions.

## Consumer pins (Prompt 11)

Go-forward release: **`v1.25.1`** (annotated tag on `main`).

```yaml
# Reusable workflows
uses: Manolii-org/ai-starter-pack/.github/workflows/ci-reusable.yml@321810f558463b2363d3372d669efcd3ea8283ec # v1.25.1
# Plugin
# /plugin install manolii-framework@v1.25.1
```

For ordinary callers, pin executable workflows and their `pack_ref` inputs to
the same full 40-character release commit SHA, with a `# v1.25.1` comment.
Version tags are immutable release discovery and compatibility identities;
executable workflow pins stay exact SHAs. Explicitly opted-in Jev shadow callers
use the [full patch release-tag exception](.github/workflows/REUSABLE-WORKFLOWS.md#opt-in-jev-shadow-exception)
for `pack_ref`; the shadow stays off by default.

For ordinary SHA-form inputs, a preset revision containing the `pack_ref`
manager groups workflow SHA and version-commented `pack_ref` updates in the
`manolii-ai-starter-pack` PR through
the consumer's owning gates. The older `#v1.25.1` preset does not include that
manager: a workflow-pin update alone does not update its `pack_ref`. Until the
consumer adopts a preset revision containing the manager, the owning adoption
agent must automatically prepare both pins together in one adoption PR and
verify that they select the same release commit. This is required adoption
behaviour, not evidence that every consumer has installed it. Publication alone
does not qualify or activate any consumer.

Renovate preset: `extends: ["github>Manolii-org/ai-starter-pack"]` (optionally pin `#v1.25.1`).

Legacy `manolii-org/master@v1` still serves `routing-lint` / `shared-config` until those move to the pack — dual Renovate extends are expected during dual-run.

### Tag namespaces

- **Pack releases** are bare `vX.Y.Z` annotated tags, cut through the existing
  automatic `.github/workflows/release-tag.yml` dispatch. It creates a new
  immutable tag and never overwrites a tag or creates/moves a major alias.
  Its optional source input still resolves `main` once for legacy callers;
  normal release preparation supplies the exact reviewed commit SHA.
- **Retained aliases**, including `v1`, keep their existing values for old
  readers. Moving an alias could activate code in an unqualified consumer,
  so immutable publication no longer performs that unrelated write. Existing
  callers remain compatible while owner automation prepares exact SHA-pinned
  adoption through normal gates; there is no new routine alias task.
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

The next major line prepares Node 24 workflow defaults for an immutable
`v2.0.0` release. Publication and consumer adoption remain gated; the example
pins above are unchanged. See [Node runtime support](docs/node-runtime-support.md)
for compatibility, explicit Node 22 overrides and qualification steps.

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
