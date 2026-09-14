"""Detect Project must classify synced empty outcome scaffolds as scaffold.

v1.15.0 compared the file to exactly {"version": 1, "workflows": {}}, so
consumer copies that include pack-sync `_sync_metadata` were "invalid" and
failed Detect Project (shared-ai-skills #298 run 34783226867).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci-reusable.yml"


def _classifier_source() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"adoption=\$\(python3 -c '([^']+)'\)", text)
    assert match, "Detect Project classifier one-liner missing from ci-reusable.yml"
    return match.group(1)


def _classify(tmp_path: Path, payload: dict) -> str:
    config = tmp_path / "config" / "workflow-outcomes.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(
        ["python3", "-c", _classifier_source()],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_pure_empty_registry_is_scaffold(tmp_path):
    assert _classify(tmp_path, {"version": 1, "workflows": {}}) == "scaffold"


def test_synced_empty_registry_with_sync_metadata_is_scaffold(tmp_path):
    assert (
        _classify(
            tmp_path,
            {
                "version": 1,
                "workflows": {},
                "_sync_metadata": {"source": "ai-starter-pack", "version": "1.15.0"},
            },
        )
        == "scaffold"
    )


def test_nonempty_workflows_is_adopted_even_with_sync_metadata(tmp_path):
    assert (
        _classify(
            tmp_path,
            {
                "version": 1,
                "workflows": {".github/workflows/ci.yml": {"outcomes": ["FAILED"]}},
                "_sync_metadata": {"source": "ai-starter-pack"},
            },
        )
        == "adopted"
    )


def test_unknown_top_level_key_is_invalid(tmp_path):
    assert (
        _classify(tmp_path, {"version": 1, "workflows": {}, "extra": True})
        == "invalid"
    )
