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


def test_kl_url_rejects_spoofed_loopback_prefixes(project, monkeypatch):
    """Codex P2: naive prefix checks let attacker-controlled hostnames
    like http://localhost.attacker.example bypass the loopback exemption."""
    mod = _load_module(project)
    spoofs = [
        "http://localhost.attacker.example/api/mcp",
        "http://127.0.0.1.attacker.example/api/mcp",
        "http://127.0.0.1@attacker.example/api/mcp",
    ]
    for url in spoofs:
        monkeypatch.setenv("KL_MCP_URL", url)
        assert mod._kl_url() is None, f"spoofed loopback should be rejected: {url}"
    # Real loopback + https are still accepted.
    monkeypatch.setenv("KL_MCP_URL", "http://localhost:8080/api/mcp")
    assert mod._kl_url() == "http://localhost:8080/api/mcp"
    monkeypatch.setenv("KL_MCP_URL", "http://127.0.0.1:8080/api/mcp")
    assert mod._kl_url() == "http://127.0.0.1:8080/api/mcp"
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.com/api/mcp")
    assert mod._kl_url() == "https://kl.example.com/api/mcp"


def test_mcp_envelope_ok_requires_non_null_result(project):
    """Codex P2: a 200 with an empty body, a bare jsonrpc envelope, or
    result:null must not count as success — a proxy could silently
    drop the tool call while returning HTTP 200."""
    mod = _load_module(project)
    assert mod._mcp_envelope_ok(b'{}') is False
    assert mod._mcp_envelope_ok(b'{"jsonrpc":"2.0","id":"1"}') is False
    assert mod._mcp_envelope_ok(b'{"jsonrpc":"2.0","id":"1","result":null}') is False
    # A minimal but real result payload still passes.
    assert mod._mcp_envelope_ok(b'{"jsonrpc":"2.0","id":"1","result":{"content":"ok"}}') is True


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

    monkeypatch.setattr(mod, "_kl_urlopen", lambda req, timeout=None: fake_urlopen(req, timeout=timeout))
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


def test_two_empty_sid_stops_same_branch_same_second_do_not_collide(project, tmp_path):
    """Codex P2: mode_stop must propagate its transcript into the record
    so _write_local_record can derive a tx: snapshot key when session_id
    is empty. Without this, two empty-sid Stops on the same branch inside
    one UTC second write the same -unknown.json path and clobber each other.
    """
    mod = _load_module(project)
    # Two distinct transcripts, both processed with empty session_id.
    t1 = tmp_path / "sess_a.jsonl"
    t1.write_text('{"role":"user","content":"a"}\n')
    t2 = tmp_path / "sess_b.jsonl"
    t2.write_text('{"role":"user","content":"b"}\n')

    mod.mode_stop("", local_only=True, transcript=str(t1))
    # Force mtime-gate off so a rapid second Stop actually writes.
    (project / ".ai" / "memory" / "retrospectives" / ".last-capture-mtime").unlink(
        missing_ok=True
    )
    mod.mode_stop("", local_only=True, transcript=str(t2), force=True)

    snaps = list((project / ".ai" / "memory" / "retrospectives").glob("*.json"))
    # Two distinct snapshot files must exist — one per transcript.
    assert len(snaps) >= 2, f"expected two snapshots, got {[p.name for p in snaps]}"
    # Neither should be the naked "-unknown.json" that the old collision produced.
    assert not any(p.name.endswith("-unknown.json") for p in snaps), \
        f"snapshot fell back to -unknown (transcript hash was unreachable): {[p.name for p in snaps]}"


def test_kl_flush_selects_oldest_unflushed_when_counter_suffix_present(project, monkeypatch):
    """Codex P2 2026-07-19: queue semantics. When two snapshots exist for
    one session (`<base>.json` and `<base>-001.json`), mode_kl_only must
    pick the OLDEST unflushed one so both captures reach KL exactly once
    in order (older was previously updated to newest-first, then revised
    to oldest-first to fix the double-upload race for concurrent Stops)."""
    import os as _os
    import time as _time
    mod = _load_module(project)
    retros = project / ".ai" / "memory" / "retrospectives"
    retros.mkdir(parents=True, exist_ok=True)
    base = "20260719T000000Z-main-sess-newest"
    older = retros / f"{base}.json"
    newer = retros / f"{base}-001.json"
    # CodeRabbit 2026-07-19: distinguish snapshots by a field mode_kl_only
    # actually surfaces in the note body (branch), not a `marker` field it
    # never reads. Otherwise both assertions pass regardless of which
    # snapshot won the selector.
    older.write_text(json.dumps({
        "session_id": "sess-newest", "branch": "old-branch",
        "dysfunction_score": 1, "failure_class": "unclassified",
    }))
    newer.write_text(json.dumps({
        "session_id": "sess-newest", "branch": "new-branch",
        "dysfunction_score": 9, "failure_class": "unclassified",
    }))
    # Ensure newer has strictly greater mtime regardless of write ordering.
    now = _time.time()
    _os.utime(older, (now - 2, now - 2))
    _os.utime(newer, (now, now))

    # Stub KL delivery: capture the record that mode_kl_only would upload.
    captured: dict = {}
    monkeypatch.setenv("KL_MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setenv("KL_MCP_API_KEY", "dummy")
    monkeypatch.setattr(mod, "_kl_ready", lambda entity, local_only=False: True)
    monkeypatch.setattr(mod, "_resolve_entity", lambda: "manolii")
    def _fake_note(*args, **kwargs):
        captured["record"] = {
            "title": kwargs.get("title", args[1] if len(args) > 1 else ""),
            "body": kwargs.get("content", kwargs.get("body", args[2] if len(args) > 2 else "")),
        }
        return True
    monkeypatch.setattr(mod, "kl_create_note", _fake_note)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **k: True)

    mod.mode_kl_only(session_id="sess-newest")
    assert captured, "mode_kl_only did not attempt a KL write"
    # Body must mention the OLDER snapshot's branch — queue semantics
    # process oldest-unflushed first so both captures reach KL exactly once.
    body = captured["record"]["body"]
    assert "old-branch" in body and "new-branch" not in body, (
        f"kl-only picked the newer snapshot instead of oldest-unflushed: body={body!r}"
    )


