#!/usr/bin/env python3
"""Test suite for safe_env.sh — secret-safe environment variable inspection."""
import subprocess
import unittest
from pathlib import Path


class TestSafeEnvHelpers(unittest.TestCase):
    """Test bash safe_env.sh helper functions."""

    def setUp(self):
        """Set up paths for tests."""
        self.pack_root = Path(__file__).resolve().parent.parent
        self.safe_env_script = self.pack_root / "scripts" / "safe_env.sh"
        self.assertTrue(self.safe_env_script.exists(), f"{self.safe_env_script} not found")

    def run_bash(self, script):
        """Helper to run bash script and return stdout."""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            cwd=str(self.pack_root),
        )
        return result.stdout.strip(), result.returncode

    def test_is_set_unset_variable(self):
        """is_set on unset variable should return 'no'."""
        script = f"""
set -u
source {self.safe_env_script}
is_set UNDEFINED_TOKEN_VAR
"""
        output, _ = self.run_bash(script)
        self.assertEqual(output, "no")

    def test_is_set_set_variable(self):
        """is_set on set variable should return 'yes'."""
        script = f"""
set -u
export TEST_TOKEN=abc123
source {self.safe_env_script}
is_set TEST_TOKEN
"""
        output, _ = self.run_bash(script)
        self.assertEqual(output, "yes")

    def test_safe_summary_unset_variable(self):
        """safe_summary on unset variable should show 'absent'."""
        script = f"""
set -u
source {self.safe_env_script}
safe_summary UNDEFINED_API_KEY
"""
        output, _ = self.run_bash(script)
        self.assertEqual(output, "absent")
        self.assertNotIn("abc", output)

    def test_safe_summary_set_variable_no_leak(self):
        """safe_summary on set variable should NOT leak the value."""
        script = f"""
set -u
export SECRET_TOKEN=abcdefghijklmnop
source {self.safe_env_script}
safe_summary SECRET_TOKEN
"""
        output, _ = self.run_bash(script)
        self.assertIn("present", output)
        self.assertIn("length=", output)
        # Key assertion: value must not appear — only a <=6-char head and
        # the length (16 >= 16 so the prefix form is emitted).
        self.assertNotIn("abcdefghijklmnop", output)
        self.assertNotIn("abcdefg", output)

    def test_safe_summary_output_format(self):
        """safe_summary should output 'present length=N' / 'present prefix=… length=N'."""
        script = f"""
set -u
export MY_SECRET=12345678
source {self.safe_env_script}
safe_summary MY_SECRET
"""
        output, _ = self.run_bash(script)
        self.assertEqual(output, "present length=8")

    def test_safe_summary_short_credential_length_only(self):
        """A 15-char credential gets NO 6-char prefix — half of it would be
        exposed. The prefix form only activates at 16+ chars (round-34
        SEC_0002: 6 of 12 exposed under the old floor of 12)."""
        script = f"""
set -u
export SHORT_SECRET=abcdefghijklmno
export EDGE_SECRET=abcdefghijklmnop
source {self.safe_env_script}
safe_summary SHORT_SECRET
safe_summary EDGE_SECRET
"""
        output, _ = self.run_bash(script)
        lines = output.splitlines()
        self.assertEqual(lines[0], "present length=15")
        # 16 chars crosses the floor — prefix form returns.
        self.assertEqual(lines[1], "present prefix=abcdef length=16")
        self.assertNotIn("abcdefghijklmno", output)

    def test_safe_length_variable(self):
        """safe_length should return length without value."""
        script = f"""
set -u
export LONG_TOKEN=this_is_a_very_long_token_value_12345abc
source {self.safe_env_script}
safe_length LONG_TOKEN
"""
        output, _ = self.run_bash(script)
        # Should be a number
        length = int(output)
        self.assertEqual(length, 40)  # len("this_is_a_very_long_token_value_12345abc")

    def test_safe_length_unset_variable(self):
        """safe_length on unset variable should return 0."""
        script = f"""
set -u
source {self.safe_env_script}
safe_length UNDEFINED_VAR
"""
        output, _ = self.run_bash(script)
        self.assertEqual(output, "0")

    def test_safe_prefix_with_default_length(self):
        """safe_prefix defaults to 6 chars and never exceeds len-4."""
        script = f"""
set -u
export API_KEY=sk_live_1234567890abcdef
source {self.safe_env_script}
safe_prefix API_KEY
"""
        output, _ = self.run_bash(script)
        self.assertIn("sk_liv", output)
        # Should NOT contain chars past the 6-char head
        self.assertNotIn("1234567890", output)

    def test_safe_prefix_with_custom_length(self):
        """safe_prefix with custom N should show first N chars."""
        script = f"""
set -u
export TOKEN=prefix_and_rest_of_token
source {self.safe_env_script}
safe_prefix TOKEN 6
"""
        output, _ = self.run_bash(script)
        self.assertIn("prefix", output)
        self.assertNotIn("rest_of_token", output)

    def test_safe_prefix_zero_padded_width(self):
        """`001` is a one-char request — zero padding must not widen it."""
        script = f"""
set -u
export TOKEN=prefix_and_rest_of_token
source {self.safe_env_script}
safe_prefix TOKEN 001
safe_prefix TOKEN 000
safe_prefix TOKEN 18446744073709551615
"""
        output, _ = self.run_bash(script)
        lines = output.split("\n")
        self.assertEqual(lines[0], "p")
        self.assertEqual(lines[1], "p")
        # overflowing decimal clamps to the 8-char cap, not a 1-char wrap
        self.assertEqual(lines[2], "prefix_a")

    def test_safe_prefix_short_secret_length_only(self):
        """len<12: a prefix would expose too much of the value — length only."""
        script = f"""
set -u
export SHORT=leakme123
source {self.safe_env_script}
safe_prefix SHORT
safe_prefix SHORT 3
"""
        output, _ = self.run_bash(script)
        self.assertNotIn("leak", output)
        self.assertIn("len=9", output)

    def test_safe_prefix_unset_variable(self):
        """safe_prefix on unset variable prints nothing."""
        script = f"""
set -u
source {self.safe_env_script}
safe_prefix UNDEFINED_TOKEN
"""
        output, _ = self.run_bash(script)
        self.assertEqual(output, "")

    def test_no_value_leakage_in_redirects(self):
        """Using safe_* helpers should not leak values even in redirects."""
        script = f"""
set -u
export SECRET=leakme123
source {self.safe_env_script}
safe_summary SECRET > /tmp/test_safe_env_out.txt
cat /tmp/test_safe_env_out.txt
"""
        output, _ = self.run_bash(script)
        # File should contain summary but not secret value
        self.assertIn("present", output)
        self.assertNotIn("leakme123", output)

    def test_multiple_helpers_in_sequence(self):
        """Multiple safe_* calls should all protect values."""
        script = f"""
set -u
export API_TOKEN=secret_key_12345
source {self.safe_env_script}
echo "Check 1:"; safe_summary API_TOKEN
echo "Check 2:"; safe_prefix API_TOKEN 3
echo "Check 3:"; safe_length API_TOKEN
"""
        output, _ = self.run_bash(script)
        # None of the three checks should leak full value
        self.assertNotIn("secret_key_12345", output)
        self.assertIn("Check 1:", output)
        self.assertIn("Check 2:", output)
        self.assertIn("Check 3:", output)


if __name__ == "__main__":
    unittest.main()
