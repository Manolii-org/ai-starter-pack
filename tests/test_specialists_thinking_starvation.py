"""Regression test: proxied OSS models reason in-band and their `thinking`
blocks share the max_tokens budget (the LiteLLM route accepts but ignores
thinking.budget_tokens / thinking.disabled — verified live 2026-10-08). A
~1k-token chain-of-thought then exhausts a skill's declared 800-token budget
and returns stop_reason=max_tokens with an empty text block — every
specialist lane silently produced zero coverage.

_call_api must therefore add headroom on top of the declared answer budget
for proxied calls and retry once at a doubled budget when the response comes
back starved (no text block + stop_reason=max_tokens). Anthropic-direct
calls keep the declared budget verbatim but retry an empty response once —
transient empty output is cheaper to re-ask than to let collapse a
specialist into an api_error skip marker (observed 2026-10-09: an
end_turn empty text block on the direct lane silently skipped
docs-fact-check).
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_specialists", ROOT / "scripts/run-specialists.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _FakeResponse(io.BytesIO):
    """BytesIO with the context-manager surface urllib responses have."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _api_payload(content_blocks: list[dict], stop_reason: str = "end_turn") -> bytes:
    return json.dumps(
        {
            "content": content_blocks,
            "stop_reason": stop_reason,
        }
    ).encode("utf-8")


_THINKING_ONLY = _api_payload(
    [{"type": "thinking", "thinking": "…"}, {"type": "text", "text": ""}],
    stop_reason="max_tokens",
)
_ANSWER = _api_payload(
    [{"type": "thinking", "thinking": "…"}, {"type": "text", "text": '{"source": "x", "findings": []}'}],
)


def _patch_endpoint(monkeypatch, proxied: bool):
    if proxied:
        endpoint = ("proxy-key", "https://proxy.example/v1/messages", True)
    else:
        endpoint = ("sk-ant-direct", "https://api.anthropic.com/v1/messages", False)
    monkeypatch.setattr(MODULE, "_endpoint", lambda direct=False: endpoint)


def _fake_urlopen(monkeypatch, responses: list[bytes]) -> list[dict]:
    """Fake _urlopen_https; returns a capture list of the sent payloads."""
    sent: list[dict] = []

    def fake(req, timeout=None, host=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse(responses[len(sent) - 1])

    monkeypatch.setattr(MODULE, "_urlopen_https", fake)
    return sent


def test_proxied_call_gets_thinking_headroom(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=True)
    sent = _fake_urlopen(monkeypatch, [_ANSWER])
    out = MODULE._call_api("sys", "user", "haiku", 800)
    assert json.loads(out)["findings"] == []
    assert len(sent) == 1
    # Declared 800 + proxy headroom — the declared answer budget must survive
    # alongside in-band reasoning.
    assert sent[0]["max_tokens"] == 800 + MODULE._PROXY_THINKING_HEADROOM


def test_starved_answer_retries_with_doubled_budget(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=True)
    sent = _fake_urlopen(monkeypatch, [_THINKING_ONLY, _ANSWER])
    out = MODULE._call_api("sys", "user", "haiku", 800)
    assert json.loads(out)["findings"] == []
    assert len(sent) == 2
    first_budget = 800 + MODULE._PROXY_THINKING_HEADROOM
    assert sent[0]["max_tokens"] == first_budget
    assert sent[1]["max_tokens"] == first_budget * 2


def test_double_starvation_returns_empty_without_third_call(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=True)
    sent = _fake_urlopen(monkeypatch, [_THINKING_ONLY, _THINKING_ONLY])
    assert MODULE._call_api("sys", "user", "haiku", 800) == ""
    assert len(sent) == 2


def test_non_max_tokens_empty_text_is_not_retried(monkeypatch):
    """An empty answer with stop_reason=end_turn is a real empty answer, not
    starvation — no retry."""
    _patch_endpoint(monkeypatch, proxied=True)
    empty_end_turn = _api_payload([{"type": "text", "text": ""}], stop_reason="end_turn")
    sent = _fake_urlopen(monkeypatch, [empty_end_turn])
    assert MODULE._call_api("sys", "user", "haiku", 800) == ""
    assert len(sent) == 1


def test_direct_call_keeps_declared_budget(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=False)
    sent = _fake_urlopen(monkeypatch, [_ANSWER])
    assert MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800) != ""
    assert len(sent) == 1
    assert sent[0]["max_tokens"] == 800
    assert sent[0]["model"] == "claude-haiku-4-5-20251001"


def test_direct_empty_response_retries_once_at_same_budget(monkeypatch):
    """A direct call returning no text block retries exactly once — transient
    empty output must not write an api_error marker on a single flake."""
    _patch_endpoint(monkeypatch, proxied=False)
    empty_end_turn = _api_payload([{"type": "text", "text": ""}], stop_reason="end_turn")
    sent = _fake_urlopen(monkeypatch, [empty_end_turn, _ANSWER])
    out = MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800)
    assert json.loads(out)["findings"] == []
    assert len(sent) == 2
    assert sent[0]["max_tokens"] == sent[1]["max_tokens"] == 800


def test_direct_persistent_empty_returns_empty_after_retry(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=False)
    empty_end_turn = _api_payload([{"type": "text", "text": ""}], stop_reason="end_turn")
    sent = _fake_urlopen(monkeypatch, [empty_end_turn, empty_end_turn])
    assert MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800) == ""
    assert len(sent) == 2