def test_kl_bearer_never_follows_redirect(project, monkeypatch):
    """Codex P1 2026-07-19: an HTTPS→HTTP (or cross-origin) 3xx from the
    KL endpoint must not cause urllib to replay the Authorization header
    at the redirect target. The no-redirect handler exposes the 3xx
    response directly and _kl_urlopen's callers treat it as failure."""
    import io
    import urllib.request as _ur
    monkeypatch.setenv("MCP_API_KEY", "leak-me")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example/mcp")
    mod = _load_module(project)

    call_log: list = []
    original_open = mod._NO_REDIRECT_OPENER.open

    class Redirect302(_ur.HTTPError):
        pass

    def fake_open(req, timeout=None):
        call_log.append(req.full_url)
        # Simulate a real 302 response so the no-redirect handler decides.
        fp = io.BytesIO(b"")
        hdrs = {"Location": "http://attacker.example/steal"}
        # Route through the actual handler chain by raising an HTTPError,
        # which the parent class would normally translate into a redirect.
        raise Redirect302(req.full_url, 302, "Found", hdrs, fp)

    monkeypatch.setattr(mod._NO_REDIRECT_OPENER, "open", fake_open)
    ok = mod.kl_create_note("acme", "t", "b", ["x"])
    assert ok is False, "kl_create_note should treat 302 as failure"
    assert call_log == ["https://kl.example/mcp"], (
        f"exactly one request must be issued, got {call_log!r}"
    )
    # Restore for sibling tests.
    monkeypatch.setattr(mod._NO_REDIRECT_OPENER, "open", original_open)


def test_kl_url_reads_mcp_json_when_env_unset(project, monkeypatch):
    """Codex P2 2026-07-19: operators configure the KL MCP endpoint once
    in .mcp.json and never set KL_MCP_URL/KNOWLEDGE_LAYER_MCP_URL — the
    background kl-only flush must still resolve the URL, or every upload
    silently no-ops.

    Codex P1 2026-07-19 addendum: file-derived URLs now require
    KL_MCP_URL_TRUSTED_HOSTS opt-in — operators enumerate their KL host
    in the deployment env so a repo-side attacker can't redirect Bearer."""
    monkeypatch.delenv("KL_MCP_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_LAYER_MCP_URL", raising=False)
    monkeypatch.setenv("KL_MCP_URL_TRUSTED_HOSTS", "kl.example.com")
    (project / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "knowledge-layer": {
                "url": "https://kl.example.com/mcp"
            }
        }
    }))
    mod = _load_module(project)
    assert mod._kl_url() == "https://kl.example.com/mcp"


def test_kl_url_reads_remote_memory_alias(project, monkeypatch):
    """Codex P1 2026-07-19: the pack's own first-run setup wizard
    (`scripts/first-run-setup.py:454-457`) registers the KL MCP server
    under key `remote-memory`. Without recognising that alias, every
    pack-configured install silently no-ops the KL flush."""
    monkeypatch.delenv("KL_MCP_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_LAYER_MCP_URL", raising=False)
    monkeypatch.setenv("KL_MCP_URL_TRUSTED_HOSTS", "memory.example.com")
    (project / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "remote-memory": {
                "type": "http",
                "url": "https://memory.example.com/mcp",
            }
        }
    }))
    mod = _load_module(project)
    assert mod._kl_url() == "https://memory.example.com/mcp"


def test_kl_url_rejects_http_from_mcp_json(project, monkeypatch):
    """Even from .mcp.json, plaintext http (non-loopback) must be
    rejected — MCP_API_KEY travels as Bearer and would leak."""
    monkeypatch.delenv("KL_MCP_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_LAYER_MCP_URL", raising=False)
    (project / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "knowledge-layer": {"url": "http://kl.example.com/mcp"}
        }
    }))
    mod = _load_module(project)
    assert mod._kl_url() is None


def test_kl_url_env_var_wins_over_mcp_json(project, monkeypatch):
    """Env var takes precedence — operator override always wins."""
    monkeypatch.setenv("KL_MCP_URL", "https://from-env.example/mcp")
    (project / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"knowledge-layer": {"url": "https://from-file.example/mcp"}}
    }))
    mod = _load_module(project)
    assert mod._kl_url() == "https://from-env.example/mcp"


def test_snapshot_reservation_is_race_free(project, tmp_path):
    """Codex P2 2026-07-19: two concurrent _write_local_record calls for the
    same (session, branch, captured_at) must produce TWO distinct snapshot
    files, not clobber each other. Exercises the O_CREAT|O_EXCL loop by
    firing N threads all racing on the same base filename."""
    import threading
    mod = _load_module(project)
    base_record = {
        "session_id": "race-sess",
        "branch": "main",
        "captured_at": "2026-07-19T00:00:00Z",
        "marker": "x",
    }
    N = 8
    errors: list = []
    results: list = []
    barrier = threading.Barrier(N)

    def worker(i: int):
        try:
            barrier.wait(timeout=5)
            rec = dict(base_record, marker=f"worker-{i}")
            results.append(mod._write_local_record(rec))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"race workers raised: {errors!r}"
    assert len(results) == N, f"expected {N} snapshot paths, got {len(results)}"
    # Distinct filenames — the exclusive-create loop must have avoided collisions.
    names = {p.name for p in results}
    assert len(names) == N, f"snapshot filenames collided: {sorted(names)}"
    # Every file must be on disk.
    for p in results:
        assert p.exists(), f"race dropped snapshot {p}"


