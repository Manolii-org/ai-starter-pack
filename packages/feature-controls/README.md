# Feature-control SDK

Version `0.1.0`: both packages (npm `@manolii/feature-controls`, Python
`manolii-feature-controls`) declare it. Its component tag
`feature-controls-v0.1.0` is cut on the `main` commit that carries this
version. Until that tag exists, there is no release to pin. Releases follow the maintainer process in
`docs/feature-controls-distribution.md` and are tagged `feature-controls-vX.Y.Z`
through `.github/workflows/release-tag.yml`. Component tags never move the
pack's `v1` alias.

## How to pin

Pin the **full commit SHA** the component tag points at, with the tag in a
comment. Never pin a branch, `main`, the moving `v1` alias, or a commit that
is not on `main` (for example a pre-squash pull-request commit):

```sh
git ls-remote https://github.com/Manolii-org/ai-starter-pack 'refs/tags/feature-controls-v0.1.0^{}'
```

- npm: `"@manolii/feature-controls": "git+https://github.com/Manolii-org/ai-starter-pack.git#<sha>"`
  and commit the lockfile that resolves it.
- Python: `manolii-feature-controls @ git+https://github.com/Manolii-org/ai-starter-pack.git@<sha>#subdirectory=packages/feature-controls/python`

pnpm consumers that grant build permission by name and version must use the
version the pinned release declares (`@manolii/feature-controls@0.1.0`);
see `docs/feature-controls.md`.
