"""Synthetic observations stay separate from the unchanged executor assertions."""
from contextlib import ExitStack, redirect_stdout
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "source-adapter-probe.py"
spec = importlib.util.spec_from_file_location("public_adapter_observed", SCRIPT)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
AUTHORITY = ("source_execution_closure_verified", "full_repository_qualified",
             "native_gate_qualified", "admission_authorized", "production_authorized", "cache_reusable")


class SourceAdapterProbeTests(unittest.TestCase):
    def test_actual_three_subjects_are_observations_only(self):
        before = (os.getcwd(), os.readlink("/proc/self/ns/net"), os.readlink("/proc/self/ns/user"),
                  len(os.listdir("/proc/self/fd")))
        record = probe.observations()
        self.assertEqual(record["status"], "observations_complete")
        self.assertEqual([row["subject"] for row in record["subjects"]],
                         ["installed_path", "original_opened_fd", "sealed_copy_fd"])
        for key in AUTHORITY:
            self.assertIs(record[key], False)
        self.assertRegex(record["adapter_sha256"], r"^[0-9a-f]{64}$")
        self.assertIs(record["adapter_identity_reobserved"], True)
        self.assertEqual(record["sealed_adapter_seals"] & 15, 15)
        for row in record["subjects"]:
            self.assertEqual(row["raw_retained_bytes"], 0)
            self.assertLessEqual(row["stdout_bytes"] + row["stderr_bytes"], probe.MAX_OUTPUT_BYTES + 1)
        self.assertEqual(before, (os.getcwd(), os.readlink("/proc/self/ns/net"),
                                 os.readlink("/proc/self/ns/user"), len(os.listdir("/proc/self/fd"))))
        # A native denial is an observation, not a reason to weaken or skip the
        # separate 17 actual source-executor qualification assertions.

    def test_capture_and_sealed_copy_have_same_real_root_owned_bytes(self):
        with ExitStack() as stack:
            original, details, raw = probe._capture_adapter(stack)
            sealed = probe._sealed(stack, raw)
            self.assertEqual(details[3], 0)
            self.assertEqual(hashlib.sha256(raw).digest(),
                             hashlib.sha256(os.pread(sealed, len(raw) + 1, 0)).digest())
            self.assertEqual(probe._read_adapter(original), (details, raw))
            for action in (lambda: os.write(sealed, b"changed"), lambda: os.ftruncate(sealed, 0)):
                with self.assertRaises(OSError):
                    action()
            self.assertEqual(fcntl.fcntl(sealed, getattr(fcntl, "F_GET_SEALS", 1034)) & 15, 15)

    def test_untrusted_owned_file_cannot_be_the_adapter(self):
        with tempfile.TemporaryFile() as stream:
            stream.write(b"synthetic untrusted input")
            stream.flush()
            # No mocked root ownership or permission override permits execution.
            with self.assertRaises(probe.ProbeError):
                probe._read_adapter(stream.fileno())

    def test_stdout_and_stderr_are_counted_but_never_returned(self):
        marker = "SYNTHETIC_NOT_RETAINED"
        result = probe._observe([sys.executable, "-c",
            f"import sys; print('{marker}'); sys.stderr.write('Permission denied {marker}'); sys.exit(7)"])
        self.assertEqual(result["returncode"], 7)
        self.assertGreater(result["stdout_bytes"], 0)
        self.assertGreater(result["stderr_bytes"], 0)
        self.assertEqual(result["error_category"], "permission_denied")
        self.assertEqual(result["raw_retained_bytes"], 0)
        self.assertNotIn(marker, json.dumps(result))

    def test_child_stdin_and_environment_cannot_supply_inputs(self):
        with patch.dict(os.environ, {"SYNTHETIC_PROBE_CREDENTIAL": "NEVER_COPY_THIS"}):
            result = probe._observe([sys.executable, "-c",
                "import os,sys; assert not sys.stdin.read(); assert 'SYNTHETIC_PROBE_CREDENTIAL' not in os.environ"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout_bytes"] + result["stderr_bytes"], 0)

    def test_timeout_does_not_stop_an_unrelated_owned_neighbor(self):
        neighbor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            result = probe._observe([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
            self.assertIs(result["timeout"], True)
            self.assertIsNone(result["returncode"])
            self.assertIsNone(neighbor.poll())
        finally:
            neighbor.terminate()
            neighbor.wait(timeout=5)

    def test_output_is_bounded_to_one_byte_beyond_limit(self):
        result = probe._observe([sys.executable, "-c", "import os; os.write(1,b'x'*65536)"], output_limit=16)
        self.assertIs(result["output_limit"], True)
        self.assertLessEqual(result["stdout_bytes"] + result["stderr_bytes"], 17)
        self.assertEqual(result["raw_retained_bytes"], 0)

    def test_invalid_budgets_are_rejected_before_launch(self):
        for values in ({"timeout": 0}, {"timeout": float("inf")}, {"timeout": True},
                       {"output_limit": 0}, {"output_limit": True}, {"output_limit": 8193}):
            with self.subTest(values=values), patch.object(probe.subprocess, "Popen") as launch:
                with self.assertRaises(probe.ProbeError):
                    probe._observe(["unreachable"], **values)
                launch.assert_not_called()

    def test_launch_denial_is_metadata_without_fallback(self):
        with patch.object(probe.subprocess, "Popen", side_effect=PermissionError(13, "synthetic")) as launch:
            result = probe._observe(["synthetic-forbidden-image"])
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result["error_category"], "launch_permission_denied")
        self.assertIsNone(result["returncode"])
        self.assertEqual(result["raw_retained_bytes"], 0)

    def test_no_arguments_can_choose_paths_commands_or_network_targets(self):
        for argument in ("--adapter=/tmp/other", "--command=whoami", "--url=https://example.invalid"):
            output = io.StringIO()
            with patch.object(probe, "observations") as observe, redirect_stdout(output):
                self.assertEqual(probe.main([argument]), 2)
            observe.assert_not_called()
            record = json.loads(output.getvalue())
            self.assertEqual(record["status"], "unsupported_probe_arguments")
            for key in AUTHORITY:
                self.assertIs(record[key], False)

    def test_unavailable_capture_never_emits_raw_error_or_authority(self):
        output = io.StringIO()
        with patch.object(probe, "observations", side_effect=probe.ProbeError("SYNTHETIC_PRIVATE_TEXT")), redirect_stdout(output):
            self.assertEqual(probe.main([]), 2)
        self.assertNotIn("SYNTHETIC_PRIVATE_TEXT", output.getvalue())
        record = json.loads(output.getvalue())
        for key in AUTHORITY:
            self.assertIs(record[key], False)

    def test_completion_with_denied_subjects_never_grants_qualification(self):
        record = probe._record("observations_complete")
        record["subjects"] = [{"subject": "sealed_copy_fd", "returncode": 1}]
        output = io.StringIO()
        with patch.object(probe, "observations", return_value=record), redirect_stdout(output):
            self.assertEqual(probe.main([]), 0)
        for key in AUTHORITY:
            self.assertIs(json.loads(output.getvalue())[key], False)


if __name__ == "__main__":
    unittest.main()
