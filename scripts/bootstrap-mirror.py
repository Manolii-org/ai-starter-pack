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
import base64
import fnmatch
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

try:
    import pwd
except ImportError:  # Windows — no getpwuid; expanduser fallback
    pwd = None

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
    # scp-style 'github.com:slug' — the leading user@ is optional in
    # valid git syntax
    r"^(?:[^@\s]+@)?github\.com:([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def _origin_slug(root: Path) -> str | None:
    """owner/repo of the checkout's github.com origin remote, lowercased —
    None when git, the remote, or a parseable GitHub slug is absent."""
    # `git config --get` returns the CONFIGURED url; `remote get-url`
    # expands url.insteadOf rewrites (e.g. auth proxies) and would hide
    # the real host the operator bound this checkout to. A PATH
    # wrapper could attest any origin — use a trusted system git.
    git = _trusted_prog("git")
    if not git:
        return None
    try:
        r = subprocess.run(
            [git, "-C", str(root), "config", "--get",
             "remote.origin.url"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    return _slug_of(url)


def _slug_of(url: str) -> str | None:
    """owner/repo of a github.com URL, lowercased; None otherwise.
    The host is matched case-insensitively ('GITHUB.COM' is the same
    DNS name git would connect to)."""
    m = _GH_HTTPS.match(url.lower()) or _GH_SCP.match(url.lower())
    return m.group(1).lower() if m else None


def _redact(url: str) -> str:
    """Strip credentials (userinfo, query, fragment) before a diagnostic
    prints the URL. The scp-style 'user@host:path' form has no '://' —
    mask a leading user@ as well (the username may be a token)."""
    url = re.sub(r"://[^/@\s]*@", "://***@", url, count=1)
    url = re.sub(r"^[^@\s:]+@", "***@", url, count=1)
    url = url.split("?", 1)[0].split("#", 1)[0]
    # A '<transport>::<address>' remote-helper destination embeds an
    # opaque helper address — credentials may be inside it with or
    # without space-separated arguments, so never echo the address.
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)::(?!/)", url)
    if m:
        # The transport NAME is also arbitrary — 'SUPERSECRETTOKEN::x'
        # puts a credential there, so withhold it too.
        return "<transport>::***"
    # The path can carry a credential ('https://h/d/SECRET/x.git',
    # 'user@h:SECRET/x.git') — withhold it for non-github hosts. A
    # github.com path is just owner/repo and identifying the
    # misdirected destination aids the fix. A local 'file://' or
    # bare-filesystem destination is opaque entirely — git accepts it
    # as a push URL and any component may be sensitive.
    if url.startswith("file:"):
        return "file:***"
    if "://" in url:
        # Keep the path only for an exact owner/repo(.git) github.com
        # URL — extra components ('/org/repo/SECRET') may carry a
        # credential, and a malformed slug aids nothing.
        if not _GH_HTTPS.match(url):
            url = re.sub(r"(://[^/\s]+)/\S*$", r"\1/***", url)
    elif re.match(r"^(?:[^@\s]+@)?[^:\s@/]+:", url):
        # scp-style 'user@host:path' — same rule.
        if not _GH_SCP.match(url):
            url = re.sub(
                r"^((?:[^@\s]+@)?[^:\s@]+):\S*$", r"\1:***", url)
    else:
        # Not a recognised URL form — '/tmp/SECRET/repo.git', '~/x',
        # './x' are all valid push destinations whose components may
        # be sensitive; never echo them.
        return "<opaque destination>"
    # A space-bearing non-URL string is opaque too — drop its arguments.
    return url.split(" ", 1)[0]


# Directories a trusted system ssh lives under (resolved with realpath
# so /bin -> /usr/bin merges still count). /usr/local and Homebrew
# prefixes are user-writable — a wrapper placed there would attest to
# its own config, so only the system dirs qualify, and the resolved
# file itself must be root-owned (POSIX) or signed (Windows, checked
# at call time — POSIX st_uid is meaningless there).
_SYS_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _windows_dir() -> str:
    """The real Windows directory reported by the OS — kernel32
    GetWindowsDirectoryW, never the SystemRoot environment variable
    (a caller can point that at a user-writable tree holding a wrapper
    ssh and still pass an env-derived allowlist). Empty string when
    the OS cannot answer or the platform is not Windows."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)  # MAX_PATH
        if ctypes.windll.kernel32.GetWindowsDirectoryW(buf, 260):
            return buf.value
    except (AttributeError, ImportError, OSError, ValueError):
        pass
    return ""


_FOLDERID_PROGRAM_FILES = "{905E63B6-C1BF-494E-B29C-65B732D3D21A}"
_FOLDERID_PROGRAM_FILES_X86 = "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}"


def _known_folder(guid_text: str) -> str:
    """A Windows KnownFolder path reported by the OS (shell32
    SHGetKnownFolderPath), never an environment variable — ProgramFiles
    env vars are caller-controlled and cannot root a trust decision.
    Empty string off-Windows or when the OS cannot answer."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("b", ctypes.c_byte * 16)]
        g = GUID()
        g.b[:] = uuid.UUID(guid_text).bytes_le
        out = wintypes.LPWSTR()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(g), 0, None, ctypes.byref(out)) == 0 \
                and out.value:
            val = str(out.value)
            ctypes.windll.ole32.CoTaskMemFree(
                ctypes.cast(out, ctypes.c_void_p))
            return val
    except (AttributeError, ImportError, OSError, TypeError,
            ValueError):
        pass
    return ""


