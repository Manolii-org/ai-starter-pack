"""Every remote `uses:` in the pack's workflows and actions is SHA-pinned.

Tag refs are mutable: a moved upstream tag silently changes the code that runs
with the caller's secrets and write token. Only local `./` paths are exempt;
docs code samples are never parsed (this reads YAML structure, not text).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"@[0-9a-f]{40}$")
DOCKER_DIGEST = re.compile(r"^docker://[^@]+@sha256:[0-9a-f]{64}$")


def _files() -> list[Path]:
    files = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
    for base in (ROOT / ".github/actions", ROOT / "actions"):
        files += sorted(base.rglob("action.y*ml"))
    return files


def _uses(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                yield value
            else:
                yield from _uses(value)
    elif isinstance(node, list):
        for item in node:
            yield from _uses(item)


def unpinned(path: Path) -> list[str]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    bad = []
    for ref in _uses(spec):
        ref = ref.strip()
        if ref.startswith("./"):
            continue
        if ref.startswith("docker://"):
            if not DOCKER_DIGEST.match(ref):
                bad.append(ref)
        elif not SHA.search(ref):
            bad.append(ref)
    return bad


def test_scan_covers_workflows_and_actions() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in _files()}
    assert ".github/workflows/migration-run-gate-reusable.yml" in names
    assert any(n.startswith(".github/actions/") for n in names)
    assert any(n.startswith("actions/") for n in names)


def test_every_remote_uses_is_sha_pinned() -> None:
    failures = {p.relative_to(ROOT).as_posix(): bad for p in _files() if (bad := unpinned(p))}
    assert not failures, (
        "pin these to a full 40-hex commit SHA with a `# vX.Y.Z` comment "
        f"(local ./ paths are exempt): {failures}"
    )


def test_lint_rejects_tags_and_accepts_shas_and_local(tmp_path: Path) -> None:
    wf = tmp_path / "wf.yml"
    wf.write_text(
        "jobs:\n"
        "  a:\n"
        "    uses: org/repo/.github/workflows/x.yml@v1\n"
        "  b:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n"
        "      - uses: ./.github/actions/local\n"
        "      - uses: docker://alpine:3\n",
        encoding="utf-8",
    )
    assert unpinned(wf) == [
        "org/repo/.github/workflows/x.yml@v1",
        "actions/checkout@v4",
        "docker://alpine:3",
    ]
