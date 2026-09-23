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
                                    frozen entries → FAIL); on an
                                    established mirror regen keeps only
                                    VENDORED-path entries + preserved
                                    mirror-owned entries still live — a
                                    new violation in the org's own tree
                                    is never written into the ratchet.
                                    The secrets allowlist is vendored
                                    verbatim — its token-shape catalogue
                                    ships in the platform tree unchanged
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

Visibility + binding: the seeder requires (a) --slug to equal the
checkout's own github.com origin remote — the digest it writes must
bind to THIS repo, not just any private repo the token can see — and
(b) `gh` to confirm that slug is private (NOT internal — internal
grants every enterprise member, incl. other orgs, read access). Either check failing
is fatal: seeding universe content into a repo of unknown visibility is
exactly the failure mode the mirror boundary exists to prevent.
Mirror-mode lint also requires an externally-supplied
MIRROR_VISIBILITY=private assertion — committed files alone can never
prove privacy, so the generated workflow verifies visibility via
`gh api` on every run and the bootstrap injects its own verified
result during regen.

Privacy: this script writes no other-org identifiers into the mirror —
the vendored tree is already org-leak-clean in the canonical repo, and
private-mirrors.txt holds digests only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent

UNIVERSES = ("manolii", "buro", "impaktful", "cpdcheck")
# The GitHub org that OWNS each universe's mirror — binding matters: the
# vendored lint exempts the origin owner's slug patterns, so a mirror
# hosted under a different org would exempt the wrong org AND seed the
# wrong universe scope.
UNIVERSE_OWNERS = {
    "manolii": "manolii-org",
    "buro": "buro-built",
    "impaktful": "impaktful-platform",
    "cpdcheck": "cpdcheck",
}
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
  # No branch filter — the mirror's default branch is org-configured
  # (main vs master vs other); the gate must cover every direct push.
  push:
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
      # Mirror mode in the lint requires a visibility assertion that is
      # NOT derivable from committed files — a public fork can carry the
      # marker and its own digest. Verify against the API on every run:
      # a mirror flipped to public after seeding drops back to the full
      # pattern set and the boundary gate re-engages.
      - name: Assert mirror privacy
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          vis=$(timeout 30 gh api "repos/${{ github.repository }}" --jq .visibility)
          # 'internal' is not private enough: on GitHub Enterprise it
          # grants every enterprise member (incl. other orgs) read access.
          if [ "$vis" != "private" ]; then
            echo "::error::mirror repo must be private (got: $vis)"
            exit 1
          fi
          echo "MIRROR_VISIBILITY=$vis" >> "$GITHUB_ENV"
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
  mirror-only workflow that first asserts repo visibility via `gh api`
  (`MIRROR_VISIBILITY=private` reaches the lint env). In a verified
  mirror the lint waives the public boundary, exempts this org's own
  repo slugs, and still FAILs on other orgs' slugs, infra identifiers,
  credential shapes, and cross-scope references. For a LOCAL lint run,
  export it yourself after confirming the repo is private:
  `MIRROR_VISIBILITY=private python3 scripts/registry-lint.py`

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
    # The universe contract is mirror-owned: write it on first seed only.
    # A refresh rerun must not clobber the org's surfaces/description/
    # promotion-policy edits.
    if not scope_file.is_file():
        scope_file.write_text(
            SCOPE_YAML.format(universe=universe, ip_owner=IP_OWNERS[universe]))


