"""Tests for scripts/lib/agent_capabilities.py — contract fail-closed."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
import agent_capabilities as ac


def _manifest(tmp_path, data):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_resolve_minimal_manifest(tmp_path):
    m = _manifest(tmp_path, {"version": 1, "capabilities": ["mcp-knowledge", "llm-proxy"]})
    out = ac.resolve(m)
    assert sorted(out) == ["LLM_API_KEY", "MCP_API_KEY"]
    assert out["MCP_API_KEY"]["capability"] == "mcp-knowledge"
    assert out["MCP_API_KEY"]["retrieval"] == "eager"


def test_broker_capability_yields_no_env(tmp_path):
    m = _manifest(tmp_path, {"capabilities": ["github-actions-dispatch", "git-read"]})
    assert ac.resolve(m) == {}


def test_unknown_capability_fails_closed(tmp_path):
    m = _manifest(tmp_path, {"capabilities": ["doppler-read", "root-everything"]})
    with pytest.raises(ValueError, match="unknown capability"):
        ac.resolve(m)


def test_legacy_mode_is_union(tmp_path):
    m = _manifest(tmp_path, {"mode": "legacy"})
    out = ac.resolve(m)
    all_names = {n for spec in ac.CANONICAL_TABLE.values() for n in spec[0]}
    assert set(out) == all_names


def test_missing_capabilities_key_fails(tmp_path):
    m = _manifest(tmp_path, {"version": 1})
    with pytest.raises((TypeError, ValueError), match="capabilities"):
        ac.resolve(m)


def test_malformed_manifest_fails(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ValueError, match="malformed"):
        ac.resolve(str(p))


def test_retrieval_override(tmp_path):
    m = _manifest(tmp_path, {
        "capabilities": ["deploy-vercel"],
        "retrieval_overrides": {"VERCEL_TOKEN": "eager"},
    })
    assert ac.resolve(m)["VERCEL_TOKEN"]["retrieval"] == "eager"


def test_extension_table(tmp_path):
    t = tmp_path / "table.json"
    t.write_text(json.dumps({"capabilities": {
        "repo-specific": {"secret_names": ["MY_REPO_TOKEN"], "source": "doppler",
                          "retrieval": "deferred", "privilege": "managed"},
    }}))
    m = _manifest(tmp_path, {"capabilities": ["repo-specific"]})
    out = ac.resolve(m, str(t))
    assert out["MY_REPO_TOKEN"]["privilege"] == "managed"


def test_extension_table_missing_source_fails(tmp_path):
    t = tmp_path / "table.json"
    t.write_text(json.dumps({"capabilities": {"bad": {"secret_names": ["X"]}}}))
    m = _manifest(tmp_path, {"capabilities": ["bad"]})
    with pytest.raises(ValueError, match="'source'"):
        ac.resolve(m, str(t))


def test_example_manifest_resolves():
    example = Path(__file__).resolve().parents[1] / "config" / "agent-capabilities.example.json"
    out = ac.resolve(str(example))
    assert "DOPPLER_TOKEN_PRD" in out and "MCP_API_KEY" in out
    assert "GH_TOKEN" in out  # github-actions-read
    assert "VERCEL_TOKEN" not in out
