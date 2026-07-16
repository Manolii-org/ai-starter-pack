#!/usr/bin/env python3
"""
Pack Drift Checker — validates AI Starter Pack self-consistency.

Scans for:
- A) ORG-LEAK: hard org identifiers (manolii, impaktful, knowledge-layer, kl_, etc.)
- B) PROVIDER-NAMES: third-party provider names (doppler, supabase, neon) — warn only
- C) README-COUNTS: agent file count in README vs actual .claude/agents/*.md
- D) FEATURE-EXCLUDES: verify paths listed in pack.manifest.yml exist
- E) AGENT-MODEL: check for opus (cost guard) and missing fields in agent frontmatter

Exit 0 if all checks PASS, 1 if any FAIL. WARN = pass but signal issue.
Run from pack root: python3 scripts/pack-drift-check.py
"""
from __future__ import annotations

import os
import re
import sys
try:
    import yaml  # PyYAML — optional; manifest/agent checks degrade to WARN without it
except Exception:  # pragma: no cover
    yaml = None
from pathlib import Path
from typing import NamedTuple


class CheckResult(NamedTuple):
    status: str  # PASS, WARN, FAIL
    check_name: str
    detail: str


# Directories never scanned or traversed: VCS, build/dist artifacts, vendored
# deps, virtualenvs, linter/test caches, and the .brand overlay. Single source
# for should_scan_file and check_feature_excludes — keep copier.yml _exclude
# in sync when adding cache/vendor entries.
SKIP_DIRS = frozenset({
    '.git', 'releases', 'dist', 'build', '.next', 'node_modules',
    '__pycache__', '.brand', '.venv', 'venv', '.ruff_cache', '.pytest_cache',
})


def find_pack_root() -> Path:
    """Resolve pack root as parent of scripts/ dir containing this file."""
    script_path = Path(__file__).resolve()
    return script_path.parent.parent


def should_scan_file(path: Path, pack_root: Path) -> bool:
    """Return True if a file should be scanned for leaks.

    Skips VCS / build / vendor dirs and the ``.brand/`` overlay (which intentionally
    holds brand values for the maintainer's branded render). IMPORTANT: it DOES scan
    ``.claude/`` and ``.ai/`` — the bulk of the pack (agents, skills, hooks, security,
    decisions) — which an earlier "skip any dot-prefixed dir" rule wrongly excluded.
    """
    try:
        rel_parts = path.relative_to(pack_root).parts
    except ValueError:
        return False
    if any(part in SKIP_DIRS for part in rel_parts):
        return False
    return path.is_file() and path.suffix.lower() in {'.md', '.py', '.sh', '.json', '.yml', '.yaml'}


# Leak detectors legitimately contain the marker list they search for — scanning
# them would always self-FAIL. Skip them in the org-leak / provider scans.
# copier.yml and tests/test_copier_render.py are template-internal (excluded
# from rendered output via copier.yml _exclude) and legitimately carry brand
# strings (branded-mode answer defaults / test assertions).
DETECTOR_FILES = {
    'scripts/pack-drift-check.py',
    'scripts/render-pack.py',
    'copier.yml',
    'tests/test_copier_render.py',
}


def template_source_variants(pack_root: Path, path_glob: str, feature: str) -> list[Path]:
    """Rendered-output path → candidate Copier template-source paths.

    Since the Copier migration the template source may store a shipped file
    under a .jinja suffix and/or a feature-conditional filename
    ('{% if <feature> %}name{% endif %}').  Full re-point of this check to
    copier.yml conditionals lands with the pack-test retarget (report §5 row 2).
    """
    p = Path(path_glob)
    cond = p.parent / ('{% if ' + feature + ' %}' + p.name + '{% endif %}')
    return [
        pack_root / p,
        pack_root / (str(p) + '.jinja'),
        pack_root / cond,
        pack_root / (str(cond) + '.jinja'),
    ]


