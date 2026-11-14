"""Actual guest-to-host Unix HTTP; synthetic provider boundary, no live login."""

import copy
from email.message import Message
import http.client
import io
import json
from pathlib import Path
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import codex_review_broker as broker  # noqa: E402
import codex_subscription_text as client  # noqa: E402
from codex_text_response import TextResponseError, text_completion  # noqa: E402


def completion():
    return {
        "object": "response",
        "status": "completed",
        "model": broker.REVIEW_MODEL,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": '{"verdict":"fixture"}'}],
            }
        ],
        "usage": {
            "input_tokens": 31,
            "output_tokens": 8,
            "input_tokens_details": {"cached_tokens": 12},
        },
    }


class Response(io.BytesIO):
    status = 200

    def __init__(self, value):
        super().__init__(json.dumps(value).encode())
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"


def auth():
    return broker.Subscription(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "fixture-host-access-only-token-123456789",
                "account_id": "fixture-host-account",
            },
        }
    )


@pytest.mark.parametrize("role", sorted(client.ROLES))
def test_real_guest_rpc_uses_only_fixed_host_subscription(tmp_path, monkeypatch, role):
    path = tmp_path / "model.sock"
    monkeypatch.setattr(client, "SOCKET", str(path))
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
        "DOPPLER_TOKEN",
    ):
        monkeypatch.setenv(name, "ambient-must-not-reach-model-or-guest-result")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://alternate.invalid")
    calls = []

    def provider(raw, headers, **_):
        calls.append(json.loads(raw))
        assert (
            headers["Authorization"]
            == "Bearer fixture-host-access-only-token-123456789"
        )
        assert headers["ChatGPT-Account-Id"] == "fixture-host-account"
        assert b"ambient-must-not-reach" not in raw
        return Response(completion())

    with broker.serve(
        path,
        auth(),
        transport=provider,
        text_only=True,
        expected_effort="medium",
        max_requests=1,
        max_seconds=5,
    ) as host:
        result = client.invoke(
            "Return JSON.",
            "Assess a synthetic fixture.",
            role=role,
            data_sensitivity="internal",
            seconds=3,
        )
        audit = host.audit()
        assert audit["requests"] == audit["successful_provider_turns"] == 1
        assert audit["text_completion"]["model_verification"] == "provider_reported"
        assert "text" not in audit["text_completion"]
    assert len(calls) == 1 and calls[0]["tools"] == []
    assert calls[0]["model"] == result["model"] == broker.REVIEW_MODEL
    assert result["text"] == '{"verdict":"fixture"}'
    assert result["usage"] == {
        "input_tokens": 31,
        "output_tokens": 8,
        "cached_input_tokens": 12,
    }
    assert result["billing_mode"] == "subscription" and result["cost_usd"] is None
    assert "fixture-host" not in json.dumps(result)


@pytest.mark.parametrize("change", ["model", "tool", "refusal", "usage", "incomplete"])
def test_host_refuses_bad_completions_without_replay(tmp_path, monkeypatch, change):
    path = tmp_path / "model.sock"
    monkeypatch.setattr(client, "SOCKET", str(path))
    value = completion()
    if change == "model":
        value["model"] = "alternate-model"
    elif change == "tool":
        value["output"].append({"type": "function_call"})
    elif change == "refusal":
        value["output"][0]["content"] = [
            {"type": "refusal", "refusal": "private-provider-detail"}
        ]
    elif change == "usage":
        value["usage"]["input_tokens"] = True
    else:
        value["status"] = "incomplete"
    calls = []

    def provider(*args, **kwargs):
        calls.append(1)
        return Response(value)

    with broker.serve(
        path, auth(), transport=provider, text_only=True, max_seconds=5
    ) as host:
        with pytest.raises(
            client.SubscriptionTextError, match="subscription_text_unavailable"
        ):
            client.invoke(
                "JSON", "fixture", role="triage", data_sensitivity="internal", seconds=3
            )
        assert host.audit()["successful_provider_turns"] == 0
        assert host.audit()["text_completion"] is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "role,sensitivity",
    [
        ("implementation", "internal"),
        ("future", "public"),
        ("assessment", "restricted"),
        ("judge", "restricted_us_oss_ok"),
        ([], "internal"),
        ("critic", []),
    ],
)
def test_scope_refusal_precedes_every_connection(monkeypatch, role, sensitivity):
    def refused(*args, **kwargs):
        raise AssertionError("must not start transport")

    monkeypatch.setattr(client, "_BrokerConnection", refused)
    with pytest.raises(client.SubscriptionTextError, match="scope_refused"):
        client.invoke("JSON", "data", role=role, data_sensitivity=sensitivity)


