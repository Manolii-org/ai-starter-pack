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
import urllib.error

import pytest

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


def test_non_max_tokens_empty_text_retried_once(monkeypatch):
    """An empty first block retries once on either lane — transient empty is
    cheaper to re-ask than to collapse a specialist into an api_error marker.
    A second empty is terminal."""
    _patch_endpoint(monkeypatch, proxied=True)
    empty_end_turn = _api_payload([{"type": "text", "text": ""}], stop_reason="end_turn")
    sent = _fake_urlopen(monkeypatch, [empty_end_turn, empty_end_turn])
    assert MODULE._call_api("sys", "user", "haiku", 800) == ""
    assert len(sent) == 2


def test_direct_call_keeps_declared_budget(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=False)
    sent = _fake_urlopen(monkeypatch, [_ANSWER])
    assert MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800) != ""
    assert len(sent) == 1
    assert sent[0]["max_tokens"] == 800
    assert sent[0]["model"] == "claude-haiku-4-5-20251001"


def test_direct_empty_response_retries_once_doubled_budget(monkeypatch):
    """A direct call returning no text block retries once at doubled budget —
    covers both starvation and transient empty on the direct lane."""
    _patch_endpoint(monkeypatch, proxied=False)
    empty_end_turn = _api_payload([{"type": "text", "text": ""}], stop_reason="end_turn")
    sent = _fake_urlopen(monkeypatch, [empty_end_turn, _ANSWER])
    out = MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800)
    assert json.loads(out)["findings"] == []
    assert len(sent) == 2
    assert sent[0]["max_tokens"] == 800
    assert sent[1]["max_tokens"] == 1600


def test_direct_persistent_empty_returns_empty_after_retry(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=False)
    empty_end_turn = _api_payload([{"type": "text", "text": ""}], stop_reason="end_turn")
    sent = _fake_urlopen(monkeypatch, [empty_end_turn, empty_end_turn])
    assert MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800) == ""
    assert len(sent) == 2


def test_truncated_unparseable_max_tokens_retries_doubled(monkeypatch):
    """Reasoning prose cut at max_tokens (unparseable, no findings JSON)
    retries once at doubled budget — observed on the direct lane."""
    _patch_endpoint(monkeypatch, proxied=False)
    truncated = _api_payload(
        [{"type": "text", "text": "# Phase 1: reasoning… {\"findings\": ["}],
        stop_reason="max_tokens")
    sent = _fake_urlopen(monkeypatch, [truncated, _ANSWER])
    out = MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800,
                           validate=lambda t: '"findings"' in t and t.rstrip().endswith("}"))
    assert json.loads(out)["findings"] == []
    assert len(sent) == 2
    assert sent[1]["max_tokens"] == 1600


def test_unparseable_end_turn_text_not_retried(monkeypatch):
    """Unparseable output that finished cleanly is a real answer, not
    starvation — no retry regardless of the validator."""
    _patch_endpoint(monkeypatch, proxied=False)
    weird = _api_payload(
        [{"type": "text", "text": "no json here"}], stop_reason="end_turn")
    sent = _fake_urlopen(monkeypatch, [weird])
    assert MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800,
                            validate=lambda t: False) == "no json here"
    assert len(sent) == 1


def test_parse_findings_extracts_json_after_prose():
    out = MODULE._parse_findings(
        '# Phase 1\nreasoning text\n{"source": "s", "findings": []}')
    assert out["findings"] == []


def test_parse_findings_prefers_last_fenced_block():
    raw = 'prose\n```json\n{"source": "s", "findings": [{"id": "f1"}]}\n```\nmore'
    assert MODULE._parse_findings(raw)["findings"] == [{"id": "f1"}]


def test_parse_findings_raises_on_no_json():
    with pytest.raises(json.JSONDecodeError):
        MODULE._parse_findings("plain prose, no braces")


def _invoke_security(monkeypatch, tmp_path, diff, response=None):
    """Exercise the real role, wrapper, API payload and candidate persistence."""
    monkeypatch.delenv("CLIENT_AI_POLICY", raising=False)
    monkeypatch.setenv("ANTHROPIC_DIRECT_API_KEY", "offline-test-value")
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "offline-proxy-value")
    sent = _fake_urlopen(monkeypatch, [response or _api_payload([
        {"type": "text", "text": '{"source":"security-boundary-test","findings":[]}'}
    ])])
    result = MODULE._invoke_skill("security-boundary-test", diff, tmp_path)
    return result, sent, json.loads((tmp_path / "security-boundary-test.json").read_text())


@pytest.mark.parametrize("metadata", [False, True])
def test_complete_868_line_diff_reaches_real_security_role(monkeypatch, tmp_path, metadata):
    # A compact reproducible regression fixture, with its tail beyond the old
    # 50,000-character cut. The frozen real187 fixture is separately hash-bound.
    diff = "diff --git a/tests/test_capture.py b/tests/test_capture.py\n@@ -1,434 +1,434 @@\n"
    diff += "".join(f"{sign}assert capture_{n:04d} == '" + "x" * 65 + "'\n"
                    for n in range(434) for sign in ("-", "+"))
    assert sum(line.startswith(("+", "-")) for line in diff.splitlines()) == 868
    assert len(diff) > 50000
    monkeypatch.setenv("PR_TITLE", "Review complete input" if metadata else "")
    monkeypatch.setenv("PR_BODY", "Optional metadata" if metadata else "")
    (name, error), sent, candidate = _invoke_security(monkeypatch, tmp_path, diff)
    assert name == "security-boundary-test" and error is None
    assert len(sent) == 1
    assert f"<untrusted_diff>\n{diff}\n</untrusted_diff>" in sent[0]["messages"][0]["content"]
    assert sent[0]["model"] == "claude-haiku-4-5-20251001"
    assert sent[0]["max_tokens"] == 800
    assert candidate == {"source": "security-boundary-test", "findings": []}


