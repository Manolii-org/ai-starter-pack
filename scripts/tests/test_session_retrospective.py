"""Hermetic unit tests for scripts/session-retrospective.py

Covers the 4 correctness fixes shipped in the WS3 pack hardening PR:
  1. _kl_url() fails closed when KL_MCP_URL is unset
  2. kl_create_note / kl_assert_fact reject MCP error envelopes on 200
  3. mode_kl_only selects the record whose session_id matches
  4. mode_precompact / mode_stop stage & merge session-scoped artifacts

The module filename contains a hyphen so we importlib-load it once per test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
SESSION_RETRO_PATH = SCRIPTS_DIR / "session-retrospective.py"


def _load_module(project_root: Path):
    """Load session-retrospective.py rooted at project_root."""
    os.environ["CLAUDE_PROJECT_DIR"] = str(project_root)
    for cached in [k for k in list(sys.modules) if k.startswith("session_retro_test_")]:
        del sys.modules[cached]
    spec = importlib.util.spec_from_file_location(
        f"session_retro_test_{id(project_root)}",
        SESSION_RETRO_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".ai" / "memory" / "retrospectives").mkdir(parents=True)
    (tmp_path / ".ai" / "retrospective-staging").mkdir(parents=True)
    (tmp_path / ".ai" / "session-logs").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # Neutralise any ambient KL creds — fixes each test must opt in explicitly.
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("KL_MCP_URL", raising=False)
    monkeypatch.delenv("KL_ENTITY", raising=False)
    monkeypatch.delenv("RETROSPECTIVE_ENTITY", raising=False)
    monkeypatch.delenv("SESSION_RETRO_DRY_RUN", raising=False)
    return tmp_path


def test_kl_url_returns_none_when_unset(project, monkeypatch):
    mod = _load_module(project)
    assert mod._kl_url() is None


def test_kl_url_returns_stripped_when_set(project, monkeypatch):
    monkeypatch.setenv("KL_MCP_URL", "https://example.test/api/mcp/")
    mod = _load_module(project)
    assert mod._kl_url() == "https://example.test/api/mcp"


def test_kl_create_note_skips_when_url_unset(project, monkeypatch, capsys):
    monkeypatch.setenv("MCP_API_KEY", "test-key")
    mod = _load_module(project)
    ok = mod.kl_create_note("acme", "t", "body", ["x"])
    assert ok is False
    err = capsys.readouterr().err
    assert "KL_MCP_URL unset" in err


def test_mcp_envelope_ok_flags_error_field(project):
    mod = _load_module(project)
    assert mod._mcp_envelope_ok(b'{"result": {"content": "ok"}}') is True
    assert mod._mcp_envelope_ok(b'{"error": {"code": -32000, "message": "boom"}}') is False
    assert mod._mcp_envelope_ok(b'{"result": {"isError": true, "content": "boom"}}') is False
    assert mod._mcp_envelope_ok(b"not json") is False
    assert mod._mcp_envelope_ok(b"") is False


def test_mcp_envelope_ok_parses_sse_stream(project):
    """MCP Streamable HTTP may return SSE — take the LAST data: event."""
    mod = _load_module(project)
    sse_ok = (
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","id":"1","result":{"content":"ok"}}\n\n'
    )
    assert mod._mcp_envelope_ok(sse_ok) is True
    # An SSE stream whose terminal event is an error envelope must fail.
    sse_err = (
        b'data: {"jsonrpc":"2.0","id":"1","result":{"content":"partial"}}\n\n'
        b'data: {"jsonrpc":"2.0","id":"1","error":{"code":-32000,"message":"boom"}}\n\n'
    )
    assert mod._mcp_envelope_ok(sse_err) is False


def test_mcp_request_carries_accept_header(project, monkeypatch):
    """Codex P1: Streamable HTTP MCP requires an Accept negotiating both JSON and SSE."""
    monkeypatch.setenv("MCP_API_KEY", "key")
    monkeypatch.setenv("KL_MCP_URL", "https://example.test/api/mcp")
    mod = _load_module(project)
    captured = {}

    class FakeResp:
        status = 200
        def read(self): return b'{"result": {"content": "ok"}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return FakeResp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.kl_create_note("acme", "t", "b", ["x"]) is True
    # urllib title-cases header names.
    accept = captured["headers"].get("Accept", "")
    assert "application/json" in accept and "text/event-stream" in accept


def test_mode_kl_only_requires_session_id(project, monkeypatch, capsys):
    monkeypatch.setenv("MCP_API_KEY", "key")
    monkeypatch.setenv("KL_MCP_URL", "https://example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)
    # No records + no session_id → early safety return, no crash.
    mod.mode_kl_only("")
    err = capsys.readouterr().err
    assert "no session_id" in err


def test_mode_kl_only_selects_matching_session_id(project, monkeypatch):
    """Verifies fix #3: kl-only picks the snapshot whose session_id matches
    the wrapper's payload — NOT the newest snapshot."""
    monkeypatch.setenv("MCP_API_KEY", "key")
    monkeypatch.setenv("KL_MCP_URL", "https://example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    monkeypatch.setenv("SESSION_RETRO_DRY_RUN", "1")
    mod = _load_module(project)

    retro_dir = project / ".ai" / "memory" / "retrospectives"

    # Newest snapshot — different session — should NOT be picked.
    (retro_dir / "20260718T121000Z-branch-a.json").write_text(json.dumps({
        "session_id": "OTHER",
        "branch": "branch/a",
        "captured_at": "2026-07-18T12:10:00Z",
        "dysfunction_score": 9,
        "failure_class": "planning",
    }))
    # Older snapshot — OUR session — must be selected.
    (retro_dir / "20260718T115900Z-branch-b.json").write_text(json.dumps({
        "session_id": "MINE",
        "branch": "branch/b",
        "captured_at": "2026-07-18T11:59:00Z",
        "dysfunction_score": 3,
        "failure_class": "unclassified",
    }))

    calls = []

    def fake_create(entity, title, content, tags):
        calls.append(("note", title, content))
        return True

    def fake_assert(entity, project_slug, fact_key, fact_value):
        calls.append(("fact", fact_key, fact_value))
        return True

    monkeypatch.setattr(mod, "kl_create_note", fake_create)
    monkeypatch.setattr(mod, "kl_assert_fact", fake_assert)
    mod.mode_kl_only("MINE")

    assert calls, "kl-only should have made KL calls for the matching record"
    # Title must reference our branch — proves session-scoped selection.
    assert any("branch/b" in call[1] for call in calls if call[0] == "note")
    # And crucially NOT the other branch (which was newer).
    assert not any("branch/a" in call[1] for call in calls if call[0] == "note")


def test_mode_precompact_writes_session_scoped_checkpoint(project, monkeypatch):
    """Verifies fix #4: staging filename + body carry session_id."""
    mod = _load_module(project)
    # Minimal fake session log the collector can chew on.
    log_dir = project / ".ai" / "session-logs"
    fake_log = log_dir / "session_test.jsonl"
    fake_log.write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")

    mod.mode_precompact(str(fake_log), session_id="SID-ABC-123")
    staging = list((project / ".ai" / "retrospective-staging").glob("checkpoint_*.json"))
    assert len(staging) == 1
    assert "SID-ABC-123" in staging[0].name
    body = json.loads(staging[0].read_text())
    assert body["session_id"] == "SID-ABC-123"


def test_mode_stop_empty_sid_leaves_tagged_checkpoints_alone(project, monkeypatch):
    """Codex P2: a Stop with NO session_id must not consume every tagged sibling.

    The old guard required BOTH sides to be non-empty before enforcing
    isolation — so an empty-payload Stop happily merged and deleted every
    session's tagged checkpoints, leaking their signals into its record
    and stealing them from the rightful Stop handlers.
    """
    mod = _load_module(project)
    staging_dir = project / ".ai" / "retrospective-staging"

    tagged_a = staging_dir / "checkpoint_SID-A_20260718_120000_000000.json"
    tagged_a.write_text(json.dumps({
        "mode": "precompact",
        "session_id": "SID-A",
        "transcript": "/nonexistent-a.jsonl",
        "signals": {"user_corrections": ["leaked from A"]},
    }))
    tagged_b = staging_dir / "checkpoint_SID-B_20260718_120500_000000.json"
    tagged_b.write_text(json.dumps({
        "mode": "precompact",
        "session_id": "SID-B",
        "transcript": "/nonexistent-b.jsonl",
        "signals": {"user_corrections": ["leaked from B"]},
    }))
    # Anonymous legacy checkpoint (no session_id) — SHOULD be consumed.
    legacy = staging_dir / "checkpoint_20260718_121000_000000.json"
    legacy.write_text(json.dumps({
        "mode": "precompact",
        "session_id": "",
        "transcript": "/nonexistent-legacy.jsonl",
        "signals": {"user_corrections": ["legacy fair game"]},
    }))

    # Stop with an empty session_id — the wrapper permits this.
    mod.mode_stop("", local_only=True, transcript="")

    assert tagged_a.exists(), "empty-sid Stop must NOT consume SID-A's checkpoint"
    assert tagged_b.exists(), "empty-sid Stop must NOT consume SID-B's checkpoint"
    assert not legacy.exists(), "empty-sid Stop SHOULD consume anonymous legacy checkpoint"

    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    text = jsonl.read_text() if jsonl.exists() else ""
    assert "leaked from A" not in text
    assert "leaked from B" not in text


def test_staging_key_pairs_precompact_and_stop_via_transcript(project, tmp_path):
    """CodeRabbit fix: an empty-sid PreCompact + empty-sid Stop must still
    route to each other via the transcript-derived staging_key."""
    mod = _load_module(project)
    # Build a real transcript so both PreCompact and Stop can canonicalise it.
    transcript = tmp_path / "session_transcript.jsonl"
    transcript.write_text('{"role":"user","content":"hi"}\n')

    # Sibling session B's transcript — must not be picked up by Stop A.
    transcript_b = tmp_path / "other_session.jsonl"
    transcript_b.write_text('{"role":"user","content":"hi from B"}\n')

    # PreCompact for session A (no session_id) — writes a tx-keyed checkpoint.
    mod.mode_precompact(str(transcript), session_id="")
    # PreCompact for session B (also no session_id) — different transcript.
    mod.mode_precompact(str(transcript_b), session_id="")

    staging_dir = project / ".ai" / "retrospective-staging"
    ckpts = sorted(staging_dir.glob("checkpoint_*.json"))
    assert len(ckpts) == 2

    # Both bodies must carry a tx: staging_key derived from their transcript.
    keys = {json.loads(p.read_text())["staging_key"] for p in ckpts}
    assert all(k.startswith("tx:") for k in keys), keys
    assert len(keys) == 2, "two distinct transcripts must yield two distinct keys"

    # Stop for session A (empty sid, A's transcript) — must consume A's
    # checkpoint and leave B's alone.
    mod.mode_stop("", local_only=True, transcript=str(transcript))
    remaining = sorted(staging_dir.glob("checkpoint_*.json"))
    assert len(remaining) == 1, f"expected only B's checkpoint to remain, got {remaining}"
    b_body = json.loads(remaining[0].read_text())
    assert "other_session" in b_body["transcript"], "wrong checkpoint survived"


def test_mode_stop_ignores_other_sessions_staging(project, monkeypatch):
    """Verifies fix #4: mode_stop only merges checkpoints matching its session_id
    AND deletes only the ones it consumed."""
    mod = _load_module(project)
    staging_dir = project / ".ai" / "retrospective-staging"

    # Our own checkpoint — should be consumed.
    ours = staging_dir / "checkpoint_MINE_20260718_120000_000000.json"
    ours.write_text(json.dumps({
        "mode": "precompact",
        "session_id": "MINE",
        "transcript": "/nonexistent.jsonl",
        "signals": {"user_corrections": ["should merge in"]},
    }))
    # Sibling session's checkpoint — must be LEFT ALONE.
    theirs = staging_dir / "checkpoint_THEIRS_20260718_120500_000000.json"
    theirs.write_text(json.dumps({
        "mode": "precompact",
        "session_id": "THEIRS",
        "transcript": "/nonexistent.jsonl",
        "signals": {"user_corrections": ["MUST NOT LEAK"]},
    }))

    # Prevent any network leg; SESSION_RETRO_DRY_RUN keeps writes to stderr
    # but still exercises the merge + staging cleanup path.
    mod.mode_stop("MINE", local_only=True, transcript="")

    # Our checkpoint gone, sibling's kept.
    assert not ours.exists(), "own-session checkpoint should be cleaned up"
    assert theirs.exists(), "sibling-session checkpoint must NOT be touched"

    # JSONL body must include our correction, not the sibling's.
    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    assert jsonl.exists()
    text = jsonl.read_text()
    assert "MUST NOT LEAK" not in text
    # Cleanup would have removed only `ours`; if it didn't, we'd double-count on rerun.


def test_mcp_payload_carries_jsonrpc_envelope(project, monkeypatch):
    """Codex P1: MCP requires jsonrpc + id fields on tools/call."""
    monkeypatch.setenv("MCP_API_KEY", "key")
    monkeypatch.setenv("KL_MCP_URL", "https://example.test/api/mcp")
    mod = _load_module(project)
    captured = {}

    class FakeResp:
        status = 200
        def read(self): return b'{"result": {"content": "ok"}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return FakeResp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    ok = mod.kl_create_note("acme", "t", "b", ["x"])
    assert ok is True
    body = json.loads(captured["data"])
    assert body.get("jsonrpc") == "2.0"
    assert "id" in body and body["id"]
    assert body.get("method") == "tools/call"


def test_stop_merges_staged_error_count(project, monkeypatch):
    """Codex P2: staged error_count must reach the classifier so pre-compaction
    tool errors can still produce external-dependency."""
    mod = _load_module(project)
    staging_dir = project / ".ai" / "retrospective-staging"
    # Six pre-compaction errors, no per-Stop transcript signals.
    (staging_dir / "checkpoint_MINE_20260718_120000_000000.json").write_text(json.dumps({
        "mode": "precompact",
        "session_id": "MINE",
        "transcript": "/pre.jsonl",
        "signals": {"error_count": 6},
    }))
    mod.mode_stop("MINE", local_only=True, transcript="")
    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    rec = json.loads(jsonl.read_text().splitlines()[-1])
    assert rec["failure_class"] == "external-dependency", \
        f"expected external-dependency from merged error_count, got {rec['failure_class']}"


def test_local_only_still_forbids_kl_even_when_creds_set(project, monkeypatch):
    """Security boundary: --local-only must never call KL, even when the env
    is fully credentialed. This is what impaktful HOLD depends on."""
    monkeypatch.setenv("MCP_API_KEY", "key")
    monkeypatch.setenv("KL_MCP_URL", "https://example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    called = {"n": 0}

    def fake_create(*a, **kw):
        called["n"] += 1
        return True

    monkeypatch.setattr(mod, "kl_create_note", fake_create)
    mod.mode_stop("SID", local_only=True, transcript="")
    assert called["n"] == 0, "local_only=True MUST prevent all KL writes"
