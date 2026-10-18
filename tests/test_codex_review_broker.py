"""Exercise the real Unix HTTP broker; never fetch a real credential."""

import base64
from email.message import Message
import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_review_broker", ROOT / "scripts/ci/codex_review_broker.py"
)
broker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker)
SYNTHETIC = "synthetic-upstream-only-credential-123456789"


class UnixClient(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(4)
        self.sock.connect(self.host)


class Response(io.BytesIO):
    status = 200

    def __init__(
        self,
        body=b'{"object":"response","status":"completed","output":[]}',
        headers=None,
    ):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Set-Cookie"] = "must-not-reach-client"
        for key, value in (headers or {}).items():
            self.headers[key] = value


def request(path, *, method="POST", url="/v1/responses", body=None, headers=None):
    client = UnixClient(str(path))
    try:
        client.request(
            method,
            url,
            body if body is not None else json.dumps({"model": broker.REVIEW_MODEL}),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
    except BrokenPipeError:
        # An early route/framing rejection may close before the body write.
        # Still require and assert its real HTTP response below.
        pass
    response = client.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    client.close()
    return result


def auth():
    return broker.Subscription(
        {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": SYNTHETIC, "account_id": "synthetic-account"},
        }
    )


def test_nonstandard_content_type_requires_completed_responses_stream(tmp_path):
    payload = {
        "type": "response.completed",
        "response": {"status": "completed", "output": []},
    }
    completed = (
        b"event: response.completed\ndata: " + json.dumps(payload).encode() + b"\n\n"
    )
    assert broker.response_content_type(completed, "text/plain") == "text/event-stream"
    for body in (
        b"OK",
        b"<html>error</html>",
        b'{"status":"completed"}',
        b'event: response.failed\ndata: {"type":"response.failed"}\n\n',
    ):
        with pytest.raises(broker.BrokerError):
            broker.response_content_type(body, "text/plain")

    def upstream(*args, **kwargs):
        return Response(completed, {"Content-Type": "text/plain"})

    # Replace the synthetic header instead of appending a duplicate MIME field.
    def actual(*args, **kwargs):
        response = upstream()
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = "text/plain"
        return response

    with broker.serve(tmp_path / "model.sock", auth(), transport=actual) as server:
        status, headers, _ = request(tmp_path / "model.sock")
        assert status == 200 and headers["Content-Type"] == "text/event-stream"
        assert server.successful_turns == 1


def test_model_is_pinned_and_refusal_never_repeats_upstream_spend(tmp_path):
    calls = []

    def upstream(*args, **kwargs):
        calls.append(1)
        raise broker.BrokerError("subscription_quota_exhausted")

    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=upstream):
        assert request(path, body=json.dumps({"model": "other-model"}))[0] == 400
        assert calls == []
        assert request(path)[0] == 429
        assert request(path)[0] == 400
        assert calls == [1]


def test_terminal_failure_is_checked_atomically_during_admission(tmp_path):
    path = tmp_path / "model.sock"
    with broker.serve(path, auth()) as server:
        server.fail("subscription_quota_exhausted")
        server.fail("subscription_upstream_refused")
        with pytest.raises(broker.BrokerError, match="subscription_quota_exhausted"):
            server.begin(object())
        assert server.calls == 0


@pytest.mark.parametrize(
    "body,headers",
    [(SYNTHETIC.encode(), None), (b"error", {"Content-Type": "text/html"})],
)
def test_withheld_or_malformed_provider_result_stops_future_calls(
    tmp_path, body, headers
):
    calls = []

    def upstream(*args, **kwargs):
        calls.append(1)
        response = Response(body)
        if headers:
            del response.headers["Content-Type"]
            response.headers["Content-Type"] = headers["Content-Type"]
        return response

    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=upstream):
        assert request(path)[0] == 502
        assert request(path)[0] == 400
        assert calls == [1]


