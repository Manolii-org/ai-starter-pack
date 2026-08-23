"""Continuity guard for agent ids removed from the standalone pack."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIRS = (
    ROOT / ".claude" / "agents",
    ROOT / "plugin" / "manolii-framework" / "agents",
)
ROUTING_COPIES = (
    ROOT / ".claude" / "model-routing.json",
    ROOT / "plugin" / "manolii-framework" / "data" / "model-routing.json",
)


# The source-repository marker. `copier.yml` exists only in the pack itself —
# it is in the template's own `_exclude`, so no rendered instance has one.
IS_PACK_REPO = (ROOT / "copier.yml").is_file()


def _present(paths: "tuple[Path, ...]") -> "list[Path]":
    """Surfaces this repository owns, and must therefore satisfy the guard.

    `plugin/**` is a pack-internal build artifact excluded from rendered
    consumers (copier.yml `_exclude`), but this test module IS shipped, so
    iterating the tuple blindly made every rendered project fail on first run
    with FileNotFoundError.

    Gated on the source-repo marker rather than on each path's existence.
    Existence-based filtering silently adopts a consumer-owned
    `plugin/manolii-framework` — a plugin checkout, or a different installed
    pack version — and enforces THIS pack's routing assertions against it, so
    version skew turns the rendered suite red again. Presence of `copier.yml`
    is what actually means "these plugin sources are ours".

    The empty case is a hard failure, not a silent skip: `.claude/` ships
    everywhere, so nothing present means the surface was renamed or dropped and
    this guard would otherwise pass vacuously.
    """
    owned = paths if IS_PACK_REPO else tuple(p for p in paths if "plugin" not in p.parts)
    found = [p for p in owned if p.exists()]
    assert found, f"no agent-routing surface found among: {[str(p) for p in owned]}"
    return found


def test_deleted_review_agent_is_not_routable() -> None:
    """The task keyword ``review`` may remain, but it must not be an agent id."""
    for path in _present(ROUTING_COPIES):
        routing = json.loads(path.read_text(encoding="utf-8"))
        assert "review" not in routing["agent_routing"], path
        assert "review" not in routing["dispatch_restricted_agents"], path
        assert "review" in routing["overrides"]["escalate_to_standard"], path


def test_deleted_review_agent_definition_and_dispatches_are_absent() -> None:
    """Every shipped agent surface must direct reviews to the supported agent."""
    for agents_dir in _present(AGENT_DIRS):
        assert not (agents_dir / "review.md").exists(), agents_dir
        generate = (agents_dir / "generate.md").read_text(encoding="utf-8")
        assert "route to `review-internal` agent" in generate, agents_dir
        assert "route to `review` agent" not in generate, agents_dir


def test_persistent_instructions_do_not_dispatch_deleted_review_agent() -> None:
    instructions = (ROOT / ".claude" / "persistent-instructions.md").read_text(encoding="utf-8")
    assert "review.md" not in instructions
    assert "review-internal.md" in instructions
    assert "security-deep-dive.md" in instructions
    assert "restricted" in instructions and "no-AI" in instructions
