"""Docs ported from the orchestrator repo must not silently reference missing files.

Every backticked or linked repository path in these docs must exist in the
pack (directly or as a Copier `.jinja` source) or be listed in the doc's own
"Paths outside this pack" section.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("docs/ai-runtime-policy.md", "docs/model-plane-boundary.md")
PATH_RE = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|md|ya?ml|json|sh|ts|toml))`|\]\(([A-Za-z0-9_./-]+)\)")


def _exists(doc: Path, ref: str) -> bool:
    ref = ref.lstrip("./") if ref.startswith("./") else ref
    candidates = [ROOT / ref, ROOT / "docs" / ref, doc.parent / ref, ROOT / ".claude" / ref,
                  ROOT / "scripts" / ref]
    return any(c.exists() or Path(f"{c}.jinja").exists() for c in candidates)


@pytest.mark.parametrize("name", DOCS)
def test_every_path_reference_resolves_or_is_declared_external(name: str) -> None:
    doc = ROOT / name
    text = doc.read_text(encoding="utf-8")
    assert "## Paths outside this pack" in text
    body, external = text.split("## Paths outside this pack", 1)
    declared = set(re.findall(r"`([^`]+)`", external))
    missing = []
    for match in PATH_RE.finditer(body):
        ref = match.group(1) or match.group(2)
        if ref.startswith(("http", "#")) or _exists(doc, ref):
            continue
        if ref in declared or any(d.endswith("/" + ref) for d in declared):
            continue
        missing.append(ref)
    assert not missing, f"{name}: undeclared missing paths {sorted(set(missing))}"