def test_error_after_completed_stream_vetoes_completion():
    completed = b'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'
    for kind in ["response.failed", "response.incomplete", "error"]:
        body = (
            completed
            + b"event: "
            + kind.encode()
            + b"\ndata: "
            + json.dumps({"type": kind}).encode()
            + b"\n\n"
        )
        assert broker.completed_turn(body, "text/event-stream") == (False, "")
        with pytest.raises(broker.BrokerError):
            broker.response_content_type(body, "text/plain")


@pytest.mark.parametrize(
    "response",
    [
        "bad",
        {"status": "completed", "output": "bad"},
        {"status": "completed", "output": ["bad"]},
        {
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": "bad"}],
        },
    ],
)
def test_malformed_completion_shapes_fail_cleanly(response):
    body = (
        b"event: response.completed\ndata: "
        + json.dumps({"type": "response.completed", "response": response}).encode()
        + b"\n\n"
    )
    assert broker.completed_turn(body, "text/event-stream") == (False, "")
    with pytest.raises(broker.BrokerError):
        broker.response_content_type(body, "text/plain")


@pytest.mark.parametrize(
    "mime,body",
    [
        ("application/json", b'{"object":"response","status":"failed"}'),
        (
            "text/event-stream",
            b'event: response.incomplete\ndata: {"type":"response.incomplete"}\n\n',
        ),
        ("text/plain", b'event: response.failed\ndata: {"type":"response.failed"}\n\n'),
    ],
)
def test_terminal_or_partial_provider_stream_never_returns_success_or_repeats(
    tmp_path, mime, body
):
    calls = []

    def upstream(*args, **kwargs):
        calls.append(1)
        response = Response(body)
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = mime
        return response

    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=upstream) as server:
        assert request(path)[0] == 502
        assert request(path)[0] == 400
        assert server.successful_turns == 0
        assert calls == [1]


def test_only_host_adds_auth_and_never_reflects_headers(tmp_path):
    calls = []

    def upstream(body, headers, **_):
        calls.append((json.loads(body), headers))
        return Response()

    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=upstream) as server:
        status, headers, body = request(
            path,
            headers={
                "Authorization": "Bearer public-placeholder",
                "Host": "evil.example",
                "Cookie": "untrusted",
            },
        )
        assert status == 200 and SYNTHETIC.encode() not in body
        assert "Set-Cookie" not in headers
        assert calls[0][1]["Authorization"] == "Bearer " + SYNTHETIC
        assert "Cookie" not in calls[0][1] and "Host" not in calls[0][1]
        assert server.audit()["requests"] == 1
    assert not path.exists()


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "/v1/responses"),
        ("CONNECT", "evil.example:443"),
        ("POST", "https://evil.example/v1/responses"),
        ("POST", "/v1/responses?redirect=evil"),
        ("POST", "/oauth/token"),
        ("POST", "/v1/models"),
    ],
)
def test_unsupported_routes_never_contact_upstream(tmp_path, method, url):
    def forbidden(*_, **__):
        pytest.fail("untrusted route reached authenticated transport")

    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=forbidden):
        status, _, _ = request(path, method=method, url=url)
        assert status in {404, 405}


@pytest.mark.parametrize(
    "value",
    [
        SYNTHETIC,
        base64.b64encode(SYNTHETIC.encode()).decode(),
        SYNTHETIC.encode().hex(),
        SYNTHETIC[::-1],
        " ".join(SYNTHETIC),
    ],
)
def test_upstream_credential_reflection_is_withheld(tmp_path, value):
    path = tmp_path / "model.sock"
    with broker.serve(
        path, auth(), transport=lambda *_, **__: Response(value.encode())
    ):
        status, _, body = request(path)
        assert status == 502 and value.encode() not in body


@pytest.mark.parametrize("code", [301, 302, 401, 403, 429, 500])
def test_provider_errors_do_not_reflect_private_response(tmp_path, code):
    def upstream(*_, **__):
        raise urllib.error.HTTPError(
            broker.UPSTREAM,
            code,
            "private " + SYNTHETIC,
            Message(),
            io.BytesIO(SYNTHETIC.encode()),
        )

    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=upstream):
        status, _, body = request(path)
        assert status == (429 if code == 429 else 502)
        assert SYNTHETIC.encode() not in body and b"private" not in body