# The mirror MUST live on GitHub — the visibility oracle is `gh api` and
# the generated gate is a GitHub Actions workflow. A non-GitHub origin that
# happens to parse (e.g. gitlab.com/<org>/<repo>) would have its visibility
# checked against an UNRELATED github.com repo of the same slug.
_GH_HTTPS = re.compile(
    r"^(?:https?|git|ssh)://(?:[^@/\s]+@)?github\.com(?::\d+)?/"
    r"([^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_GH_SCP = re.compile(
    r"^[^@\s]+@github\.com:([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def _origin_slug(root: Path) -> str | None:
    """owner/repo of the checkout's github.com origin remote, lowercased —
    None when git, the remote, or a parseable GitHub slug is absent."""
    # `git config --get` returns the CONFIGURED url; `remote get-url`
    # expands url.insteadOf rewrites (e.g. auth proxies) and would hide
    # the real host the operator bound this checkout to.
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "config", "--get",
             "remote.origin.url"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    return _slug_of(url)


def _slug_of(url: str) -> str | None:
    """owner/repo of a github.com URL, lowercased; None otherwise."""
    m = _GH_HTTPS.match(url) or _GH_SCP.match(url)
    return m.group(1).lower() if m else None


def _push_targets_ok(root: Path, slug: str) -> str | None:
    """None when every effective PUSH destination resolves to the verified
    slug; otherwise a short description of the config that would redirect
    the push, for the refusal diagnostic.
    `git push` honours remote.origin.pushurl and the
    url.<base>.insteadOf / url.<base>.pushInsteadOf rewrites — any of
    them can redirect the seeded universe content to a different,
    possibly public, repo even though the fetch URL bound to the
    private one. `remote get-url --push` applies git's own resolution
    (pushurl list, pushInsteadOf precedence, insteadOf fallback), so
    it returns exactly the URLs `git push` would use.

    The remote is resolved the same way a plain `git push` resolves
    it: branch.<name>.pushRemote > remote.pushDefault >
    branch.<name>.remote > origin. The selected value may be a
    remote name OR a literal URL (git-push's <repository> accepts
    both) — `remote get-url` only accepts names, so a URL destination
    gets the push rewrite chain applied directly."""
    def _cfg(key: str) -> str:
        lines = _cfg_lines(key)
        return lines[0] if lines else ""

    def _cfg_lines(*args: str) -> list[str]:
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "config", *args],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [ln for ln in r.stdout.splitlines() if ln] \
            if r.returncode == 0 else []

    def _rules(suffix: str) -> list[tuple[str, str]]:
        # url.<base>.<suffix> = <prefix>: URLs starting with <prefix>
        # (the value) are rewritten to start with <base> (the key's
        # middle part). Longest matching prefix wins.
        out: list[tuple[str, str]] = []
        for line in _cfg_lines("--get-regexp", rf"^url\..*\.{suffix}$"):
            key, _, prefix = line.partition(" ")
            repl = key[len("url."):-len(f".{suffix}")]
            if prefix:
                out.append((prefix, repl))
        return out

    def _rewrite(url: str, rules: list[tuple[str, str]]) -> str:
        for prefix, repl in sorted(rules, key=lambda r: -len(r[0])):
            if url.startswith(prefix):
                return repl + url[len(prefix):]
        return url

    try:
        b = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--short", "-q",
             "HEAD"],
            capture_output=True, text=True, timeout=10)
        branch = b.stdout.strip() if b.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        branch = ""
    remote = ((branch and _cfg(f"branch.{branch}.pushRemote"))
              or _cfg("remote.pushDefault")
              or (branch and _cfg(f"branch.{branch}.remote"))
              or "origin")

    names = set()
    try:
        nr = subprocess.run(["git", "-C", str(root), "remote"],
                            capture_output=True, text=True, timeout=10)
        if nr.returncode == 0:
            names = set(nr.stdout.split())
    except (OSError, subprocess.TimeoutExpired):
        pass
    if remote in names:
        # remote.<name>.vcs delegates the transport to git-remote-<vcs>,
        # which can forward the pack anywhere — the configured URL is no
        # longer evidence of the real destination.
        if _cfg(f"remote.{remote}.vcs"):
            return (f"remote.{remote}.vcs delegates the push transport "
                    f"to git-remote-{_cfg(f'remote.{remote}.vcs')}")
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "remote", "get-url", "--push",
                 "--all", remote],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return f"could not resolve push URLs for remote '{remote}'"
        urls = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()] \
            if r.returncode == 0 else []
    else:
        # Literal URL: a matching pushInsteadOf rule wins; when none
        # matches THIS url git falls back to insteadOf rules — the
        # same per-URL chain `get-url --push` applies to remote names.
        eff = _rewrite(remote, _rules("pushinsteadof"))
        if eff == remote:
            eff = _rewrite(remote, _rules("insteadof"))
        urls = [eff]
    if not urls:
        return f"could not resolve push URLs for '{remote}'"
    ssh_override = _cfg("core.sshCommand")
    git_proxy = _cfg("core.gitProxy")
    for url in urls:
        if _slug_of(url) == slug:
            # core.sshCommand replaces the ssh transport entirely — an
            # ssh/scp URL that parses to the verified slug can still
            # land anywhere the command chooses.
            if ssh_override and (url.startswith("ssh://")
                                 or _GH_SCP.match(url)):
                return ("core.sshCommand overrides the ssh transport "
                        f"for push url '{url}'")
            # core.gitProxy replaces the direct connection for git:// —
            # same class of transport override.
            if git_proxy and url.startswith("git://"):
                return ("core.gitProxy overrides the git transport "
                        f"for push url '{url}'")
            continue
        # Devin-box auth proxy: forwards pushes to the github.com slug
        # embedded in its path (the box's global insteadOf rewrites every
        # github.com URL through it — pushes still land on that repo).
        proxy = "https://git-manager.devin.ai/proxy/github.com/"
        if not (url.startswith(proxy)
                and _slug_of("https://github.com/" + url[len(proxy):])
                == slug):
            return (f"push destination '{remote}' resolves to '{url}'")
    return None


