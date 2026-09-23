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
import re
import sys
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

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "registry"

UNIVERSES = ("manolii", "buro", "impaktful", "cpdcheck")
LOCAL_SCOPES = ("repo", "personal")
ALL_SCOPES = ("platform",) + UNIVERSES + LOCAL_SCOPES

# Org identifiers. `manolii` is the registry's own publisher — platform content
# must not carry ANY universe id including manolii (that's the whole point of
# platform scope). Universe scopes may name themselves only.
ORG_TERMS = {
    "manolii": r"\bmanolii\b",
    "impaktful": r"\bimpaktful\b",
    "buro": r"\bburo\b|\bburo-built\b",
    "cpdcheck": r"\bcpdcheck\b|\bensombl\b",
}
EXTRA_ORG = r"\bknowledge-layer\b|\bpicklebugs\b|\blead-converter\b|\bhiha\b"

SECRET_PATTERNS = [
    r"ghp_[A-Za-z0-9]{20,}", r"gho_[A-Za-z0-9]{20,}", r"ghs_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}", r"sk-(?:live|proj)-[A-Za-z0-9_-]{20,}",
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY",
    r"xox[bpoas]-[A-Za-z0-9-]{10,}", r"dp\.(?:st|ct|sa)\.[A-Za-z0-9_-]{20,}",
    r"vercel_[a-z]+_[A-Za-z0-9]{20,}",
]
MANIFEST_FILES = {".claude-plugin/plugin.json", ".devin-plugin/plugin.json",
                  "scope.yaml", "plugins.json", "CODEOWNERS", "README.md"}

# Ratchet allowlist: files (relative to registry/) grandfathered for the
# ORG-LEAK scan only. Today's platform seeds legitimately
# reference org identifiers (doc links, redaction detector data, entity
# examples) — the allowlist freezes that set: new leaks FAIL, stale entries
# FAIL, and P3 cleanup burns the list down. Regenerate with --write-allowlist.
ALLOWLIST_PATH = REGISTRY / "leak-allowlist.txt"
# Always-exempt detector data (the secret-shapes corpus would self-triage).
DETECTOR_DATA = {"platform/framework/data/token-shapes.json"}

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
    if len(parts) <= 2:  # registry root files + scope.yaml
        return True
    tail = "/".join(parts[-2:])
    return tail in MANIFEST_FILES or parts[-1] == "README.md"


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
    # every plugin dir under a scope must be indexed
    for scope in ALL_SCOPES:
        sdir = REGISTRY / scope
        if not sdir.is_dir():
            continue
        for child in sdir.iterdir():
            if child.is_dir() and (child / ".claude-plugin" / "plugin.json").exists():
                if (scope, child.name) not in seen:
                    report("FAIL", "INDEX", f"plugin dir not indexed: {scope}/{child.name}")
    if not any(f.status == "FAIL" and f.check == "INDEX" for f in results):
        report("PASS", "INDEX", f"{len(seen)} plugins indexed, filesystem consistent")


def check_manifests() -> None:
    bad = 0
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


def load_allowlist() -> set[str]:
    """Per-line ratchet entries: `registry-rel/path#sha8` where sha8 is the
    first 8 hex of sha256(stripped-lowercase line). A line grandfathered here
    suppresses ORG-LEAK for exactly that line — new identifiers on OTHER lines
    of the same file still fail."""
    if not ALLOWLIST_PATH.is_file():
        return set()
    return {line.strip() for line in ALLOWLIST_PATH.read_text().splitlines()
            if line.strip() and not line.startswith("#")}


def line_key(rel_s: str, line: str) -> str:
    h = hashlib.sha256(line.strip().lower().encode()).hexdigest()[:8]
    return f"{rel_s}#{h}"


def is_text_file(path: Path) -> bool:
    """Sniff for binary content — the content scans cover every text file in
    the registry regardless of extension (.env, Dockerfile, extensionless
    executables): a credential or org name must not evade the gate by living
    in a file type nobody enumerated."""
    try:
        return b"\x00" not in path.read_bytes()[:8192]
    except OSError:
        return False


def content_scan_files(org_leak: bool = True) -> list[Path]:
    """Files scanned for org identifiers (org_leak=True) or secrets (False).
    Org-leak exempts scope metadata (allowlist entries, scope.yaml, DETECTOR_DATA);
    secrets exempts only DETECTOR_DATA — a credential committed in a manifest or
    a scope.yaml must still fail."""
    out = []
    for path in sorted(REGISTRY.rglob("*")):
        if not path.is_file() or not is_text_file(path):
            continue
        rel = path.relative_to(REGISTRY)
        if rel.as_posix() in DETECTOR_DATA:
            continue
        if org_leak and (scope_of(path) is None or exempt(rel)):
            continue
        out.append(path)
    return out


def check_org_leak() -> None:
    fails = warns = 0
    allow = load_allowlist()
    used: set[str] = set()
    extra_re = re.compile(EXTRA_ORG, re.IGNORECASE)
    for path in content_scan_files():
        scope = scope_of(path)
        rel = path.relative_to(REGISTRY)
        rel_s = rel.as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
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
    for stale in sorted(allow - used):
        if (REGISTRY / stale.rsplit("#", 1)[0]).exists():
            report("WARN", "ORG-LEAK", f"allowlist line clean now — remove it: {stale}")
        else:
            report("WARN", "ORG-LEAK", f"stale allowlist entry — remove it: {stale}")
    if warns:
        report("WARN", "ORG-LEAK", f"kl_* (opt-in remote memory) referenced in {warns} lines — allowed when gated")
    if fails:
        report("FAIL", "ORG-LEAK", f"{fails} new leak(s) outside the {len(allow)}-line ratchet allowlist")
    else:
        report("PASS", "ORG-LEAK", f"no new leaks ({len(used)} lines ratchet-allowlisted)")


def check_secrets() -> None:
    # The ORG-LEAK ratchet allowlist does NOT apply here — an org-name
    # grandfather must never suppress credential detection.
    pats = [re.compile(p) for p in SECRET_PATTERNS]
    fails = 0
    for path in content_scan_files(org_leak=False):
        rel = path.relative_to(REGISTRY)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in pats):
                report("FAIL", "SECRETS", f"{rel}:{i} — credential-shaped string")
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


def check_xscope() -> None:
    fails = 0
    for path in REGISTRY.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".py", ".sh", ".json"}:
            continue
        scope = scope_of(path)
        rel = path.relative_to(REGISTRY)
        if scope is None or exempt(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for other in ALL_SCOPES:
            if other != scope and f"../{other}/" in text:
                report("FAIL", "XSCOPE", f"{rel}: references ../{other}/")
                fails += 1
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


def main() -> int:
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
               check_secrets, check_skills, check_xscope):
        fn()
    fails = sum(1 for f in results if f.status == "FAIL")
    warns = sum(1 for f in results if f.status == "WARN")
    for f in results:
        print(f"{f.status:<5} {f.check:<8} {f.detail}")
    print(f"\n{fails} FAIL, {warns} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
