"""PR Assessment must skip drafts and not start SAST when classify is skipped."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = ROOT / ".github/workflows/pr-assessment.yml"
REUSABLE = ROOT / ".github/workflows/pr-assessment-reusable.yml"


def _on(spec: dict) -> dict:
    return spec.get("on") or spec.get(True) or {}


@pytest.mark.skipif(not STANDALONE.exists(), reason="pr-assessment.yml not in this checkout")
def test_standalone_listens_for_ready_and_skips_drafts() -> None:
    spec = yaml.safe_load(STANDALONE.read_text(encoding="utf-8"))
    types = set((_on(spec).get("pull_request") or {}).get("types") or [])
    assert "ready_for_review" in types
    classify = spec["jobs"]["classify"].get("if") or ""
    assert "draft != true" in classify


@pytest.mark.skipif(not STANDALONE.exists(), reason="pr-assessment.yml not in this checkout")
def test_standalone_downstream_requires_classify_success() -> None:
    spec = yaml.safe_load(STANDALONE.read_text(encoding="utf-8"))
    for job in ("sast", "specialists", "broad-agents"):
        cond = spec["jobs"][job].get("if") or ""
        assert "needs.classify.result == 'success'" in cond, job


@pytest.mark.skipif(
    not REUSABLE.exists(),
    reason="pr-assessment-reusable.yml is pack-only",
)
def test_reusable_skips_drafts_on_pull_request_only() -> None:
    spec = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    classify = spec["jobs"]["classify"].get("if") or ""
    assert "draft != true" in classify
    assert "github.event_name != 'pull_request'" in classify


@pytest.mark.skipif(
    not REUSABLE.exists(),
    reason="pr-assessment-reusable.yml is pack-only",
)
def test_reusable_downstream_requires_classify_success() -> None:
    spec = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    for job in ("sast", "specialists", "broad-agents"):
        cond = spec["jobs"][job].get("if") or ""
        assert "needs.classify.result == 'success'" in cond, job
