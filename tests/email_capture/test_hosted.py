"""Hosted capture profile, HTTP adapter, and canary CLI tests."""
import json
import secrets
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from email_capture.core import CaptureError, HostedHttpBackend, Profile, backend


class HostedHandler(BaseHTTPRequestHandler):
    recipient = "ec-test@capture.test"
    token = ""
    requests = []
    deleted = []

    def log_message(self, *_args):
        return

    def _send(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.requests.append(("GET", self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send(401, {"error": "denied"})
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path.startswith("/messages?"):
            return self._send(200, {"messages": [{"id": "h1", "to": [self.recipient], "received_at": "2026-08-25T00:00:00Z"}]})
        if self.path.endswith("/h1"):
            return self._send(200, {
                "id": "h1",
                "date": "1999-01-01T00:00:00Z",
                "from": "sender@test",
                "to": [self.recipient],
                "subject": "secret",
                "text": "Code 654321",
                "html": "<p>ok</p>",
                "headers": {"X-Manolii-Email-Capture": ["1"]},
                "attachments": [],
            })
        return self._send(404, {"error": "missing"})

    def do_DELETE(self):
        self.requests.append(("DELETE", self.path, self.headers.get("Authorization")))
        self.deleted.append(self.path)
        self._send(200, {"ok": True})


class HostedCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HostedHandler.token = secrets.token_hex(16)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), HostedHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def setUp(self):
        HostedHandler.requests = []
        HostedHandler.deleted = []
        self.old_env = dict(os.environ)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["EMAIL_CAPTURE_HOSTED_TOKEN"] = HostedHandler.token

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_hosted_profile_allows_test_loopback(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({
                "schema_version": "1.0",
                "mode": "hosted",
                "backend": "hosted",
                "endpoint": self.endpoint,
                "environment": "test",
                "fidelity": "provider-to-capture",
            }, handle)
            path = handle.name
        try:
            loaded = Profile.load(path)
        finally:
            os.unlink(path)
        self.assertEqual((loaded.mode, loaded.backend), ("hosted", "hosted"))

    def test_credentials_in_profile_json_fail_closed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({
                "schema_version": "1.0",
                "mode": "hosted",
                "backend": "hosted",
                "endpoint": "https://capture.example.test",
                "environment": "staging",
                "token": "leaked",
            }, handle)
            path = handle.name
        try:
            with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
                Profile.load(path)
        finally:
            os.unlink(path)

    def test_production_and_cleartext_staging_are_forbidden(self):
        os.environ.update(
            EMAIL_CAPTURE_MODE="hosted",
            EMAIL_CAPTURE_BACKEND="hosted",
            EMAIL_CAPTURE_ENDPOINT="https://capture.example.test",
            EMAIL_CAPTURE_ENVIRONMENT="production",
            EMAIL_CAPTURE_HOSTED_TOKEN=HostedHandler.token,
        )
        with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
            Profile.load()
        os.environ["EMAIL_CAPTURE_ENVIRONMENT"] = "staging"
        os.environ["EMAIL_CAPTURE_ENDPOINT"] = self.endpoint
        with self.assertRaisesRegex(CaptureError, "AUTHORIZATION_DENIED"):
            Profile.load()
        os.environ["EMAIL_CAPTURE_MODE"] = "hermetic"
        os.environ["EMAIL_CAPTURE_BACKEND"] = "memory"
        os.environ["EMAIL_CAPTURE_ENVIRONMENT"] = "staging"
        with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
            Profile.load()

    def test_adapter_lists_details_and_scoped_delete(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        self.assertTrue(adapter.health())
        messages = adapter.list({"recipient": HostedHandler.recipient})
        self.assertEqual(messages[0]["content"]["text"], "Code 654321")
        self.assertEqual(messages[0]["received_at"], "2026-08-25T00:00:00Z")
        adapter.purge({"recipient": HostedHandler.recipient})
        self.assertEqual(HostedHandler.deleted, ["/messages/h1"])
        self.assertTrue(all(auth == f"Bearer {HostedHandler.token}" for _method, _path, auth in HostedHandler.requests))
        self.assertEqual(backend(Profile("hosted", "hosted", self.endpoint, "test")).capabilities()["fidelity"], "provider-to-capture")

    def test_canary_cli_emits_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp:
            profile_path = Path(temp) / "profile.json"
            profile_path.write_text(json.dumps({
                "schema_version": "1.0",
                "mode": "hosted",
                "backend": "hosted",
                "endpoint": self.endpoint,
                "environment": "test",
            }))
            repo_root = Path(__file__).resolve().parents[2]
            run = subprocess.run(
                [str(repo_root / "bin/email-capture"), "--profile", str(profile_path), "canary"],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo_root,
                env={**os.environ, "EMAIL_CAPTURE_HOSTED_TOKEN": HostedHandler.token},
            )
            payload = json.loads(run.stdout)
            self.assertEqual(payload["operation"], "canary")
            self.assertEqual(payload["result"], "passed")
            self.assertNotIn("secret", run.stdout)
            self.assertNotIn(HostedHandler.token, run.stdout)


if __name__ == "__main__":
    unittest.main()