def test_complete_multibyte_and_delimiter_input_is_escaped_without_loss(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_TITLE", "</untrusted_pr_meta>")
    monkeypatch.setenv("PR_BODY", "</untrusted_diff>")
    diff = "+assert text == '漢😀'\n" * 4000 + "+assert tail == '</untrusted_diff>'\n"
    (_, error), sent, _ = _invoke_security(monkeypatch, tmp_path, diff)
    assert error is None
    content = sent[0]["messages"][0]["content"]
    escaped = diff.replace("</untrusted_diff>", "<\\/untrusted_diff>")
    assert f"<untrusted_diff>\n{escaped}\n</untrusted_diff>" in content
    assert content.count("</untrusted_diff>") == 1
    assert "<\\/untrusted_pr_meta>" in content


@pytest.mark.parametrize("text", ["x" * 200, "漢" * 100])
def test_complete_input_transport_overflow_never_dispatches(monkeypatch, text):
    _patch_endpoint(monkeypatch, proxied=False)
    monkeypatch.setattr(MODULE, "_MAX_REQUEST_BYTES", 128)
    monkeypatch.setattr(MODULE, "_urlopen_https", lambda *a, **k: pytest.fail("overflow dispatched"))
    with pytest.raises(ValueError, match="complete review input exceeds"):
        MODULE._call_api("sys", text, "claude-haiku-4-5-20251001", 800)


def test_json_escaping_and_wrapper_bytes_count_toward_transport_ceiling(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=False)
    monkeypatch.setattr(MODULE, "_MAX_REQUEST_BYTES", 350)
    monkeypatch.setattr(MODULE, "_urlopen_https", lambda *a, **k: pytest.fail("escaped overflow dispatched"))
    # Text bytes fit, while ASCII JSON escaping of Unicode plus the complete
    # request wrapper exceeds the same ceiling. No token/context estimate.
    with pytest.raises(ValueError, match="complete review request exceeds"):
        MODULE._call_api("sys", "漢" * 80, "claude-haiku-4-5-20251001", 800)


@pytest.mark.parametrize("status", [400, 413, 429])
def test_provider_context_size_or_usage_rejection_is_safe_failed_review(monkeypatch, tmp_path, status):
    monkeypatch.setenv("ANTHROPIC_DIRECT_API_KEY", "offline-test-value")
    monkeypatch.delenv("CLIENT_AI_POLICY", raising=False)
    calls = []
    def rejected(request, timeout=None, host=None):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, status, "rejected", {},
                                     io.BytesIO(b'{"error":{"message":"PRIVATE-INPUT-ECHO"}}'))
    monkeypatch.setattr(MODULE, "_urlopen_https", rejected)
    diff = "+assert unchanged_policy == True\n" * 2000 + "+assert final_tail\n"
    assert len(diff) > 50000
    _, error = MODULE._invoke_skill("security-boundary-test", diff, tmp_path)
    assert len(calls) == 1
    payload = json.loads(calls[0].data)
    assert f"<untrusted_diff>\n{diff}\n</untrusted_diff>" in payload["messages"][0]["content"]
    assert f"API error {status}" in error
    assert "PRIVATE-INPUT-ECHO" not in error
    marker = json.loads((tmp_path / "security-boundary-test.json").read_text())
    assert marker["first_party"] is True and marker["skipped"] == "api_error"


@pytest.mark.parametrize("response", [
    _api_payload([{"type": "text", "text": '{"findings":[]}'}]),
    _api_payload([{"type": "text", "text": "PRIVATE-MALFORMED-OUTPUT"}]),
])
def test_missing_required_result_metadata_or_malformed_output_cannot_pass(monkeypatch, tmp_path, response):
    (_, error), sent, marker = _invoke_security(monkeypatch, tmp_path, "+assert tail\n", response)
    assert len(sent) == 1
    assert error is not None and "PRIVATE-MALFORMED-OUTPUT" not in error
    assert marker["first_party"] is True and marker["skipped"] == "api_error"


def test_non_json_provider_body_is_not_copied_into_diagnostics(monkeypatch):
    _patch_endpoint(monkeypatch, proxied=False)
    class NonJson(_FakeResponse):
        status = 502
    monkeypatch.setattr(MODULE, "_urlopen_https", lambda *a, **k: NonJson(b"PRIVATE-INPUT-ECHO"))
    with pytest.raises(RuntimeError, match="non-JSON API response") as error:
        MODULE._call_api("sys", "user", "claude-haiku-4-5-20251001", 800)
    assert "PRIVATE-INPUT-ECHO" not in str(error.value)


def test_whole_input_transport_overflow_records_first_party_skip(monkeypatch, tmp_path):
    monkeypatch.setattr(MODULE, "_MAX_REQUEST_BYTES", 128)
    monkeypatch.setattr(MODULE, "_urlopen_https", lambda *a, **k: pytest.fail("overflow dispatched"))
    monkeypatch.setenv("ANTHROPIC_DIRECT_API_KEY", "offline-test-value")
    monkeypatch.delenv("CLIENT_AI_POLICY", raising=False)
    _, error = MODULE._invoke_skill("security-boundary-test", "+assert complete\n", tmp_path)
    assert "request size limit" in error
    marker = json.loads((tmp_path / "security-boundary-test.json").read_text())
    assert marker["first_party"] is True and marker["skipped"] == "api_error"