def test_force_retry_same_second_does_not_overwrite_snapshot(project, tmp_path):
    """Codex P2 2026-07-19: two Stops on the same (session, branch) within
    one UTC second (supported --force retry path) must produce TWO durable
    snapshot files, not silently overwrite the first."""
    mod = _load_module(project)
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text('{"role":"user","content":"a"}\n')

    mod.mode_stop("sess-collide", local_only=True, transcript=str(transcript))
    (project / ".ai" / "memory" / "retrospectives" / ".last-capture-mtime").unlink(
        missing_ok=True
    )
    mod.mode_stop("sess-collide", local_only=True, transcript=str(transcript), force=True)

    snaps = list((project / ".ai" / "memory" / "retrospectives").glob("*.json"))
    assert len(snaps) >= 2, (
        f"force-retry produced only {len(snaps)} snapshot(s): {[p.name for p in snaps]}"
    )


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

    monkeypatch.setattr(mod, "_kl_urlopen", lambda req, timeout=None: fake_urlopen(req, timeout=timeout))
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


def test_mtime_gate_is_per_transcript(project, monkeypatch):
    """CodeRabbit 2026-07-19: sentinel is a transcript-keyed map, not a singleton.

    Sequence: stop A → stop B (different transcript) → stop A unchanged.
    Regression: A's second Stop MUST hit the gate; the old singleton sentinel
    would have been overwritten by B and let A's re-stop write a duplicate.
    """
    mod = _load_module(project)
    logs_dir = project / ".ai" / "session-logs"
    log_a = logs_dir / "session_A.jsonl"
    log_b = logs_dir / "session_B.jsonl"
    log_a.write_text("{}\n")
    log_b.write_text("{}\n")
    # Distinct mtimes.
    os.utime(log_a, (1_700_000_000, 1_700_000_000))
    os.utime(log_b, (1_700_000_100, 1_700_000_100))

    # First stop for A: gate miss → then write sentinel.
    assert mod._mtime_gate_hit(log_a) is False
    mod._write_mtime_sentinel(log_a)

    # First stop for B: gate miss (different transcript) → write sentinel.
    assert mod._mtime_gate_hit(log_b) is False
    mod._write_mtime_sentinel(log_b)

    # Re-stop A unchanged: MUST be gated. Singleton would have been evicted by B.
    assert mod._mtime_gate_hit(log_a) is True, \
        "per-transcript sentinel must survive an intervening capture of a different transcript"
    # Re-stop B unchanged: still gated too.
    assert mod._mtime_gate_hit(log_b) is True


def test_mtime_sentinel_accepts_legacy_singleton_shape(project, monkeypatch):
    """Backwards compatibility: an on-disk legacy sentinel {path,mtime,captured_at}
    (pre-migration shape) must still gate its own transcript."""
    mod = _load_module(project)
    logs_dir = project / ".ai" / "session-logs"
    log_a = logs_dir / "session_legacy.jsonl"
    log_a.write_text("{}\n")
    os.utime(log_a, (1_700_000_000, 1_700_000_000))
    mod.MTIME_SENTINEL.write_text(
        json.dumps({"path": str(log_a), "mtime": 1_700_000_000, "captured_at": "2026-07-19T00:00:00Z"})
    )
    assert mod._mtime_gate_hit(log_a) is True


def test_mcp_json_url_accepted_by_default(project, monkeypatch):
    """Codex P2 2026-07-19 (Lead-Converter line 293): a checked-in `.mcp.json`
    knowledge-layer URL must be trusted by default. Gating it behind an
    undocumented KL_MCP_URL_TRUSTED_HOSTS silently disabled every KL
    upload in every repo that opted in the intended way (file + MCP_API_KEY).
    An attacker with `.mcp.json` write access can already run arbitrary
    code via other MCP server entries, so the extra gate provided no
    meaningful defence."""
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"knowledge-layer": {"url": "https://kl.corp.example/api/mcp"}}})
    )
    monkeypatch.delenv("KL_MCP_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_LAYER_MCP_URL", raising=False)
    monkeypatch.delenv("KL_MCP_URL_TRUSTED_HOSTS", raising=False)
    mod = _load_module(project)
    assert mod._kl_url() == "https://kl.corp.example/api/mcp"


def test_mcp_json_url_still_validated_as_https(project, monkeypatch):
    """Trusting the file by default does NOT bypass the https-or-loopback
    guard — an http:// URL in `.mcp.json` still fails closed."""
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"knowledge-layer": {"url": "http://plain.example/api/mcp"}}})
    )
    monkeypatch.delenv("KL_MCP_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_LAYER_MCP_URL", raising=False)
    monkeypatch.delenv("KL_MCP_URL_TRUSTED_HOSTS", raising=False)
    mod = _load_module(project)
    assert mod._kl_url() is None


def test_mcp_json_url_trusted_hosts_still_restricts_when_set(project, monkeypatch):
    """When KL_MCP_URL_TRUSTED_HOSTS IS set, it acts as an optional
    defence-in-depth restriction: only listed hosts (or loopback) pass."""
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"knowledge-layer": {"url": "https://attacker.example/api/mcp"}}})
    )
    monkeypatch.delenv("KL_MCP_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_LAYER_MCP_URL", raising=False)
    monkeypatch.setenv("KL_MCP_URL_TRUSTED_HOSTS", "kl.corp.example")
    mod = _load_module(project)
    assert mod._kl_url() is None