def test_request_budget_never_becomes_another_model_call(tmp_path):
    calls = []
    path = tmp_path / "model.sock"

    def upstream(*_, **__):
        calls.append(1)
        return Response()

    with broker.serve(path, auth(), transport=upstream, max_requests=1) as server:
        assert request(path)[0] == 200
        for _ in range(2):
            status, _, body = request(path)
            assert status == 400
            assert json.loads(body)["error"] == "review_request_budget_exhausted"
        assert server.terminal_error == "review_request_budget_exhausted"
    assert len(calls) == 1


def test_auth_redirects_have_no_followup_request():
    handler = broker.NoRedirect()
    assert (
        handler.redirect_request(
            None, None, 302, "redirect", {}, "https://evil.example"
        )
        is None
    )


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"{}", b'{"model":1}'])
def test_invalid_payload_fails_before_upstream(tmp_path, body):
    def forbidden(*_, **__):
        pytest.fail("invalid input reached authenticated transport")

    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=forbidden):
        assert request(path, body=body)[0] == 400


@pytest.mark.parametrize(
    "value",
    [
        base64.b64encode(SYNTHETIC.encode()).decode().rstrip("="),
        base64.urlsafe_b64encode(SYNTHETIC.encode()).decode().rstrip("="),
        "".join("\\u%04x" % ord(c) for c in SYNTHETIC),
    ],
)
def test_encoded_json_reflection_is_withheld(tmp_path, value):
    raw = ('{"text":"' + value + '"}').encode()
    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=lambda *_, **__: Response(raw)):
        assert request(path)[0] == 502


def test_sse_split_unicode_reflection_is_withheld():
    chunks = [SYNTHETIC[:20], SYNTHETIC[20:]]
    raw = b": comment\n" + b"".join(
        (
            'data: {"delta":"' + "".join("\\u%04x" % ord(c) for c in chunk) + '"}\n\n'
        ).encode()
        for chunk in chunks
    )
    assert not auth().response_safe(raw)


@pytest.mark.parametrize(
    "framing",
    [
        b"Content-Length: 1\r\nContent-Length: 1\r\n",
        b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
        b"Content-Length: -1\r\n",
        b"Content-Length: 99999999\r\n",
        b"Content-Length: nonsense\r\n",
    ],
)
def test_invalid_framing_never_contacts_upstream(tmp_path, framing):
    path = tmp_path / "model.sock"

    def forbidden(*_, **__):
        pytest.fail("invalid framing reached upstream")

    with broker.serve(path, auth(), transport=forbidden):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(str(path))
            client.sendall(
                b"POST /v1/responses HTTP/1.0\r\nContent-Type: application/json\r\n"
                + framing
                + b"\r\n{}"
            )
            assert b" 400 " in client.recv(8192)


def test_output_limit_is_real(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "MAX_OUTPUT", 1024)
    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), transport=lambda *_, **__: Response(b"x" * 1025)):
        assert request(path)[0] == 502


def test_idle_connection_admission_and_absolute_parse_deadline(tmp_path):
    import time

    path = tmp_path / "model.sock"
    clients = []
    with broker.serve(path, auth(), max_connections=2, parse_seconds=0.15) as server:
        try:
            for _ in range(8):
                client = socket.socket(socket.AF_UNIX)
                client.settimeout(1)
                try:
                    client.connect(str(path))
                except BlockingIOError:
                    client.close()
                    continue
                clients.append(client)
                time.sleep(0.005)
            until = time.monotonic() + 1
            largest = 0
            while time.monotonic() < until:
                with server.lock:
                    largest = max(largest, len(server.connections))
                    remaining = len(server.connections)
                if largest and not remaining:
                    break
                time.sleep(0.01)
            assert largest <= 2
            assert not server.connections and server.calls == 0
        finally:
            for client in clients:
                client.close()


