# Feature-control runtime distribution

Maintainer guide for the canonical `Manolii-org/ai-starter-pack` checkout, not a
consumer installation guide. Copier excludes this document and the release/upgrade
tools; run the commands below only from that canonical checkout.

This is an opt-in runtime package lane, not an agent capability or Copier
installation feature. Nothing here activates flags, provisions a provider,
changes routing/security settings, deploys an application, opens consumer PRs,
creates tags, or publishes packages.

## Version and trust boundaries

The SDK has its own version, initially illustrated as `0.1.0`; it is not the
pack version or the rendered `pack-components.yml` version. Wire schema versions
are independent again. Supported component tags are
`feature-controls-vX.Y.Z` and `feature-controls-vX.Y.Z-{alpha,beta,rc}.N`.
Python prereleases use the equivalent PEP 440 spellings (`rc1`, `a1`, `b1`).
Never move a component release tag or the global `v1` alias to upgrade the SDK.

The canonical source is `Manolii-org/ai-starter-pack`. Root `package.json` is the npm Git
bridge, not a consumer template. Python installs from
`packages/feature-controls/python`. Both static package versions must match
the SDK version. The selected committed contract schema must declare
`properties.schema_version.const` either at the root or in its versioned `$defs`.
All versioned definitions must have the same constant value and type; unversioned
helper definitions are allowed. Contract source, package source and release
metadata must be merged into one reviewed integration commit **before** a
consumer pins it. A worker branch is not a release.

Builds execute trusted committed npm build/prepare and Python backend code.
Installation scripts are disabled during npm dependency installation; build
processes receive a minimal environment, not ambient service credentials.
Use a disposable, credential-free builder with Python 3.12+, npm and pip.
Pin npm dependencies in a committed root `package-lock.json`; pin the Python
build backend in `pyproject.toml`. Python wheel build isolation may download
build requirements; npm may download locked build dependencies.

## Prepare and verify, without publication

From a clean canonical checkout on the intended full commit SHA:

```sh
SHA=$(git rev-parse HEAD)
python3 scripts/feature-controls-release.py prepare \
  --source-sha "$SHA" --version 0.1.0 --tag feature-controls-v0.1.0 \
  --schema contracts/feature-controls/schema.json \
  --output /path/outside-checkout/feature-controls-0.1.0 \
  --allow-untagged
python3 scripts/feature-controls-release.py verify \
  --release /path/outside-checkout/feature-controls-0.1.0 \
  --allow-untagged
```

The output must be a new directory outside the checkout. Source is exported
from Git, built twice in separate temporary directories with fixed
`SOURCE_DATE_EPOCH`, and rejected if artifact names or bytes differ.
An npm tarball, Python wheel, contract schema and `release.json` are emitted.
The manifest records its format version, SDK/schema/package versions, canonical
source repository, full source SHA, component tag/tag-object identity, contract
source path and SHA-256 for each artifact/contract.

`--allow-untagged` records a **pending** component tag for the source-SHA bridge.
It does not claim a published/immutable release. Without that option an existing
annotated component tag must target the exact source commit. Verification checks
digests, artifact package identities, schema/source metadata, source cleanliness
and recorded tag identity. It rejects unmanifested files, unsafe paths,
symlinks, wrong repository/version/source and changed tag objects.
Hashes detect drift relative to the manifest, not authenticity of an untrusted
manifest: obtain the manifest from the reviewed builder. Remote tag protection,
trusted publication and provenance attestation remain future human-reviewed
release wiring; a local tag check cannot certify remote immutability.

## Public opt-in inventory contract

The public source of truth is `INVENTORY_SCHEMA` and the corresponding strict
validator in `scripts/feature-controls-upgrade.py`. Export its JSON Schema:

```sh
python3 scripts/feature-controls-upgrade.py schema > /private/path/inventory.schema.json
```

A private control plane may reference this schema and report readiness. Keep
actual repo/environment inventories private; do not fork the updater or add
another copy-sync engine. No automatic repository discovery is supported.