def check_visibility(slug: str) -> str | None:
    """Require a CONFIRMED private repo before seeding — a self-declared
    slug + digest pair is not evidence of privacy, and universe content
    in a public repo is the failure mode this whole design exists to
    prevent. `internal` does NOT count: on GitHub Enterprise it grants
    every enterprise member (including other orgs) read access — the
    cross-org boundary is org-private, not enterprise-internal. `gh` is
    the only cheap visibility oracle; when it cannot answer (not
    installed, not authed, repo unreachable) the check fails closed — a
    warn-and-continue would let an operator seed into a public
    destination without ever noticing."""
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{slug}", "--jq", ".visibility"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if r is None or r.returncode != 0:
        sys.stderr.write(
            f"FAIL: could not verify {slug} visibility via gh — seeding "
            "refused. The mirror MUST be a private repo: create it first, "
            "then authenticate gh (GH_TOKEN) with access to it and retry.\n")
        return None
    vis = r.stdout.strip().lower()
    if vis == "private":
        print(f"OK: {slug} visibility={vis}")
        return vis
    sys.stderr.write(
        f"FAIL: {slug} visibility={vis or 'unknown'} — a mirror must be a "
        "private repo; universe content never belongs in a public tree\n")
    return None


def merge_index(src_reg: Path, reg: Path, refresh_platform: bool) -> bool:
    """registry/plugins.json — canonical platform entries merged with the
    mirror's own non-platform entries (so --refresh-platform never drops
    plugins the org added to its universe scope). A malformed existing
    index aborts the run: falling back to {} would silently discard every
    mirror-owned entry.

    refresh_platform=False keeps the EXISTING platform entries: the
    platform tree on disk is unchanged, so importing a newer canonical's
    platform index would desync index↔tree (indexed-but-missing dirs)."""
    canonical = json.loads((src_reg / "plugins.json").read_text())
    dst = reg / "plugins.json"
    if dst.is_file():
        try:
            existing = json.loads(dst.read_text())
        except json.JSONDecodeError as exc:
            sys.stderr.write(
                f"FAIL: {dst} is malformed JSON ({exc}) — aborting; fix or "
                "restore the file and retry, or the refresh would discard "
                "the mirror's own plugin entries\n")
            return False
        if refresh_platform:
            mine = [p for p in existing.get("plugins", [])
                    if isinstance(p, dict) and p.get("scope") != "platform"]
            canonical["plugins"] = (
                [p for p in canonical.get("plugins", [])
                 if p.get("scope") == "platform"] + mine)
        else:
            canonical["plugins"] = existing.get("plugins", [])
    dst.write_text(json.dumps(canonical, indent=2) + "\n")
    return True


# Ratchet files the vendored lint maintains.
ALLOWLIST_FILES = (
    "registry/leak-allowlist.txt",
    "registry/pack-surface-allowlist.txt",
)

