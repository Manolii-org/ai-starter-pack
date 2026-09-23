#!/usr/bin/env python3
"""Seed a per-org private capability-registry mirror.

Run inside an EMPTY clone of the org's private mirror repo:

    python3 scripts/bootstrap-mirror.py \
        --root /path/to/<org>-registry --universe buro \
        --slug <org>/<org>-registry   # lowercase owner/repo of the mirror

Creates the full mirror scaffold:

    registry/.private-mirror        marker that waives the PUBLIC boundary
    registry/<universe>/scope.yaml  this org's universe contract
    registry/platform/**            vendored copy of canonical platform
                                    content (public anyway — gives the
                                    resolver ONE --registry root for
                                    platform + own-universe deps)
    registry/plugins.json           canonical index merged with the
                                    mirror's own non-platform entries
    registry/private-mirrors.txt    sha256 of this repo's own slug — the
                                    marker waiver verifies against it
    registry/*-allowlist.txt        org-leak + pack-surface ratchets
                                    REGENERATED against the seeded tree
                                    (copying canonical's would go stale:
                                    exempt own-org patterns still have
                                    frozen entries → FAIL); the secrets
                                    allowlist is vendored verbatim — its
                                    token-shape catalogue ships in the
                                    platform tree unchanged
    scripts/registry-lint.py        vendored lint gate
    schemas/registry-scope.schema.json
    .github/workflows/registry-lint.yml  generated (minimal — the canonical
                                    workflow invokes build/test files a
                                    mirror does not carry)
    README.md                       mirror's role, in generic terms

Deliberately NOT copied: CODEOWNERS (org-specific reviewers), other
universes' scope contracts (a mirror carries its own scope only — cross-org
contracts add nothing and the cross-org PACK-SURFACE patterns still FAIL on
them), the canonical CI workflow (see above), or the org-leak/pack-surface
allowlists (regenerated instead).
Re-run with --refresh-platform to overwrite the platform tree + lint/schema
vendoring from a newer canonical checkout; the mirror's own universe scope,
non-platform index entries, and marker are preserved.

Visibility: the seeder asks `gh` whether --slug resolves to a private (or
internal) repo and REFUSES on 'public' — a self-declared slug+digest is no
evidence of privacy; when gh is unavailable the check degrades to a WARN
(the marker waiver itself is lint-side advisory — the canonical repo can
never be waived, so the load-bearing boundary stays upstream).

Privacy: this script writes no other-org identifiers into the mirror —
the vendored tree is already org-leak-clean in the canonical repo, and
private-mirrors.txt holds digests only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
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

# Mirrors get a minimal generated workflow: the canonical one also invokes
# build-registry.py, ai-resolve.py, the manifest schema, and the test suite —
# none of which the scaffold seeds — so vendoring it guarantees a red first
# push. The lint is the whole gate here.
MIRROR_WORKFLOW = """\
name: registry-lint

on:
  pull_request:
    paths: ['**']
  push:
    branches: [main]
    paths: ['**']

permissions:
  contents: read

jobs:
  registry-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install deps
        run: timeout 120 pip install pyyaml jsonschema
      - name: Lint the registry
        run: python3 scripts/registry-lint.py
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
  checkout; do not edit in place — upstream is canonical. Refresh merges
  the index: this mirror's own (non-platform) plugin entries are kept.
- `scripts/registry-lint.py` + `.github/workflows/registry-lint.yml` —
  the same lint gate the canonical repo runs, driven by a minimal
  mirror-only workflow. In a verified mirror it waives the public
  boundary, exempts this org's own repo slugs, and still FAILs on other
  orgs' slugs, infra identifiers, credential shapes, and cross-scope
  references.

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


def check_visibility(slug: str) -> int:
    """Refuse to seed a PUBLIC repo — a self-declared slug + digest pair
    is not evidence of privacy, and universe content in a public repo is
    the failure mode this whole design exists to prevent. `gh` is the
    only cheap visibility oracle; when it cannot answer (not installed,
    not authed, no network) warn and continue — the canonical repo's own
    gate is still fail-closed upstream."""
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{slug}", "--jq", ".visibility"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if r is None or r.returncode != 0:
        print(f"WARN: could not verify {slug} visibility via gh "
              "(missing/unreachable) — seeding anyway; the mirror MUST be "
              "private")
        return 0
    vis = r.stdout.strip().lower()
    if vis == "public":
        sys.stderr.write(
            f"FAIL: {slug} is PUBLIC — a mirror must be a private repo; "
            "universe content never belongs in a public tree\n")
        return 2
    print(f"OK: {slug} visibility={vis}")
    return 0


def merge_index(src_reg: Path, reg: Path) -> None:
    """registry/plugins.json — fresh canonical platform entries merged with
    the mirror's own non-platform entries (so --refresh-platform never drops
    plugins the org added to its universe scope)."""
    canonical = json.loads((src_reg / "plugins.json").read_text())
    dst = reg / "plugins.json"
    if dst.is_file():
        try:
            existing = json.loads(dst.read_text())
        except json.JSONDecodeError:
            existing = {}
        mine = [p for p in existing.get("plugins", [])
                if isinstance(p, dict) and p.get("scope") != "platform"]
        canonical["plugins"] = (
            [p for p in canonical.get("plugins", [])
             if p.get("scope") == "platform"] + mine)
    dst.write_text(json.dumps(canonical, indent=2) + "\n")


def regen_allowlists(root: Path) -> None:
    """Regenerate the ratchet allowlists against the seeded tree via the
    vendored lint (its REPO resolves from its own location). Mirror mode is
    already active — marker + mirrors.txt are written first — so exempt
    own-org hits never enter the list and no canonical entries go stale.
    The PACK-SURFACE/ORG-LEAK scans enumerate `git ls-files` — stage the
    scaffold first or the allowlists come out empty and the first pushed
    CI run flags every vendored hit as new."""
    subprocess.run(["git", "-C", str(root), "add", "-A"],
                   capture_output=True, timeout=30)
    lint = root / "scripts" / "registry-lint.py"
    for flag in ("--write-allowlist", "--write-pack-allowlist"):
        r = subprocess.run([sys.executable, str(lint), flag],
                           cwd=root, capture_output=True, text=True)
        if r.returncode != 0:
            sys.stderr.write(f"WARN: lint {flag} regen failed: "
                             f"{r.stderr.strip() or r.stdout.strip()}\n")


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

    if check_visibility(slug):
        return 2

    reg = root / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    (reg / ".private-mirror").write_text("")
    seed_scope(root, args.universe)

    # Vendored canonical content: the whole platform tree + merged index.
    src_reg = PACK / "registry"
    dst_plat = reg / "platform"
    if args.refresh_platform or not dst_plat.exists():
        if dst_plat.exists():
            shutil.rmtree(dst_plat)
        shutil.copytree(src_reg / "platform", dst_plat)
    merge_index(src_reg, reg)

    (reg / "private-mirrors.txt").write_text(
        MIRRORS_TXT.format(
            digest=hashlib.sha256(slug.encode()).hexdigest()))

    for vendored in ("scripts/registry-lint.py",
                     "schemas/registry-scope.schema.json",
                     "registry/secrets-allowlist.txt"):
        src = PACK / vendored
        if not src.is_file():
            continue
        dst = root / vendored
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    wf = root / ".github" / "workflows" / "registry-lint.yml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(MIRROR_WORKFLOW)

    (root / "README.md").write_text(README)
    regen_allowlists(root)
    print(f"seeded mirror for {args.universe} at {root} (slug {slug})")
    print("next: git add -A && git commit && git push, then add the slug "
          "digest to the canonical registry/private-mirrors.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