Generic example (replace the illustrative installed SHA and package name with
the installed release's metadata):

```json
{
  "manifest_version": 1,
  "consumers": [
    {
      "repository": "example-org/example-app",
      "ecosystem": "example",
      "environment": "staging",
      "base": "main",
      "checkout": "example-app",
      "opt_in": true,
      "installed": {
        "sdk_version": "0.1.0",
        "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "pins": [
          {
            "manager": "npm",
            "file": "package.json",
            "section": "dependencies",
            "name": "@manolii/feature-controls",
            "pin": "git+https://github.com/Manolii-org/ai-starter-pack.git#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
          }
        ]
      }
    }
  ]
}
```

For Python, a pin uses `manager: "python"`, `file: "requirements.txt"`, no
`section`, the release's Python package name, and this exact direct reference:

```text
manolii-feature-controls @ git+https://github.com/Manolii-org/ai-starter-pack.git@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa#subdirectory=packages/feature-controls/python
```

The first adapter supports explicit npm `package.json` dependency sections and
Python requirements files only. It does not pretend to update Poetry, uv, pnpm
or Yarn locks. Add such native-manager adapters with tests before opting those
consumers in. Installed SDK/version metadata is inventory evidence supplied by
the owning repository; the updater validates its full source pin against the
declared dependency, not by discovering or querying another ecosystem.

## Dry-run proposals and check mode

```sh
python3 scripts/feature-controls-upgrade.py propose \
  --inventory /private/path/consumers.json \
  --release /path/outside-checkout/feature-controls-0.1.0 \
  --source-root /path/to/canonical-source \
  --workspace /path/to/local-consumer-checkouts \
  --owner example-org --ecosystem example --environment staging \
  > /private/path/proposal.json
```

`propose` is always read-only: JSON on stdout contains reviewed unified patches,
before-file SHA-256, declared base and observed base commit. It does not apply
patches, write dependency files or call GitHub. It selects only explicitly
opted-in entries for the requested ecosystem/environment and rejects a selection
spanning another GitHub owner or downgrading the declared installed SDK. Each checkout must be clean, inside the supplied
workspace, on its declared base, and have the exact declared origin repository.
Only listed dependency files and the release's exact package names are eligible.
Installed-pin drift, duplicate/ambiguous entries and escaping/symlinked paths
fail closed. Unrelated dependencies, flags, credentials and provider state are
not edited.

Repo identity uses the literal local `origin` URL rather than machine-specific
Git URL rewrite rules; it is not remote repository authentication. Inventories,
checkouts and the reviewed release manifest remain owner-controlled inputs.

Have the consumer's existing PR workflow apply the patch against the recorded
base/file hashes, regenerate its native locks and run compatibility tests.
Git dependency installs can execute the package's prepare/build hooks: run them
only for the reviewed integrated source commit in a credential-free environment.
Hold existing Red/production/human-review gates; merging an upgrade is not
permission to activate feature controls or deploy.

Run the same command with `check` instead of `propose` after recording the
merged installation in the inventory. It exits nonzero for dependency drift;
npm also requires `package-lock.json` to declare and resolve the exact Git SHA.
Pins remain exact: npm's HTTPS, normalized SSH, and GitHub-shorthand spellings
are accepted only for the canonical GitHub repository and identical full SHA.
Check mode never applies an update. Python requirements provide the exact
direct package pin; separately lock provider/transitive dependencies and retain
the owning application's compatibility checks. Source-SHA pinning alone is not
proof of a reproducible transitive environment.

The report includes target `source_revision`, `sdk_version` and
`wire_schema_version`. Only a successful per-consumer `check` emits `pin_record`
with `sourceSHA`, `sdkVersion`, `wireSchemaVersion` and `consumerSHA`; the last
is the observed clean consumer commit whose declarations (and npm lock) match
the verified release. Read dependency files must also match their committed
blobs: ignored/untracked files and hidden worktree edits fail closed.
Proposals and failed checks emit `pin_record: null`:
their base commit is not a future adopted consumer commit. This is dependency
declaration evidence, not proof of an installed runtime, compatible provider,
deployment or activation. The inventory input schema is unchanged.

## Renovate and future CI

The SDK-specific last rule in `default.json` disables automerge, including pin,
digest and source upgrades, without changing other package rules. Prereleases
remain excluded by default and any opt-in needs dashboard approval plus a
reviewed PR. Preserve these constraints in consumer overrides. The Git bridge
can be proposed by the report-only tool; native registry updates use existing
Renovate once npm/PyPI publication authority and package identity are established.
Do not add a second Git updater or activate packages through agent manifests.

Copier excludes runtime source/contracts, maintainer scripts/tests/build output
and the root npm bridge/lock/config. This maintainer document is also excluded
by its exact root path; consumer installation documentation remains separate.
Consumers keep their own dependency files and any existing local document.

No workflows are changed here. Minimal future human-reviewed CI:

1. Existing pytest discovery runs `tests/test_feature_controls_distribution.py`.
2. Add explicit nested TypeScript/Python lint/type/conformance tests with
   internal changed-file detection; required checks must always report.
3. Run package preparation from a clean integration commit, retain the manifest,
   verify two-build reproducibility and attest artifacts.
4. Only after publication authority is established, add a restricted publisher
   for protected component tags. Never move global pack aliases or bypass
   production/guard policies.

Official install syntax:
[npm Git installs](https://docs.npmjs.com/cli/v10/commands/npm-install),
[pip VCS direct references](https://pip.pypa.io/en/stable/topics/vcs-support/),
[Python reproducible builds](https://packaging.python.org/en/latest/guides/reproducible-builds/).
