"""Core and CLI conformance tests for email capture v1."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from email_capture.core import CaptureError, MemoryBackend, Profile, allocate, assert_messages, await_messages, backend, extract, normalise_message, receipt, release_allocation


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = dict(os.environ)
        os.environ["EMAIL_CAPTURE_PRIVATE_DIR"] = self.temp.name
        self.profile = Profile("hermetic", "memory", f"{self.temp.name}/mail.json", "test", 1)
        self.backend = MemoryBackend(self.profile)
        self.request = {"schema_version": "1.0", "entity": "manolii", "repository": "repo", "environment": "ci", "run_id": self.id()}
        self.allocation = allocate(self.request, self.profile)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def _write_messages(self, rows):
        Path(self.profile.endpoint).write_text(json.dumps(rows))

    def test_idempotent_high_entropy_allocation_and_release_retirement(self):
        self.assertEqual(self.allocation, allocate(self.request, self.profile))
        self.assertRegex(self.allocation["recipient"], r"ec-[0-9a-f]{32}@")
        release_allocation(self.backend, self.allocation)
        replacement = allocate(self.request, self.profile)
        self.assertNotEqual(self.allocation["allocation_id"], replacement["allocation_id"])

    def test_isolation_cursor_extraction_and_scoped_release(self):
        rows = [
            {"id": "1", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]], "subject": "secret", "text": "Code 123456 https://example.test/x"},
            {"id": "2", "date": "2026-08-21T00:00:01Z", "to": ["other@capture.test"]},
        ]
        self._write_messages(rows)
        messages, cursor = await_messages(self.backend, self.allocation, 0.1)
        self.assertEqual(self.allocation["cursor"], cursor)
        self.assertEqual(extract(messages[0], "code"), ["123456"])
        self.assertEqual(extract(messages[0], "link"), ["https://example.test/x"])
        self.assertEqual(assert_messages(messages, {"exact_count": 1})["result"], "passed")
        release_allocation(self.backend, self.allocation)
        self.assertEqual(json.loads(Path(self.profile.endpoint).read_text()), [rows[1]])

    def test_stable_cursor_rejects_stale_reconsumption(self):
        self._write_messages([{"id": "1", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]]}])
        await_messages(self.backend, self.allocation, 0.1)
        with self.assertRaisesRegex(CaptureError, "MESSAGE_TIMEOUT"):
            await_messages(self.backend, self.allocation, 0.01)

    def test_missing_stable_timestamp_is_infrastructure_failure(self):
        self._write_messages([{"id": "1", "to": [self.allocation["recipient"]]}])
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INFRA_UNAVAILABLE"):
            await_messages(self.backend, self.allocation, 0.01)

    def test_timeout_ambiguity_and_replay(self):
        with self.assertRaisesRegex(CaptureError, "MESSAGE_TIMEOUT"):
            await_messages(self.backend, self.allocation, 0.01)
        self._write_messages([
            {"id": "1", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]]},
            {"id": "2", "date": "2026-08-21T00:00:01Z", "to": [self.allocation["recipient"]]},
        ])
        with self.assertRaisesRegex(CaptureError, "MESSAGE_AMBIGUOUS"):
            await_messages(self.backend, self.allocation, 0.01)
        self._write_messages([
            {"id": "1", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]]},
            {"id": "1", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]]},
        ])
        with self.assertRaisesRegex(CaptureError, "MESSAGE_REPLAYED"):
            await_messages(self.backend, self.allocation, 0.01, count=2)

    def test_bounded_negative_waits_full_window_and_rejects_arrival(self):
        started = time.monotonic()
        messages, cursor = await_messages(self.backend, self.allocation, 0.03, count=0)
        self.assertGreaterEqual(time.monotonic() - started, 0.025)
        self.assertEqual((messages, cursor), ([], "0:"))
        self._write_messages([{"id": "1", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]]}])
        with self.assertRaisesRegex(CaptureError, "ASSERTION_FAILED"):
            await_messages(self.backend, self.allocation, 0.03, count=0)

    def test_receiver_specific_normalisation(self):
        inbucket = normalise_message({"id": "i1", "date": "2026-08-21T00:00:00Z", "from": "from@test", "to": ["to@test"], "subject": "s", "body": {"text": "123456", "html": "<b>x</b>"}, "header": {"X-Test": ["yes"]}, "attachments": [{"filename": "a.txt", "content-type": "text/plain"}]}, "inbucket")
        mailpit = normalise_message({"ID": "m1", "Date": "2026-08-21T00:00:00Z", "From": {"Address": "from@test"}, "To": [{"Address": "to@test"}], "Text": "body", "Attachments": [{"FileName": "a.txt", "ContentType": "text/plain", "Size": 4}]}, "mailpit")
        self.assertEqual(extract(inbucket, "header", "x-test"), ["yes"])
        self.assertEqual(inbucket["content"]["text"], "123456")
        self.assertEqual(mailpit["attachments"][0]["size"], 4)

    def test_limits_and_domain_validation(self):
        with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
            allocate({**self.request, "run_id": "different", "domain": "bad\n.example"}, self.profile)
        with self.assertRaisesRegex(CaptureError, "CAPABILITY_UNSUPPORTED"):
            normalise_message({"id": "large", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]], "text": "x" * (1024 * 1024 + 1)}, "memory")
        with self.assertRaisesRegex(CaptureError, "CAPABILITY_UNSUPPORTED"):
            normalise_message({"id": "many", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]], "attachments": [{}] * 21}, "memory")

    def test_fail_closed_and_safe_receipt(self):
        os.environ.update(EMAIL_CAPTURE_MODE="hermetic", EMAIL_CAPTURE_BACKEND="memory", EMAIL_CAPTURE_ENVIRONMENT="production")
        with self.assertRaisesRegex(CaptureError, "CONFIG_INVALID"):
            Profile.load()
        output = receipt("await", "passed", time.monotonic(), recipient="no", subject="no", message_count=1)
        self.assertNotIn("recipient", output)
        self.assertNotIn("subject", output)
        with self.assertRaisesRegex(CaptureError, "CAPABILITY_UNSUPPORTED"):
            backend(Profile("off", "memory", "", "production"))

    def test_cli_private_outputs_and_persisted_cursor(self):
        self._write_messages([{"id": "1", "date": "2026-08-21T00:00:00Z", "to": [self.allocation["recipient"]], "text": "secret 123456"}])
        profile_path = Path(self.temp.name) / "profile.json"
        profile_path.write_text(json.dumps({"schema_version": "1.0", "mode": "hermetic", "backend": "memory", "endpoint": self.profile.endpoint, "environment": "test"}))
        allocation_path = Path(self.temp.name) / "allocation.json"
        allocation_path.write_text(json.dumps(self.allocation))
        allocation_path.chmod(0o600)
        output_path = Path(self.temp.name) / "messages.json"
        run = subprocess.run(["bin/email-capture", "--profile", str(profile_path), "await", "--allocation", f"@{allocation_path}", "--timeout", ".1", "--output", str(output_path)], capture_output=True, text=True, check=True)
        self.assertNotIn("secret", run.stdout)
        self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(json.loads(allocation_path.read_text())["cursor"], "0:")


if __name__ == "__main__":
    unittest.main()