def test_unavailable_socket_has_no_network_or_retry_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("no TCP transport")
    )
    with pytest.raises(client.SubscriptionTextError, match="transport_unknown"):
        client.invoke(
            "JSON", "data", role="critic", data_sensitivity="public", seconds=1
        )


def test_sse_final_response_or_done_items_are_not_duplicated():
    value = completion()
    done = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": value["output"][0],
    }
    for omit in (False, True):
        final = copy.deepcopy(value)
        if omit:
            del final["output"]
        stream = (
            b"\n\n".join(
                b"data: " + json.dumps(e).encode()
                for e in (
                    done,
                    {"type": "response.completed", "response": final},
                )
            )
            + b"\n\ndata: [DONE]\n\n"
        )
        assert (
            text_completion(stream, "text/event-stream", broker.REVIEW_MODEL)["text"]
            == '{"verdict":"fixture"}'
        )


@pytest.mark.parametrize(
    "mutation", ["no_final", "duplicate_final", "duplicate_index", "tool_delta"]
)
def test_sse_never_promotes_ambiguous_or_partial_output(mutation):
    value = completion()
    done = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": value["output"][0],
    }
    final = {"type": "response.completed", "response": value}
    events = {
        "no_final": [done],
        "duplicate_final": [final, final],
        "duplicate_index": [done, done, final],
        "tool_delta": [
            {"type": "response.function_call_arguments.delta", "delta": "hidden"},
            final,
        ],
    }[mutation]
    stream = b"\n\n".join(b"data: " + json.dumps(e).encode() for e in events)
    with pytest.raises(TextResponseError):
        text_completion(stream, "text/event-stream", broker.REVIEW_MODEL)


def test_text_mode_cannot_admit_review_tools_before_reading_auth(tmp_path):
    path = tmp_path / "model.sock"
    subscription = auth()
    subscription.headers = lambda: pytest.fail("must not read upstream auth")
    payload = {
        "model": broker.REVIEW_MODEL,
        "store": False,
        "stream": True,
        "reasoning": {"effort": "medium"},
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "data"}]}
        ],
        "tools": [
            {"type": "function", "name": "exec", "parameters": {"type": "object"}}
        ],
    }

    class Unix(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(3)
            self.sock.connect(str(path))

    with broker.serve(path, subscription, text_only=True, max_seconds=5) as host:
        # The very same valid request is admissible to the legacy review
        # protocol; its rejection must come from the new text-only boundary.
        assert json.loads(broker.review_request(json.dumps(payload).encode(), host))[
            "tools"
        ]
        connection = Unix("unused")
        try:
            connection.request(
                "POST",
                "/v1/responses",
                json.dumps(payload),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 400
            response.read()
        finally:
            connection.close()
        assert host.audit()["requests"] == 0


@pytest.mark.parametrize(
    "change",
    ["missing_model", "missing_usage", "negative_usage", "invalid_cache", "no_output"],
)
def test_provider_metadata_cannot_be_inferred_from_selection(change):
    value = completion()
    if change == "missing_model":
        del value["model"]
    elif change == "missing_usage":
        del value["usage"]
    elif change == "negative_usage":
        value["usage"]["output_tokens"] = -1
    elif change == "invalid_cache":
        value["usage"]["input_tokens_details"]["cached_tokens"] = 32
    else:
        value["output"] = []
    with pytest.raises(TextResponseError):
        text_completion(
            json.dumps(value).encode(), "application/json", broker.REVIEW_MODEL
        )


@pytest.mark.parametrize(
    "option,value",
    [
        ("max_tokens", True),
        ("max_tokens", 32001),
        ("seconds", 0),
        ("seconds", 301),
        ("reasoning_effort", "low"),
        ("reasoning_effort", []),
    ],
)
def test_invalid_budget_never_opens_the_broker(monkeypatch, option, value):
    def refused(*args, **kwargs):
        raise AssertionError("must not start transport")

    monkeypatch.setattr(client, "_BrokerConnection", refused)
    with pytest.raises(client.SubscriptionTextError, match="budget_invalid"):
        client.invoke(
            "JSON", "data", role="judge", data_sensitivity="internal", **{option: value}
        )


def test_quota_failure_never_replays_the_physical_call(tmp_path, monkeypatch):
    path = tmp_path / "model.sock"
    monkeypatch.setattr(client, "SOCKET", str(path))
    calls = []

    def quota(*args, **kwargs):
        calls.append(1)
        raise broker.BrokerError("subscription_quota_exhausted")

    with broker.serve(
        path, auth(), transport=quota, text_only=True, max_seconds=5
    ) as host:
        with pytest.raises(client.SubscriptionTextError, match="unavailable"):
            client.invoke(
                "JSON", "data", role="triage", data_sensitivity="internal", seconds=3
            )
        assert host.audit()["successful_provider_turns"] == 0
    assert len(calls) == 1
