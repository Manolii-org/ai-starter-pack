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

Cite the `.jinja`, not the 4-line `README-STARTER-PACK.md` beside it. The
basename means two different things depending on where you are standing, which
is what makes it easy to cite wrongly:

  * in THIS repo, `README-STARTER-PACK.md` is a 4-line License stub, and
    `README-STARTER-PACK.md.jinja` is the template (real Jinja — line 3 holds
    `{{ATTRIBUTION_LINE}}`);
  * in a consumer, the `.jinja` is absent and `README-STARTER-PACK.md` is what
    Copier rendered FROM it.

So the template is the authoritative source for the README a consumer receives
— not a file they get — and it is the only place the quoted text exists.

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

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests import test_broad_agents_complete_input as broad
from tests import test_protected_review as protected
from tests.test_broad_agents_complete_input import deep_output

pytest_plugins = ["tests.test_broad_agents_complete_input"]

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


def test_review_evidence_and_companion_ship_without_drift():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for prefix in ("plugin/manolii-framework", "registry/platform/framework"):
        assert (root / prefix / "scripts/ci/review_evidence.py").read_bytes() == (root / "scripts/ci/review_evidence.py").read_bytes()
        assert (root / prefix / "skills/autonomy-follow-through/review-evidence.md").read_bytes() == (root / ".claude/skills/autonomy-follow-through/review-evidence.md").read_bytes()
        assert (root / prefix / "skills/autonomy-follow-through/SKILL.md").read_bytes() == (root / ".claude/skills/autonomy-follow-through/SKILL.md").read_bytes()


def test_generated_policy_and_entry_points_match_canonical():
    for prefix in ['plugin/manolii-framework', 'registry/platform/framework']:
        for path in ['scripts/lib/agent_capabilities.py', 'scripts/run-pr-classifier.py',
                     'scripts/run-specialists.py', 'scripts/run-judge.py', 'scripts/run-broad-agents.py']:
            assert (ROOT / prefix / path).read_bytes() == (ROOT / path).read_bytes()


@pytest.mark.parametrize('directory', ['plugin/manolii-framework', 'registry/platform/framework'])
def test_deep_generated_runtime_has_real_builder_parser_and_guard_closure(runner, deep_repo, directory):
    spec = importlib.util.spec_from_file_location('distributed_deep_' + directory.replace('/', '_'),
                                                  ROOT / directory / 'scripts/run-broad-agents.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    packet = module.build_security_deep_dive_input(deep_repo[0], **deep_repo[2])
    parsed = module.parse_security_deep_dive_output(json.dumps(deep_output(packet)), packet, stop_reason='end_turn')
    assert parsed['status'] == 'provisional_complete'
    assert parsed['provider_execution'] == 'unknown'
    assert len(parsed['promoted']) == len(parsed['dismissed']) == 1



@pytest.mark.parametrize('directory', ['plugin/manolii-framework', 'registry/platform/framework'])
@pytest.mark.parametrize('change', ['modify', 'add', 'delete', 'quoted', 'unicode', 'raw_unicode',
                                   'empty_add', 'mode_only', 'rename'])
def test_generated_retained_diff_sources(runner, deep_repo, monkeypatch, directory, change):
    broad.test_deep_retained_diff_requires_nontriggering_old_and_new_sources(
        runner, deep_repo, monkeypatch, directory, change)


@pytest.mark.parametrize('directory', ['plugin/manolii-framework', 'registry/platform/framework'])
@pytest.mark.parametrize('mutation', ['fabricated_head', 'missing_base', 'missing_head', 'false_absent',
                                    'mode', 'both_absent'])
def test_generated_unchanged_source_pairs(runner, deep_repo, monkeypatch, directory, mutation):
    broad.test_deep_unchanged_evidence_requires_identical_source_pairs(
        runner, deep_repo, monkeypatch, directory, mutation)


@pytest.mark.parametrize('layout', ['plugin/manolii-framework/', 'registry/platform/framework/'])
@pytest.mark.parametrize('identity', [None, 'original-id'])
def test_generated_protected_identity(monkeypatch, tmp_path, layout, identity):
    protected.estate.__wrapped__(monkeypatch)
    protected.deny_unmocked_network.__wrapped__(monkeypatch)
    protected.test_protected_identity_is_stable_in_all_shipped_judges(
        monkeypatch, tmp_path, layout, identity)


def test_rendered_review_contracts_run_without_generated_artifacts(tmp_path):
    """Consumers run canonical contracts without pack-only artifact dependencies."""
    from tests.test_copier_render import render

    default_render = tmp_path / "consumer"
    render(default_render)
    for path in ("plugin", "registry", "tests/test_pack_plugin_parity.py"):
        assert not (default_render / path).exists(), path
    modules = ["tests/test_review_route_admission.py",
               "tests/test_broad_agents_complete_input.py", "tests/test_protected_review.py"]
    for path in modules:
        assert (default_render / path).is_file(), path
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *modules],
        cwd=default_render, capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-2000:]
    assert "skipped" not in result.stdout
