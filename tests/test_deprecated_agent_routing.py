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


def test_deleted_review_agent_is_not_routable() -> None:
    """The task keyword ``review`` may remain, but it must not be an agent id."""
    for path in ROUTING_COPIES:
        routing = json.loads(path.read_text(encoding="utf-8"))
        assert "review" not in routing["agent_routing"], path
        assert "review" not in routing["dispatch_restricted_agents"], path
        assert "review" in routing["overrides"]["escalate_to_standard"], path


def test_deleted_review_agent_definition_and_dispatches_are_absent() -> None:
    """Every shipped agent surface must direct reviews to the supported agent."""
    for agents_dir in AGENT_DIRS:
        assert not (agents_dir / "review.md").exists(), agents_dir
        generate = (agents_dir / "generate.md").read_text(encoding="utf-8")
        assert "route to `review-internal` agent" in generate, agents_dir
        assert "route to `review` agent" not in generate, agents_dir