def scan_org_leak(pack_root: Path) -> list[CheckResult]:
    """Check A: scan for organisation identifiers.

    FAIL on unambiguous org names / repo slugs that have no generic use. WARN on
    the opt-in ``kl_`` prefix (knowledge-layer tools are an optional remote-memory
    feature, so references are expected when that feature is documented). The
    generic word "mesh" is intentionally NOT flagged ("service mesh" is common).
    """
    results: list[CheckResult] = []
    fail_pattern = re.compile(
        r'\bmanolii\b|\bimpaktful\b|\bknowledge-layer\b|\bpicklebugs\b'
        r'|\blead-converter\b|\bhiha\b',
        re.IGNORECASE,
    )
    warn_pattern = re.compile(r'\bkl_', re.IGNORECASE)
    kl_files: set[str] = set()

    for path in pack_root.rglob('*'):
        if not should_scan_file(path, pack_root):
            continue
        rel = path.relative_to(pack_root).as_posix()
        if rel in DETECTOR_FILES:
            continue
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, 1):
                    for match in fail_pattern.finditer(line):
                        results.append(CheckResult(
                            status='FAIL',
                            check_name='ORG-LEAK',
                            detail=f'{rel}:{i} — {match.group().strip()}'
                        ))
                    if warn_pattern.search(line):
                        kl_files.add(rel)
        except (OSError, UnicodeError):
            # Unreadable / binary file — skip it; this is a best-effort scan.
            continue

    if kl_files:
        results.append(CheckResult(
            status='WARN',
            check_name='ORG-LEAK',
            detail=f'kl_ prefix (opt-in remote-memory feature) referenced in: {sorted(kl_files)}'
        ))
    if not any(r.status == 'FAIL' for r in results):
        results.append(CheckResult('PASS', 'ORG-LEAK', 'no hardcoded organisation names'))
    return results


def scan_provider_names(pack_root: Path) -> list[CheckResult]:
    """Check B: scan for provider names (WARN only, except token-shapes.json)."""
    results = []
    provider_pattern = re.compile(r'\b(doppler|supabase|neon)\b', re.IGNORECASE)
    exclude_path = '.ai/security/token-shapes.json'

    for path in pack_root.rglob('*'):
        if not should_scan_file(path, pack_root):
            continue
        rel = path.relative_to(pack_root).as_posix()
        if rel in DETECTOR_FILES or exclude_path in rel:
            continue  # Skip detector files and expected provider references
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                if provider_pattern.search(content):
                    results.append(CheckResult(
                        status='WARN',
                        check_name='PROVIDER-NAMES',
                        detail=f'{path.relative_to(pack_root)}'
                    ))
        except (OSError, UnicodeError):
            # Unreadable / binary file — skip it; this is a best-effort scan.
            continue

    # Deduplicate file-level warnings
    warned = sorted({r.detail for r in results})
    if warned:
        return [CheckResult('WARN', 'PROVIDER-NAMES', ', '.join(warned))]
    return [CheckResult('PASS', 'PROVIDER-NAMES', 'no provider names found')]