# Paths this bootstrap OWNS (vendored or generated). On an established
# mirror, allowlist regen may only refresh entries under these paths —
# hits anywhere else belong to the org's own tree and must surface as
# lint FAILs, never be ratcheted by a routine refresh.
VENDORED_PATHS = (
    "registry/platform/",
    # plugins.json is deliberately NOT vendored despite being rewritten by
    # refresh: merge_index preserves the mirror's own non-platform entries,
    # so the file is mixed-ownership. Per-line ratchets cannot split file
    # ownership — classifying it as vendored would ratchet NEW hits inside
    # the mirror's own entries (e.g. another org's slug in a universe
    # plugin path) instead of letting them FAIL. Old entries still live
    # are preserved by the merge rule; only NEW hits are refused.
    "registry/secrets-allowlist.txt",
    "registry/private-mirrors.txt",
    "registry/.private-mirror",
    "scripts/registry-lint.py",
    "schemas/registry-scope.schema.json",
    ".github/workflows/registry-lint.yml",
    "README.md",
)


def _entry_path(line: str) -> str | None:
    """Path part of a `path#sha8` ratchet entry; None for comments/headers."""
    s = line.strip()
    if not s or s.startswith("#") or "#" not in s:
        return None
    return s.split("#", 1)[0]


def _vendored(path: str) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p))
               for p in VENDORED_PATHS)


