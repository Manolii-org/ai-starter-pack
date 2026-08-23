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


PLUGIN_ROOT = ROOT / "plugin"

# Source-pack identity. NO single filename works — three were tried and each
# matched a real consumer shape:
#   - `copier.yml` present: a destination that is itself a Copier template has
#     its own; `_exclude` only stops OURS from being copied.
#   - `.copier-answers.yml` absent: the prebuilt release zip ships without one
#     by design (README-STARTER-PACK.md, "Copier updates for zip-based installs").
#   - `pack.manifest.yml` present: rendered consumers are told by their OWN
#     shipped README (README-STARTER-PACK.md §"Scope & What's Not Included")
#     that feature flags live in a file of exactly that name.
#
# So require all three signals together. A consumer would have to be a Copier
# template, AND have shed its answers file, AND carry a pack.manifest.yml before
# being mistaken for the source pack. Each condition is individually plausible;
# the conjunction is not.
#
# Getting this wrong is not symmetric. A false NEGATIVE only skips a pack-only
# guard here; a false POSITIVE enters the fail-closed branch below and fails a
# CONSUMER's shipped suite — which is the bug this whole change exists to fix.
IS_PACK_REPO = (
    (ROOT / "copier.yml").is_file()
    and not (ROOT / ".copier-answers.yml").is_file()
    and (ROOT / "pack.manifest.yml").is_file()
)


def _present(paths: "tuple[Path, ...]") -> "list[Path]":
    """Surfaces this repository owns, and must therefore satisfy the guard.

    `plugin/**` is a pack-internal build artifact excluded from rendered
    consumers (copier.yml `_exclude`), but this test module IS shipped, so
    iterating the tuple blindly made every rendered project fail on first run
    with FileNotFoundError.

    Gated on the rendered-instance marker rather than on each path's existence.
    Existence-based filtering silently adopts a consumer-owned
    `plugin/manolii-framework` — a plugin checkout, or a different installed
    pack version — and enforces THIS pack's routing assertions against it, so
    version skew turns the rendered suite red again.

    The plugin paths are matched against `ROOT / "plugin"` exactly, via
    `Path.parents`. Testing `"plugin" in p.parts` looked equivalent and was not:
    these are ABSOLUTE paths, so a checkout that merely lives under a directory
    named `plugin` (`/workspace/plugin/my-project`) matched every candidate —
    including the consumer's own `.claude/` ones — emptying the tuple and
    failing the assertion below purely because of where the repo was cloned.

    In the source pack the check is fail-CLOSED on every owned path: we commit
    both surfaces, so a deleted or renamed one is a regression, not something to
    skip. Filtering by existence there would have let a dropped plugin surface
    pass silently as long as the `.claude` one survived — which is the opposite
    of this helper's purpose. Only a consumer filters, and only the
    pack-excluded plugin paths.
    """
    if IS_PACK_REPO:
        missing = [p for p in paths if not p.exists()]
        assert not missing, (
            f"owned agent-routing surface missing from the pack: "
            f"{[str(p) for p in missing]} — deleted or renamed?"
        )
        return list(paths)

    consumer_owned = [p for p in paths if PLUGIN_ROOT not in p.parents]
    found = [p for p in consumer_owned if p.exists()]
    assert found, (
        f"no agent-routing surface found among: {[str(p) for p in consumer_owned]}"
    )
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