def check_readme_counts(pack_root: Path) -> list[CheckResult]:
    """Check C: verify README agent count."""
    readme_path = pack_root / 'README-STARTER-PACK.md'
    if not readme_path.exists():
        # Copier template source stores the README under a .jinja suffix
        readme_path = pack_root / 'README-STARTER-PACK.md.jinja'
    if not readme_path.exists():
        return [CheckResult('WARN', 'README-COUNTS', 'README-STARTER-PACK.md not found')]

    with open(readme_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    # Look for "(N files)" near "Agents"
    agents_section = re.search(r'\*\*Agents\*\*.*?\((\d+) files\)', content)
    if not agents_section:
        return [CheckResult('WARN', 'README-COUNTS', 'Could not find "(N files)" pattern near Agents')]

    readme_count = int(agents_section.group(1))
    agents_dir = pack_root / '.claude' / 'agents'
    # '.md' in name (not glob '*.md') so Copier conditional filenames like
    # '{% if codex_adversarial %}codex-adversarial.md{% endif %}' still count
    actual_count = (len([f for f in agents_dir.iterdir() if f.is_file() and '.md' in f.name])
                    if agents_dir.is_dir() else 0)

    if readme_count != actual_count:
        return [CheckResult(
            status='WARN',
            check_name='README-COUNTS',
            detail=f'README says {readme_count}, actual {actual_count}'
        )]
    return [CheckResult('PASS', 'README-COUNTS', '')]


# Whitespace-tolerant: Jinja accepts {%if x%} / {% if  x %} variants in names
CONDITIONAL_NAME_RE = re.compile(r'^\{%\s*if\s+(\w+)\s*%\}(.+)\{%\s*endif\s*%\}(\.jinja)?$')


def copier_bool_flags(pack_root: Path) -> set[str] | None:
    """Bool question names from copier.yml (None if unreadable/absent)."""
    copier_path = pack_root / 'copier.yml'
    if not copier_path.exists() or yaml is None:
        return None
    try:
        with open(copier_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except Exception:
        return None
    if not isinstance(config, dict):
        # Empty or malformed copier.yml parses to None/non-dict
        return None
    return {
        name for name, spec in config.items()
        if isinstance(name, str) and not name.startswith('_')
        and isinstance(spec, dict) and spec.get('type') == 'bool'
    }


def check_feature_excludes(pack_root: Path) -> list[CheckResult]:
    """Check D: validate feature-gated files against copier.yml conditionals.

    Since the Copier migration, the source of truth for feature exclusion is
    the conditional filename ('{% if <flag> %}name{% endif %}') paired with a
    bool question in copier.yml — not pack.manifest.yml feature_excludes
    (legacy, cross-checked as WARN until its Phase-D retirement).
    """
    if yaml is None:
        return [CheckResult('WARN', 'FEATURE-EXCLUDES', 'PyYAML not installed — skipped')]

    flags = copier_bool_flags(pack_root)
    if flags is None:
        return [CheckResult('WARN', 'FEATURE-EXCLUDES', 'copier.yml not found/parseable — skipped')]

    results = []
    skip_dirs = SKIP_DIRS

    # 1) Every conditional filename must reference a declared bool flag.
    # os.walk with in-place pruning: rglob would still traverse skipped trees
    # (node_modules/.git in rendered instances), which is needlessly slow.
    conditional_count = 0
    for walk_root, walk_dirs, walk_files in os.walk(pack_root):
        walk_dirs[:] = [d for d in walk_dirs if d not in skip_dirs]
        for name in walk_dirs + walk_files:
            path = Path(walk_root) / name
            if '{%' not in name:
                continue
            m = CONDITIONAL_NAME_RE.match(name)
            rel = path.relative_to(pack_root)
            if not m:
                results.append(CheckResult(
                    status='FAIL',
                    check_name='FEATURE-EXCLUDES',
                    detail=f'{rel} — malformed conditional filename'
                ))
                continue
            conditional_count += 1
            flag = m.group(1)
            if flag not in flags:
                results.append(CheckResult(
                    status='FAIL',
                    check_name='FEATURE-EXCLUDES',
                    detail=f'{rel} — gated by unknown copier.yml flag "{flag}"'
                ))

    if conditional_count == 0:
        results.append(CheckResult(
            status='WARN',
            check_name='FEATURE-EXCLUDES',
            detail='no conditional-named files found — gating may have been flattened'
        ))

    # 2) Legacy cross-check: pack.manifest.yml feature_excludes entries should
    #    still resolve to a template-source file (WARN — manifest is legacy).
    manifest_path = pack_root / 'pack.manifest.yml'
    if manifest_path.exists():
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = yaml.safe_load(f)
            if not isinstance(manifest, dict):
                manifest = {}
            for feature, paths in (manifest.get('feature_excludes') or {}).items():
                if not isinstance(paths, list):
                    continue
                for path_glob in paths:
                    if not any(t.exists() for t in
                               template_source_variants(pack_root, path_glob, feature)) \
                       and not list(pack_root.glob(path_glob)):
                        results.append(CheckResult(
                            status='WARN',
                            check_name='FEATURE-EXCLUDES',
                            detail=f'{path_glob} (legacy manifest, feature: {feature}) — no template-source file'
                        ))
        except Exception as e:
            results.append(CheckResult('WARN', 'FEATURE-EXCLUDES', f'Error parsing legacy manifest: {e}'))

    if not any(r.status == 'FAIL' for r in results):
        results.append(CheckResult('PASS', 'FEATURE-EXCLUDES',
                                   f'{conditional_count} conditional file(s) validated against copier.yml'))
    return results


def check_agent_models(pack_root: Path) -> list[CheckResult]:
    """Check E: scan agent frontmatter for opus and missing fields."""
    results = []
    agents_dir = pack_root / '.claude' / 'agents'

    if not agents_dir.exists():
        return [CheckResult('WARN', 'AGENT-MODEL', 'agents/ dir not found')]
    if yaml is None:
        return [CheckResult('WARN', 'AGENT-MODEL', 'PyYAML not installed — skipped')]

    # '.md' in name (not glob '*.md') so Copier conditional filenames are scanned
    for agent_file in sorted(f for f in agents_dir.iterdir() if f.is_file() and '.md' in f.name):
        with open(agent_file, 'r') as f:
            content = f.read()

        # Extract YAML frontmatter
        match = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
        if not match:
            results.append(CheckResult(
                status='WARN',
                check_name='AGENT-MODEL',
                detail=f'{agent_file.name} — no YAML frontmatter'
            ))
            continue

        try:
            frontmatter = yaml.safe_load(match.group(1))
        except Exception as e:
            results.append(CheckResult(
                status='WARN',
                check_name='AGENT-MODEL',
                detail=f'{agent_file.name} — YAML parse error: {e}'
            ))
            continue

        if not isinstance(frontmatter, dict):
            results.append(CheckResult('WARN', 'AGENT-MODEL', f'{agent_file.name} — YAML frontmatter is not a mapping'))
            continue

        # Check for opus
        model = str(frontmatter.get('model') or '')
        if 'opus' in model.lower():
            results.append(CheckResult(
                status='FAIL',
                check_name='AGENT-MODEL',
                detail=f'{agent_file.name} — model is {model} (use sonnet or haiku)'
            ))

        # Check for missing name / description
        if not frontmatter.get('name'):
            results.append(CheckResult(
                status='WARN',
                check_name='AGENT-MODEL',
                detail=f'{agent_file.name} — missing name field'
            ))
        if not frontmatter.get('description'):
            results.append(CheckResult(
                status='WARN',
                check_name='AGENT-MODEL',
                detail=f'{agent_file.name} — missing description field'
            ))

    if not results:
        results.append(CheckResult('PASS', 'AGENT-MODEL', ''))

    return results


def main() -> int:
    """Run all checks and report results."""
    pack_root = find_pack_root()
    print(f"Scanning pack root: {pack_root}")
    print()

    all_results: list[CheckResult] = []

    # Run all checks
    all_results.extend(scan_org_leak(pack_root))
    all_results.extend(scan_provider_names(pack_root))
    all_results.extend(check_readme_counts(pack_root))
    all_results.extend(check_feature_excludes(pack_root))
    all_results.extend(check_agent_models(pack_root))

    # Print results grouped by status
    for status in ['FAIL', 'WARN', 'PASS']:
        matching = [r for r in all_results if r.status == status]
        for result in matching:
            if result.detail:
                print(f'[{result.status}] {result.check_name} — {result.detail}')
            else:
                print(f'[{result.status}] {result.check_name}')

    # Summary
    print()
    fail_count = sum(1 for r in all_results if r.status == 'FAIL')
    warn_count = sum(1 for r in all_results if r.status == 'WARN')
    pass_count = sum(1 for r in all_results if r.status == 'PASS')
    print(f'Summary: {pass_count} PASS, {warn_count} WARN, {fail_count} FAIL')

    return 1 if fail_count > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
