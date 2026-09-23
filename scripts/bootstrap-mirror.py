#!/usr/bin/env python3
"""Seed a per-org private capability-registry mirror.

Run inside an EMPTY clone of the org's private mirror repo:

    python3 scripts/bootstrap-mirror.py \
        --root /path/to/<org>-registry --universe buro \
        --slug buro-built/buro-registry

Creates the full mirror scaffold:

    registry/.private-mirror        marker that waives the PUBLIC boundary
    registry/<universe>/scope.yaml  this org's universe contract
    registry/platform/**            vendored copy of canonical platform
                                    content (public anyway — gives the
                                    resolver ONE --registry root for
                                    platform + own-universe deps)
    registry/plugins.json           index copied from canonical
    registry/private-mirrors.txt    sha256 of this repo's own slug — the
                                    marker waiver verifies against it
    scripts/registry-lint.py        vendored lint gate
    schemas/registry-scope.schema.json
    .github/workflows/registry-lint.yml
    README.md                       mirror's role, in generic terms

Deliberately NOT copied: allowlists (a fresh repo starts with an empty
ratchet), CODEOWNERS (org-specific reviewers), other universes' scope
contracts (a mirror carries its own scope only — cross-org contracts add
nothing and the cross-org PACK-SURFACE patterns still FAIL on them).
Re-run with --refresh-platform to overwrite the platform tree from a
newer canonical checkout; everything else is left untouched.

Privacy: this script writes no other-org identifiers into the mirror —
the vendored tree is already org-leak-clean in the canonical repo, and
private-mirrors.txt holds digests only.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent

UNIVERSES = ("manolii", "buro", "impaktful", "cpdcheck")
IP_OWNERS = {
    "manolii": "Manolii Pty Ltd",
    "buro": "Buro Built",
    "impaktful": "Impaktful Pty Ltd",
    "cpdcheck": "Ensombl Pty Ltd",
}

SCOPE_YAML = """\
schema_version: 1
scope: {universe}
parent_scope: platform
ip_owner: "{ip_owner}"
visibility: {universe}
description: >
  Universe scope — capabilities owned by and visible only to the {universe}
  universe. Only repos whose ai-manifest.yaml declares universe: {universe}
  may require plugins from this scope; the resolver fails closed otherwise.
surfaces: [claude-code, devin, cursor, codex, openhands]
promotion_policy:
  auto_promote: false
  cost_threshold_usd: null
"""

MIRRORS_TXT = """\
# Trusted private mirrors — one sha256(lowercase owner/repo slug) per line.
# This mirror's own slug digest is seeded by bootstrap-mirror.py; the
# .private-mirror marker waives the PUBLIC boundary only when the origin
# slug's digest appears here. Other mirrors' digests are added by the
# canonical registry, never edited by hand here.
{digest}
"""

README = """\
# Capability registry — private mirror

This repo is a **private mirror** of the capability registry model (see
`registry/platform/**` for the platform-scope content vendored from the
canonical pack). It exists because universe-scope content must never land
in the public canonical repo — it lives here, inside this org's own
private boundary.

## Layout

- `registry/.private-mirror` — marker waiving the registry-lint PUBLIC
  boundary check (verified against `private-mirrors.txt` digests).
- `registry/<universe>/` — this org's universe scope: `scope.yaml`
  contract plus the org's own plugins. Add plugins under
  `registry/<universe>/<plugin-name>/` with the usual
  `.claude-plugin/plugin.json` + `.devin-plugin/plugin.json` manifests,
  then index them in `registry/plugins.json`.
- `registry/platform/` — vendored canonical platform content. Refresh
  with `bootstrap-mirror.py --refresh-platform` from a newer pack
  checkout; do not edit in place — upstream is canonical.
- `scripts/registry-lint.py` + `.github/workflows/registry-lint.yml` —
  the same gate the canonical repo runs. In a verified mirror it waives
  the public boundary, exempts this org's own repo slugs, and still
  FAILs on other orgs' slugs, infra identifiers, credential shapes, and
  cross-scope references.

## Consumers

A consumer repo in this universe points the resolver at this checkout
for its universe plugins (platform resolves from the vendored tree):

    ai-resolve.py --manifest ai-manifest.yaml --registry <this-repo> --apply

The manifest's `universe:` field must equal this registry's scope.
"""


def seed_scope(root: Path, universe: str) -> None:
    sdir = root / "registry" / universe
    sdir.mkdir(parents=True, exist_ok=True)
    scope_file = sdir / "scope.yaml"
    scope_file.write_text(
        SCOPE_YAML.format(universe=universe, ip_owner=IP_OWNERS[universe]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path,
                    help="path to the mirror repo checkout")
    ap.add_argument("--universe", required=True, choices=UNIVERSES)
    ap.add_argument("--slug", required=True,
                    help="owner/repo slug of the mirror repo, lowercase")
    ap.add_argument("--refresh-platform", action="store_true",
                    help="overwrite the vendored platform tree as well")
    args = ap.parse_args()

    root: Path = args.root
    slug = args.slug.strip().lower()
    if not root.is_dir() or slug.count("/") != 1:
        sys.stderr.write("FAIL: --root must be an existing checkout and "
                         "--slug must be owner/repo\n")
        return 2

    reg = root / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    (reg / ".private-mirror").write_text("")
    seed_scope(root, args.universe)

    # Vendored canonical content: the whole platform tree + the index.
    src_reg = PACK / "registry"
    dst_plat = reg / "platform"
    if args.refresh_platform or not dst_plat.exists():
        if dst_plat.exists():
            shutil.rmtree(dst_plat)
        shutil.copytree(src_reg / "platform", dst_plat)
    shutil.copy2(src_reg / "plugins.json", reg / "plugins.json")

    (reg / "private-mirrors.txt").write_text(
        MIRRORS_TXT.format(
            digest=hashlib.sha256(slug.encode()).hexdigest()))

    for vendored in ("scripts/registry-lint.py",
                     "schemas/registry-scope.schema.json",
                     ".github/workflows/registry-lint.yml"):
        src = PACK / vendored
        dst = root / vendored
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    (root / "README.md").write_text(README)
    print(f"seeded mirror for {args.universe} at {root} (slug {slug})")
    print("next: git add -A && git commit && git push, then add the slug "
          "digest to the canonical registry/private-mirrors.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
