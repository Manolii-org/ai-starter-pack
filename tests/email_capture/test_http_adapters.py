"""HTTP adapter request/response contract tests."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from email_capture.core import CaptureError, HttpBackend, Profile


class ReceiverHandler(BaseHTTPRequestHandler):
    backend = "mailpit"
    recipient = "allocated@capture.test"
    requests = []

    def log_message(self, *_args):
        return

    def _send(self, value, content_type="application/json"):
        body = json.dumps(value).encode() if content_type == "application/json" else value.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.requests.append(("GET", self.path, None))
        if self.backend == "mailpit":
            if self.path == "/api/v1/info": return self._send({"version": "test"})
            if self.path.startswith("/api/v1/messages?"): return self._send({"messages": [{"ID": "m1", "To": [{"Address": self.recipient}], "Created": "2026-08-21T00:00:00Z"}]})
            if self.path.endswith("/headers"): return self._send({"X-Test": ["yes"]})
            return self._send({"ID": "m1", "Date": "1999-01-01T00:00:00Z", "From": {"Address": "sender@test"}, "To": [{"Address": self.recipient}], "Text": "Code 123456", "Attachments": []})
        if self.backend == "maildev":
            if self.path == "/healthz": return self._send(True)
            row = {"id": "d1", "time": "2026-08-21T00:00:00Z", "from": [{"address": "sender@test"}], "to": [{"address": self.recipient}], "text": "Code 123456", "headers": {}}
            return self._send([row] if self.path.startswith("/email?") else row)
        if self.path.endswith("email-capture-healthcheck"): return self._send([])
        summary = {"id": "i1", "date": "2026-08-21T00:00:00Z", "from": "sender@test", "to": [self.recipient]}
        detail = {**summary, "body": {"text": "Code 123456", "html": ""}, "header": {"X-Test": ["yes"]}, "attachments": []}
        return self._send(detail if self.path.endswith("/i1") else [summary])

    def do_DELETE(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else None
        self.requests.append(("DELETE", self.path, body))
        self._send("OK")


class HttpAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ReceiverHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def setUp(self):
        ReceiverHandler.requests = []
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.allocation = {"recipient": ReceiverHandler.recipient}

    def test_all_http_adapters_fetch_details_and_delete_scoped_messages(self):
        for backend_name in ("mailpit", "maildev", "inbucket"):
            with self.subTest(backend=backend_name):
                ReceiverHandler.backend = backend_name
                adapter = HttpBackend(Profile("hermetic", backend_name, self.endpoint, "test"))
                self.assertTrue(adapter.health())
                messages = adapter.list(self.allocation)
                self.assertEqual(messages[0]["content"]["text"], "Code 123456")
                self.assertEqual(messages[0]["received_at"], "2026-08-21T00:00:00.000000Z")
                adapter.purge(self.allocation)
                deletes = [request for request in ReceiverHandler.requests if request[0] == "DELETE"]
                self.assertTrue(deletes)
                if backend_name == "mailpit":
                    self.assertEqual(deletes[-1][1:], ("/api/v1/messages", {"IDs": ["m1"]}))
                ReceiverHandler.requests = []

    def test_mailpit_non_object_detail_is_a_typed_infra_error(self):
        """A null/non-object mailpit detail body must fail closed as retryable infra.

        The mailpit branch assigned `detail["headers"]` before the shape check, so
        an empty or non-JSON-object detail response raised a bare TypeError. The
        CLI catches TypeError as CONFIG_INVALID (retryable=False), so a transient
        receiver fault was reported as a permanent configuration error.
        """
        adapter = HttpBackend(Profile("hermetic", "mailpit", self.endpoint, "test"))
        summaries = {"messages": [{"ID": "m1", "To": [{"Address": ReceiverHandler.recipient}]}]}

        # Every call after the listing returns an empty body. Unbounded on purpose:
        # a fixed sequence would run out on the pre-fix code (which evaluates the
        # /headers request before the failing subscript) and mask the real defect.
        def _request(path, *_a, **_k):
            return summaries if path.startswith("/api/v1/messages") else None

        adapter._request = _request
        with self.assertRaises(CaptureError) as caught:
            adapter.list(self.allocation)
        self.assertEqual(caught.exception.code, "CAPTURE_INFRA_UNAVAILABLE")
        self.assertTrue(caught.exception.as_dict()["retryable"])


if __name__ == "__main__":
    unittest.main()
