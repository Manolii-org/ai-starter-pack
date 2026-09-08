# Versioned LiteLLM product releases

This directory is a **distribution mirror**, not a second canonical source. The
canonical contract remains `Manolii-org/master`; each version here records its full
source revision and exact source-manifest/file SHA-256 values.

`litellm-product-vX.Y.Z` tags publish a deterministic tarball, checksum and GitHub
build-provenance attestation from this public repository. Consumers need no Manolii
production or private-repository credential:

```bash
gh release download litellm-product-v0.4.1 \
  --repo Manolii-org/ai-starter-pack \
  --pattern 'litellm-product-0.4.1.tar.gz*'
sha256sum -c litellm-product-0.4.1.tar.gz.sha256
gh attestation verify litellm-product-0.4.1.tar.gz \
  --repo Manolii-org/ai-starter-pack
```

Start an independently owned runtime profile from
`config/litellm-product-profile.example.json`. Validate the effective config against
the downloaded contract before deployment. Provider keys, proxy keys, Fly apps,
telemetry and rollback remain owned by the consuming legal entity.

## Publishing

1. Export only the portable, non-secret files named by the canonical source manifest.
2. Add `releases/X.Y.Z/release.json` with a full canonical commit SHA and exact hashes.
3. Run `python3 scripts/build-litellm-product-asset.py --version X.Y.Z` twice and verify
   identical SHA-256 output.
4. Merge the reviewed change, create annotated tag `litellm-product-vX.Y.Z` at that
   merge commit, and push it. The release workflow attests and publishes the asset.

Never overwrite an existing version or tag. Publish a new product version instead.
