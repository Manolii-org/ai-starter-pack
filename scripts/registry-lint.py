#!/usr/bin/env python3
"""Registry CI lint — privacy and integrity gate for the capability registry.

Checks, fail-closed:
  1. INDEX     registry/plugins.json <-> filesystem consistency; each plugin
               dir is indexed, each index entry exists.
  2. MANIFEST  every plugin carries .claude-plugin/plugin.json (name+version+
               description, name == dir) and .devin-plugin/plugin.json (name).
  3. SCOPE     every scope dir has a valid scope.yaml; visibility matches the
               scope's own id; universe scopes parent to platform.
  4. ORG-LEAK  platform/* must not name any universe identifier; a universe
               scope must not name OTHER universes (its own id is fine).
               Plugin manifests + scope.yaml are exempt (publisher metadata).
  5. SECRETS   no credential-shaped strings anywhere under registry/.
  6. SKILL     every SKILL.md carries `name` + `description` frontmatter
               (warns on missing version/type/safety_tier — pack spec is
               lighter than master's manifest-spec, deliberately).
  7. XSCOPE    no plugin file references another scope's tree (../<scope>/).

Exit 1 on any FAIL. Mirrors pack-drift-check.py's org-leak approach — the two
gates merge here: pack lint guards the consumer template, this guards the
registry store.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("FAIL: PyYAML required (pip install pyyaml)\n")
    sys.exit(2)

try:
    import jsonschema
except ImportError:
    jsonschema = None

try:
    import tomllib
except ImportError:
    tomllib = None  # Python < 3.11 — TOML decode scan unavailable

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "registry"

UNIVERSES = ("manolii", "buro", "impaktful", "cpdcheck")
LOCAL_SCOPES = ("repo", "personal")
ALL_SCOPES = ("platform",) + UNIVERSES + LOCAL_SCOPES

# Org identifiers. `manolii` is the registry's own publisher — platform content
# must not carry ANY universe id including manolii (that's the whole point of
# platform scope). Universe scopes may name themselves only.
# Boundaries are identifier-aware: `\b` treats `_` as a word character, so
# manolii_infrastructure_dependencies and CPDCHECK_INTERNAL would slip through.
# Only [A-Za-z0-9] counts as a word char here — underscores are separators.
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
ORG_TERMS = {
    "manolii": _B + "manolii" + _E,
    "impaktful": _B + "impaktful" + _E,
    "buro": _B + "(?:buro|buro-built)" + _E,
    "cpdcheck": _B + "(?:cpdcheck|ensombl)" + _E,
}
EXTRA_ORG = _B + "(?:knowledge-layer|picklebugs|lead-converter|hiha)" + _E

SECRET_PATTERNS = [
    r"ghp_[A-Za-z0-9]{20,}", r"gho_[A-Za-z0-9]{20,}", r"ghs_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}", r"sk-(?:live|proj)-[A-Za-z0-9_-]{20,}",
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY",
    r"xox[bpoas]-[A-Za-z0-9-]{10,}", r"dp\.(?:st|ct|sa)\.[A-Za-z0-9_-]{20,}",
    r"vercel_[a-z]+_[A-Za-z0-9]{20,}",
]
# The shipped detector corpus is the canonical credential-shape catalog —
# the scanner derives from it so new provider patterns cover this gate
# automatically (sk-ant-*, sk-or-*, ghr_*, ASIA*, JWT, stripe, ...). Loaded
# per check_secrets() call so REGISTRY reassignment in tests is honoured.
TOKEN_SHAPES_REL = "platform/framework/data/token-shapes.json"
MANIFEST_FILES = {".claude-plugin/plugin.json", ".devin-plugin/plugin.json",
                  "scope.yaml", "plugins.json", "CODEOWNERS", "README.md"}

# Ratchet allowlist: files (relative to registry/) grandfathered for the
# ORG-LEAK scan only. Today's platform seeds legitimately
# reference org identifiers (doc links, redaction detector data, entity
# examples) — the allowlist freezes that set: new leaks FAIL, stale entries
# FAIL, and P3 cleanup burns the list down. Regenerate with --write-allowlist.
ALLOWLIST_PATH = REGISTRY / "leak-allowlist.txt"
# Detector data corpus — exempt from ORG-LEAK only (it is by-design org
# identifier samples); NOT exempt from SECRETS — a real credential planted
# there must still fail. Self-matching lines are ratcheted per-line via
# secrets-allowlist.txt.
DETECTOR_DATA = {TOKEN_SHAPES_REL}
SECRETS_ALLOWLIST_PATH = REGISTRY / "secrets-allowlist.txt"

# PACK-SURFACE: this whole repo is PUBLIC — private-repo slugs and
# infrastructure identifiers anywhere in it are recon surface even when
# harmless. Org names are NOT flagged here: Manolii is the publisher and
# the other universe names are already public scope contracts. Per-line
# ratchet (same path#sha8 format) grandfathers today's references.
PACK_SURFACE_ALLOWLIST = REGISTRY / "pack-surface-allowlist.txt"
# VCS/vendor/cache dirs — only consulted by the NON-GIT fallback. In a
# real checkout the scan enumerates `git ls-files` instead: anything
# committed is published, whatever its directory is called.
PACK_SKIP_DIRS = {".git", "registry", "node_modules", "__pycache__",
                  "venv", ".venv", "dist", "build", ".next",
                  ".ruff_cache", ".pytest_cache", ".brand"}
PACK_SURFACE_EXEMPT = {"scripts/registry-lint.py", "tests/test_registry.py"}
PACK_SURFACE_PATTERNS = [
    # The canonical-repo exception must end at a true slug delimiter —
    # `\b` also fires before '-' and '.', which would exempt prefixed
    # PRIVATE repos like ai-starter-pack-private / ai-starter-pack.x.
    # `.git` stays exempt: it's the public repo's own clone suffix.
    r"Manolii-org/(?!ai-starter-pack(?:\.git)?(?![A-Za-z0-9_.-]))"
    r"[A-Za-z0-9_.-]+",
    r"Buro-Built/[A-Za-z0-9_.-]+",
    r"Impaktful-Platform/[A-Za-z0-9_.-]+",
    r"CPDcheck-[A-Za-z0-9_.-]+",
    r"prj_[A-Za-z0-9]{15,}",
    r"db\.[a-z0-9]+\.supabase\.co",
    r"ep-[a-z0-9-]+\.[a-z0-9.-]*neon\.tech",
    r"\b[a-z0-9-]+\.internal\b",
]
# Owner/repo slugs and hostnames are matched case-insensitively — DNS and
# git URLs don't care about casing, so `manolii-org/private-repo` must
# trip the gate just as `Manolii-org/private-repo` does.
PACK_SURFACE_RES = [re.compile(p, re.IGNORECASE)
                    for p in PACK_SURFACE_PATTERNS]

# The first four patterns are private-repo slugs keyed by org OWNER
# (the slug prefix before '/'; CPDcheck uses its repo-name prefix).
# In a verified private mirror the OWNING org's slug is legitimate — a
# buro mirror names its sibling buro repos on purpose — while every
# OTHER org's slugs stay violations (that is the cross-org privacy
# boundary). Infra patterns (index 4+) are sensitive anywhere and never
# relax.
PACK_SURFACE_ORG_OWNERS = ("manolii-org", "buro-built",
                           "impaktful-platform", "cpdcheck")

CANONICAL_PACK_SLUG = "manolii-org/ai-starter-pack"

# Trusted private mirrors — registry/private-mirrors.txt holds the
# sha256(lowercase owner/repo slug) of each org-owned repo that may carry
# universe content. Digests, not plaintext slugs: this repo is public, so
# a committed slug list would itself disclose the private repo names the
# PACK-SURFACE scan exists to keep out (and would fail that scan the
# moment a real mirror was declared). The marker waiver requires the
# origin slug's digest to appear here: `slug != canonical` alone is not
# proof of privacy (a public fork is also non-canonical).
PRIVATE_MIRRORS_PATH = REGISTRY / "private-mirrors.txt"

SCOPE_SCHEMA_PATH = REPO / "schemas" / "registry-scope.schema.json"
try:
    SCOPE_SCHEMA = json.loads(SCOPE_SCHEMA_PATH.read_text()) if jsonschema else None
except (OSError, json.JSONDecodeError):
    SCOPE_SCHEMA = None

@dataclass
class Finding:
    status: str  # FAIL | WARN | PASS
    check: str
    detail: str

results: list[Finding] = []


def report(status: str, check: str, detail: str) -> None:
    results.append(Finding(status, check, detail))


def scope_of(path: Path) -> str | None:
    rel = path.relative_to(REGISTRY).parts
    return rel[0] if rel and rel[0] in ALL_SCOPES else None


def exempt(rel_to_registry: Path) -> bool:
    """Manifest/root metadata files exempt from content scans."""
    parts = rel_to_registry.parts
    if len(parts) == 1:  # registry root files (plugins.json, README, allowlists)
        return True
    if len(parts) == 2 and parts[-1] == "scope.yaml":
        return True  # scope metadata — anything else at a scope root is scanned
    tail = "/".join(parts[-2:])
    # README.md is deliberately NOT exempt: nested plugin READMEs are
    # distributed content, and exempting them by basename would bypass the
    # per-line ratchet entirely.
    return tail in MANIFEST_FILES


def check_index() -> None:
    index_path = REGISTRY / "plugins.json"
    if not index_path.is_file():
        report("FAIL", "INDEX", "registry/plugins.json missing")
        return
    index = json.loads(index_path.read_text())
    seen = set()
    for p in index.get("plugins", []):
        key = (p.get("scope"), p.get("name"))
        seen.add(key)
        path = REPO / p.get("path", "")
        if not path.is_dir():
            report("FAIL", "INDEX", f"{key}: path missing: {p.get('path')}")
        if key[0] not in ALL_SCOPES:
            report("FAIL", "INDEX", f"{key}: unknown scope '{key[0]}'")
        expected = f"registry/{key[0]}/{key[1]}"
        if p.get("path") != expected:
            report("FAIL", "INDEX",
                   f"{key}: path '{p.get('path')}' != '{expected}' — "
                   "the resolver would materialise a different plugin than named")
    # Every registry-root dir is a scope — an undeclared dir (e.g.
    # registry/acme/private/) is invisible to the scope loop and has
    # scope_of() == None, so ORG-LEAK and XSCOPE skip it entirely. Fail
    # closed on every directory not declared in ALL_SCOPES.
    for child in REGISTRY.iterdir():
        if child.is_dir() and child.name not in ALL_SCOPES:
            report("FAIL", "INDEX",
                   f"undeclared scope dir: {child.name}/ — every registry "
                   "scope must be one of " + "/".join(ALL_SCOPES))
    # every plugin dir under a scope must be indexed
    for scope in ALL_SCOPES:
        sdir = REGISTRY / scope
        # A symlinked scope root passes is_dir() — but rglob() does not
        # descend through directory symlinks, so MANIFEST/ORG-LEAK/SECRETS/
        # XSCOPE would skip the plugin bytes behind it while INDEX passes.
        # A scope root must be a real directory.
        if sdir.is_symlink():
            report("FAIL", "INDEX",
                   f"scope root is a symlink: {scope}/ — refusing to approve "
                   "content the scans cannot descend into")
            continue
        if not sdir.is_dir():
            continue
        scope_root = sdir.resolve()
        for child in sdir.iterdir():
            # A symlinked plugin dir aliases another scope's tree — the
            # resolver would distribute the target's content under this
            # plugin's name/scope. The resolved dir must also stay inside
            # its declared scope.
            if (child.is_symlink()
                    or not child.resolve().is_relative_to(scope_root)):
                report("FAIL", "INDEX",
                       f"plugin dir is a symlink or escapes its scope: "
                       f"{scope}/{child.name} — refusing to follow")
                continue
            # Every dir under a scope is a plugin candidate — requiring
            # .claude-plugin/plugin.json here would let an unindexed dir that
            # also lacks its manifest pass INDEX and escape MANIFEST too.
            if child.is_dir() and (scope, child.name) not in seen:
                report("FAIL", "INDEX", f"plugin dir not indexed: {scope}/{child.name}")
            if child.is_dir():
                # Symlinked FILES inside a real plugin dir — the resolver's
                # file loop follows links, so agents/leak.md -> /etc/passwd
                # would ship the target's bytes to consumers.
                for p in child.rglob("*"):
                    if p.is_symlink():
                        report("FAIL", "INDEX",
                               f"symlinked file inside plugin: "
                               f"{p.relative_to(REGISTRY)} -> {p.readlink()}")
    if not any(f.status == "FAIL" and f.check == "INDEX" for f in results):
        report("PASS", "INDEX", f"{len(seen)} plugins indexed, filesystem consistent")


def check_manifests() -> None:
    bad = 0
    # Every indexed plugin must carry BOTH manifests — a directory that lacks
    # .claude-plugin/plugin.json is never visited by the file loop below, so
    # require it explicitly (the resolver would silently treat it as v0.0.0).
    index_path = REGISTRY / "plugins.json"
    if index_path.is_file():
        for p in json.loads(index_path.read_text()).get("plugins", []):
            pdir = REGISTRY / p.get("scope", "") / p.get("name", "")
            for man in (".claude-plugin/plugin.json", ".devin-plugin/plugin.json"):
                if not (pdir / man).is_file():
                    report("FAIL", "MANIFEST",
                           f"{pdir.relative_to(REGISTRY)}: indexed plugin missing {man}")
                    bad += 1
    for pj in REGISTRY.rglob(".claude-plugin/plugin.json"):
        rel = pj.relative_to(REGISTRY)
        plugin_dir = pj.parent.parent
        try:
            m = json.loads(pj.read_text())
        except json.JSONDecodeError:
            report("FAIL", "MANIFEST", f"{rel}: invalid JSON")
            bad += 1
            continue
        for key in ("name", "version", "description"):
            if key not in m:
                report("FAIL", "MANIFEST", f"{rel}: missing '{key}'")
                bad += 1
        v = m.get("version")
        if "version" in m and (not isinstance(v, str)
                               or not re.fullmatch(r"v?\d+(?:\.\d+){0,2}", v)):
            report("FAIL", "MANIFEST",
                   f"{rel}: 'version' must be a semver string (x[.y[.z]]), got {v!r}")
            bad += 1
        if m.get("name") != plugin_dir.name:
            report("FAIL", "MANIFEST",
                   f"{rel}: name '{m.get('name')}' != dir '{plugin_dir.name}'")
            bad += 1
        dv = plugin_dir / ".devin-plugin" / "plugin.json"
        if not dv.is_file():
            report("FAIL", "MANIFEST", f"{plugin_dir.relative_to(REGISTRY)}: missing .devin-plugin/plugin.json")
            bad += 1
        else:
            try:
                dm = json.loads(dv.read_text())
                if dm.get("name") != plugin_dir.name:
                    report("FAIL", "MANIFEST", f"{dv.relative_to(REGISTRY)}: name mismatch")
                    bad += 1
            except json.JSONDecodeError:
                report("FAIL", "MANIFEST", f"{dv.relative_to(REGISTRY)}: invalid JSON")
                bad += 1
    if not bad:
        report("PASS", "MANIFEST", "plugin manifests valid")


def check_scopes() -> None:
    bad = 0
    if SCOPE_SCHEMA is None:
        report("FAIL", "SCOPE",
               "schemas/registry-scope.schema.json unreadable or jsonschema "
               "missing — scope contracts cannot be validated")
        bad += 1
    for scope in ALL_SCOPES:
        sdir = REGISTRY / scope
        if not sdir.is_dir():
            report("WARN", "SCOPE", f"scope dir missing: {scope} (ok if unused)")
            continue
        sy = sdir / "scope.yaml"
        if not sy.is_file():
            report("FAIL", "SCOPE", f"{scope}: missing scope.yaml")
            bad += 1
            continue
        try:
            doc = yaml.safe_load(sy.read_text()) or {}
        except yaml.YAMLError:
            report("FAIL", "SCOPE", f"{scope}: scope.yaml invalid YAML")
            bad += 1
            continue
        if SCOPE_SCHEMA is not None:
            validator_cls = jsonschema.validators.validator_for(SCOPE_SCHEMA)
            for err in sorted(validator_cls(SCOPE_SCHEMA).iter_errors(doc),
                              key=lambda e: list(e.absolute_path)):
                where = "/".join(str(p) for p in err.absolute_path) or "<root>"
                report("FAIL", "SCOPE", f"{scope}: schema violation at {where}: {err.message}")
                bad += 1
        if doc.get("scope") != scope:
            report("FAIL", "SCOPE", f"{scope}: scope field '{doc.get('scope')}' != dir")
            bad += 1
        vis = doc.get("visibility")
        if scope == "platform" and vis != "all-universes":
            report("FAIL", "SCOPE", f"platform visibility must be all-universes, got {vis}")
            bad += 1
        if scope in UNIVERSES and vis != scope:
            report("FAIL", "SCOPE", f"{scope}: visibility must be '{scope}', got {vis}")
            bad += 1
        if scope in LOCAL_SCOPES and vis != "local-only":
            report("FAIL", "SCOPE", f"{scope}: visibility must be local-only, got {vis}")
            bad += 1
        if scope in UNIVERSES and doc.get("parent_scope") != "platform":
            report("FAIL", "SCOPE", f"{scope}: parent_scope must be platform")
            bad += 1
        if not doc.get("ip_owner"):
            report("FAIL", "SCOPE", f"{scope}: missing ip_owner")
            bad += 1
    if not bad:
        report("PASS", "SCOPE", "scope contracts valid")


def load_line_allowlist(path: Path) -> set[str]:
    """Per-line ratchet entries: `registry-rel/path#sha8` where sha8 is the
    first 8 hex of sha256(stripped-lowercase line). A line grandfathered here
    suppresses the scan for exactly that line — new hits on OTHER lines
    of the same file still fail."""
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")}


def line_key(rel_s: str, line: str) -> str:
    h = hashlib.sha256(line.strip().lower().encode()).hexdigest()[:8]
    return f"{rel_s}#{h}"


def _decoded_json_strings(doc) -> list[str]:
    """Every string key and value in a parsed JSON document."""
    out = []
    if isinstance(doc, str):
        out.append(doc)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(k, str):
                out.append(k)
            out += _decoded_json_strings(v)
    elif isinstance(doc, list):
        for v in doc:
            out += _decoded_json_strings(v)
    return out


_SOURCE_ESC = re.compile(
    r"\\(?:x([0-9a-fA-F]{2})|u\{([0-9a-fA-F]{1,6})\}|u([0-9a-fA-F]{4})"
    r"|U([0-9a-fA-F]{8})|([0-7]{1,3})|N\{([A-Za-z0-9 \-]+)\})")


def _decode_source_escapes(line: str) -> str:
    """Decode \\xNN / \\uXXXX / \\UXXXXXXXX / \\u{X..XXXXXX} / \\ooo /
    \\N{name} escapes as a Python/JS/TS consumer would — source files can
    embed a credential behind runtime-decoded escapes ("ghp_\\x41",
    "ghp_\\101", "ghp_\\N{LATIN CAPITAL LETTER A}...") that raw text
    cannot match. `\\\\` is protected first so a literal backslash doesn't
    double-decode."""
    if "\\" not in line:
        return line

    def rep(m: re.Match) -> str:
        if m.group(5) is not None:
            return chr(int(m.group(5), 8))
        if m.group(6) is not None:
            try:
                return unicodedata.lookup(m.group(6))
            except KeyError:
                return m.group(0)
        for g in m.groups()[:4]:
            if g:
                try:
                    return chr(int(g, 16))
                except ValueError:
                    return m.group(0)
        return m.group(0)

    t = line.replace("\\\\", "\x00")
    t = _SOURCE_ESC.sub(rep, t)
    return t.replace("\x00", "\\")


def scan_text_lines(path: Path) -> list[str]:
    """Encoding-normalized text lines for content scans. UTF-16/32 encode
    ASCII-shaped credentials with NUL separators — stripping NULs recovers
    the ASCII bytes so encoding alone cannot exempt a credential. JSON files
    additionally yield their DECODED string values — \\uXXXX and \\/ escapes
    become the real value when a consumer parses the document, so the raw
    text must not be the only form the scans see."""
    raw = path.read_bytes()
    # Decode the file's ACTUAL encoding for structured parsing — a UTF-16/32
    # JSON document must parse as JSON (an escaped credential inside it is
    # reconstructed by the consumer's parser). BOM is definitive; BOM-less
    # UTF-16/32 keeps the NUL-stripped appendix for line coverage AND as the
    # structured-parse candidate.
    enc = "utf-8"
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        enc = "utf-32"
    elif raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"
    text = raw.decode(enc, errors="ignore")
    parsable = text
    if enc == "utf-8" and b"\x00" in raw:
        stripped = raw.replace(b"\x00", b"").decode("utf-8", errors="ignore")
        text += "\n" + stripped
        parsable = stripped
    lines = text.splitlines()
    # Source-language escape pass: \\xNN / \\uXXXX in .py/.js/.ts/.sh/... is
    # decoded by the consumer's runtime, so scan the decoded form too.
    # Structured formats are excluded — their own decoders (below) already
    # handle their escape sets, and re-decoding raw JSON/YAML/TOML text would
    # double-report legitimately escaped values. Same escaped-form dedupe:
    # a decoded line is only added when its own escaped form is absent.
    if path.suffix not in (".json", ".yaml", ".yml", ".toml"):
        for line in list(lines):
            d = _decode_source_escapes(line)
            if d != line and json.dumps(d)[1:-1] not in text:
                lines.append(d)
    docs = []
    if path.suffix == ".json":
        try:
            doc = json.loads(parsable)
        except json.JSONDecodeError:
            doc = None
        if doc is not None:
            docs = [doc]
    elif path.suffix in (".yaml", ".yml"):
        # YAML decodes the same \uXXXX escapes inside quoted scalars — a
        # token: "ghp_\u0041..." value is a credential once PyYAML reads it
        # while invisible to the raw text scan.
        try:
            docs = [d for d in yaml.safe_load_all(parsable)]
        except yaml.YAMLError:
            docs = []
    elif path.suffix == ".toml" and tomllib is not None:
        # TOML basic strings decode \uXXXX the same way — token = "ghp_\u0041"
        # is a credential once a TOML consumer parses it.
        try:
            docs = [tomllib.loads(parsable)]
        except tomllib.TOMLDecodeError:
            docs = []
    for doc in docs:
        for s in _decoded_json_strings(doc):
            for piece in s.splitlines():
                # A decoded line only adds coverage when the value is hidden
                # behind escapes — i.e. the file does NOT contain the line's
                # own JSON-escaped form. Legitimately escaped values (\u2014,
                # \\d, \") reproduce exactly what the file shows and are
                # skipped; escape-hidden values surface.
                esc = json.dumps(piece)[1:-1]
                if piece and esc not in text:
                    lines.append(piece)
    return lines


def content_scan_files(org_leak: bool = True) -> list[Path]:
    """Files scanned for org identifiers (org_leak=True) or secrets (False).
    Every file under registry/ is scanned — no extension allowlist and no
    content-sniffed binary exclusion either: a NUL byte or unusual name must
    not be able to exempt a committed credential. Org-leak additionally
    exempts scope metadata (allowlist entries, scope.yaml, manifests);
    secrets exempts only DETECTOR_DATA."""
    out = []
    for path in sorted(REGISTRY.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(REGISTRY)
        if org_leak and (rel.as_posix() in DETECTOR_DATA
                       or scope_of(path) is None or exempt(rel)):
            continue
        out.append(path)
    return out


def check_org_leak() -> None:
    fails = warns = 0
    allow = load_line_allowlist(ALLOWLIST_PATH)
    used: set[str] = set()
    extra_re = re.compile(EXTRA_ORG, re.IGNORECASE)
    for path in content_scan_files():
        scope = scope_of(path)
        rel = path.relative_to(REGISTRY)
        rel_s = rel.as_posix()
        try:
            lines = scan_text_lines(path)
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            key = line_key(rel_s, line)
            grandfathered = key in allow
            if grandfathered:
                used.add(key)
            for org, pat in ORG_TERMS.items():
                if scope in UNIVERSES and org == scope:
                    continue  # a universe may name itself
                if re.search(pat, line, re.IGNORECASE) and not grandfathered:
                    report("FAIL", "ORG-LEAK", f"{rel}:{i} — '{org}' in {scope} scope")
                    fails += 1
            if not grandfathered and extra_re.search(line):
                report("FAIL", "ORG-LEAK", f"{rel}:{i} — internal identifier")
                fails += 1
            if re.search(r"\bkl_", line):
                warns += 1
    # Stale entries FAIL, not warn — a grandfathered line hash is reusable:
    # reintroducing the identical leaked line at the same path is silently
    # allowlisted while the stale entry sits. Burn-down must be enforced.
    for stale in sorted(allow - used):
        if (REGISTRY / stale.rsplit("#", 1)[0]).exists():
            report("FAIL", "ORG-LEAK", f"allowlist line clean now — remove it: {stale}")
        else:
            report("FAIL", "ORG-LEAK", f"stale allowlist entry — remove it: {stale}")
    if warns:
        report("WARN", "ORG-LEAK", f"kl_* (opt-in remote memory) referenced in {warns} lines — allowed when gated")
    if fails:
        report("FAIL", "ORG-LEAK", f"{fails} new leak(s) outside the {len(allow)}-line ratchet allowlist")
    else:
        report("PASS", "ORG-LEAK", f"no new leaks ({len(used)} lines ratchet-allowlisted)")


def check_secrets() -> None:
    # The ORG-LEAK ratchet allowlist does NOT apply here — an org-name
    # grandfather must never suppress credential detection. The corpus file
    # is scanned too; only lines matching a secrets-ratchet entry
    # (secrets-allowlist.txt, same path#sha8 format) are suppressed — a real
    # credential planted inside token-shapes.json cannot hide there.
    pats = [re.compile(p) for p in SECRET_PATTERNS]
    fails = 0
    # Derive the scanner from the shipped catalog — and fail closed when the
    # catalog can't be read, rather than silently degrading to the short
    # hardcoded list (that would leave Anthropic/OpenRouter/JWT/… uncovered).
    try:
        _shapes = json.loads(
            (REGISTRY / TOKEN_SHAPES_REL).read_text(encoding="utf-8"))
        _cat = [p["regex"] for p in _shapes.get("patterns", [])
                if p.get("regex")]
        pats += [re.compile(p) for p in _cat if p not in SECRET_PATTERNS]
    except (OSError, json.JSONDecodeError, AttributeError):
        report("FAIL", "SECRETS",
               f"{TOKEN_SHAPES_REL} unreadable — credential catalog cannot be "
               "derived; scanning with the base pattern list only")
        fails += 1
    allow = load_line_allowlist(SECRETS_ALLOWLIST_PATH)
    used: set[str] = set()
    for path in content_scan_files(org_leak=False):
        rel = path.relative_to(REGISTRY)
        rel_s = rel.as_posix()
        try:
            lines = scan_text_lines(path)
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if any(p.search(line) for p in pats):
                key = line_key(rel_s, line)
                if key in allow:
                    used.add(key)
                    continue
                report("FAIL", "SECRETS", f"{rel}:{i} — credential-shaped string")
                fails += 1
    # Stale entries FAIL — an unused credential exemption is reusable:
    # reintroducing the identical line at the same path would silently
    # pass. Same ratchet contract as ORG-LEAK and PACK-SURFACE.
    for stale in sorted(allow - used):
        report("FAIL", "SECRETS",
               f"stale allowlist entry — remove it: {stale}")
        fails += 1
    if not fails:
        report("PASS", "SECRETS", "no credential shapes")


def check_skills() -> None:
    bad = warn = 0
    for sm in REGISTRY.rglob("SKILL.md"):
        rel = sm.relative_to(REGISTRY)
        text = sm.read_text(encoding="utf-8", errors="ignore")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if not m:
            report("WARN", "SKILL", f"{rel}: no frontmatter (legacy flat skill)")
            warn += 1
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            report("FAIL", "SKILL", f"{rel}: frontmatter invalid YAML")
            bad += 1
            continue
        for key in ("name", "description"):
            if key not in fm:
                report("FAIL", "SKILL", f"{rel}: missing '{key}'")
                bad += 1
        for key in ("version", "type", "safety_tier", "requires_mcp"):
            if key not in fm:
                warn += 1
    if warn:
        report("WARN", "SKILL", f"{warn} optional frontmatter field(s) absent (pack spec is lighter than manifest-spec)")
    if not bad:
        report("PASS", "SKILL", "SKILL.md frontmatter valid")


def _norm_scope_path(line: str) -> str:
    """Collapse JSON-slash escapes, duplicate separators and `.`/`..`
    segments so equivalent spellings of a scope path cannot evade the
    XSCOPE substring match. Iterated to a fixpoint — each pass can
    expose a new collapsible pair (a/b/../c/../x -> a/../x -> x)."""
    t = line.replace("\\/", "/")
    prev = None
    while prev != t:
        prev = t
        t = t.replace("//", "/").replace("/./", "/")
        t = re.sub(r"/[^/\s\"']+/\.\./", "/", t)
    return t


def check_xscope() -> None:
    fails = 0
    for path in sorted(REGISTRY.rglob("*")):
        # Every regular file — no extension allowlist: hooks, scripts and
        # other plugin assets are valid content and a .ts/.yaml/.toml or
        # extensionless file can wire ../<scope>/ just as well as .md.
        if not path.is_file():
            continue
        scope = scope_of(path)
        rel = path.relative_to(REGISTRY)
        # Manifest metadata is exempt from the org-NAME scan (publisher info)
        # but NOT from this path check — a plugin.json can wire components
        # into another scope via ../<scope>/ references.
        is_manifest = rel.name == "plugin.json"
        if scope is None or (exempt(rel) and not is_manifest):
            continue
        try:
            lines = scan_text_lines(path)
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            # JSON string values escape '/' as '\/': a manifest carrying
            # "registry\/manolii\/private" parses to the forbidden path but
            # slips the raw-text match — normalize escapes before matching.
            # Dot segments get the same treatment: .././manolii/ and
            # registry/x/../manolii/ resolve into another scope's tree
            # without containing the literal substrings checked below.
            norm = _norm_scope_path(line)
            for other in ALL_SCOPES:
                if other == scope:
                    continue
                # ../<other>/ is the relative form; registry/<other>/ is the
                # repository-root form a manifest 'path' field uses — both
                # wire one scope's tree into another's consumers.
                if (f"../{other}/" in norm
                        or f"registry/{other}/" in norm):
                    report("FAIL", "XSCOPE", f"{rel}:{i} references {other}/")
                    fails += 1
                    break
    if not fails:
        report("PASS", "XSCOPE", "no cross-scope path references")


def org_leak_lines() -> list[str]:
    """Per-line ratchet entries for every org-identifier line under registry/
    (for --write-allowlist regeneration)."""
    out = set()
    extra_re = re.compile(EXTRA_ORG, re.IGNORECASE)
    for path in content_scan_files():
        scope = scope_of(path)
        rel_s = path.relative_to(REGISTRY).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            hit = extra_re.search(line) is not None
            if not hit:
                for org, pat in ORG_TERMS.items():
                    if scope in UNIVERSES and org == scope:
                        continue
                    if re.search(pat, line, re.IGNORECASE):
                        hit = True
                        break
            if hit:
                out.add(line_key(rel_s, line))
    return sorted(out)


def _tracked_files() -> list[Path] | None:
    """Every git-tracked file in the repo, or None outside a git checkout.
    Tracked enumeration replaces directory-name skips entirely: a slug
    committed under .brand/ (or any other directory name) is published
    content and must be scanned — only untracked caches/vendor trees may
    be skipped, and untracked files are absent from ls-files anyway."""
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z"],
            capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = []
    for rel in r.stdout.decode("utf-8", errors="ignore").split("\x00"):
        if rel:
            out.append(REPO / rel)
    return out


def pack_surface_files() -> list[Path]:
    """Files covered by the PACK-SURFACE scan. In a git checkout this is
    every tracked file (including registry/ and .brand/ — no other check
    covers the PACK-SURFACE pattern set there); without git, an rglob
    fallback keeps the VCS/vendor/cache skips."""
    tracked = _tracked_files()
    if tracked is not None:
        return [p for p in tracked if p.is_file()]
    return [
        p for p in sorted(REPO.rglob("*"))
        if p.is_file()
        and not any(part in PACK_SKIP_DIRS
                    for part in p.relative_to(REPO).parts)
    ]


def pack_surface_hits() -> list[str]:
    """Every current PACK-SURFACE line-key (for --write-pack-allowlist).
    Uses the active pattern set — in a verified mirror, own-org hits are
    legitimate and never enter the ratchet."""
    out = []
    res = _pack_surface_res()
    for path in pack_surface_files():
        rel_s = path.relative_to(REPO).as_posix()
        if rel_s in PACK_SURFACE_EXEMPT:
            continue
        try:
            lines = scan_text_lines(path)
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if any(p.search(line) for p in res):
                out.append(line_key(rel_s, line))
    return sorted(set(out))


def _pack_surface_res() -> list[re.Pattern]:
    """Active PACK-SURFACE patterns for this checkout.

    In a VERIFIED private mirror the origin owner's slug pattern is
    removed: a buro mirror names Buro-Built/* repos legitimately — the
    check's job there is the cross-org boundary (other orgs' slugs) plus
    infra ids, which stay active. In the canonical public repo (or an
    unverified checkout) every pattern applies."""
    slug = _verified_mirror_slug()
    if slug is None:
        return PACK_SURFACE_RES
    owner = slug.split("/", 1)[0]
    return [p for i, p in enumerate(PACK_SURFACE_RES)
            if i >= len(PACK_SURFACE_ORG_OWNERS)
            or PACK_SURFACE_ORG_OWNERS[i] != owner]


def check_pack_surface() -> None:
    """No private-repo slugs or infra identifiers anywhere in the tracked
    tree. registry/ is included — ORG-LEAK covers org NAMES there, but
    not the infra-id shapes (project refs, endpoints, .internal) this
    check owns; credentials stay with SECRETS + Detect Secrets."""
    fails = 0
    res = _pack_surface_res()
    allow = load_line_allowlist(PACK_SURFACE_ALLOWLIST)
    for path in pack_surface_files():
        rel_s = path.relative_to(REPO).as_posix()
        if rel_s in PACK_SURFACE_EXEMPT:
            continue
        try:
            lines = scan_text_lines(path)
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if (any(p.search(line) for p in res)
                    and line_key(rel_s, line) not in allow):
                report("FAIL", "PACK-SURFACE",
                       f"{rel_s}:{i} — private slug/infra id in public repo")
                fails += 1
    # Stale entries are reusable exemptions — FAIL them (same contract
    # as ORG-LEAK's ratchet).
    for stale in sorted(set(allow) - set(pack_surface_hits())):
        report("FAIL", "PACK-SURFACE",
               f"stale allowlist entry — remove it: {stale}")
    if not fails:
        report("PASS", "PACK-SURFACE",
               "no private slugs/infra ids in tracked files")


def _origin_slug() -> str | None:
    """owner/repo of the origin remote, lowercased — or None when the
    remote is absent/unparseable. The marker waiver must be tied to a
    VERIFIED private context: a committed `.private-mirror` file alone
    cannot disable the public boundary, or one PR could plant both the
    marker and universe content and still pass."""
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"[:/]([^/\s]+/[^/\s]+?)\.git$", r.stdout.strip()) \
        or re.search(r"[:/]([^/\s]+/[^/\s]+?)/?$", r.stdout.strip())
    return m.group(1).lower() if m else None


def _mirror_visibility_asserted() -> bool:
    """Mirror mode needs an externally-supplied visibility assertion —
    MIRROR_VISIBILITY=private|internal set by the caller from an
    out-of-repo check (CI runs `gh api repos/<repo> --jq .visibility`;
    bootstrap-mirror.py sets it from its own verified result). A
    checkout's own committed files can never prove its repo is private:
    a public fork can carry both the marker and its own slug digest."""
    return os.environ.get("MIRROR_VISIBILITY", "").strip().lower() in (
        "private", "internal")


def _verified_mirror_slug() -> str | None:
    """The origin slug when this checkout is a VERIFIED private mirror
    (marker present, origin resolvable, slug not canonical, slug digest
    in registry/private-mirrors.txt, AND MIRROR_VISIBILITY asserted) —
    else None. Shared by the PUBLIC boundary check and the mirror-mode
    PACK-SURFACE pattern selection."""
    if not (REGISTRY / ".private-mirror").is_file():
        return None
    slug = _origin_slug()
    if slug is None or slug == CANONICAL_PACK_SLUG:
        return None
    if hashlib.sha256(slug.encode()).hexdigest() not in _trusted_mirrors():
        return None
    if not _mirror_visibility_asserted():
        return None
    return slug


def _trusted_mirrors() -> set[str]:
    """Declared private-mirror digests (sha256 of the lowercase
    owner/repo slug) from registry/private-mirrors.txt — the committed,
    non-disclosing trust record the marker waiver checks against."""
    if not PRIVATE_MIRRORS_PATH.is_file():
        return set()
    return {line.strip().lower()
            for line in PRIVATE_MIRRORS_PATH.read_text().splitlines()
            if re.fullmatch(r"[0-9a-f]{64}", line.strip())}


def check_public_boundary() -> None:
    """This registry is PUBLIC — universe scopes never land here; they live
    in per-org private mirrors (a mirror carries registry/.private-mirror to
    opt out of this guard). repo/personal scopes live inside consumer
    repos and never sync upstream either. The scope.yaml CONTRACTS scaffold
    here is public-safe metadata; anything beyond it is content."""
    if (REGISTRY / ".private-mirror").is_file():
        slug = _origin_slug()
        if slug == CANONICAL_PACK_SLUG:
            report("FAIL", "PUBLIC",
                   "registry/.private-mirror committed in the canonical "
                   f"public repo ({slug}) — the marker only has meaning "
                   "in a private mirror; remove it")
            return
        if slug is None:
            report("FAIL", "PUBLIC",
                   "registry/.private-mirror present but the origin remote "
                   "cannot be verified as a private mirror — refusing to "
                   "waive the public boundary on an unverifiable marker")
            return
        digest = hashlib.sha256(slug.encode()).hexdigest()
        if digest not in _trusted_mirrors():
            report("FAIL", "PUBLIC",
                   f"origin '{slug}' is not a declared private mirror — "
                   "a public fork can carry .private-mirror, so the waiver "
                   "applies only to slug digests listed in "
                   "registry/private-mirrors.txt")
            return
        if not _mirror_visibility_asserted():
            report("FAIL", "PUBLIC",
                   "mirror declared but MIRROR_VISIBILITY is not asserted "
                   "to private/internal — CI must set it from an API check "
                   "(the generated registry-lint workflow does); committed "
                   "files alone cannot prove this repo is private")
            return
        report("PASS", "PUBLIC",
               f"declared private mirror ({slug}) — boundary waived")
        return
    fails = 0
    for scope in UNIVERSES + LOCAL_SCOPES:
        sdir = REGISTRY / scope
        if not sdir.is_dir():
            continue
        for child in sorted(sdir.iterdir()):
            if child.name == "scope.yaml" and child.is_file():
                continue
            report("FAIL", "PUBLIC",
                   f"{scope}/{child.name} in the public registry — {scope} "
                   "content lives in its private mirror or in consumer "
                   "repos; only scope.yaml may scaffold here")
            fails += 1
    if not fails:
        report("PASS", "PUBLIC",
               "universe/local scopes hold contracts only (scope.yaml)")


def main() -> int:
    if "--write-pack-allowlist" in sys.argv:
        PACK_SURFACE_ALLOWLIST.write_text(
            "# Per-line ratchet allowlist — `path#sha8(stripped-line)` entries\n"
            "# grandfathered for the PACK-SURFACE scan (private slugs/infra ids).\n"
            "# New hits FAIL. Regenerate:\n"
            "#   python3 scripts/registry-lint.py --write-pack-allowlist\n"
            + "\n".join(pack_surface_hits()) + "\n")
        print(f"wrote {PACK_SURFACE_ALLOWLIST.relative_to(REPO)}")
        return 0
    if "--write-allowlist" in sys.argv:
        lines = org_leak_lines()
        ALLOWLIST_PATH.write_text(
            "# Per-line ratchet allowlist — `path#sha8(stripped-line)` entries\n"
            "# grandfathered for the ORG-LEAK scan only (SECRETS scans all files).\n"
            "# New identifier lines FAIL; consumed entries that stop matching warn.\n"
            "# Regenerate: python3 scripts/registry-lint.py --write-allowlist\n"
            + "\n".join(lines) + "\n")
        print(f"wrote {len(lines)} entries -> {ALLOWLIST_PATH.relative_to(REPO)}")
        return 0
    if not REGISTRY.is_dir():
        sys.stderr.write(f"FAIL: {REGISTRY} missing\n")
        return 1
    for fn in (check_index, check_manifests, check_scopes, check_org_leak,
               check_secrets, check_skills, check_xscope,
               check_pack_surface, check_public_boundary):
        fn()
    fails = sum(1 for f in results if f.status == "FAIL")
    warns = sum(1 for f in results if f.status == "WARN")
    for f in results:
        print(f"{f.status:<5} {f.check:<8} {f.detail}")
    print(f"\n{fails} FAIL, {warns} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
