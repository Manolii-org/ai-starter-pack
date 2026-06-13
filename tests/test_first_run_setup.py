#!/usr/bin/env python3
"""Test suite for first-run-setup.py — interactive setup and secrets verification."""
import tempfile
import unittest
import importlib.util
from pathlib import Path

# Load first-run-setup.py as a module (hyphenated filename)
setup_module_path = Path(__file__).resolve().parent.parent / "scripts" / "first-run-setup.py"
spec = importlib.util.spec_from_file_location("first_run_setup", setup_module_path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


# Sample pack-components.yml matching machine-emitted format
SAMPLE_CONTRACT = """schema_version: 1

components:
  litellm_proxy:
    channel: fly-config
    enabled: true

instance:
  name: "t"
  doppler_project: "proj-x"
  doppler_config: "prd"
  fly_app: "t-litellm"
  repo_vars:
    - LITELLM_PROXY_URL
  required_secrets:
    github:
      - ANTHROPIC_API_KEY
    doppler:
      project: "proj-x"
      keys:
        - LITELLM_MASTER_KEY
    fly:
      - LITELLM_MASTER_KEY
"""

# Keys that must be scrubbed from environment in every verify test
SCRUB_KEYS = [
    "LITELLM_MASTER_KEY",
    "MESH_BEARER_AGENT",
    "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "MCP_API_KEY",
]


class TestFallbackParser(unittest.TestCase):
    """Test _parse_components_fallback() function."""

    def test_fallback_parser(self):
        """_parse_components_fallback() should extract instance, repo_vars, and required_secrets."""
        result = m._parse_components_fallback(SAMPLE_CONTRACT)

        self.assertIn("instance", result)
        instance = result["instance"]

        self.assertIn("repo_vars", instance)
        self.assertEqual(instance["repo_vars"], ["LITELLM_PROXY_URL"])

        self.assertIn("required_secrets", instance)
        secrets = instance["required_secrets"]

        # GitHub secrets
        self.assertIn("github", secrets)
        self.assertEqual(secrets["github"], ["ANTHROPIC_API_KEY"])

        # Doppler secrets
        self.assertIn("doppler", secrets)
        doppler_info = secrets["doppler"]
        self.assertEqual(doppler_info["project"], "proj-x")
        self.assertEqual(doppler_info["keys"], ["LITELLM_MASTER_KEY"])

        # Fly secrets
        self.assertIn("fly", secrets)
        self.assertEqual(secrets["fly"], ["LITELLM_MASTER_KEY"])

    def test_fallback_parser_empty_lists(self):
        """Variant with empty lists should parse correctly."""
        contract_with_empty = """schema_version: 1

components:
  litellm_proxy:
    channel: fly-config
    enabled: true

instance:
  name: "t"
  doppler_project: "proj-x"
  repo_vars:
    []
  required_secrets:
    github:
      []
    doppler:
      project: "proj-x"
      keys:
        []
    fly:
      []
"""
        result = m._parse_components_fallback(contract_with_empty)
        instance = result["instance"]

        self.assertEqual(instance["repo_vars"], [])
        secrets = instance["required_secrets"]
        self.assertEqual(secrets["github"], [])
        self.assertEqual(secrets["doppler"]["keys"], [])
        self.assertEqual(secrets["fly"], [])

    def test_fallback_parser_handles_comments(self):
        """Parser should skip comments and blank lines."""
        contract_with_comments = """# This is a comment
schema_version: 1

# Another comment
components:

  litellm_proxy:
    channel: fly-config

instance:
  name: "test"
  doppler_project: "proj-x"
  repo_vars:
    - VAR_ONE
"""
        result = m._parse_components_fallback(contract_with_comments)
        instance = result["instance"]
        self.assertEqual(instance["repo_vars"], ["VAR_ONE"])


class TestVerifyYamlNone(unittest.TestCase):
    """Test verify_required_secrets() when yaml is None."""

    def test_verify_yaml_none_missing_key(self, monkeypatch=None):
        """With yaml=None, fallback parser should fail loudly on missing key."""
        if monkeypatch is None:
            # Called directly; create a fixture-like monkeypatch
            from _pytest.monkeypatch import MonkeyPatch
            monkeypatch = MonkeyPatch()
        self.addCleanup(monkeypatch.undo)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            contract_file = tmp_path / "pack-components.yml"
            contract_file.write_text(SAMPLE_CONTRACT)

            # Scrub all SCRUB_KEYS from environment
            for key in SCRUB_KEYS:
                monkeypatch.delenv(key, raising=False)

            monkeypatch.chdir(tmp_path)
            monkeypatch.setattr(m, "yaml", None)

            rc = m.verify_required_secrets()
            self.assertEqual(rc, 2)

            # Now set the missing key and retry
            monkeypatch.setenv("LITELLM_MASTER_KEY", "test-value")
            rc = m.verify_required_secrets()
            self.assertEqual(rc, 0)


    def test_verify_yaml_none_with_all_keys(self, monkeypatch=None):
        """With yaml=None and all keys set, should succeed."""
        if monkeypatch is None:
            from _pytest.monkeypatch import MonkeyPatch
            monkeypatch = MonkeyPatch()
        self.addCleanup(monkeypatch.undo)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            contract_file = tmp_path / "pack-components.yml"
            contract_file.write_text(SAMPLE_CONTRACT)

            # Scrub all SCRUB_KEYS
            for key in SCRUB_KEYS:
                monkeypatch.delenv(key, raising=False)

            monkeypatch.chdir(tmp_path)
            monkeypatch.setattr(m, "yaml", None)

            # Set all required keys
            monkeypatch.setenv("LITELLM_MASTER_KEY", "abc123")
            monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

            rc = m.verify_required_secrets()
            self.assertEqual(rc, 0)



class TestVerifyExtraKeys(unittest.TestCase):
    """Test verify_required_secrets() with extra_doppler_keys parameter."""

    def test_verify_extra_keys_missing(self, monkeypatch=None):
        """With extra_doppler_keys, missing keys should fail."""
        if monkeypatch is None:
            from _pytest.monkeypatch import MonkeyPatch
            monkeypatch = MonkeyPatch()
        self.addCleanup(monkeypatch.undo)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            contract_file = tmp_path / "pack-components.yml"
            contract_file.write_text(SAMPLE_CONTRACT)

            # Scrub environment
            for key in SCRUB_KEYS:
                monkeypatch.delenv(key, raising=False)

            monkeypatch.chdir(tmp_path)
            # yaml is available, so use normal path

            # Set only the contract key
            monkeypatch.setenv("LITELLM_MASTER_KEY", "abc123")

            # Request an additional key that's missing
            rc = m.verify_required_secrets(["BROWSERBASE_API_KEY"])
            self.assertEqual(rc, 2)

            # Now set the extra key
            monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_live_xyz")
            rc = m.verify_required_secrets(["BROWSERBASE_API_KEY"])
            self.assertEqual(rc, 0)


    def test_verify_extra_keys_multiple(self, monkeypatch=None):
        """Should handle multiple extra keys."""
        if monkeypatch is None:
            from _pytest.monkeypatch import MonkeyPatch
            monkeypatch = MonkeyPatch()
        self.addCleanup(monkeypatch.undo)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            contract_file = tmp_path / "pack-components.yml"
            contract_file.write_text(SAMPLE_CONTRACT)

            for key in SCRUB_KEYS:
                monkeypatch.delenv(key, raising=False)

            monkeypatch.chdir(tmp_path)

            # Set contract key and extra keys
            monkeypatch.setenv("LITELLM_MASTER_KEY", "abc123")
            monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-xyz")
            monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-xyz")

            rc = m.verify_required_secrets(["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"])
            self.assertEqual(rc, 0)



class TestVerifyMissingContract(unittest.TestCase):
    """Test verify_required_secrets() when pack-components.yml is absent."""

    def test_verify_missing_contract(self, monkeypatch=None):
        """Absent contract should gracefully return 0 (pre-1.2.0 compatibility)."""
        if monkeypatch is None:
            from _pytest.monkeypatch import MonkeyPatch
            monkeypatch = MonkeyPatch()
        self.addCleanup(monkeypatch.undo)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            # Don't create pack-components.yml

            for key in SCRUB_KEYS:
                monkeypatch.delenv(key, raising=False)

            monkeypatch.chdir(tmp_path)

            rc = m.verify_required_secrets()
            self.assertEqual(rc, 0)



class TestEnvFileFallback(unittest.TestCase):
    """Test verify_required_secrets() with .env file fallback."""

    def test_env_file_fallback(self, monkeypatch=None):
        """Keys in .env file should be recognized as set."""
        if monkeypatch is None:
            from _pytest.monkeypatch import MonkeyPatch
            monkeypatch = MonkeyPatch()
        self.addCleanup(monkeypatch.undo)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            contract_file = tmp_path / "pack-components.yml"
            contract_file.write_text(SAMPLE_CONTRACT)

            # Create .env with the required key
            env_file = tmp_path / ".env"
            env_file.write_text('LITELLM_MASTER_KEY="abc123"\n')

            for key in SCRUB_KEYS:
                monkeypatch.delenv(key, raising=False)

            monkeypatch.chdir(tmp_path)

            rc = m.verify_required_secrets()
            self.assertEqual(rc, 0)


    def test_env_file_with_comments_and_blanks(self, monkeypatch=None):
        """Parser should handle comments and blank lines in .env."""
        if monkeypatch is None:
            from _pytest.monkeypatch import MonkeyPatch
            monkeypatch = MonkeyPatch()
        self.addCleanup(monkeypatch.undo)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            contract_file = tmp_path / "pack-components.yml"
            contract_file.write_text(SAMPLE_CONTRACT)

            env_file = tmp_path / ".env"
            env_file.write_text("""
# Secrets from Doppler
LITELLM_MASTER_KEY="secret123"

# Comment without blank line
ANOTHER_KEY=value
""")

            for key in SCRUB_KEYS:
                monkeypatch.delenv(key, raising=False)

            monkeypatch.chdir(tmp_path)

            rc = m.verify_required_secrets()
            self.assertEqual(rc, 0)



class TestChoiceRequiredKeysShape(unittest.TestCase):
    """Test CHOICE_REQUIRED_KEYS constant shape and coverage."""

    def test_choice_required_keys_has_all_features(self):
        """CHOICE_REQUIRED_KEYS should cover exactly the four features."""
        expected_features = {
            "oss_routing",
            "langfuse_telemetry",
            "browserbase",
            "remote_memory",
        }
        self.assertEqual(set(m.CHOICE_REQUIRED_KEYS.keys()), expected_features)

    def test_choice_required_keys_all_non_empty_lists(self):
        """Each feature should map to a non-empty list of strings."""
        for feature, keys in m.CHOICE_REQUIRED_KEYS.items():
            self.assertIsInstance(keys, list, f"{feature} should map to a list")
            self.assertGreater(len(keys), 0, f"{feature} list should not be empty")
            for key in keys:
                self.assertIsInstance(key, str, f"Key in {feature} should be string: {key}")

    def test_choice_required_keys_correct_mappings(self):
        """Verify exact key mappings for each feature."""
        self.assertEqual(m.CHOICE_REQUIRED_KEYS["oss_routing"], ["LITELLM_MASTER_KEY"])
        self.assertEqual(
            m.CHOICE_REQUIRED_KEYS["langfuse_telemetry"],
            ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"],
        )
        self.assertEqual(
            m.CHOICE_REQUIRED_KEYS["browserbase"],
            ["BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"],
        )
        self.assertEqual(m.CHOICE_REQUIRED_KEYS["remote_memory"], ["MCP_API_KEY"])


class TestParseEnvFile(unittest.TestCase):
    """Test parse_env_file() utility."""

    def test_parse_env_file_basic(self):
        """Basic .env parsing should extract key=value pairs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("""
KEY1=value1
KEY2=value2
KEY3="quoted_value"
""")
            keys = m.parse_env_file(env_file)
            self.assertEqual(keys, {"KEY1", "KEY2", "KEY3"})

    def test_parse_env_file_nonexistent(self):
        """Nonexistent file should return empty set."""
        missing_file = Path("/tmp/nonexistent-file-xyz-12345.env")
        keys = m.parse_env_file(missing_file)
        self.assertEqual(keys, set())

    def test_parse_env_file_empty_values_ignored(self):
        """Empty values should not be included in result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("""
KEY1=value
KEY2=
KEY3=  """)
            keys = m.parse_env_file(env_file)
            self.assertEqual(keys, {"KEY1"})

    def test_parse_env_file_comments_ignored(self):
        """Lines starting with # should be ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("""
# This is a comment
KEY1=value1
# KEY2=should_not_appear
KEY3=value3
""")
            keys = m.parse_env_file(env_file)
            self.assertEqual(keys, {"KEY1", "KEY3"})


if __name__ == "__main__":
    unittest.main()


class TestMainInteractiveFlow:
    """End-to-end main() runs (stdin closed → ask_yn defaults). Regression for
    the marker_written signature mismatch print_summary crash (PR #2185)."""

    def _run_main(self, cwd, env_extra=None):
        import os as _os
        import subprocess
        import sys as _sys
        env = {k: v for k, v in _os.environ.items() if k not in SCRUB_KEYS}
        env.update(env_extra or {})
        return subprocess.run(
            [_sys.executable, "scripts/first-run-setup.py"],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
        )

    def _render_dir(self, tmp_path, contract: str):
        d = tmp_path / "inst"
        (d / "scripts").mkdir(parents=True)
        (d / "pack-components.yml").write_text(contract)
        import shutil
        shutil.copy(setup_module_path, d / "scripts" / "first-run-setup.py")
        return d

    def test_main_success_writes_marker(self, tmp_path):
        contract = SAMPLE_CONTRACT.replace("        - LITELLM_MASTER_KEY\n", "        []\n", 1)
        d = self._render_dir(tmp_path, contract)
        result = self._run_main(d)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (d / ".ai" / "setup-complete").exists()
        assert "Setup written to" in result.stdout

    def test_main_failure_keeps_setup_rerunnable(self, tmp_path):
        d = self._render_dir(tmp_path, SAMPLE_CONTRACT)
        result = self._run_main(d)
        assert result.returncode == 2, result.stdout + result.stderr
        assert not (d / ".ai" / "setup-complete").exists()
        assert "NOT marked complete" in result.stdout
        # And a second run still enters setup (not "already complete")
        result2 = self._run_main(d, env_extra={"LITELLM_MASTER_KEY": "v"})
        assert result2.returncode == 0
        assert (d / ".ai" / "setup-complete").exists()
