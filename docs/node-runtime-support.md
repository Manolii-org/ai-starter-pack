# Node runtime support and v2 qualification

The v2 candidate changes `e2e-playwright-reusable.yml` and
`secret-scan-reusable.yml` defaults from `22` to `24`; `ci-reusable.yml` already
defaults to `24`. The standalone secret scan also selects `24`. The workflow
decision log requires a new major for default changes, so publish this through
the reviewed immutable `v2.0.0` lane, keeping v1 tags and its alias unchanged.
This document describes a candidate until the release actually exists.

On 24 October 2026 (Australia/Sydney), the official [distribution index](https://nodejs.org/dist/index.json)
reports `24.22.1` as the latest Node 24 patch and `22.23.4` as the latest Node 22
patch. The [release schedule](https://raw.githubusercontent.com/nodejs/Release/main/schedule.json)
keeps Node 22 supported until April 2027 and Node 24 until April 2028. Node 26
is Current; Node 20 and 25 are EOL. Recheck these sources at qualification.
The workflows use the existing pinned `setup-node` action on the caller's
chosen runner; they do not set an application host's production runtime.

## Caller and package compatibility

- Every reusable keeps `node_version` and passes it to `setup-node`. Omit it
  for the Node 24 primary lane, or pass an exact supported patch such as
  `22.23.4`. A newer supported caller selection is never rewritten to 24.
- `ci-reusable` keeps `package_manager`, command/skip overrides and pnpm's
  empty-version behaviour: `package.json#packageManager` owns that version.
  A non-Node caller still skips package installation and dependent jobs.
- Secret scan still sets up Node only when its existing package/lock detector
  requires it. No audit, fork guard, required-check name or credential rule is
  relaxed. Playwright's existing install command override remains available
  for pnpm consumers.
- CI caches use the observed Node patch and ABI, OS/architecture, package
  manager (where configurable), and all root lockfiles. No broad restore
  prefix admits an older runtime. Rebuild native dependencies with a clean
  install whenever the runtime or lockfile changes.
- Root feature-controls `engines.node >=20.19` and exploratory harness
  `>=22.12.0` describe package compatibility. They are not production runtime
  selections. Keep the floors and ES2022 output only with minimum-runtime
  compatibility tests; an EOL compatibility test does not authorize an EOL
  deployed runtime. `@types/node` is a separate compile-time declaration set.

## Immutable release and independent adoption

After exact-source local validation, independent review and required hosted
checks pass, merge the owning PR and verify that merged commit before invoking
the existing release lane for `v2.0.0` with its full SHA. The lane also moves
the matching `v2` alias; it must not move `v1` or rewrite released tags.
No production consumer should adopt a floating global ref.

Each consumer opens its own PR pinned to the release commit or immutable tag.
For Playwright also set `pack_ref` to that same immutable release, since the
workflow cannot discover its own ref. Example after publication:

```yaml
uses: Manolii-org/ai-starter-pack/.github/workflows/e2e-playwright-reusable.yml@v2.0.0
with:
  pack_ref: v2.0.0
  node_version: '24.22.1'
```

Canary representative npm, pnpm and non-Node callers at the exact source
identity; include a maintained Node 22 override. Run clean installs, library
build/typecheck/tests, workflow-contract tests, Copier render, resolver and
generated-artifact freshness checks. Local fixtures qualify source behaviour;
they do not prove an independent application's installed workflow or runtime.
Buro and CPD retain their own runtime, legal, approval and adoption decisions.

Retain the exact prior consumer commit and settings for recovery. For the
current v1 line, `v1.22.0` resolves to
`5a4538e858e29ac258b7949e297bbb57e5a0a7ce`; pair any retained workflow with an
explicit supported Node 24 or 22 selection and requalify it. Prefer roll-forward;
never rewrite a tag or restore an EOL runtime as a shortcut.

For application adoption, verify the routed commit/image and private runtime,
auth/core flows, leases/retries and a scheduled cycle or isolated equivalent.
Unless the owner has stricter gates, require zero new smoke/auth/data failures
and 30 minutes of representative observation. Stop expansion for a server-error
increase over one percentage point or p95 regression over 20%, using adequate
baseline and sample sizes. A source-only release does not prove those outcomes.
