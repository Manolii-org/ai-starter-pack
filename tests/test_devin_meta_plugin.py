"""The Devin meta-plugin baseline must install without a Knowledge Layer.

Devin auto-installs `requiredPlugins` recursively and fails the whole install
when one is blocked; `optionalPlugins` are endorsed but never auto-installed.
`registry/platform/om` needs a KL backend, so it must stay optional.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / ".devin-plugin/plugin.json").read_text(encoding="utf-8"))


def _paths(key: str) -> list[str]:
    return [entry["path"] for entry in MANIFEST.get(key, [])]


def test_baseline_requires_only_kl_independent_plugins() -> None:
    assert _paths("requiredPlugins") == ["registry/platform/framework"]


def test_om_is_endorsed_but_not_required() -> None:
    assert "registry/platform/om" in _paths("optionalPlugins")
    assert "registry/platform/om" not in _paths("requiredPlugins")


def test_every_listed_plugin_exists() -> None:
    for key in ("requiredPlugins", "optionalPlugins"):
        for entry in MANIFEST[key]:
            assert entry["source"] == "git-subdir"
            assert (ROOT / entry["path"] / ".devin-plugin/plugin.json").is_file(), entry
