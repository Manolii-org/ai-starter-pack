"""When the LiteLLM proxy is down, the classifier must not steer sub-agents to
an Anthropic model in place of the open-weights tiers (ADR-0002 amendment
2026-10-29). It used to print "Substitute: tier-2-agentic → claude-sonnet-4-6,
tier-1-fast → haiku, …" from `proxy_fallback_aliases`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/classify-message.py"


def _run(env_extra: dict[str, str]) -> str:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT), **env_extra}
    result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({"prompt": "implement and test a refactor"}),
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_proxy_down_names_no_substitute_model():
    out = _run({"LITELLM_FALLBACK_MODE": "1"})
    line = next(line for line in out.splitlines() if line.startswith("PROXY DOWN"))
    assert "Substitute" not in line
    assert "→" not in line
    assert "claude-" not in line and "haiku" not in line and "sonnet" not in line


def test_proxy_up_emits_no_proxy_down_line():
    assert "PROXY DOWN" not in _run({"LITELLM_FALLBACK_MODE": "0"})


def test_routing_registry_carries_no_proxy_fallback_aliases():
    routing = json.loads((ROOT / ".claude/model-routing.json").read_text(encoding="utf-8"))
    assert "proxy_fallback_aliases" not in routing