def test_slow_body_cannot_extend_parse_deadline(tmp_path):
    import time

    path = tmp_path / "model.sock"
    with broker.serve(path, auth(), parse_seconds=0.12) as server:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            client.connect(str(path))
            client.sendall(
                b"POST /v1/responses HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
            )
            started = time.monotonic()
            while time.monotonic() - started < 0.4:
                try:
                    client.sendall(b" ")
                except OSError:
                    break
                time.sleep(0.02)
            assert client.recv(8192) == b""
            assert time.monotonic() - started < 0.4 and server.calls == 0


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_real_provider_child_is_cancelled_at_absolute_deadline(
    tmp_path, monkeypatch, phase
):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import subprocess
    import threading
    import time

    stop = threading.Event()
    reached = threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_POST(self):
            reached.set()
            if phase == "headers":
                stop.wait(3)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while not stop.wait(0.01):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    upstream.daemon_threads = True
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    original = subprocess.Popen
    children = []

    def local_child(args, **kwargs):
        # Exercise the real provider code against a controlled local endpoint.
        # No endpoint override exists in the production CLI or broker protocol.
        code = (
            "import runpy;d=runpy.run_path(%r);f=d['provider_child'];f.__globals__['UPSTREAM']=%r;f()"
            % (
                str(ROOT / "scripts/ci/codex_review_broker.py"),
                "http://127.0.0.1:%d/responses" % upstream.server_port,
            )
        )
        kwargs["env"] = {"NO_PROXY": "127.0.0.1"}
        child = original([args[0], "-I", "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(broker.subprocess, "Popen", local_child)
    path = tmp_path / "model.sock"
    started = time.monotonic()
    try:
        with broker.serve(path, auth(), turn_seconds=0.3):
            try:
                assert request(path)[0] == 502
            except (
                ConnectionError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
            ):
                # The absolute watchdog may close the client before the error.
                pass
        assert reached.is_set()
        assert time.monotonic() - started < 1.5
        assert children and all(child.poll() is not None for child in children)
    finally:
        stop.set()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


def test_unicode_json_keys_are_checked():
    escaped = "".join("\\u%04x" % ord(c) for c in SYNTHETIC)
    assert not auth().response_safe(('{"' + escaped + '":true}').encode())


def test_realistic_sse_metadata_cannot_interrupt_delta_filter():
    frames = []
    for fragment in (SYNTHETIC[:20], SYNTHETIC[20:]):
        escaped = "".join("\\u%04x" % ord(c) for c in fragment)
        frames.append(
            (
                'data: {"type":"response.output_text.delta","item_id":"msg_1","delta":"'
                + escaped
                + '"}\n\n'
            ).encode()
        )
    assert not auth().response_safe(b"".join(frames))


def test_host_records_only_actual_completed_provider_turns(tmp_path, monkeypatch):
    observed = []
    original = broker.BrokerHandler.end_headers

    def before_client_observes_success(handler):
        observed.append(handler.server.audit()["successful_provider_turns"])
        return original(handler)

    monkeypatch.setattr(broker.BrokerHandler, "end_headers", before_client_observes_success)
    response = {
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Synthetic final review"}],
            }
        ],
    }
    path = tmp_path / "model.sock"
    with broker.serve(
        path, auth(), transport=lambda *_, **__: Response(json.dumps(response).encode())
    ) as server:
        assert request(path)[0] == 200
        assert server.audit()["successful_provider_turns"] == 1
        assert server.last_verdict == "Synthetic final review"
    with broker.serve(
        path, auth(), transport=lambda *_, **__: Response(b'{"status":"failed"}')
    ) as server:
        assert request(path)[0] == 502
        assert (
            server.audit()["successful_provider_turns"] == 0 and not server.last_verdict
        )
    assert observed == [1, 0]
