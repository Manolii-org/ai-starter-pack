"""Hosted capture profile, HTTP adapter, and canary CLI tests."""
import json
import secrets
import os
import subprocess
import tempfile
import time
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from email_capture.core import CaptureError, HostedHttpBackend, Profile, _read_bounded_http_body, await_messages, backend


class HostedHandler(BaseHTTPRequestHandler):
    recipient = "ec-test@capture.test"
    token = ""
    requests = []
    deleted = []
    health_payload = {"ok": True}
    health_oversize = False
    list_payload = None
    detail_override = None
    delete_empty = False
    health_raw = None
    list_delay = 0

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
            if self.health_raw is not None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(self.health_raw)))
                self.end_headers()
                self.wfile.write(self.health_raw)
                return
            if self.health_oversize:
                blob = b"x" * (2 * 1024 * 1024 + 64)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            return self._send(200, self.health_payload)
        if self.path.startswith("/messages?"):
            if self.list_delay:
                time.sleep(self.list_delay)
            payload = self.list_payload if self.list_payload is not None else {"messages": [{"id": "h1", "to": [self.recipient], "received_at": "2026-08-25T00:00:00Z"}]}
            return self._send(200, payload)
        if self.path.endswith("/h1"):
            if self.detail_override is not None:
                return self._send(200, self.detail_override)
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
        if self.delete_empty:
            self.send_response(204)
            self.end_headers()
            return
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
        HostedHandler.health_payload = {"ok": True}
        HostedHandler.health_oversize = False
        HostedHandler.list_payload = None
        HostedHandler.detail_override = None
        HostedHandler.delete_empty = False
        HostedHandler.health_raw = None
        HostedHandler.list_delay = 0
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


    def test_health_requires_explicit_ok_true(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        for payload in ({}, [], {"ok": 0}, {"ok": False}):
            HostedHandler.health_payload = payload
            with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
                adapter.health()
        HostedHandler.health_payload = {"ok": True}
        self.assertTrue(adapter.health())

    def test_hosted_response_body_is_bounded(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.health_oversize = True
        with self.assertRaisesRegex(CaptureError, "size limit"):
            adapter.health()


    def test_list_requires_messages_field(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {}
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_detail_must_match_allocation(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "other",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "to": ["other@capture.test"],
            "subject": "leaked",
            "text": "secret-from-other-allocation",
            "html": "",
            "headers": {},
            "attachments": [],
        }
        with self.assertRaisesRegex(CaptureError, "AUTHORIZATION_DENIED"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_delete_accepts_empty_204(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.delete_empty = True
        adapter.purge({"recipient": HostedHandler.recipient})
        self.assertEqual(HostedHandler.deleted, ["/messages/h1"])


    def test_env_style_token_key_in_profile_json_is_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({
                "schema_version": "1.0",
                "mode": "hosted",
                "backend": "hosted",
                "endpoint": self.endpoint,
                "environment": "test",
                "EMAIL_CAPTURE_HOSTED_TOKEN": HostedHandler.token,
            }, handle)
            path = handle.name
        try:
            with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
                Profile.load(path)
        finally:
            os.unlink(path)

    def test_malformed_detail_body_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "to": [HostedHandler.recipient],
            "subject": "secret",
            "text": [],
            "html": "",
            "headers": {},
            "attachments": [],
        }
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_purge_revalidates_detail_before_delete(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "to": ["other@capture.test"],
            "subject": "leaked",
            "text": "nope",
            "html": "",
            "headers": {},
            "attachments": [],
        }
        with self.assertRaisesRegex(CaptureError, "AUTHORIZATION_DENIED"):
            adapter.purge({"recipient": HostedHandler.recipient})
        self.assertEqual(HostedHandler.deleted, [])


    def test_unknown_and_secret_profile_keys_are_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({
                "schema_version": "1.0",
                "mode": "hosted",
                "backend": "hosted",
                "endpoint": self.endpoint,
                "environment": "test",
                "password": "nope",
            }, handle)
            path = handle.name
        try:
            with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
                Profile.load(path)
        finally:
            os.unlink(path)

    def test_malformed_summary_to_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {"messages": [{"id": "h1", "received_at": "2026-08-25T00:00:00Z"}]}
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_non_string_subject_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "to": [HostedHandler.recipient],
            "subject": [],
            "text": "ok",
            "html": "",
            "headers": {},
            "attachments": [],
        }
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_null_summary_to_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {"messages": [{"id": "h1", "to": None, "received_at": "2026-08-25T00:00:00Z"}]}
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_purge_skips_shared_multi_recipient_messages(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        shared = [HostedHandler.recipient, "other@capture.test"]
        HostedHandler.list_payload = {"messages": [{"id": "h1", "to": shared, "received_at": "2026-08-25T00:00:00Z"}]}
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "to": shared,
            "cc": [],
            "subject": "shared",
            "text": "keep",
            "html": "",
            "headers": {},
            "attachments": [],
        }
        adapter.purge({"recipient": HostedHandler.recipient})
        self.assertEqual(HostedHandler.deleted, [])

    def test_hosted_schema_rejects_production_and_empty_endpoint(self):
        schema = json.loads((Path(__file__).resolve().parents[2] / "schemas/email-capture/profile.schema.json").read_text())
        hosted = next(block["then"] for block in schema["allOf"] if block.get("if", {}).get("properties", {}).get("mode") == {"const": "hosted"})
        self.assertEqual(hosted["properties"]["environment"]["enum"], ["test", "ci", "preview", "preprod", "staging"])
        self.assertFalse(any(block.get("if", {}).get("properties", {}).get("backend") == {"const": "hosted"} for block in schema["allOf"]))
        nested = hosted["allOf"]
        test_pattern = nested[0]["then"]["properties"]["endpoint"]["pattern"]
        staging_pattern = nested[1]["then"]["properties"]["endpoint"]["pattern"]
        self.assertIn("http://", test_pattern)
        self.assertNotIn("http://", staging_pattern)
        import re
        self.assertIsNone(re.fullmatch(staging_pattern, "https://:443"))
        self.assertIsNone(re.fullmatch(staging_pattern, "https://."))
        self.assertIsNone(re.fullmatch(staging_pattern, "https://example..com"))
        self.assertIsNone(re.fullmatch(staging_pattern, "https://capture.example:99999"))
        self.assertIsNone(re.fullmatch(staging_pattern, "https://user:pass@host.example"))
        self.assertIsNotNone(re.fullmatch(staging_pattern, "https://capture.example.test"))
        self.assertIn("environment", hosted["required"])

    def test_off_mode_hosted_backend_loads_without_endpoint(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({
                "schema_version": "1.0",
                "mode": "off",
                "backend": "hosted",
                "environment": "production",
            }, handle)
            path = handle.name
        try:
            loaded = Profile.load(path)
        finally:
            os.unlink(path)
        self.assertEqual(loaded.mode, "off")

    def test_malformed_attachment_size_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "to": [HostedHandler.recipient],
            "subject": "ok",
            "text": "ok",
            "html": "",
            "headers": {},
            "attachments": [{"filename": "a.bin", "size": "unknown"}],
        }
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})


    def test_blank_summary_to_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {"messages": [{"id": "h1", "to": "", "received_at": "2026-08-25T00:00:00Z"}]}
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_malformed_cc_blocks_exclusive_delete(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "to": [HostedHandler.recipient],
            "cc": [{}],
            "subject": "ok",
            "text": "ok",
            "html": "",
            "headers": {},
            "attachments": [],
        }
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.purge({"recipient": HostedHandler.recipient})
        self.assertEqual(HostedHandler.deleted, [])

    def test_invalid_utf8_json_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.health_raw = b'{"ok": true, "x": "\xff"}'.replace(b"\\xff", b"\xff")
        HostedHandler.health_raw = b'{"ok":true}\xff'
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.health()

    def test_empty_or_angle_only_to_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        for value in ([], ["<>"]):
            HostedHandler.list_payload = {"messages": [{"id": "h1", "to": value, "received_at": "2026-08-25T00:00:00Z"}]}
            with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
                adapter.list({"recipient": HostedHandler.recipient})

    def test_unparseable_cc_blocks_delete(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "to": [HostedHandler.recipient],
            "cc": "other@example.com <",
            "subject": "ok",
            "text": "ok",
            "html": "",
            "headers": {},
            "attachments": [],
        }
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.purge({"recipient": HostedHandler.recipient})
        self.assertEqual(HostedHandler.deleted, [])

    def test_hosted_requires_explicit_environment(self):
        os.environ.pop("NODE_ENV", None)
        os.environ.pop("EMAIL_CAPTURE_ENVIRONMENT", None)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({
                "schema_version": "1.0",
                "mode": "hosted",
                "backend": "hosted",
                "endpoint": "https://capture.example.test",
            }, handle)
            path = handle.name
        try:
            with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
                Profile.load(path)
        finally:
            os.unlink(path)

    def test_reordered_to_aliases_are_the_same_set(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "To": [HostedHandler.recipient, "other@capture.test"],
            "to": ["other@capture.test", HostedHandler.recipient],
            "subject": "ok",
            "text": "ok",
            "html": "",
            "headers": {},
            "attachments": [],
        }
        messages = adapter.list({"recipient": HostedHandler.recipient})
        self.assertEqual(len(messages), 1)

    def test_zero_timeout_still_polls_once(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {"messages": []}
        with self.assertRaisesRegex(CaptureError, "MESSAGE_TIMEOUT"):
            await_messages(adapter, {"recipient": HostedHandler.recipient, "cursor": "0:"}, timeout=0)
        self.assertTrue(any(method == "GET" and path.startswith("/messages") for method, path, _auth in HostedHandler.requests))

    def test_offset_timestamps_normalize_to_utc(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {
            "messages": [{"id": "h1", "to": [HostedHandler.recipient], "received_at": "2026-08-25T00:30:00+01:00"}],
        }
        messages = adapter.list({"recipient": HostedHandler.recipient})
        self.assertEqual(messages[0]["received_at"], "2026-08-24T23:30:00Z")

    def test_hosted_body_read_respects_deadline(self):
        class Drip:
            def __init__(self):
                self.data = b'{"ok":true}'
                self.index = 0

            def read(self, _size):
                if self.index >= len(self.data):
                    return b""
                time.sleep(0.08)
                chunk = self.data[self.index:self.index + 1]
                self.index += 1
                return chunk

        with self.assertRaisesRegex(CaptureError, "MESSAGE_TIMEOUT"):
            _read_bounded_http_body(Drip(), deadline=time.monotonic() + 0.2)

    def test_nonsortable_summary_timestamp_is_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {"messages": [{"id": "h1", "to": [HostedHandler.recipient], "received_at": "zzz"}]}
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_positive_timeout_bounds_first_hosted_poll(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {"messages": []}
        HostedHandler.list_delay = 2
        started = time.monotonic()
        with self.assertRaises(CaptureError) as ctx:
            await_messages(adapter, {"recipient": HostedHandler.recipient, "cursor": "0:"}, timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn(ctx.exception.code, {"MESSAGE_TIMEOUT", "CAPTURE_INFRA_UNAVAILABLE"})

    def test_conflicting_to_aliases_are_infra_failure(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.detail_override = {
            "id": "h1",
            "date": "1999-01-01T00:00:00Z",
            "from": "sender@test",
            "To": [HostedHandler.recipient],
            "to": ["other@capture.test"],
            "subject": "ok",
            "text": "secret-for-other",
            "html": "",
            "headers": {},
            "attachments": [],
        }
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            adapter.list({"recipient": HostedHandler.recipient})

    def test_expired_await_deadline_stops_hosted_requests(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        adapter._deadline = 0
        with self.assertRaisesRegex(CaptureError, "MESSAGE_TIMEOUT"):
            adapter.health()

    def test_invalid_hosted_authority_is_config_error(self):
        os.environ.update(
            EMAIL_CAPTURE_MODE="hosted",
            EMAIL_CAPTURE_BACKEND="hosted",
            EMAIL_CAPTURE_ENVIRONMENT="staging",
            EMAIL_CAPTURE_HOSTED_TOKEN=HostedHandler.token,
        )
        for endpoint in ("https://example.com:abc", "https://.", "https://example..com", "https://exa mple.com"):
            os.environ["EMAIL_CAPTURE_ENDPOINT"] = endpoint
            with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
                Profile.load()

    def test_await_timeout_clears_deadline_for_later_ops(self):
        adapter = HostedHttpBackend(Profile("hosted", "hosted", self.endpoint, "test"))
        HostedHandler.list_payload = {"messages": []}
        with self.assertRaisesRegex(CaptureError, "MESSAGE_TIMEOUT"):
            await_messages(adapter, {"recipient": HostedHandler.recipient, "cursor": "0:"}, timeout=0.01)
        self.assertIsNone(getattr(adapter, "_deadline", "missing"))
        self.assertTrue(adapter.health())

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
