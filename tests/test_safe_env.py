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
        """is_set on unset variable should return 'unset'."""
        script = f"""
set -u
source {self.safe_env_script}
is_set UNDEFINED_TOKEN_VAR
"""
        output, _ = self.run_bash(script)
        self.assertEqual(output, "unset")

    def test_is_set_set_variable(self):
        """is_set on set variable should return 'set'."""
        script = f"""
set -u
export TEST_TOKEN=abc123
source {self.safe_env_script}
is_set TEST_TOKEN
"""
        output, _ = self.run_bash(script)
        self.assertEqual(output, "set")

    def test_safe_summary_unset_variable(self):
        """safe_summary on unset variable should show 'unset'."""
        script = f"""
set -u
source {self.safe_env_script}
safe_summary UNDEFINED_API_KEY
"""
        output, _ = self.run_bash(script)
        self.assertIn("unset", output)
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
        self.assertIn("set", output)
        self.assertIn("len=", output)
        # Key assertion: value must not appear
        self.assertNotIn("abcdefghijklmnop", output)
        self.assertNotIn("abc", output)

    def test_safe_summary_output_format(self):
        """safe_summary should output 'NAME: set (len=N)' format."""
        script = f"""
set -u
export MY_SECRET=12345678
source {self.safe_env_script}
safe_summary MY_SECRET
"""
        output, _ = self.run_bash(script)
        self.assertIn("MY_SECRET:", output)
        self.assertIn("len=8", output)

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
        """safe_prefix with default N=4 should show first 4 chars."""
        script = f"""
set -u
export API_KEY=sk_live_1234567890abcdef
source {self.safe_env_script}
safe_prefix API_KEY
"""
        output, _ = self.run_bash(script)
        self.assertIn("sk_l", output)
        self.assertIn("[REDACTED:", output)
        # Should NOT contain full token
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

    def test_safe_prefix_unset_variable(self):
        """safe_prefix on unset variable should show (unset)."""
        script = f"""
set -u
source {self.safe_env_script}
safe_prefix UNDEFINED_TOKEN
"""
        output, _ = self.run_bash(script)
        self.assertIn("(unset)", output)

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
        self.assertIn("set", output)
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
