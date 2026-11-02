"""Native Resend receiving adapter, retained-mail lifecycle and inspection tests."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from email_capture import cli
from email_capture.core import CaptureError, Profile, ResendReceivingBackend, allocate, await_messages, backend


class Fake(BaseHTTPRequestHandler):
    emails: list = []
    pages_served = 0
    detail_requests: list = []
    throttle_first = 0
    status_override = 0
    infinite = False

    def log_message(self, *_a):
        return

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        raw and self.wfile.write(raw)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer qa-receiving-key":
            return self._send(401, {"message": "bad key"})
        if Fake.status_override:
            return self._send(Fake.status_override, {})
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/emails/receiving":
            if Fake.throttle_first > 0:
                Fake.throttle_first -= 1
                return self._send(429, {"message": "slow"}, {"Retry-After": "0.05"})
            Fake.pages_served += 1
            limit = int(query.get("limit", ["100"])[0])
            rows = Fake.emails
            if "after" in query:
                ids = [r["id"] for r in rows]
                rows = rows[ids.index(query["after"][0]) + 1:]
            page = rows[:limit]
            has_more = Fake.infinite or len(rows) > limit
            summaries = [{k: v for k, v in r.items() if k not in {"html", "text", "headers"}} for r in page]
            return self._send(200, {"object": "list", "has_more": has_more, "data": summaries})
        if url.path.startswith("/emails/receiving/"):
            identifier = url.path.rsplit("/", 1)[1]
            Fake.detail_requests.append(identifier)
            for row in Fake.emails:
                if row["id"] == identifier:
                    return self._send(200, {"object": "email", **row})
        return self._send(404, {})


def mail(identifier, to, created, text="Reset: https://app.example.test/auth/confirm?t=abc", html=None, subject="Reset"):
    return {"id": identifier, "to": to if isinstance(to, list) else [to], "from": "no-reply@example.test", "created_at": created, "subject": subject, "text": text, "html": html, "headers": {}, "attachments": []}


class ResendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Fake.emails, Fake.pages_served, Fake.detail_requests, Fake.throttle_first, Fake.status_override, Fake.infinite = [], 0, [], 0, 0, False
        self.old_env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        for key in [k for k in os.environ if k.startswith("EMAIL_CAPTURE_")]:
            del os.environ[key]
        os.environ["EMAIL_CAPTURE_PRIVATE_DIR"] = self.tmp.name
        os.environ["EMAIL_CAPTURE_HOSTED_TOKEN"] = "qa-receiving-key"
        self.profile_path = Path(self.tmp.name) / "profile.json"
        self.write_profile({})
        self.profile = Profile.load(str(self.profile_path))
        self.allocation = allocate({"entity": "buro", "repository": "bcp-core", "environment": "staging", "run_id": "r1", "domain": "qa.example.test"}, self.profile)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def write_profile(self, overrides):
        profile = {"schema_version": "1.0", "mode": "hosted", "backend": "resend", "endpoint": self.endpoint, "environment": "test", "retention": "provider-retained", "ttl_seconds": 900, **overrides}
        self.profile_path.write_text(json.dumps(profile))

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--profile", str(self.profile_path), *args])
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_profile_requires_explicit_retention_and_pins_vendor_host(self):
        for overrides in ({"retention": "strict-purge"}, {"retention": "bogus"}):
            self.write_profile(overrides)
            with self.assertRaises(CaptureError) as caught:
                Profile.load(str(self.profile_path))
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.write_profile({"backend": "hosted"})
        with self.assertRaises(CaptureError):
            Profile.load(str(self.profile_path))
        self.write_profile({"environment": "staging", "endpoint": "https://evil.example.test"})
        with self.assertRaises(CaptureError):
            Profile.load(str(self.profile_path))
        self.write_profile({"environment": "staging", "endpoint": "https://api.resend.com"})
        self.assertEqual(Profile.load(str(self.profile_path)).retention, "provider-retained")

    def test_missing_token_never_falls_back_to_application_key(self):
        del os.environ["EMAIL_CAPTURE_HOSTED_TOKEN"]
        os.environ["RESEND_API_KEY"] = "re_application_key"
        with self.assertRaises(CaptureError) as caught:
            Profile.load(str(self.profile_path))
        self.assertEqual(caught.exception.code, "AUTHORIZATION_DENIED")

    def test_exact_recipient_filter_before_body_fetch_across_pages(self):
        other = "ec-other@qa.example.test"
        Fake.emails = [mail(f"m{i}", other, f"2026-11-02T01:{i:02d}:00.000Z") for i in range(150)]
        Fake.emails.append(mail("mine", self.allocation["recipient"].upper(), "2026-11-02T02:00:00.000Z"))
        Fake.emails.append(mail("shared", [other, "ec-x@qa.example.test"], "2026-11-02T02:01:00.000Z"))
        messages, cursor = await_messages(backend(self.profile), self.allocation, timeout=3)
        self.assertEqual(len(messages), 1)
        self.assertEqual(Fake.detail_requests, ["mine"])
        self.assertGreaterEqual(Fake.pages_served, 2)
        self.assertEqual(messages[0]["content"]["text_present"], True)
        self.assertEqual(messages[0]["content"]["html"], "")
        self.assertTrue(cursor.endswith(messages[0]["opaque_id"]))

    def test_detail_fetch_is_not_repeated_across_polls(self):
        Fake.emails = [mail("mine", self.allocation["recipient"], "2026-11-02T02:00:00.000Z")]
        selected = backend(self.profile)
        selected.list(self.allocation)
        selected.list(self.allocation)
        self.assertEqual(Fake.detail_requests, ["mine"])

    def test_incomplete_enumeration_is_infra_failure_not_no_mail(self):
        from email_capture import core
        Fake.emails = [mail(f"m{i}", "ec-other@qa.example.test", "2026-11-02T01:00:00.000Z") for i in range(250)]
        original, core.RESEND_MAX_PAGES = core.RESEND_MAX_PAGES, 2
        try:
            with self.assertRaises(CaptureError) as caught:
                backend(self.profile).list(self.allocation)
        finally:
            core.RESEND_MAX_PAGES = original
        self.assertEqual(caught.exception.code, "CAPTURE_INFRA_UNAVAILABLE")
        self.assertIn("incomplete", caught.exception.safe_detail)
        Fake.infinite = True
        with self.assertRaises(CaptureError) as caught:
            backend(self.profile).list(self.allocation)
        self.assertEqual(caught.exception.code, "CAPTURE_INFRA_UNAVAILABLE")

    def test_authentication_and_server_failures_are_typed(self):
        os.environ["EMAIL_CAPTURE_HOSTED_TOKEN"] = "wrong"
        with self.assertRaises(CaptureError) as caught:
            backend(self.profile).list(self.allocation)
        self.assertEqual(caught.exception.code, "AUTHORIZATION_DENIED")
        os.environ["EMAIL_CAPTURE_HOSTED_TOKEN"] = "qa-receiving-key"
        Fake.status_override = 500
        with self.assertRaises(CaptureError) as caught:
            backend(self.profile).list(self.allocation)
        self.assertEqual(caught.exception.code, "CAPTURE_INFRA_UNAVAILABLE")

    def test_throttle_backs_off_within_deadline_and_fails_when_no_time_remains(self):
        Fake.emails = [mail("mine", self.allocation["recipient"], "2026-11-02T02:00:00.000Z")]
        Fake.throttle_first = 1
        messages, _ = await_messages(backend(self.profile), self.allocation, timeout=3)
        self.assertEqual(len(messages), 1)
        Fake.throttle_first = 50
        with self.assertRaises(CaptureError) as caught:
            await_messages(backend(self.profile), self.allocation, timeout=0.02)
        self.assertEqual(caught.exception.code, "CAPTURE_INFRA_UNAVAILABLE")

    def test_quiet_window_detects_delayed_duplicate(self):
        Fake.emails = [mail("one", self.allocation["recipient"], "2026-11-02T02:00:00.000Z")]
        threading.Timer(0.4, lambda: Fake.emails.append(mail("two", self.allocation["recipient"], "2026-11-02T02:00:05.000Z"))).start()
        with self.assertRaises(CaptureError) as caught:
            await_messages(backend(self.profile), self.allocation, timeout=3, quiet_window=2)
        self.assertEqual(caught.exception.code, "MESSAGE_AMBIGUOUS")

    def test_quiet_window_clean_pass_returns_original_message(self):
        Fake.emails = [mail("one", self.allocation["recipient"], "2026-11-02T02:00:00.000Z")]
        started = time.monotonic()
        messages, _ = await_messages(backend(self.profile), self.allocation, timeout=3, quiet_window=1.2)
        self.assertEqual(len(messages), 1)
        self.assertGreaterEqual(time.monotonic() - started, 1.1)

    def test_strict_release_is_rejected_and_retire_is_not_a_purge(self):
        allocation_file = Path(self.tmp.name) / "allocation.json"
        allocation_file.write_text(json.dumps(self.allocation))
        allocation_file.chmod(0o600)
        code, payload = self.run_cli("release", "--allocation", f"@{allocation_file}")
        self.assertEqual((code, payload["code"]), (2, "CAPABILITY_UNSUPPORTED"))
        code, payload = self.run_cli("retire", "--allocation", f"@{allocation_file}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["cleanup_state"], "retired_provider_retained")
        self.assertEqual(payload["retention_state"], "provider_retained")
        self.assertNotIn("purge", json.dumps(payload))
        code, payload = self.run_cli("retire", "--allocation", f"@{allocation_file}")
        self.assertEqual((code, payload["cleanup_state"]), (0, "already_released"))
        code, payload = self.run_cli("await", "--allocation", f"@{allocation_file}", "--timeout", "0", "--count", "0", "--output", str(Path(self.tmp.name) / "out.json"))
        self.assertEqual((code, payload["code"]), (2, "AUTHORIZATION_DENIED"))

    def test_retire_reachable_after_expiry_and_pending_purge(self):
        registry = Path(self.tmp.name) / "email-capture" / "allocations.json"
        records = json.loads(registry.read_text())
        for record in records.values():
            record["expires_at"] = time.time() - 10
            record["pending_purge"] = True
        registry.write_text(json.dumps(records))
        allocation_file = Path(self.tmp.name) / "allocation.json"
        allocation_file.write_text(json.dumps(self.allocation))
        allocation_file.chmod(0o600)
        code, payload = self.run_cli("retire", "--allocation", f"@{allocation_file}")
        self.assertEqual((code, payload["cleanup_state"]), (0, "retired_provider_retained"))

    def test_strict_receivers_cannot_retire(self):
        from email_capture.core import MemoryBackend, retire_allocation
        memory = Profile.load(None) if False else Profile("hermetic", "memory", "", "test")
        with self.assertRaises(CaptureError) as caught:
            retire_allocation(MemoryBackend(memory), self.allocation)
        self.assertEqual(caught.exception.code, "CAPABILITY_UNSUPPORTED")

    def test_capabilities_declare_retention_and_no_purge(self):
        capabilities = backend(self.profile).capabilities()
        self.assertEqual((capabilities["purge_scope"], capabilities["retention"]), ("none", "provider-retained"))

    def test_inspect_keeps_secrets_off_stdout_and_filters_links(self):
        html = '<style>x{}</style><script>evil()</script><p>Hi</p><a href="https://app.example.test/auth/confirm?t=abc">go</a><a href="https://evil.example.test/auth/confirm?t=abc">bad</a><img src="https://track.example.test/p.png">Code 123456'
        Fake.emails = [mail("mine", self.allocation["recipient"], "2026-11-02T02:00:00.000Z", text=None, html=html)]
        messages, _ = await_messages(backend(self.profile), self.allocation, timeout=3)
        messages_file = Path(self.tmp.name) / "messages.json"
        messages_file.write_text(json.dumps(messages))
        allocation_file = Path(self.tmp.name) / "allocation.json"
        allocation_file.write_text(json.dumps(self.allocation))
        output = Path(self.tmp.name) / "inspection.json"
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--profile", str(self.profile_path), "inspect", "--allocation", f"@{allocation_file}", "--messages", f"@{messages_file}", "--allow-host", "app.example.test", "--allow-path-prefix", "/auth/", "--output", str(output)])
        self.assertEqual(code, 0, out.getvalue())
        for secret in ("abc", "123456", "evil"):
            self.assertNotIn(secret, out.getvalue())
        record = json.loads(output.read_text())[0]
        self.assertEqual(record["links"], ["https://app.example.test/auth/confirm?t=abc"])
        self.assertEqual(record["rejected_link_count"], 1)
        self.assertNotIn("evil()", record["text"])
        self.assertNotIn("track.example.test", record["text"])
        self.assertEqual(record["codes"], ["123456"])
        self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")

    def test_inspect_denied_for_foreign_message_and_expired_allocation(self):
        foreign = [{"envelope": {"to": ["ec-other@qa.example.test"]}, "content": {"text": "x", "html": ""}, "subject": "s"}]
        messages_file = Path(self.tmp.name) / "messages.json"
        messages_file.write_text(json.dumps(foreign))
        allocation_file = Path(self.tmp.name) / "allocation.json"
        allocation_file.write_text(json.dumps(self.allocation))
        args = ["--profile", str(self.profile_path), "inspect", "--allocation", f"@{allocation_file}", "--messages", f"@{messages_file}", "--allow-host", "app.example.test", "--output", str(Path(self.tmp.name) / "o.json")]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(args), 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "AUTHORIZATION_DENIED")
        registry = Path(self.tmp.name) / "email-capture" / "allocations.json"
        records = json.loads(registry.read_text())
        for record in records.values():
            record["expires_at"] = time.time() - 10
        registry.write_text(json.dumps(records))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(args), 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "AUTHORIZATION_DENIED")


if __name__ == "__main__":
    unittest.main()