def test_kl_drain_retries_stranded_prior_session_snapshots(project, monkeypatch):
    """Codex P2 2026-07-19 (manolii-platform line 94): when kl_create_note
    fails transiently, mode_kl_only clears its lease but future Stops only
    look at their own session_id. mode_kl_drain must walk all unflushed
    snapshots and retry them per session_id."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    snap_dir = project / ".ai" / "memory" / "retrospectives"
    # Stranded snapshot from prior session — old enough to bypass the 30s
    # safety window and cleared lease (no kl_in_flight_at, kl_written=False).
    stranded = snap_dir / "20260718T000000Z-main-SID-old-stranded.json"
    stranded.write_text(json.dumps({
        "session_id": "SID-old",
        "branch": "main",
        "kl_written": False,
    }, indent=2))
    # Backdate mtime so age > 30s.
    os.utime(stranded, (1_700_000_000, 1_700_000_000))

    uploaded_sids = []
    def track_create(entity, title, content, tags):
        for tag in tags or []:
            if tag.startswith("branch:"):
                pass
        uploaded_sids.append(title)
        return True
    monkeypatch.setattr(mod, "kl_create_note", track_create)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_drain()
    assert len(uploaded_sids) == 1, \
        f"drain must retry the stranded snapshot: got {uploaded_sids}"
    after = json.loads(stranded.read_text())
    assert after.get("kl_written") is True


def test_kl_drain_skips_snapshots_younger_than_30s(project, monkeypatch):
    """The drain must NOT race the current session's own kl-only worker —
    snapshots younger than 30s are left for the per-session worker."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    snap_dir = project / ".ai" / "memory" / "retrospectives"
    fresh = snap_dir / "20260719T000000Z-main-SID-fresh.json"
    fresh.write_text(json.dumps({
        "session_id": "SID-fresh",
        "branch": "main",
        "kl_written": False,
    }, indent=2))
    # Leave the mtime at "now" so age < 30s.

    uploads = []
    monkeypatch.setattr(mod, "kl_create_note",
                        lambda *a, **kw: uploads.append(kw) or True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_drain()
    assert uploads == [], "drain must skip fresh snapshots (<30s old)"


def test_kl_drain_skips_snapshots_with_fresh_lease(project, monkeypatch):
    """The drain must respect a fresh in-flight lease — another worker is
    handling that snapshot."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    from datetime import datetime as _dt, timezone as _tz
    fresh_iso = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap_dir = project / ".ai" / "memory" / "retrospectives"
    leased = snap_dir / "20260718T000000Z-main-SID-leased.json"
    leased.write_text(json.dumps({
        "session_id": "SID-leased",
        "branch": "main",
        "kl_written": False,
        "kl_in_flight_at": fresh_iso,
    }, indent=2))
    os.utime(leased, (1_700_000_000, 1_700_000_000))  # snap old enough

    uploads = []
    monkeypatch.setattr(mod, "kl_create_note",
                        lambda *a, **kw: uploads.append(kw) or True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_drain()
    assert uploads == [], "drain must skip snapshots with a fresh lease"


def test_mcp_json_url_trusted_hosts_allows_matching_host(project, monkeypatch):
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"knowledge-layer": {"url": "https://kl.corp.example/api/mcp"}}})
    )
    monkeypatch.delenv("KL_MCP_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_LAYER_MCP_URL", raising=False)
    monkeypatch.setenv("KL_MCP_URL_TRUSTED_HOSTS", "kl.corp.example, other.example")
    mod = _load_module(project)
    assert mod._kl_url() == "https://kl.corp.example/api/mcp"


def test_kl_flush_event_appended_on_success(project, monkeypatch):
    """Codex P2 2026-07-19: successful KL upload must emit a completion
    event to session-retrospectives.jsonl so consumers see the flush."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    # Seed a snapshot the way mode_stop would have.
    snap_dir = project / ".ai" / "memory" / "retrospectives"
    snap = snap_dir / "20260719T000000Z-main-SID-fixture.json"
    record = {
        "session_id": "SID-fixture",
        "captured_at": "2026-07-19T00:00:00Z",
        "branch": "main",
        "dysfunction_score": 0.1,
        "failure_class": "unclassified",
        "kl_written": False,
    }
    snap.write_text(json.dumps(record, indent=2))

    monkeypatch.setattr(mod, "kl_create_note", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_only(session_id="SID-fixture")

    # Snapshot flipped.
    updated = json.loads(snap.read_text())
    assert updated["kl_written"] is True

    # And an append-only JSONL event was emitted.
    jsonl = snap_dir / "session-retrospectives.jsonl"
    assert jsonl.exists()
    events = [json.loads(ln) for ln in jsonl.read_text().splitlines() if ln.strip()]
    flushes = [e for e in events if e.get("event") == "kl-flushed"]
    assert len(flushes) == 1
    assert flushes[0]["session_id"] == "SID-fixture"


def test_reserve_mtime_gate_is_atomic_check_and_set(project, monkeypatch):
    """Codex P2 2026-07-19: two concurrent Stops for the same unchanged
    transcript must not both pass the gate. Simulate the race: two threads
    call _try_reserve_mtime_gate on the same path/mtime; exactly ONE must
    receive the go-ahead."""
    import threading
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_race.jsonl"
    log.write_text("{}\n")
    os.utime(log, (1_700_000_000, 1_700_000_000))

    results = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(mod._try_reserve_mtime_gate(log))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly ONE thread got True (the go-ahead); the other three saw
    # the reservation already present and returned False.
    assert results.count(True) == 1, f"expected 1 winner, got {results}"
    assert results.count(False) == 3


def test_reserve_mtime_gate_returns_true_when_transcript_missing(project, monkeypatch):
    """Fail-open contract: if the transcript path is None or unreadable,
    the reservation must not block capture."""
    mod = _load_module(project)
    assert mod._try_reserve_mtime_gate(None) is True
    assert mod._try_reserve_mtime_gate(project / ".ai" / "session-logs" / "nonexistent.jsonl") is True


def test_reserve_mtime_gate_force_bypass(project, monkeypatch):
    """SESSION_RETRO_FORCE / force=True must always bypass the reservation."""
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_force.jsonl"
    log.write_text("{}\n")
    os.utime(log, (1_700_000_000, 1_700_000_000))
    # First call: reservation succeeds.
    assert mod._try_reserve_mtime_gate(log) is True
    # Second call without force: reservation collides → False.
    assert mod._try_reserve_mtime_gate(log) is False
    # Third call with force=True: bypass.
    assert mod._try_reserve_mtime_gate(log, force=True) is True
    monkeypatch.setenv("SESSION_RETRO_FORCE", "1")
    assert mod._try_reserve_mtime_gate(log) is True


def test_reservation_released_when_capture_fails(project, monkeypatch):
    """Codex P2 2026-07-19: if _write_local_record raises after the mtime
    reservation was recorded, the reservation MUST be rolled back so the
    next Stop can retry — otherwise the transcript is silently skipped
    forever until it changes or --force is used."""
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_release.jsonl"
    log.write_text("{}\n")
    os.utime(log, (1_700_000_000, 1_700_000_000))

    # Force _write_local_record to blow up.
    def boom(record):
        raise OSError("disk full (simulated)")
    monkeypatch.setattr(mod, "_write_local_record", boom)

    with pytest.raises(OSError, match="disk full"):
        mod.mode_stop("SID-release", local_only=True, transcript=str(log))

    # Reservation must have been rolled back — next call must succeed at the gate.
    assert mod._try_reserve_mtime_gate(log) is True, \
        "sentinel entry not released after capture failure — transcript would be stuck"


def test_dry_run_does_not_reserve_mtime_gate(project, monkeypatch):
    """Codex P2 2026-07-19: SESSION_RETRO_DRY_RUN must NOT persist a mtime
    reservation, otherwise a later real --mode stop on the unchanged
    transcript would return at the gate and silently drop the retrospective."""
    monkeypatch.setenv("SESSION_RETRO_DRY_RUN", "1")
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_dryrun.jsonl"
    log.write_text("{}\n")
    os.utime(log, (1_700_000_000, 1_700_000_000))

    mod.mode_stop("SID-dry", local_only=True, transcript=str(log))
    # After dry-run, the sentinel MUST NOT record this transcript — so the
    # next reservation attempt (as a real Stop would) succeeds.
    monkeypatch.delenv("SESSION_RETRO_DRY_RUN", raising=False)
    assert mod._try_reserve_mtime_gate(log) is True, \
        "dry-run reserved the mtime gate — a real Stop would now skip"


def test_retry_streak_resets_at_message_boundaries(project, tmp_path):
    """Codex P2 2026-07-19: three identical tool_use calls separated by
    ordinary user/assistant text turns must NOT count as a retry streak.
    The stream only crosses the retry threshold when the calls are
    truly consecutive (no non-tool_use message between them)."""
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_streak.jsonl"
    # Assistant sends the same tool_use, then a user text message intervenes,
    # then assistant sends it again, then user text, then assistant again.
    entries = []
    tool_call = {"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}
    for _ in range(3):
        entries.append({"message": {"role": "assistant", "content": [tool_call]}})
        entries.append({"message": {"role": "user", "content": "please continue"}})
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    sigs = mod.extract_signals(log)
    # Same tool called 3 times but separated by user messages → NOT a retry.
    assert sigs["tool_retries"].get("Read", 0) < 3, \
        f"streak should reset at message boundaries, got {sigs['tool_retries']}"


def test_retry_streak_survives_tool_result_envelope(project, tmp_path):
    """Codex P2 2026-07-19 (manolii-platform line 475): in real Claude
    transcripts, an assistant tool_use is followed by a SEPARATE user-role
    tool_result envelope before the assistant can retry. The tool_result
    envelope contains no tool_use, so a naive per-entry reset would fire
    between the failed attempt and the retry — every retry restarts at
    streak=1 and tool_retries never crosses the threshold. Fix: entries
    carrying a tool_result are transparent to the streak."""
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_retry_across_result.jsonl"
    tool_call = {"type": "tool_use", "name": "Bash",
                 "input": {"command": "psql -c 'SELECT 1'"}}
    err_result = {"type": "tool_result", "is_error": True,
                  "content": "Error: connection refused"}
    # assistant tool_use → user tool_result(err) → assistant tool_use (retry)
    # → user tool_result(err) → assistant tool_use (retry). Three attempts
    # of the same key should reach the retry threshold.
    entries = [
        {"message": {"role": "assistant", "content": [tool_call]}},
        {"message": {"role": "user", "content": [err_result]}},
        {"message": {"role": "assistant", "content": [tool_call]}},
        {"message": {"role": "user", "content": [err_result]}},
        {"message": {"role": "assistant", "content": [tool_call]}},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    sigs = mod.extract_signals(log)
    assert sigs["tool_retries"].get("Bash", 0) >= 3, \
        f"three same-key attempts across tool_result envelopes should count as retry, " \
        f"got {sigs['tool_retries']}"


def test_retry_streak_still_fires_for_true_consecutive_calls(project, tmp_path):
    """Sanity check the fix doesn't over-reset: three back-to-back identical
    tool_use calls in one assistant message DO cross the retry threshold."""
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_true_retry.jsonl"
    tool_call = {"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}
    entries = [
        {"message": {"role": "assistant", "content": [tool_call, tool_call, tool_call]}},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    sigs = mod.extract_signals(log)
    assert sigs["tool_retries"].get("Read", 0) >= 3, \
        f"three back-to-back identical calls should still count as retry, got {sigs['tool_retries']}"


def test_kl_only_dry_run_does_not_mark_flushed(project, monkeypatch):
    """Codex P2 2026-07-19: --dry-run makes kl_create_note return True
    without any network call. mode_kl_only MUST NOT then flip kl_written
    or emit a kl-flushed event — that would be telemetry lying about
    delivery that never happened."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    monkeypatch.setenv("SESSION_RETRO_DRY_RUN", "1")
    mod = _load_module(project)

    snap_dir = project / ".ai" / "memory" / "retrospectives"
    snap = snap_dir / "20260719T000000Z-main-SID-dry.json"
    record = {
        "session_id": "SID-dry",
        "captured_at": "2026-07-19T00:00:00Z",
        "branch": "main",
        "dysfunction_score": 0.1,
        "failure_class": "unclassified",
        "kl_written": False,
    }
    snap.write_text(json.dumps(record, indent=2))

    # kl_create_note's own dry-run short-circuit returns True.
    mod.mode_kl_only(session_id="SID-dry")

    updated = json.loads(snap.read_text())
    assert updated["kl_written"] is False, "dry-run must not flip kl_written"
    jsonl = snap_dir / "session-retrospectives.jsonl"
    if jsonl.exists():
        events = [json.loads(ln) for ln in jsonl.read_text().splitlines() if ln.strip()]
        assert not [e for e in events if e.get("event") == "kl-flushed"], \
            "dry-run must not emit a kl-flushed event"


def test_mode_inject_skips_kl_flushed_events(project, monkeypatch):
    """Codex P2 2026-07-19: kl-flushed completion events carry branch and
    dysfunction_score for JSONL consumers, but they're not retrospectives.
    mode_inject must NOT count them as a second warning row."""
    mod = _load_module(project)
    branch = mod._get_branch()

    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        json.dumps({"branch": branch, "captured_at": "2026-07-19T00:00:00Z",
                    "dysfunction_score": 8, "failure_class": "tooling"}) + "\n"
        + json.dumps({"event": "kl-flushed", "branch": branch,
                      "session_id": "SID", "dysfunction_score": 8,
                      "failure_class": "tooling",
                      "at": "2026-07-19T00:01:00Z"}) + "\n"
    )
    mod.mode_inject()
    txt = mod.NAVIGATION_WARNING_FILE.read_text(encoding="utf-8")
    # Exactly one "- 2026" bulleted warning — the kl-flushed row must NOT
    # produce a second entry.
    assert txt.count("- 2026") == 1, f"expected 1 warning row, got:\n{txt}"


def test_kl_only_picks_oldest_unflushed_snapshot(project, monkeypatch):
    """Codex P2 2026-07-19: two Stops for the same session_id before any
    background kl-only runs create two snapshots. Each background flush
    must process the OLDEST unflushed snapshot — queue semantics — so
    both captures reach KL exactly once."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    snap_dir = project / ".ai" / "memory" / "retrospectives"
    older = snap_dir / "20260719T000000Z-main-SID-first.json"
    newer = snap_dir / "20260719T000030Z-main-SID-second.json"
    for p, ts in ((older, 1_700_000_000), (newer, 1_700_000_060)):
        p.write_text(json.dumps({
            "session_id": "SID",
            "branch": "main",
            "captured_at": "2026-07-19T00:00:00Z",
            "dysfunction_score": 0.1,
            "failure_class": "unclassified",
            "kl_written": False,
        }, indent=2))
        os.utime(p, (ts, ts))

    picked_titles = []
    def track_create(entity, title, content, tags):
        picked_titles.append(title)
        return True
    monkeypatch.setattr(mod, "kl_create_note", track_create)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    # First flush picks the older snapshot and marks it flushed.
    mod.mode_kl_only(session_id="SID")
    assert json.loads(older.read_text())["kl_written"] is True, \
        "first flush should have marked the older snapshot flushed"
    assert json.loads(newer.read_text())["kl_written"] is False, \
        "first flush must not touch the newer snapshot"

    # Second flush picks the newer snapshot (older is now kl_written=true).
    mod.mode_kl_only(session_id="SID")
    assert json.loads(newer.read_text())["kl_written"] is True
    assert len(picked_titles) == 2, \
        f"both captures should reach KL exactly once, got {len(picked_titles)} uploads"


def test_release_mtime_reservation_preserves_newer_entry(project, monkeypatch):
    """Codex line-790 refinement 2026-07-19: rollback must NOT clobber a
    concurrent handler's newer reservation. Handler A reserves M1 and its
    capture fails; handler B has meanwhile reserved M2 for the same
    transcript. A's rollback must leave B's entry intact."""
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_concurrent.jsonl"
    log.write_text("{}\n")
    # Handler A observes M1.
    os.utime(log, (1_700_000_000, 1_700_000_000))
    assert mod._try_reserve_mtime_gate(log) is True
    reserved_by_a = mod._session_log_mtime(log)

    # Handler B (simulated) observes M2, overwrites the sentinel entry.
    os.utime(log, (1_700_000_100, 1_700_000_100))
    entries = mod._load_mtime_sentinel_map()
    entries[str(log)] = {"mtime": 1_700_000_100.0, "captured_at": "2026-07-19T00:00:00Z"}
    mod.MTIME_SENTINEL.write_text(json.dumps({"entries": entries}))

    # Handler A's rollback should NOT remove B's newer entry.
    mod._release_mtime_reservation(log, reserved_mtime=reserved_by_a)
    entries_after = mod._load_mtime_sentinel_map()
    assert str(log) in entries_after, \
        "A's rollback must not clobber B's newer reservation"
    assert float(entries_after[str(log)]["mtime"]) == 1_700_000_100.0


def test_forced_capture_persists_mtime_sentinel(project, monkeypatch):
    """Codex line-1088 2026-07-19: --force bypasses the pre-capture
    reservation, but the sentinel must be recorded AFTER the successful
    write so a subsequent ordinary Stop for the unchanged transcript
    doesn't duplicate the retrospective."""
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_force_persist.jsonl"
    log.write_text("{}\n")
    os.utime(log, (1_700_000_000, 1_700_000_000))

    # Real Stop capture path with force=True.
    mod.mode_stop("SID-force", local_only=True, force=True, transcript=str(log))

    # A subsequent ordinary (non-force) Stop must find the transcript gated.
    assert mod._try_reserve_mtime_gate(log) is False, \
        "forced capture must persist the mtime sentinel so a follow-up Stop is gated"


def test_forced_dry_run_still_does_not_persist_sentinel(project, monkeypatch):
    """--force + --dry-run must not persist the sentinel (no durable record
    was written and telemetry must not lie)."""
    monkeypatch.setenv("SESSION_RETRO_DRY_RUN", "1")
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    log = logs / "session_force_dryrun.jsonl"
    log.write_text("{}\n")
    os.utime(log, (1_700_000_000, 1_700_000_000))

    mod.mode_stop("SID-force-dry", local_only=True, force=True, transcript=str(log))
    monkeypatch.delenv("SESSION_RETRO_DRY_RUN", raising=False)
    # No sentinel written → next Stop can reserve.
    assert mod._try_reserve_mtime_gate(log) is True


def test_kl_only_atomic_claim_prevents_duplicate_upload(project, monkeypatch):
    """Codex P2 2026-07-19 line 994: two concurrent kl-only workers for the
    same session_id must not both upload the same oldest-unflushed snapshot.
    The scan + kl_written=true flip live under _retro_lock() so only one
    worker takes ownership."""
    import threading
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    snap_dir = project / ".ai" / "memory" / "retrospectives"
    snap = snap_dir / "20260719T000000Z-main-SID-only.json"
    snap.write_text(json.dumps({
        "session_id": "SID",
        "branch": "main",
        "captured_at": "2026-07-19T00:00:00Z",
        "dysfunction_score": 0.1,
        "failure_class": "unclassified",
        "kl_written": False,
    }, indent=2))
    os.utime(snap, (1_700_000_000, 1_700_000_000))

    upload_count = 0
    upload_lock = threading.Lock()
    # Simulate a slow network so both threads race the claim/upload boundary.
    def slow_create(entity, title, content, tags):
        nonlocal upload_count
        # Sleep OUTSIDE the retrospective lock so a real race is possible
        # only if the claim was not held under _retro_lock().
        import time as _t
        _t.sleep(0.05)
        with upload_lock:
            upload_count += 1
        return True
    monkeypatch.setattr(mod, "kl_create_note", slow_create)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    barrier = threading.Barrier(4)
    def worker():
        barrier.wait()
        mod.mode_kl_only(session_id="SID")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert upload_count == 1, \
        f"atomic claim must serialize: expected exactly 1 upload, got {upload_count}"
    assert json.loads(snap.read_text())["kl_written"] is True


def test_kl_only_rollback_clears_lease_on_failure(project, monkeypatch):
    """Codex P2 2026-07-19 (Lead-Converter line 990): when the KL network
    call fails after the lease claim, `kl_in_flight_at` must be cleared and
    `kl_written` must NOT be True so the next retry (or a sibling worker)
    can pick the snapshot up immediately."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    snap_dir = project / ".ai" / "memory" / "retrospectives"
    snap = snap_dir / "20260719T000000Z-main-SID-rollback.json"
    snap.write_text(json.dumps({
        "session_id": "SID",
        "branch": "main",
        "captured_at": "2026-07-19T00:00:00Z",
        "dysfunction_score": 0.1,
        "failure_class": "unclassified",
        "kl_written": False,
    }, indent=2))

    monkeypatch.setattr(mod, "kl_create_note", lambda *a, **kw: False)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_only(session_id="SID")
    after = json.loads(snap.read_text())
    assert after.get("kl_written") is not True, \
        "network failure must NOT leave kl_written=True (Codex Lead-Converter line 990)"
    assert "kl_in_flight_at" not in after, \
        "failure path must clear the in-flight lease so the next retry can claim"


def test_kl_only_stale_lease_is_reclaimed_after_ttl(project, monkeypatch):
    """Codex P2 2026-07-19 (Lead-Converter line 990): if a worker crashes
    between claim and confirmation, the lease timestamp expires after
    KL_CLAIM_TTL_SEC and a new worker can reclaim the snapshot. Otherwise
    the retrospective would be stuck forever."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    # Snapshot with a lease from FAR in the past → older than any sane TTL.
    snap_dir = project / ".ai" / "memory" / "retrospectives"
    snap = snap_dir / "20260719T000000Z-main-SID-stale.json"
    snap.write_text(json.dumps({
        "session_id": "SID",
        "branch": "main",
        "captured_at": "2026-07-19T00:00:00Z",
        "dysfunction_score": 0.1,
        "failure_class": "unclassified",
        "kl_in_flight_at": "2000-01-01T00:00:00Z",  # ancient
    }, indent=2))

    uploads = []
    monkeypatch.setattr(mod, "kl_create_note", lambda *a, **kw: uploads.append(kw) or True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_only(session_id="SID")
    assert len(uploads) == 1, "stale lease must be reclaimable and uploaded"
    after = json.loads(snap.read_text())
    assert after.get("kl_written") is True
    assert "kl_in_flight_at" not in after


def test_kl_only_fresh_lease_blocks_concurrent_claim(project, monkeypatch):
    """Codex P2 2026-07-19: a fresh lease (younger than KL_CLAIM_TTL_SEC)
    must prevent a second worker from picking the same snapshot up. This
    is the correctness half of the lease semantics — the atomic-claim
    test covers the lock, this one covers the lease TTL."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    snap_dir = project / ".ai" / "memory" / "retrospectives"
    snap = snap_dir / "20260719T000000Z-main-SID-fresh-lease.json"
    # Use *just now* — well within the 60s TTL.
    from datetime import datetime as _dt, timezone as _tz
    fresh_iso = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap.write_text(json.dumps({
        "session_id": "SID",
        "branch": "main",
        "kl_in_flight_at": fresh_iso,
    }, indent=2))

    uploads = []
    monkeypatch.setattr(mod, "kl_create_note", lambda *a, **kw: uploads.append(kw) or True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_only(session_id="SID")
    assert uploads == [], "fresh lease must block a second concurrent claim"


def test_kl_only_fresh_lease_also_suppresses_jsonl_fallback(project, monkeypatch):
    """Codex P2 2026-07-19 (ai-starter-pack line 1047): when the snapshot
    scan skips a fresh-leased snapshot for this session, the JSONL fallback
    must ALSO be suppressed — otherwise a concurrent worker would upload
    the same narrative row without a claim, producing duplicate KL notes."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    from datetime import datetime as _dt, timezone as _tz
    fresh_iso = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap_dir = project / ".ai" / "memory" / "retrospectives"
    leased = snap_dir / "20260719T000000Z-main-SID-leased.json"
    leased.write_text(json.dumps({
        "session_id": "SID",
        "branch": "main",
        "kl_written": False,
        "kl_in_flight_at": fresh_iso,
    }, indent=2))
    # And a corresponding JSONL narrative row for the same session_id.
    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    jsonl.write_text(json.dumps({
        "session_id": "SID",
        "branch": "main",
        "captured_at": "2026-07-19T00:00:00Z",
        "dysfunction_score": 0.1,
        "failure_class": "unclassified",
    }) + "\n")

    uploads = []
    monkeypatch.setattr(mod, "kl_create_note",
                        lambda *a, **kw: uploads.append(kw) or True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_only(session_id="SID")
    assert uploads == [], \
        "fresh in-flight lease must suppress the JSONL fallback for the same session_id"


def test_kl_only_jsonl_fallback_treats_kl_flushed_event_as_delivered(project, monkeypatch):
    """CodeRabbit 2026-07-19 (line 1006-1031): when the snapshot has been
    pruned but the JSONL still contains both a narrative row AND a later
    `kl-flushed` event for the same session_id, the fallback must treat
    the event as a delivered marker and NOT re-upload the narrative row."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    # No snapshots. JSONL has the narrative row FOLLOWED BY a kl-flushed event.
    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    jsonl.write_text(
        json.dumps({
            "session_id": "SID",
            "branch": "main",
            "captured_at": "2026-07-19T00:00:00Z",
            "dysfunction_score": 0.1,
            "failure_class": "unclassified",
        }) + "\n"
        + json.dumps({
            "event": "kl-flushed",
            "session_id": "SID",
            "branch": "main",
            "dysfunction_score": 0,
            "failure_class": "unclassified",
            "at": "2026-07-19T00:00:01Z",
        }) + "\n"
    )

    uploads = []
    monkeypatch.setattr(mod, "kl_create_note", lambda *a, **kw: uploads.append(kw) or True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_only(session_id="SID")
    assert uploads == [], \
        "kl-flushed event in JSONL must gate the narrative row from re-upload"


def test_kl_only_does_not_fall_back_to_jsonl_when_snap_already_flushed(project, monkeypatch):
    """Codex P2 2026-07-19 (ai-starter-pack line 965): once a snapshot for
    the session_id has been flushed, mode_kl_only must NOT fall back to the
    append-only JSONL and re-upload from a stale narrative row (or a
    kl-flushed completion event)."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    snap_dir = project / ".ai" / "memory" / "retrospectives"
    snap = snap_dir / "20260719T000000Z-main-SID-alreadyflushed.json"
    snap.write_text(json.dumps({
        "session_id": "SID",
        "branch": "main",
        "kl_written": True,  # already delivered
    }, indent=2))
    # Stale narrative row in JSONL for the same session_id.
    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    jsonl.write_text(json.dumps({
        "session_id": "SID",
        "branch": "main",
        "captured_at": "2026-07-19T00:00:00Z",
        "dysfunction_score": 0.1,
        "failure_class": "unclassified",
    }) + "\n")

    upload_calls = []
    monkeypatch.setattr(mod, "kl_create_note",
                        lambda *a, **kw: upload_calls.append(kw) or True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_only(session_id="SID")
    assert upload_calls == [], \
        "already-flushed snapshot must suppress the JSONL fallback"


def test_kl_only_jsonl_fallback_skips_kl_flushed_event_rows(project, monkeypatch):
    """Codex P2 2026-07-19 (ai-starter-pack line 965): when the JSONL scan
    IS reachable (no matching snapshot), it must skip completion-event rows
    so a `kl-flushed` marker isn't uploaded as a retrospective."""
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("KL_MCP_URL", "https://kl.example.test/api/mcp")
    monkeypatch.setenv("KL_ENTITY", "acme")
    mod = _load_module(project)

    # No snapshots, but JSONL has ONLY a kl-flushed event for the session_id.
    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    jsonl.write_text(json.dumps({
        "event": "kl-flushed",
        "session_id": "SID",
        "branch": "main",
        "dysfunction_score": 0,
        "failure_class": "unclassified",
        "at": "2026-07-19T00:00:00Z",
    }) + "\n")

    upload_calls = []
    monkeypatch.setattr(mod, "kl_create_note",
                        lambda *a, **kw: upload_calls.append(kw) or True)
    monkeypatch.setattr(mod, "kl_assert_fact", lambda *a, **kw: True)

    mod.mode_kl_only(session_id="SID")
    assert upload_calls == [], \
        "kl-flushed event rows must not be uploaded as retrospectives"


def test_latest_session_log_uses_mtime_not_lexical_sort(project, monkeypatch):
    """Codex P2 2026-07-19 (Lead-Converter line 720): session-log filenames
    contain UUIDs, so reverse-lexical sort can pick an older, unrelated
    log. The helper must pick by mtime instead."""
    mod = _load_module(project)
    logs = project / ".ai" / "session-logs"
    # UUID-shaped names — lexically 'z…' > 'a…' but 'a…' is the newer file.
    older = logs / "session_zzzzzzzz-old.jsonl"
    newer = logs / "session_aaaaaaaa-new.jsonl"
    older.write_text("{}\n")
    newer.write_text("{}\n")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_500, 1_700_000_500))

    picked = mod._latest_session_log()
    assert picked == newer, \
        f"must select newest by mtime; got {picked!r}, expected {newer!r}"


def test_write_local_record_atomic_snapshot_failure_does_not_leak_jsonl(project, monkeypatch):
    """Codex P2 2026-07-19 line 533: if snapshot write fails after JSONL
    append, mode_stop() rolls back the mtime reservation and the next
    Stop for the unchanged transcript would append a SECOND JSONL row
    for the same capture. Fix: write snapshot FIRST, JSONL SECOND, so a
    snapshot failure never leaks a narrative row into the JSONL."""
    mod = _load_module(project)

    # Force the exclusive snapshot open to fail. Path via os.open lets us
    # simulate ENOSPC / EACCES cleanly with a monkeypatched raiser.
    real_open = os.open
    def boom_open(path, flags, *a, **kw):
        # Fail only for retrospective JSON files, not for other opens the
        # capture path may do (e.g. the lock file).
        if str(path).endswith(".json") and ".retrospectives" not in str(path):
            raise OSError("simulated ENOSPC")
        return real_open(path, flags, *a, **kw)
    monkeypatch.setattr(mod.os, "open", boom_open)

    record = {
        "session_id": "SID-atomic",
        "branch": "main",
        "captured_at": "2026-07-19T00:00:00Z",
        "dysfunction_score": 0.1,
        "failure_class": "unclassified",
        "transcript": "/tmp/nope.jsonl",
    }
    with pytest.raises(OSError):
        mod._write_local_record(record)

    # The JSONL must NOT contain a leaked row, otherwise a retry would
    # produce a duplicate on next Stop.
    jsonl = project / ".ai" / "memory" / "retrospectives" / "session-retrospectives.jsonl"
    assert not jsonl.exists() or jsonl.read_text() == "", \
        f"snapshot failure must not leak a JSONL row: contents={jsonl.read_text() if jsonl.exists() else '<absent>'!r}"
