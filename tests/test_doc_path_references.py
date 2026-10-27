"""Docs ported from the orchestrator repo must not silently reference missing files.

Every repository path in these docs must exist in the pack (directly or as a
Copier `.jinja` source) or be listed in the doc's own "Paths outside this pack"
section.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("docs/ai-runtime-policy.md", "docs/model-plane-boundary.md")
FILE_RE = re.compile(r"[A-Za-z0-9_.-]+\.(?:py|md|mdc|ya?ml|json|sh|ts|toml)")
# Line (`x.py:18,60`) and symbol (`x.py::fn`) suffixes are not part of the path.
SUFFIX_RE = re.compile(r"(?:::[A-Za-z_][\w.]*|:[0-9][0-9,-]*)$")


def references(body: str):
    """Every repository path in a code span or Markdown link.

    The input space is each whitespace-separated token of every code span (so
    paths inside commands count) and every link target. A token is a path when
    it contains `/` or is a bare `name.ext`; API paths and slash commands
    (leading `/`), URLs, placeholders (`<...>`, `$...`) and flags are not.
    """
    spans = re.findall(r"`([^`\n]+)`", body) + re.findall(r"\]\(([^)\s#]+)", body)
    for span in spans:
        for token in span.split():
            token = SUFFIX_RE.sub("", token.strip("\"'(),;"))
            if not token or token.startswith(("/", "<", "$", "-", "http")) or "://" in token:
                continue
            if "/" in token or FILE_RE.fullmatch(token):
                yield token


def _exists(doc: Path, ref: str) -> bool:
    ref = ref[2:] if ref.startswith("./") else ref
    candidates = [ROOT / ref, ROOT / "docs" / ref, doc.parent / ref, ROOT / ".claude" / ref,
                  ROOT / "scripts" / ref]
    if "*" in ref:
        return any(any(c.parent.glob(c.name)) for c in candidates)
    return any(c.exists() or Path(f"{c}.jinja").exists() for c in candidates)


def test_extraction_covers_commands_directories_globs_and_suffixes() -> None:
    body = ("`python3 scripts/a.py --repo <owner/name>` `scripts/hooks/` `.cursor/rules/*.mdc` "
            "`scripts/b.py:18,60` `scripts/c.py::fn` `/model/info` `/unfreeze` `CLAUDE.md` "
            "[link](docs/d.md#sec) [anchor](#here) `https://example.com/x`")
    assert list(references(body)) == [
        "scripts/a.py", "scripts/hooks/", ".cursor/rules/*.mdc", "scripts/b.py",
        "scripts/c.py", "CLAUDE.md", "docs/d.md",
    ]


@pytest.mark.parametrize("name", DOCS)
def test_every_path_reference_resolves_or_is_declared_external(name: str) -> None:
    doc = ROOT / name
    text = doc.read_text(encoding="utf-8")
    assert "## Paths outside this pack" in text
    body, external = text.split("## Paths outside this pack", 1)
    declared = set(re.findall(r"`([^`]+)`", external))
    missing = []
    for ref in references(body):
        if _exists(doc, ref):
            continue
        if ref in declared or any(d.endswith("/" + ref) for d in declared):
            continue
        missing.append(ref)
    assert not missing, f"{name}: undeclared missing paths {sorted(set(missing))}"
