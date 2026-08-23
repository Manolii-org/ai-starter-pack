"""Pack-only guards over `plugin/manolii-framework/` — never shipped.

`plugin/**` is a build artifact that copier.yml `_exclude`s from every rendered
instance, so assertions about it can only hold in this repository. This module
is excluded too, which is what makes those assertions safe to state
unconditionally.

It exists because the alternative did not work. Keeping these checks in the
shipped test modules meant guessing at runtime whether the checkout was the
source pack or a consumer, and every filename-based signal tried was defeated by
a legitimate consumer shape:

  * `copier.yml` present      — a destination that is itself a Copier template
                                has its own; `_exclude` only stops OURS being
                                copied.
  * `.copier-answers.yml` absent — the prebuilt release zip ships without one by
                                design: README-STARTER-PACK.md.jinja:353
                                ("Copier updates for zip-based installs"),
                                :355 "The prebuilt release zip intentionally
                                ships **without** `.copier-answers.yml`".
  * `pack.manifest.yml` present — rendered consumers are told by their own
                                shipped README that feature flags live in a file
                                of exactly that name
                                (README-STARTER-PACK.md.jinja:30).
  * all three together        — a Copier-template repo that adopted the pack via
                                the zip flow has precisely that shape.

Cite the `.jinja`, not the 4-line `README-STARTER-PACK.md` beside it: the
template is what a consumer actually receives as their README, and it is the
only place this text exists.

A false positive there is not cosmetic: it drops a CONSUMER into the pack-only
branch and fails their suite over `plugin/` paths they were never meant to have.
Excluding the file removes the question instead of answering it — the assertions
are simply absent from a consumer's checkout, so no marker can misfire.

The shipped modules keep every consumer-relevant assertion (`tests/
test_deprecated_agent_routing.py` for the `.claude/` routing surface, `tests/
test_run_judge_review_lookup.py` for the pagination and author-authentication
regressions on the `scripts/run-judge.py` they actually run).
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugin" / "manolii-framework"


def test_plugin_routing_copy_does_not_route_the_deleted_review_agent() -> None:
    """The task keyword ``review`` may remain, but it must not be an agent id."""
    path = PLUGIN_ROOT / "data" / "model-routing.json"
    assert path.is_file(), f"pack-owned routing surface missing: {path}"

    routing = json.loads(path.read_text(encoding="utf-8"))
    assert "review" not in routing["agent_routing"], path
    assert "review" not in routing["dispatch_restricted_agents"], path
    assert "review" in routing["overrides"]["escalate_to_standard"], path


def test_plugin_agents_dispatch_reviews_to_the_supported_agent() -> None:
    """The plugin agent surface must direct reviews to the supported agent."""
    agents_dir = PLUGIN_ROOT / "agents"
    assert agents_dir.is_dir(), f"pack-owned agent surface missing: {agents_dir}"
    assert not (agents_dir / "review.md").exists(), agents_dir

    generate = (agents_dir / "generate.md").read_text(encoding="utf-8")
    assert "route to `review-internal` agent" in generate, agents_dir
    assert "route to `review` agent" not in generate, agents_dir


def test_plugin_judge_copy_is_in_sync() -> None:
    """The pack ships two copies of run-judge.py; fixing one only is a silent regression.

    Fail-closed on absence: both copies are committed here, so a deleted or
    renamed one is the regression this guard exists to catch — not a reason to
    skip.
    """
    plugin_copy = PLUGIN_ROOT / "scripts" / "run-judge.py"
    assert plugin_copy.is_file(), f"pack-owned judge copy missing: {plugin_copy}"

    assert (ROOT / "scripts" / "run-judge.py").read_text() == plugin_copy.read_text(), (
        "scripts/run-judge.py and the plugin copy have drifted"
    )