def regen_allowlists(root: Path, established: bool, vis: str) -> bool:
    """Regenerate the ratchet allowlists via the vendored lint (its REPO
    resolves from its own location). Mirror mode is already active — marker
    + mirrors.txt are written first — so exempt own-org hits never enter
    the list and no canonical entries go stale. The scans enumerate
    `git ls-files` — stage the scaffold first or the lists come out empty.

    On an ESTABLISHED mirror the raw regen would freeze every live hit —
    including brand-new violations in the org's own universe tree — into
    the ratchet where no review would ever see them. Merge instead: keep
    fresh hits under vendored paths only, and preserve pre-existing
    mirror-owned entries that are still live (a stale one drops out, the
    org's ratchet burns down on its own). A violation that arrives fresh
    in a mirror-owned path is simply not written — the next lint run
    FAILs on it, which is the whole point of the gate.

    Fatal: without PyYAML/jsonschema the vendored lint dies on import and
    no allowlists get written — a mirror pushed in that state flags every
    grandfathered platform hit in its first CI run."""
    r = subprocess.run(["git", "-C", str(root), "add", "-A"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        # Without staged files the vendored lint falls back to rglob —
        # which SKIPS registry/ — and writes a pack-surface allowlist that
        # omits every vendored hit. Once the dir is later initialised and
        # pushed, CI scans those tracked files and fails. --root must be a
        # git checkout: this is fatal, not a warning.
        sys.stderr.write("FAIL: could not stage the scaffold — --root must "
                         "be a git checkout (run inside the mirror clone, "
                         "or `git init` it first)\n")
        return False
    old: dict[str, list[str]] = {}
    if established:
        for rel in ALLOWLIST_FILES:
            p = root / rel
            if p.is_file():
                old[rel] = p.read_text(encoding="utf-8").splitlines()
    lint = root / "scripts" / "registry-lint.py"
    # Mirror-mode lint requires an externally-supplied visibility
    # assertion — bootstrap already verified it via gh above, so inject
    # the confirmed value into the vendored lint subprocesses.
    lint_env = {**os.environ, "MIRROR_VISIBILITY": vis}
    for flag in ("--write-allowlist", "--write-pack-allowlist"):
        r = subprocess.run([sys.executable, str(lint), flag],
                           cwd=root, capture_output=True, text=True,
                           env=lint_env)
        if r.returncode != 0:
            sys.stderr.write(f"FAIL: lint {flag} regen failed: "
                             f"{r.stderr.strip() or r.stdout.strip()}\n")
            return False
    if established:
        for rel in ALLOWLIST_FILES:
            p = root / rel
            if not p.is_file():
                continue
            fresh = p.read_text(encoding="utf-8").splitlines()
            fresh_entries = {ln for ln in fresh if _entry_path(ln) is not None}
            headers = [ln for ln in fresh if _entry_path(ln) is None]
            keep = [ln for ln in fresh
                    if _entry_path(ln) is not None
                    and _vendored(_entry_path(ln))]
            preserved = [ln for ln in old.get(rel, [])
                         if _entry_path(ln) is not None
                         and not _vendored(_entry_path(ln))
                         and ln in fresh_entries]
            p.write_text("\n".join(headers + keep + preserved) + "\n",
                         encoding="utf-8")
    return True


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

    # The seeded digest must bind to THIS checkout's repo — a --slug that
    # names any other accessible private repo would pass visibility yet
    # fail the first CI run, which hashes the real origin remote. The
    # same check catches a non-git root before anything is written.
    origin = _origin_slug(root)
    if origin is None:
        sys.stderr.write("FAIL: --root must be a git checkout with an "
                         "'origin' remote pointing at github.com — run "
                         "inside the mirror clone (the visibility check "
                         "and generated CI gate both assume GitHub)\n")
        return 2
    if origin != slug:
        sys.stderr.write(
            f"FAIL: --slug '{slug}' does not match the checkout's origin "
            f"'{origin}' — the digest must bind to this repo, not any "
            "accessible private repo\n")
        return 2

    # The mirror's universe is bound to the repo's owning org — the
    # vendored lint exempts patterns by origin owner, so a mismatch both
    # seeds the wrong scope AND exempts the wrong org's slugs.
    expected_owner = UNIVERSE_OWNERS[args.universe]
    if origin.split("/", 1)[0] != expected_owner:
        sys.stderr.write(
            f"FAIL: --universe {args.universe} requires the mirror to live "
            f"under {expected_owner}, but origin is '{origin}'\n")
        return 2

    # Pushes must land on the verified repo too — pushurl, push
    # remote/default overrides, URL rewrites, or a vcs transport helper
    # can redirect `git push` to a different (possibly public) repo
    # than the fetch url we just bound.
    why = _push_targets_ok(root, slug)
    if why is not None:
        sys.stderr.write(
            f"FAIL: {why} — the seeded universe content could land in "
            f"a repo other than '{slug}'; refusing\n")
        return 2

    vis = check_visibility(slug)
    if vis is None:
        return 2

    reg = root / "registry"
    # An existing registry/ means this is a rerun/refresh — allowlist regen
    # must merge (preserve live mirror-owned entries, never ratchet new
    # violations in the org's own tree) rather than freeze the whole tree.
    # A first seed into a NON-EMPTY clone gets the same filtering: tracked
    # files predating the scaffold are the org's own content, and their
    # hits must surface as lint FAILs, not be silently grandfathered.
    tracked = subprocess.run(["git", "-C", str(root), "ls-files"],
                            capture_output=True, text=True, timeout=10)
    established = reg.is_dir() or bool(
        tracked.returncode == 0 and tracked.stdout.strip())
    # A mirror is scoped once — a rerun naming a different universe than
    # an existing scope is a misconfiguration, not a re-seed.
    for u in UNIVERSES:
        if u != args.universe and (reg / u / "scope.yaml").is_file():
            sys.stderr.write(
                f"FAIL: this mirror is already scoped to universe '{u}' "
                f"(registry/{u}/scope.yaml exists) — --universe "
                f"{args.universe} does not match; refusing\n")
            return 2
    reg.mkdir(parents=True, exist_ok=True)
    (reg / ".private-mirror").write_text("")
    seed_scope(root, args.universe)

    # Vendored canonical content: the whole platform tree + merged index.
    src_reg = PACK / "registry"
    dst_plat = reg / "platform"
    refreshing = args.refresh_platform or not dst_plat.exists()
    if refreshing:
        if dst_plat.exists():
            shutil.rmtree(dst_plat)
        shutil.copytree(src_reg / "platform", dst_plat)
    if not merge_index(src_reg, reg, refreshing):
        return 2

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
    if not regen_allowlists(root, established, vis):
        return 2
    print(f"seeded mirror for {args.universe} at {root} (slug {slug})")
    print("next: git add -A && git commit && git push, then add the slug "
          "digest to the canonical registry/private-mirrors.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
