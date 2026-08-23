"""Continuity guard for agent ids removed from the standalone pack.

Scoped to the `.claude/` surface, which every rendered instance carries. The
pack's own `plugin/manolii-framework/` copies are asserted in
tests/test_pack_plugin_parity.py, which copier.yml `_exclude`s — see that
module for why runtime pack-vs-consumer detection was abandoned.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".claude" / "agents"
ROUTING_JSON = ROOT / ".claude" / "model-routing.json"


def test_deleted_review_agent_is_not_routable() -> None:
    """The task keyword ``review`` may remain, but it must not be an agent id."""
    assert ROUTING_JSON.is_file(), f"routing surface missing: {ROUTING_JSON}"

    routing = json.loads(ROUTING_JSON.read_text(encoding="utf-8"))
    assert "review" not in routing["agent_routing"], ROUTING_JSON
    assert "review" not in routing["dispatch_restricted_agents"], ROUTING_JSON
    assert "review" in routing["overrides"]["escalate_to_standard"], ROUTING_JSON


def test_deleted_review_agent_definition_and_dispatches_are_absent() -> None:
    """Every shipped agent surface must direct reviews to the supported agent."""
    assert AGENTS_DIR.is_dir(), f"agent surface missing: {AGENTS_DIR}"
    assert not (AGENTS_DIR / "review.md").exists(), AGENTS_DIR

    generate = (AGENTS_DIR / "generate.md").read_text(encoding="utf-8")
    assert "route to `review-internal` agent" in generate, AGENTS_DIR
    assert "route to `review` agent" not in generate, AGENTS_DIR


def test_persistent_instructions_do_not_dispatch_deleted_review_agent() -> None:
    instructions = (ROOT / ".claude" / "persistent-instructions.md").read_text(encoding="utf-8")
    assert "review.md" not in instructions
    assert "review-internal.md" in instructions
    assert "security-deep-dive.md" in instructions
    assert "restricted" in instructions and "no-AI" in instructions
