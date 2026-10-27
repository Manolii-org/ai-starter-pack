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
FENCE_RE = re.compile(r"^```([^\n`]*)\n(.*?)^```[ \t]*$", re.M | re.S)
# A fence tagged `text` holds data (e.g. route-table output), not paths.
DATA_FENCES = {"text"}


def fences(body: str):
    """(info string, content) of every fenced code block."""
    return [(m.group(1).strip(), m.group(2)) for m in FENCE_RE.finditer(body)]


def references(body: str):
    """Every repository path in a code span or Markdown link.

    The input space is each whitespace-separated token of every code span (so
    paths inside commands count), every line of a fenced block not tagged as
    data, and every link target. A token is a path when it contains `/` or is
    a bare `name.ext`; API paths and slash commands (leading `/`), URLs,
    placeholders (`<...>`, `$...`) and flags are not.
    """
    fenced = [line for info, content in fences(body) if info not in DATA_FENCES
              for line in content.splitlines()]
    prose = FENCE_RE.sub("", body)
    spans = re.findall(r"`([^`\n]+)`", prose) + re.findall(r"\]\(([^)\s#]+)", prose) + fenced
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
            "[link](docs/d.md#sec) [anchor](#here) `https://example.com/x`\n"
            "```bash\npython3 scripts/e.py  # comment\n```\n"
            "```text\nhaiku -> provider/model\n```\n")
    assert list(references(body)) == [
        "scripts/a.py", "scripts/hooks/", ".cursor/rules/*.mdc", "scripts/b.py",
        "scripts/c.py", "CLAUDE.md", "docs/d.md", "scripts/e.py",
    ]


@pytest.mark.parametrize("name", DOCS)
def test_every_fence_declares_whether_it_holds_paths(name: str) -> None:
    """Codex review, PR #245: fenced commands were not scanned. Every fence is
    now scanned unless tagged as data, so an untagged fence must choose."""
    untagged = [content for info, content in fences((ROOT / name).read_text(encoding="utf-8"))
                if not info]
    assert not untagged, f"{name}: tag each fence (e.g. bash, or text for data): {untagged}"


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
