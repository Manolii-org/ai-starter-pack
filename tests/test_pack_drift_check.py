#!/usr/bin/env python3
"""Test suite for pack-drift-check.py — pack self-consistency validation."""
import tempfile
import unittest
import importlib.util
from pathlib import Path

# Load pack-drift-check.py as a module (hyphenated filename)
check_module_path = Path(__file__).resolve().parent.parent / "scripts" / "pack-drift-check.py"
spec = importlib.util.spec_from_file_location("pack_drift_check", check_module_path)
pack_drift_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack_drift_check)


class TestPackDriftFindPackRoot(unittest.TestCase):
    """Test find_pack_root() function."""

    def test_find_pack_root_from_script(self):
        """find_pack_root() should find parent of scripts dir."""
        root = pack_drift_check.find_pack_root()
        self.assertTrue((root / "scripts").exists())
        self.assertTrue((root / ".claude").exists())


class TestPackDriftShouldScanFile(unittest.TestCase):
    """Test should_scan_file() function."""

    def test_should_scan_claude_agent_file(self):
        """Should scan .claude/agents/*.md files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            agents_dir = pack_root / ".claude" / "agents"
            agents_dir.mkdir(parents=True)
            agent_file = agents_dir / "test_agent.md"
            agent_file.touch()
            
            result = pack_drift_check.should_scan_file(agent_file, pack_root)
            self.assertTrue(result)

    def test_should_scan_skill_file(self):
        """Should scan .claude/skills/*/SKILL.md files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            skill_dir = pack_root / ".claude" / "skills" / "myskill"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.touch()
            
            result = pack_drift_check.should_scan_file(skill_file, pack_root)
            self.assertTrue(result)

    def test_should_skip_brand_dir(self):
        """Should skip .brand/ directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            brand_dir = pack_root / ".brand"
            brand_dir.mkdir()
            brand_file = brand_dir / "branded.yml"
            brand_file.touch()
            
            result = pack_drift_check.should_scan_file(brand_file, pack_root)
            self.assertFalse(result)

    def test_should_skip_pycache(self):
        """Should skip __pycache__ directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            cache_dir = pack_root / "__pycache__"
            cache_dir.mkdir()
            cache_file = cache_dir / "module.pyc"
            cache_file.touch()
            
            result = pack_drift_check.should_scan_file(cache_file, pack_root)
            self.assertFalse(result)

    def test_should_skip_git_dir(self):
        """Should skip .git directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            git_dir = pack_root / ".git"
            git_dir.mkdir()
            git_file = git_dir / "config"
            git_file.touch()
            
            result = pack_drift_check.should_scan_file(git_file, pack_root)
            self.assertFalse(result)

    def test_should_scan_ai_security_file(self):
        """Should scan .ai/security/ files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            security_dir = pack_root / ".ai" / "security"
            security_dir.mkdir(parents=True)
            security_file = security_dir / "token-shapes.json"
            security_file.touch()
            
            result = pack_drift_check.should_scan_file(security_file, pack_root)
            self.assertTrue(result)

    def test_should_skip_unsupported_extension(self):
        """Should skip files with unsupported extensions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            bin_file = pack_root / "binary.bin"
            bin_file.touch()
            
            result = pack_drift_check.should_scan_file(bin_file, pack_root)
            self.assertFalse(result)


class TestPackDriftScanOrgLeak(unittest.TestCase):
    """Test scan_org_leak() function."""

    def test_scan_org_leak_clean_pack(self):
        """Clean pack with no org names should PASS."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            doc_file = pack_root / "README.md"
            doc_file.write_text("# My Package\nThis is a generic package.")
            
            results = pack_drift_check.scan_org_leak(pack_root)
            pass_results = [r for r in results if r.status == "PASS"]
            self.assertGreater(len(pass_results), 0)

    def test_scan_org_leak_finds_planted_org_name(self):
        """Should detect a planted org name and FAIL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            doc_file = pack_root / "README.md"
            # Build org name without putting literal in test source
            org_name = "man" + "olii"
            doc_file.write_text(f"This package is from {org_name}.")
            
            results = pack_drift_check.scan_org_leak(pack_root)
            fail_results = [r for r in results if r.status == "FAIL"]
            self.assertGreater(len(fail_results), 0)

    def test_scan_org_leak_finds_second_planted_org_name(self):
        """Should detect a second planted org name and FAIL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            doc_file = pack_root / "README.md"
            org_name = "impakt" + "ful"
            doc_file.write_text(f"Built by {org_name} team.")
            
            results = pack_drift_check.scan_org_leak(pack_root)
            fail_results = [r for r in results if r.status == "FAIL"]
            self.assertGreater(len(fail_results), 0)

    def test_scan_org_leak_skips_detector_files(self):
        """Should skip pack-drift-check.py itself when scanning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            scripts_dir = pack_root / "scripts"
            scripts_dir.mkdir()
            detector = scripts_dir / "pack-drift-check.py"
            # Even though it contains a planted org name, it should be skipped
            org_name = "man" + "olii"
            detector.write_text(f"# This file checks for {org_name}")
            
            results = pack_drift_check.scan_org_leak(pack_root)
            fail_results = [r for r in results if r.status == "FAIL"]
            self.assertEqual(len(fail_results), 0)


@unittest.skipIf(pack_drift_check.yaml is None, "PyYAML unavailable")
class TestFeatureExcludesCopier(unittest.TestCase):
    """Test check_feature_excludes() with Copier conditionals."""

    def test_conditional_file_valid_flag(self):
        """Conditional filename with declared bool flag should PASS."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            copier_yml = pack_root / "copier.yml"
            copier_yml.write_text("myflag:\n  type: bool\n  default: false\n")

            feature_file = pack_root / "{% if myflag %}feature.md{% endif %}"
            feature_file.write_text("# Feature\n")

            results = pack_drift_check.check_feature_excludes(pack_root)
            pass_results = [r for r in results if r.status == "PASS"]
            self.assertGreater(len(pass_results), 0)

    def test_conditional_file_unknown_flag(self):
        """Conditional filename with undeclared flag should FAIL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            copier_yml = pack_root / "copier.yml"
            copier_yml.write_text("myflag:\n  type: bool\n  default: false\n")

            feature_file = pack_root / "{% if otherflag %}feature.md{% endif %}"
            feature_file.write_text("# Feature\n")

            results = pack_drift_check.check_feature_excludes(pack_root)
            fail_results = [r for r in results if r.status == "FAIL"]
            self.assertGreater(len(fail_results), 0)
            self.assertTrue(any("otherflag" in r.detail for r in fail_results))

    def test_malformed_conditional_filename(self):
        """Malformed conditional (contains {% but does not match pattern) should FAIL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            copier_yml = pack_root / "copier.yml"
            copier_yml.write_text("myflag:\n  type: bool\n  default: false\n")

            bad_file = pack_root / "{% if broken feature.md"
            bad_file.write_text("# Broken\n")

            results = pack_drift_check.check_feature_excludes(pack_root)
            fail_results = [r for r in results if r.status == "FAIL"]
            self.assertGreater(len(fail_results), 0)
            self.assertTrue(any("malformed" in r.detail for r in fail_results))

    def test_missing_copier_yml(self):
        """Missing copier.yml should warn (not fail)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)

            results = pack_drift_check.check_feature_excludes(pack_root)
            warn_results = [r for r in results if r.status == "WARN"]
            fail_results = [r for r in results if r.status == "FAIL"]
            self.assertGreater(len(warn_results), 0)
            self.assertEqual(len(fail_results), 0)

    def test_legacy_manifest_nonexistent_path(self):
        """Legacy pack.manifest.yml with nonexistent path should WARN (not FAIL)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            copier_yml = pack_root / "copier.yml"
            copier_yml.write_text("myflag:\n  type: bool\n  default: false\n")

            feature_file = pack_root / "{% if myflag %}valid.md{% endif %}"
            feature_file.write_text("# Valid\n")

            manifest_yml = pack_root / "pack.manifest.yml"
            manifest_yml.write_text("feature_excludes:\n  legacy_feature:\n    - nonexistent/path.md\n")

            results = pack_drift_check.check_feature_excludes(pack_root)
            fail_results = [r for r in results if r.status == "FAIL"]
            warn_results = [r for r in results if r.status == "WARN"]
            self.assertEqual(len(fail_results), 0)
            self.assertGreater(len(warn_results), 0)
            self.assertTrue(any("legacy" in r.detail for r in warn_results))


class TestPackDriftIntegration(unittest.TestCase):
    """Integration tests simulating real pack scenarios."""

    def test_realistic_clean_pack(self):
        """A realistic clean pack should pass org-leak check."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            # Create typical pack structure
            (pack_root / ".claude" / "agents").mkdir(parents=True)
            (pack_root / ".claude" / "hooks").mkdir(parents=True)
            (pack_root / ".ai" / "security").mkdir(parents=True)
            (pack_root / "scripts").mkdir()
            
            # Add clean files
            (pack_root / ".claude" / "agents" / "test.md").write_text("# Test Agent\n\nGeneric agent.")
            (pack_root / "README.md").write_text("# AI Starter Pack\nA generic AI framework.")
            (pack_root / ".ai" / "security" / "guards.json").write_text("{}")
            
            results = pack_drift_check.scan_org_leak(pack_root)
            fail_results = [r for r in results if r.status == "FAIL"]
            self.assertEqual(len(fail_results), 0)

    def test_realistic_leaked_pack(self):
        """Pack with org names should FAIL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            (pack_root / ".claude" / "agents").mkdir(parents=True)
            
            org_name = "lead" + "-converter"
            (pack_root / ".claude" / "agents" / "sales.md").write_text(
                f"# Sales Agent\n\nIntegrates with {org_name}."
            )
            
            results = pack_drift_check.scan_org_leak(pack_root)
            fail_results = [r for r in results if r.status == "FAIL"]
            self.assertGreater(len(fail_results), 0)


    def test_agent_model_handles_empty_and_null_frontmatter(self):
        """AGENT-MODEL should warn, not crash, on malformed frontmatter shapes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            agents = pack_root / ".claude" / "agents"
            agents.mkdir(parents=True)
            (agents / "empty.md").write_text("---\n\n---\n# Empty\n", encoding="utf-8")
            (agents / "null-model.md").write_text("---\nmodel:\n---\n# Null model\n", encoding="utf-8")

            results = pack_drift_check.check_agent_models(pack_root)
            self.assertTrue(any(r.status == "WARN" and "frontmatter" in r.detail for r in results))
            self.assertFalse(any(r.status == "FAIL" for r in results))

    def test_readme_counts_reports_mismatch(self):
        """README-COUNTS should compare README count to agent files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            agents = pack_root / ".claude" / "agents"
            agents.mkdir(parents=True)
            (pack_root / "README-STARTER-PACK.md").write_text("**Agents** (2 files)\n", encoding="utf-8")
            (agents / "one.md").write_text("---\nmodel: haiku\n---\n# One\n", encoding="utf-8")

            results = pack_drift_check.check_readme_counts(pack_root)
            self.assertTrue(any(r.status == "WARN" for r in results))


if __name__ == "__main__":
    unittest.main()