def _nt_prog_dirs(name: str) -> tuple[str, ...]:
    """Windows trust roots for `name`, every component OS-derived
    (kernel32 / shell32 — no environment variables)."""
    if name == "ssh":
        w = _windows_dir()
        return (os.path.normcase(os.path.join(
            w, "System32", "OpenSSH")) + os.sep,) if w else ()
    pf = (_known_folder(_FOLDERID_PROGRAM_FILES),
          _known_folder(_FOLDERID_PROGRAM_FILES_X86))
    sub = {"git": "Git", "gh": "GitHub CLI"}.get(name, name)
    return tuple(os.path.normcase(os.path.join(d, sub)) + os.sep
                 for d in pf if d)


def _exe_verified(path: str) -> bool:
    """True when the resolved executable is system-authentic:
    POSIX — owned by the superuser; Windows — a Valid Authenticode
    signature reported by the system PowerShell (resolved under the
    OS-derived Windows directory, never PATH — the wrapper would
    shadow it too)."""
    if os.name != "nt":
        try:
            return os.stat(path).st_uid == 0
        except OSError:
            return False
    windir = _windows_dir()
    if not windir:
        return False
    ps = os.path.join(windir, "System32", "WindowsPowerShell",
                      "v1.0", "powershell.exe")
    if not os.path.isfile(ps):
        return False
    try:
        s = subprocess.run(
            [ps, "-NoProfile", "-Command",
             "(Get-AuthenticodeSignature -LiteralPath '"
             + path.replace("'", "''") + "').Status"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return s.returncode == 0 and s.stdout.strip() == "Valid"


def _trusted_prog(name: str) -> str:
    """Absolute path of a TRUSTED system executable, or '' when the
    PATH-resolved binary cannot be authenticated. A PATH-shadowing
    wrapper can answer every attestation below correctly and still
    intercept the real operation, so the binary itself must be trusted:
    POSIX — resolved under a system bin dir and root-owned; Windows —
    resolved under an OS-derived install dir and Authenticode-signed.
    MIRROR_TRUST_DIRS (os.pathsep-separated) may declare extra roots
    for binaries a platform installs outside the system dirs — the
    operator's declaration is itself the attestation (env is the
    trust channel, like MIRROR_GITHUB_PROXY_PREFIX), so those paths
    skip the per-file verification."""
    p = shutil.which(name)
    rp = os.path.normcase(os.path.realpath(p)) if p else ""
    if not rp:
        return ""
    if os.name == "nt":
        dirs = _nt_prog_dirs(name)
    else:
        dirs = tuple(os.path.normcase(d) + os.sep
                     for d in _SYS_BIN_DIRS)
    extra = tuple(os.path.normcase(os.path.realpath(d)) + os.sep
                  for d in os.environ.get(
                      "MIRROR_TRUST_DIRS", "").split(os.pathsep) if d)
    if any(rp.startswith(d) for d in extra):
        return rp
    if any(rp.startswith(d) for d in dirs) and _exe_verified(rp):
        return rp
    return ""


def _match_exec_in(paths: list[str]) -> bool:
    """True when any ssh_config source declares a 'Match exec'
    criterion — arbitrary, state-dependent local execution during
    config parsing that the -G snapshot cannot attest (a condition
    false at check time can be true at push time). 'exec' must be a
    criterion TOKEN: 'Match host exec.example.com' is a hostname
    pattern, not execution."""
    seen = 0
    for p in paths:
        if seen >= 64:
            # More sources than we can scan — approving a partial
            # scan would let a later file carry 'Match exec'.
            return True
        seen += 1
        try:
            if os.path.getsize(p) > 1 << 20:
                return True  # unverifiable → treat as presence
            data = Path(p).read_text(errors="replace")
        except OSError:
            continue
        for ln in data.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            # OpenSSH accepts quoted arguments AND optional '=' keyword
            # separators (Match=exec "cmd", Match exec="cmd") — split on
            # both, then walk criterion/argument PAIRS: a criterion name
            # in argument position ('Match host exec') is a value, not a
            # condition, while criteria like 'final'/'canonical'/'all'
            # take no argument.
            f = [t.strip("\"'") for t in re.split(r"[=\s]+", ln.lower())
                 if t]
            if f and f[0] == "match":
                arg_criteria = {"exec", "host", "originalhost",
                                "localnetwork", "tagged", "command",
                                "user", "localuser"}
                i = 1
                while i < len(f):
                    if f[i] == "exec":
                        return True
                    i += 2 if f[i] in arg_criteria else 1
    return False


# GitHub's published SSH host-key fingerprints — public values GitHub
# ships for authenticating its servers (docs.github.com 'GitHub's SSH
# key fingerprints'), verified against a live ssh-keyscan. Pinning is
# what makes an approved known-hosts DIRECTORY trustworthy: location
# says where the file lives, not whose keys it holds, and ssh accepts
# ANY matching entry — a seeded '~/.ssh/attacker_hosts' would otherwise
# pass the path check while authenticating an interceptor's key.
_GITHUB_HOST_KEY_SHA256 = frozenset({
    "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",  # ssh-rsa
    "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",  # ecdsa-p256
    "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",  # ssh-ed25519
})


def _kh_covers(field: str, port: str) -> bool:
    """A known-hosts host field that can answer a lookup for github.com:
    comma-separated globs ('github.com', '*.github.com', '*'), bracketed
    '[host]:port' forms, or a '|1|salt|hash' HMAC-SHA1 token."""
    if field.startswith("|1|"):
        try:
            s64, h64 = field[3:].split("|", 1)
            salt = base64.b64decode(s64)
            dig = base64.b64decode(h64)
        except ValueError:
            return False
        cands = {"github.com", "[github.com]:22"}
        if port:
            cands.add(f"[github.com]:{port}")
        return any(hmac.compare_digest(
            hmac.new(salt, c.encode(), hashlib.sha1).digest(), dig)
            for c in cands)
    for pat in field.split(","):
        p = pat
        if p.startswith("["):
            p = p[1:p.find("]")] if "]" in p else p[1:]
        elif ":" in p:
            p = p.split(":", 1)[0]
        if fnmatch.fnmatchcase("github.com", p.lower()):
            return True
    return False


def _kh_pin_problem(files: list[str], port: str) -> str | None:
    """None when no github.com entry contradicts GitHub's published host
    keys; a refusal reason otherwise. ssh accepts ANY matching known-
    hosts entry, so EVERY github.com entry must fingerprint to a
    published key — a single unpinned one is a seeded MITM anchor. A
    certificate-authority entry covering github.com defeats pinning
    outright (a CA signs arbitrary host keys). No github.com entry at
    all is fine: first contact then falls to the interactive
    StrictHostKeyChecking decision already checked."""
    for rp in files:
        try:
            if os.path.getsize(rp) > 8 << 20:  # absurd for known_hosts
                # ssh still reads the file — skipping it would let a
                # padded forged entry past verification. Fail closed.
                return ("a known-hosts file exceeds the size that can "
                        "be verified safely")
            data = Path(rp).read_text(errors="replace")
        except OSError:
            continue
        for ln in data.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            f = ln.split()
            if f[0].startswith("@"):
                if (f[0] == "@cert-authority" and len(f) > 2
                        and _kh_covers(f[1], port)):
                    return ("a certificate-authority known-hosts entry "
                            "can sign arbitrary github.com host keys")
                continue
            if len(f) < 3 or not _kh_covers(f[0], port):
                continue
            try:
                blob = base64.b64decode(f[2])
            except ValueError:
                continue
            fp = "SHA256:" + base64.b64encode(
                hashlib.sha256(blob).digest()).decode().rstrip("=")
            if fp not in _GITHUB_HOST_KEY_SHA256:
                return ("known-hosts file holds a github.com host key "
                        "outside GitHub's published fingerprints")
    return None


def _ssh_host_unchanged(url: str) -> str | None:
    """None when ssh's effective config for this URL's user/host/port
    verifies a direct, authenticated connection to github.com; a short
    refusal reason otherwise. `ssh -G` resolves OpenSSH's effective
    config: HostName rewrites, ProxyCommand/ProxyJump tunnels, and
    disabled host-key verification would each let a push to the
    verified slug land elsewhere or be intercepted. Query with the
    same user/host/port arguments git would pass so `Match user`/
    `Match port` blocks evaluate identically. The ssh executable is
    resolved through the same PATH git uses and must be a system ssh —
    a PATH-shadowing wrapper cannot attest to itself. Fail closed on
    any doubt."""
    if url.lower().startswith("ssh://"):
        m = re.match(r"^ssh://(?:([^@/\s]+)@)?github\.com(?::(\d+))?/",
                     url, re.IGNORECASE)
        user, port = m.groups()
        # git percent-decodes URL userinfo before invoking ssh — the
        # -G target must carry the same decoded user or Match user
        # blocks evaluate differently than the real push.
        user = unquote(user) if user else ""
        args = (["-p", port] if port else []) + \
            [f"{user}@github.com" if user else "github.com"]
    else:  # scp-style [user@]github.com:slug — user may be omitted
        user = url.split("@", 1)[0] if "@" in url else ""
        port = None
        args = [f"{user}@github.com" if user else "github.com"]
    ssh = _trusted_prog("ssh")
    if not ssh:
        return ("ssh transport cannot be verified: 'ssh' does not "
                "resolve to a trusted system executable")
    try:
        # '-v' adds the debug trace naming every config source ssh
        # actually read — needed for the Match exec scan below.
        r = subprocess.run([ssh, "-G", "-v", *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "'ssh -G' could not verify the effective host"
    if r.returncode != 0:
        return "'ssh -G' could not verify the effective host"
    eff = {}
    for ln in r.stdout.splitlines():
        key, _, value = ln.partition(" ")
        eff[key] = value.strip()
    # 'Match exec' runs arbitrary code while ssh PARSES the config,
    # and its result is state-dependent — a condition testing for the
    # file this bootstrap creates is false at check time and true at
    # the real push, so the -G snapshot cannot attest it. Scan every
    # source ssh actually read (the -v 'Reading configuration data'
    # trace covers Include'd files too) for the criterion.
    srcs = []
    for ln in r.stderr.splitlines():
        m = re.match(r"debug\d+:\s*Reading configuration data\s+(.+)$",
                     ln.strip())
        if m:
            srcs.append(m.group(1).strip())
    if _match_exec_in(srcs):
        return ("ssh client config declares a 'Match exec' condition "
                "(state-dependent local execution)")
    # hostname alone is insufficient: a ProxyCommand/ProxyJump still
    # reports the target host while connecting elsewhere.
    if eff.get("hostname", "").lower() != "github.com":
        return "ssh client config redirects github.com elsewhere"
    if (eff.get("proxycommand", "none").lower() != "none"
            or eff.get("proxyjump", "none").lower() != "none"):
        return ("ssh client config tunnels github.com through "
                "ProxyCommand/ProxyJump")
    # OpenSSH 9.6+ canonicalises 'no'/'off' to 'false' in -G output —
    # check every spelling of disabled server authentication.
    # 'accept-new' authenticates automatically on first use (TOFU):
    # on a host without a github.com entry an interceptor supplies the
    # first key and the push still lands — refuse it too.
    if eff.get("stricthostkeychecking", "").lower() in (
            "no", "off", "false", "0", "accept-new"):
        return ("ssh host key verification is disabled "
                "(StrictHostKeyChecking no/off/accept-new)")
    # HostKeyAlias replaces the hostname used for host-key lookup — an
    # alias pointing at an attacker-owned entry verifies the
    # interceptor's key while hostname still reports github.com.
    alias = eff.get("hostkeyalias", "").lower()
    if alias and alias != "github.com":
        return ("ssh client config overrides the host key lookup "
                "(HostKeyAlias)")
    # KnownHostsCommand supplies host keys beyond the known-hosts
    # files — a configured command can emit an attacker-controlled
    # github.com key that 'yes'-level checking then accepts.
    if eff.get("knownhostscommand", "").strip().lower() not in ("", "none"):
        return ("ssh client config installs a dynamic host-key source "
                "(KnownHostsCommand)")
    # PermitLocalCommand + LocalCommand runs the command locally after
    # connecting — it can read the staged universe files and upload
    # them anywhere while hostname/host-key checks stay green.
    if (eff.get("permitlocalcommand", "").lower() in
            ("yes", "true", "on", "1")
            and eff.get("localcommand", "").strip()):
        return ("ssh client config executes a local command after "
                "connecting (PermitLocalCommand/LocalCommand)")
    # ControlMaster multiplexing attaches the push to an EXISTING
    # connection at ControlPath — the session's real peer may differ
    # from github.com entirely while -G still reports a clean direct
    # config. Any enabled form (yes/auto/ask variants) fails closed;
    # ControlPath alone is inert without ControlMaster.
    if eff.get("controlmaster", "no").lower() not in (
            "", "no", "false", "off", "0"):
        return ("ssh client config multiplexes through a shared "
                "connection (ControlMaster/ControlPath)")
    # Provider libraries dlopen during authentication — a configured
    # path executes attacker code mid-push with the staged files
    # readable, every host check still green. Defaults only:
    # pkcs11provider none; securitykeyprovider internal (the builtin).
    if eff.get("pkcs11provider", "none").lower() not in ("", "none"):
        return ("ssh client config loads a PKCS#11 provider library "
                "(PKCS11Provider)")
    if eff.get("securitykeyprovider", "internal").lower() not in \
            ("", "none", "internal"):
        return ("ssh client config loads a security-key provider "
                "library (SecurityKeyProvider)")
    # 'none' disables the file entirely (documented for both knobs) —
    # filtering it like /dev/null keeps the remaining file validated
    # instead of resolving a sentinel as a filesystem path.
    kh = [p for p in (eff.get("userknownhostsfile", "").split()
                      + eff.get("globalknownhostsfile", "").split())
          if p.lower() not in ("/dev/null", "none")]
    if not kh:
        return ("ssh host key verification has no known-hosts file "
                "(UserKnownHostsFile /dev/null)")
    # A custom known-hosts path can name an attacker-seeded file that
    # 'yes'-level checking then trusts as the host-key database — each
    # file must live where host keys are actually kept (~/.ssh or
    # /etc/ssh). ssh resolves '~' via getpwuid, not $HOME.
    try:
        pw = pwd.getpwuid(os.getuid()) if pwd else None
        home = pw.pw_dir if pw else os.path.expanduser("~")
        user = pw.pw_name if pw else ""
    except (KeyError, AttributeError):
        home, user = os.path.expanduser("~"), ""
    # Trusted locations: ~/.ssh plus the system file (/etc/ssh on POSIX,
    # %ProgramData%\ssh on Windows, where the OpenSSH port keeps it).
    # Compare normcase'd realpaths so 'C:\Users\…' matches its own
    # prefix — hard-coded forward slashes never match a Windows path.
    global_kh = (os.path.join(os.environ.get(
        "ProgramData", r"C:\ProgramData"), "ssh")
        if os.name == "nt" else "/etc/ssh")
    allowed = tuple(os.path.normcase(d + os.sep) for d in
                    (os.path.join(home, ".ssh"), global_kh))
    # 'ssh -G' emits a quoted path containing spaces WITHOUT its
    # quoting — 'a b' may be one file or two. When a spaced join of
    # consecutive fragments names a real file the parse is ambiguous
    # (the real path was never inspected) — fail closed.
    for frags in (eff.get("userknownhostsfile", "").split(),
                  eff.get("globalknownhostsfile", "").split()):
        for i in range(len(frags)):
            for j in range(i + 2, len(frags) + 1):
                joined = os.path.expanduser(
                    " ".join(frags[i:j])
                    .replace("%d", home).replace("%u", user))
                if os.path.exists(joined):
                    return ("a known-hosts file list is ambiguous "
                            "(a pathname may contain spaces)")
    trusted: list[str] = []
    for p in kh:
        rp = os.path.normcase(os.path.realpath(os.path.expanduser(
            p.replace("%d", home).replace("%u", user))))
        if not rp.startswith(allowed):
            return ("ssh host key verification uses an untrusted "
                    "known-hosts file path")
        trusted.append(rp)
    # ssh looks up '[github.com]:<port>' for non-default ports — the
    # EFFECTIVE port from -G (a 'Host github.com / Port 443' block),
    # not the URL's, names the token the real lookup uses.
    eff_port = eff.get("port", "") or (port or "")
    return _kh_pin_problem(trusted, eff_port)


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
    gets the push rewrite chain applied directly.
    Every attestation below asks git about git — a PATH-shadowing
    wrapper could answer all of them truthfully and still intercept
    the real `git add`/`git push`. All calls go through one
    authenticated system binary."""
    git = _trusted_prog("git")
    if not git:
        return ("git does not resolve to a trusted system executable "
                "— a PATH wrapper could attest to itself")

    def _cfg(key: str) -> str:
        lines = _cfg_lines(key)
        return lines[0] if lines else ""

    def _cfg_lines(*args: str) -> list[str]:
        try:
            r = subprocess.run(
                [git, "-C", str(root), "config", *args],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [ln for ln in r.stdout.splitlines() if ln] \
            if r.returncode == 0 else []

    def _rules(suffix: str) -> list[tuple[str, str]]:
        # url.<base>.<suffix> = <prefix>: URLs starting with <prefix>
        # (the value) are rewritten to start with <base> (the key's
        # middle part). Longest matching prefix wins. Records are read
        # NUL-separated (`--get-regexp -z` emits 'key\nvalue\0') — a
        # <base> may legitimately contain spaces (e.g. an
        # `url."ext::… ".insteadOf` helper URL), and splitting the
        # entry on whitespace would corrupt the key and silently drop
        # the rewrite rule.
        out: list[tuple[str, str]] = []
        try:
            r = subprocess.run(
                [git, "-C", str(root), "config", "--get-regexp", "-z",
                 rf"^url\..*\.{suffix}$"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if r.returncode != 0:
            return []
        for rec in r.stdout.split("\0"):
            if not rec:
                continue
            key, _, prefix = rec.partition("\n")
            repl = key[len("url."):-len(f".{suffix}")]
            if prefix:
                out.append((prefix, repl))
        return out

    # githooks(5): the recommended `git add -A && git commit &&
    # git push` invokes every hook below — index updates
    # (post-index-change, fsmonitor-watchman), auto-gc (pre-auto-gc),
    # commit flow (pre-commit .. post-commit), ref transactions
    # (reference-transaction), and the push itself (pre-push). Each
    # runs arbitrary code with the freshly staged universe files
    # readable, so any of them can exfiltrate the content even when
    # every transport check passes. `rev-parse --git-path` resolves
    # the effective hooks dir (core.hooksPath included); any present
    # hook file fails closed — executability is platform-dependent,
    # and such a file has no benign role in this bootstrap.
    try:
        hp = subprocess.run(
            [git, "-C", str(root), "rev-parse", "--git-path",
             "hooks"],
            capture_output=True, text=True, timeout=10)
        hooks_dir = Path(root) / hp.stdout.strip() \
            if hp.returncode == 0 and hp.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        hooks_dir = None
    if hooks_dir is None:
        return "the hooks directory could not be resolved"
    for name in ("post-index-change", "fsmonitor-watchman",
                 "pre-auto-gc", "pre-commit", "prepare-commit-msg",
                 "commit-msg", "post-commit", "reference-transaction",
                 "pre-push"):
        if (hooks_dir / name).exists():
            return (f"a '{name}' hook can exfiltrate the staged "
                    "universe content during the add/commit/push")
    # External programs git execs on the staged content itself:
    # core.fsmonitor=<path> runs during index operations (git add),
    # and filter.<name>.clean/.process run during staging for any
    # attributes-bound path. Neither is a hooks-dir file — refuse the
    # configured commands outright. On git <2.35.1 every non-empty
    # core.fsmonitor is a hook pathname (a PATH 'true' would exec);
    # only 2.35.1+ gives booleans the builtin meaning.
    fsm = _cfg("core.fsmonitor").strip()
    if fsm:
        try:
            vr = subprocess.run([git, "--version"],
                                capture_output=True, text=True,
                                timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            vr = None
        vm = (re.search(r"(\d+)\.(\d+)", vr.stdout)
              if vr is not None else None)
        modern = (vm is not None
                  and (int(vm.group(1)), int(vm.group(2))) >= (2, 35))
        if not modern or fsm.lower() not in (
                "true", "false", "yes", "no", "on", "off", "0", "1"):
            return ("a configured core.fsmonitor command can "
                    "exfiltrate the staged universe content")
    # filter.<name>.clean/.process exec the configured program on
    # staged file contents during 'git add' — but only on paths an
    # attributes rule binds to that filter. A configured-but-unbound
    # filter (e.g. a system-wide git-lfs install) is inert, so refuse
    # on a LIVE binding, not mere presence. `check-attr` resolves
    # every attributes source (.gitattributes, info/attributes,
    # core.attributesFile); candidates are every path 'git add -A'
    # would stage — tracked plus untracked.
    filt = set()
    for ln in _cfg_lines("--get-regexp",
                         r"^filter\..*\.(clean|process)$"):
        parts = ln.split(None, 1)
        if len(parts) > 1 and parts[1].strip():
            filt.add(parts[0][len("filter."):].rsplit(".", 1)[0])
    if filt:
        cand = b""
        for ls_args in (["ls-files", "-z"],
                        ["ls-files", "-o", "-z", "--exclude-standard"]):
            try:
                lp = subprocess.run(
                    [git, "-C", str(root), *ls_args],
                    capture_output=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                return ("candidate paths could not be enumerated "
                        "for filter checks")
            if lp.returncode != 0:
                return ("candidate paths could not be enumerated "
                        "for filter checks")
            cand += lp.stdout
        names = [n for n in cand.split(b"\0") if n]
        # 'git add -A' also stages what this bootstrap WILL write — a
        # binding like 'registry/** filter=leak' has no current
        # candidate yet still receives every seeded file. Probe every
        # path the script writes — VENDORED_PATHS (incl. README.md and
        # the vendored schema) plus the generated allowlists,
        # plugins.json, and each universe scope — plus every file the
        # vendored registry tree contributes (a path need not exist
        # for check-attr to report its binding).
        scaffold: list[bytes] = []
        for p in (*VENDORED_PATHS,
                  "registry/plugins.json",
                  "registry/leak-allowlist.txt",
                  "registry/pack-surface-allowlist.txt"):
            scaffold.append(p.encode())
            if p.endswith("/"):
                # A directory prefix binds nothing as a probe path —
                # probe a representative file under it instead.
                scaffold.append((p + "scope.yaml").encode())
        scaffold += [f"registry/{u}/scope.yaml".encode()
                     for u in UNIVERSE_OWNERS]
        names += scaffold
        reg_src = PACK / "registry"
        names += [
            str(p.relative_to(PACK)).encode()
            for p in reg_src.rglob("*") if p.is_file()
        ] if reg_src.is_dir() else []
        probe = b"".join(n + b"\0" for n in names)
        try:
            ca = subprocess.run(
                [git, "-C", str(root), "check-attr", "-z",
                 "--stdin", "filter"],
                input=probe, capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return ("attributes could not be resolved "
                    "for filter checks")
        if ca.returncode != 0:
            return ("attributes could not be resolved "
                    "for filter checks")
        recs = ca.stdout.split(b"\0")
        for i in range(0, len(recs) - 2, 3):
            if recs[i + 2].decode("utf-8", "replace") in filt:
                return ("an attributes-bound clean/process filter can "
                        "exfiltrate the staged universe content")

    # Commit signing execs the configured program during 'git commit' —
    # gpg.program / gpg.ssh.program / gpg.openpgp.program /
    # gpg.x509.program. A LOCAL-scope program is untrusted input and
    # runs after the scaffold is staged; it only fires when signing is
    # effectively on (commit.gpgSign / tag.gpgSign at any scope — a
    # global program is the operator's own tool, the trusted channel).
    # The printed next step also carries --no-gpg-sign.
    if (_cfg("commit.gpgsign").strip().lower()
            in ("true", "yes", "on", "1")
            or _cfg("tag.gpgsign").strip().lower()
            in ("true", "yes", "on", "1")):
        for ln in _cfg_lines("--show-scope", "--get-regexp",
                             r"^gpg\.(program|ssh\.program"
                             r"|openpgp\.program|x509\.program)$"):
            if ln.split("\t", 1)[0] in ("local", "worktree"):
                return ("a repository-local commit-signing program can "
                        "exfiltrate the staged universe content")

    def _rewrite(url: str, rules: list[tuple[str, str]]) -> str:
        for prefix, repl in sorted(rules, key=lambda r: -len(r[0])):
            if url.startswith(prefix):
                return repl + url[len(prefix):]
        return url

    try:
        b = subprocess.run(
            [git, "-C", str(root), "symbolic-ref", "--short", "-q",
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
        nr = subprocess.run([git, "-C", str(root), "remote"],
                            capture_output=True, text=True, timeout=10)
        if nr.returncode == 0:
            names = set(nr.stdout.split())
    except (OSError, subprocess.TimeoutExpired):
        pass
    if remote in names:
        # remote.<name>.vcs delegates the transport to git-remote-<vcs>,
        # which can forward the pack anywhere — the configured URL is no
        # longer evidence of the real destination. Neither the config
        # value nor the remote name is echoed: both may carry a
        # credential.
        if _cfg(f"remote.{remote}.vcs"):
            return ("a configured remote's vcs delegates the push "
                    "transport to a remote helper")
        try:
            r = subprocess.run(
                [git, "-C", str(root), "remote", "get-url", "--push",
                 "--all", remote],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return ("could not resolve push URLs for remote "
                    f"'{_redact(remote)}'")
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
        return f"could not resolve push URLs for '{_redact(remote)}'"
    # core.sshCommand (or the GIT_SSH_COMMAND/GIT_SSH environment
    # variables) replaces the ssh transport entirely — an ssh/scp URL
    # that parses to the verified slug can still land anywhere the
    # command chooses.
    ssh_src = ("core.sshCommand" if _cfg("core.sshCommand") else
               "GIT_SSH_COMMAND" if os.environ.get("GIT_SSH_COMMAND") else
               "GIT_SSH" if os.environ.get("GIT_SSH") else "")

    def _tls_problem(url: str) -> str:
        """A refusal reason when the accepted https destination's trust
        evaluation is unsafe; '' otherwise. Verification disabled is the
        obvious MITM — but a custom CA bundle or CA path is just as
        dangerous: verification stays on while trusting a trust root the
        attacker controls, so a forged github.com certificate verifies.
        GIT_SSL_NO_VERIFY is defined by presence (even '=0'), and
        `git config --get-urlmatch` applies git's own precedence for
        http.<base>.* — the longest match wins and, at equal
        specificity, the later scope wins."""
        if "GIT_SSL_NO_VERIFY" in os.environ:
            return "tls verification disabled"
        # An explicitly-EMPTY http.sslVerify canonicalises to false in
        # git, but --get-urlmatch reports it as a BLANK record — which
        # must not read as 'enabled'. The exit status separates
        # 'set to empty' (rc 0) from 'unset' (rc 1).
        try:
            r = subprocess.run(
                [git, "-C", str(root), "config", "--get-urlmatch",
                 "http.sslVerify", url],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return "tls verification state could not be verified"
        if r.returncode == 0:
            lines = r.stdout.splitlines()
            eff = lines[-1].strip().lower() if lines else ""
            if eff in ("", "false", "0", "no", "off"):
                return "tls verification disabled"
        for var in ("GIT_SSL_CAINFO", "GIT_SSL_CAPATH"):
            if var in os.environ:
                return f"custom CA trust store via {var}"
        for key in ("http.sslCAInfo", "http.sslCAPath"):
            vals = _cfg_lines("--get-urlmatch", key, url)
            if vals and vals[-1].strip():
                return f"custom CA trust store via {key}"
        return ""

    def _proxy_ok(url: str) -> bool:
        # Auth-proxy exception: an environment may rewrite github.com
        # URLs through an auth proxy whose path still embeds the real
        # slug. The prefix is supplied by the operator via the
        # MIRROR_GITHUB_PROXY_PREFIX env var — environment-specific
        # infrastructure hostnames do not belong in this public repo.
        proxy = os.environ.get("MIRROR_GITHUB_PROXY_PREFIX", "")
        # Require an https prefix: anything else (an 'ext::… ' helper
        # URL, 'git://', 'ssh://') would still hand the push to a
        # transport we cannot verify — the exemption exists for an
        # https auth proxy only.
        return bool(proxy.startswith("https://")
                    and url.startswith(proxy)
                    and _slug_of("https://github.com/"
                                 + url[len(proxy):]) == slug)

    saw_https = False
    for url in urls:
        # Transport dispatch is case-insensitive — 'HTTPS://', 'SSH://'
        # and 'GitHub.com:' are the same schemes/host to git, so the
        # checks below must see the same normalised form _slug_of did.
        # The ORIGINAL url is kept for user/port extraction (scp users
        # are case-sensitive) and for diagnostics.
        low = url.lower()
        # Plaintext transports (http, git) carry the private pack
        # unencrypted and unauthenticated — refuse them outright like
        # any misdirected destination.
        m = re.match(r"(http|git)://", low)
        if m:
            return (f"push destination '{_redact(remote)}' resolves to "
                    f"plaintext {m.group(1)} url '{_redact(url)}'")
        if _slug_of(url) == slug or _proxy_ok(url):
            if low.startswith("https://"):
                saw_https = True
                # GIT_EXEC_PATH swaps which git-remote-https helper the
                # push execs — a verified URL is no longer evidence of
                # the transport that carries the pack.
                if "GIT_EXEC_PATH" in os.environ:
                    return ("push url handled by an overridden git "
                            "exec path (GIT_EXEC_PATH)")
                tls = _tls_problem(url)
                if tls:
                    return f"{tls} for push url '{_redact(url)}'"
            if low.startswith("ssh://") or _GH_SCP.match(low):
                if ssh_src:
                    return (f"{ssh_src} overrides the ssh transport "
                            f"for push url '{_redact(url)}'")
                prob = _ssh_host_unchanged(url)
                if prob:
                    return f"{prob} for push url '{_redact(url)}'"
            continue
        return (f"push destination '{_redact(remote)}' resolves to "
                f"'{_redact(url)}'")
    # A credential.helper runs during the push on HTTPS destinations —
    # including a '!shell command' form — and core.askPass answers the
    # authentication prompt the same way. A program configured from
    # INSIDE the clone (local config or a file it includes) is part of
    # the untrusted input and can exfiltrate the staged content while
    # every transport check stays green. Operator-side origins (system,
    # global, command line, env) are the trust channel — same basis as
    # MIRROR_TRUST_DIRS.
    if saw_https:
        root_p = os.path.normcase(os.path.realpath(str(root))) + os.sep
        # Both predicates are needed. Scope alone misses a GLOBAL config
        # whose include.path pulls a file from inside the clone (scope
        # stays 'global'); origin alone misses a LOCAL config whose
        # include.path points outside the checkout (scope stays 'local').
        for ln in _cfg_lines("--show-scope", "--show-origin",
                             "--get-regexp",
                             r"^credential\..*\.helper$"
                             r"|^credential\.helper$|^core\.askpass$"):
            parts = ln.split("\t")
            if len(parts) < 3:
                continue
            scope, origin = parts[0], parts[1]
            op = os.path.normcase(os.path.realpath(
                os.path.join(str(root), origin[5:]))) \
                if origin.startswith("file:") else ""
            if (scope in ("local", "worktree")
                    or (op and op.startswith(root_p))):
                return ("a repository-local credential.helper or "
                        "core.askPass program can exfiltrate the "
                        "staged universe content")
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
    # A PATH-shadowing 'gh' wrapper could report any visibility —
    # require a trusted binary before asking it.
    gh = _trusted_prog("gh")
    if not gh:
        sys.stderr.write(
            "FAIL: 'gh' does not resolve to a trusted system "
            "executable — visibility cannot be verified.\n")
        return None
    try:
        # GH_HOST can point gh at an Enterprise instance — a private
        # same-slug repo there would pass while the bound github.com
        # repo is public. Pin the query to github.com.
        r = subprocess.run(
            [gh, "api", f"repos/{slug}", "--jq", ".visibility",
             "--hostname", "github.com"],
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
    git = _trusted_prog("git")
    if not git:
        sys.stderr.write(
            "FAIL: 'git' does not resolve to a trusted system "
            "executable — staging cannot be verified.\n")
        return False
    r = subprocess.run([git, "-C", str(root), "add", "-A"],
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
    # An untrusted git makes the tracked-file answer unverifiable —
    # an empty read would treat pre-existing content as disposable,
    # so fail closed to the merge (preserve) path.
    git = _trusted_prog("git")
    if git:
        tracked = subprocess.run(
            [git, "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=10)
        has_tracked = bool(tracked.returncode == 0
                           and tracked.stdout.strip())
    else:
        has_tracked = True
    established = reg.is_dir() or has_tracked
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
    # 'git commit -m --no-gpg-sign': never the bare command — a
    # configured editor (core.editor/GIT_EDITOR) launches on it and a
    # commit.gpgSign signing program (gpg.program/gpg.ssh.program)
    # execs on it; either can read the freshly staged private scaffold.
    print("next: git add -A && git commit -m 'seed private mirror' "
          "--no-gpg-sign && git push, then add the slug "
          "digest to the canonical registry/private-mirrors.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
