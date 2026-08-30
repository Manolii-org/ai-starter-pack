"""Tests for guard_check module — path matching and decision logic."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from guard_check import (
    check_edit,
    check_bash,
    _path_matches,
    _strip_quoted,
    _load_guards,
    _region_changed,
)


class TestPathMatches:
    """Test glob-pattern matching for guarded paths."""

    def test_exact_match(self):
        """Exact filename match."""
        assert _path_matches("model-routing.json", ["model-routing.json"])

    def test_glob_wildcard_single_star(self):
        """Single * matches within a directory level (fnmatch allows nested)."""
        assert _path_matches("migrations/0001_init.sql", ["migrations/*.sql"])
        # fnmatch treats * as matching any chars including /, so nested paths match too
        assert _path_matches("migrations/subdir/0001_init.sql", ["migrations/*.sql"])

    def test_glob_wildcard_double_star(self):
        """Double ** matches across directory levels."""
        assert _path_matches("migrations/0001_init.sql", ["migrations/**"])
        assert _path_matches("migrations/subdir/0001_init.sql", ["migrations/**"])

    def test_no_match(self):
        """Unrelated path does not match."""
        assert not _path_matches("src/main.py", ["model-routing.json"])

    def test_absolute_path_fallback_suffix(self):
        """Absolute paths (relativization failed) match via */pattern suffix."""
        assert _path_matches("/unknown/model-routing.json", ["model-routing.json"])

    def test_multiple_patterns_any_match(self):
        """Any pattern match returns True."""
        patterns = [".ai/guards.json", "model-routing.json"]
        assert _path_matches("model-routing.json", patterns)
        assert _path_matches(".ai/guards.json", patterns)
        assert not _path_matches("other.txt", patterns)


class TestStripQuoted:
    """Test quote and heredoc stripping for Bash command analysis."""

    def test_single_quoted_string(self):
        """Single-quoted strings are blanked."""
        result = _strip_quoted("echo 'hello > world'")
        assert "> world" not in result
        assert "echo" in result

    def test_double_quoted_string(self):
        """Double-quoted strings are blanked."""
        result = _strip_quoted('echo "data > file.txt"')
        assert "> file.txt" not in result

    def test_escaped_quote_in_double_quoted(self):
        """Escaped quotes in double-quoted strings are handled."""
        result = _strip_quoted('echo "say \\"hello\\""')
        # Should not crash; the escaped quote is handled
        assert len(result) == len('echo "say \\"hello\\""')

    def test_heredoc_stripped(self):
        """Heredoc body is blanked but markers remain."""
        cmd = """cat << EOF
> /etc/passwd
EOF"""
        result = _strip_quoted(cmd)
        assert "/etc/passwd" not in result
        assert "EOF" in result

    def test_heredoc_with_dash_and_quotes(self):
        """Heredoc with <<- and quoted marker."""
        cmd = """cat <<- 'EOF'
write this file
EOF"""
        result = _strip_quoted(cmd)
        assert "write this file" not in result
        assert "EOF" in result

    def test_mixed_quotes_and_unquoted(self):
        """Mixed quoted and unquoted text."""
        result = _strip_quoted("echo 'safe' > output.txt")
        # The unquoted > output.txt should remain visible
        assert "> output.txt" in result


class TestLoadGuards:
    """Test guards.json loading and error handling."""

    def test_missing_guards_file(self, tmp_path):
        """Missing .ai/guards.json returns empty config."""
        result = _load_guards(tmp_path)
        assert result == {"guards": [], "session_unfreezes": []}

    def test_valid_guards_json(self, tmp_path):
        """Valid guards.json is loaded correctly."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(
            json.dumps({
                "guards": [{"id": "test-1", "paths": ["*.json"]}],
                "session_unfreezes": ["test-1"]
            }),
            encoding="utf-8"
        )
        result = _load_guards(tmp_path)
        assert len(result["guards"]) == 1
        assert result["session_unfreezes"] == ["test-1"]

    def test_malformed_json(self, tmp_path):
        """Malformed JSON returns empty config."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text("{ invalid json }", encoding="utf-8")
        result = _load_guards(tmp_path)
        assert result == {"guards": [], "session_unfreezes": []}


class TestRegionChanged:
    """Test JSON region-level guard logic."""

    def test_region_unchanged_simple(self, tmp_path):
        """Edit that doesn't touch the guarded region."""
        json_file = tmp_path / "config.json"
        json_file.write_text(json.dumps({
            "protected": {"key": "value"},
            "other": "data"
        }), encoding="utf-8")
        # Edit only the "other" field
        result = _region_changed(
            json_file,
            "protected.key",
            '"other": "data"',
            '"other": "modified"'
        )
        assert result is False

    def test_region_changed_direct(self, tmp_path):
        """Edit that changes the guarded region."""
        json_file = tmp_path / "config.json"
        json_file.write_text(json.dumps({
            "protected": {"key": "value"}
        }), encoding="utf-8")
        result = _region_changed(
            json_file,
            "protected.key",
            '"key": "value"',
            '"key": "changed"'
        )
        assert result is True

    def test_region_write_full_content(self, tmp_path):
        """Write tool (no edit_old) triggers conservative block."""
        json_file = tmp_path / "config.json"
        json_file.write_text(json.dumps({"protected": {"key": "value"}}), encoding="utf-8")
        result = _region_changed(
            json_file,
            "protected.key",
            None,  # Write tool sends no edit_old
            json.dumps({"protected": {"key": "new"}})
        )
        assert result is True

    def test_region_fragment_not_found(self, tmp_path):
        """Fragment not found in file triggers conservative block."""
        json_file = tmp_path / "config.json"
        json_file.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        result = _region_changed(
            json_file,
            "key",
            "NOT FOUND",
            '"key": "new"'
        )
        assert result is True

    def test_region_nonexistent_json_path(self, tmp_path):
        """Nonexistent path: None -> None is unchanged."""
        json_file = tmp_path / "config.json"
        json_file.write_text(json.dumps({"other": "data"}), encoding="utf-8")
        result = _region_changed(
            json_file,
            "protected.missing",
            None,
            json.dumps({"other": "data"})
        )
        # None (old) -> None (new) is unchanged
        assert result is False

    def test_region_missing_file_write(self, tmp_path):
        """Writing to a nonexistent file is conservative block."""
        json_file = tmp_path / "new.json"
        result = _region_changed(
            json_file,
            "key",
            None,
            json.dumps({"key": "value"})
        )
        assert result is True


class TestCheckEdit:
    """Test edit guard checks."""

    def test_unguarded_path_allowed(self, tmp_path):
        """Path not matching any guard is allowed."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({"guards": [], "session_unfreezes": []}), encoding="utf-8")

        result = check_edit("src/main.py", tmp_path)
        assert result["action"] == "allow"

    def test_guarded_path_blocked(self, tmp_path):
        """Path matching a guard is blocked."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [
                {
                    "id": "test-guard",
                    "paths": ["model-routing.json"],
                    "reason": "guarded routing config"
                }
            ],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_edit("model-routing.json", tmp_path)
        assert result["action"] == "block"
        assert result["guard_id"] == "test-guard"
        assert "routing config" in result["reason"]

    def test_session_unfreeze_allows(self, tmp_path):
        """Session unfreeze allows a guarded edit."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [
                {
                    "id": "test-guard",
                    "paths": ["model-routing.json"]
                }
            ],
            "session_unfreezes": ["test-guard"]
        }), encoding="utf-8")

        result = check_edit("model-routing.json", tmp_path)
        assert result["action"] == "allow_unfreeze"
        assert result["guard_id"] == "test-guard"

    def test_guards_file_unfreeze_only_edit_allowed(self, tmp_path):
        """Edit to .ai/guards.json touching only session_unfreezes is allowed."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["*.json"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_edit(
            str(guards_file),
            tmp_path,
            edit_old='"session_unfreezes": []',
            edit_new='"session_unfreezes": ["g1"]'
        )
        assert result["action"] == "allow_unfreeze"
        assert result["operator_reason"] == "session_unfreezes_only_edit"

    def test_guards_file_other_edit_blocked(self, tmp_path):
        """Edit to .ai/guards.json changing guards is blocked."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["*.json"]}]
        }), encoding="utf-8")

        result = check_edit(
            str(guards_file),
            tmp_path,
            edit_old='"id": "g1"',
            edit_new='"id": "modified"'
        )
        # This will be blocked by the guards-config guard
        assert result["action"] == "block"

    def test_relative_path_normalized(self, tmp_path):
        """Relative paths are correctly normalized against repo root."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["config/*.json"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_edit("config/routing.json", tmp_path)
        assert result["action"] == "block"

    def test_absolute_path_handling(self, tmp_path):
        """Absolute paths are correctly resolved."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["*.json"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        target = tmp_path / "test.json"
        result = check_edit(str(target), tmp_path)
        assert result["action"] == "block"

    def test_region_guard_unchanged_region_allowed(self, tmp_path):
        """Region guard allows edit if guarded region is unchanged."""
        json_file = tmp_path / "config.json"
        json_file.write_text(json.dumps({
            "protected": {"key": "value"},
            "other": "data"
        }), encoding="utf-8")

        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [
                {
                    "id": "region-guard",
                    "paths": ["config.json"],
                    "regions": [{"json_path": "protected.key"}]
                }
            ],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_edit(
            str(json_file),
            tmp_path,
            edit_old='"other": "data"',
            edit_new='"other": "modified"'
        )
        assert result["action"] == "allow"


class TestCheckBash:
    """Test Bash command write-pattern detection."""

    def test_redirect_detected(self, tmp_path):
        """Bash > redirect is detected and checked."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["model-routing.json"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_bash("echo 'data' > model-routing.json", tmp_path)
        assert result["action"] == "block"
        assert result["guard_id"] == "g1"

    def test_append_detected(self, tmp_path):
        """Bash >> append is detected and checked."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["*.log"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_bash("echo 'log' >> output.log", tmp_path)
        assert result["action"] == "block"

    def test_tee_detected(self, tmp_path):
        """tee command is detected."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["secrets.txt"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_bash("echo 'data' | tee secrets.txt", tmp_path)
        assert result["action"] == "block"

    def test_sed_in_place_detected(self, tmp_path):
        """sed -i command is detected."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": [".ai/guards.json"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_bash("sed -i 's/old/new/' .ai/guards.json", tmp_path)
        assert result["action"] == "block"

    def test_dd_of_detected(self, tmp_path):
        """dd of=path is detected."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["disk.img"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_bash("dd if=/dev/sda of=disk.img", tmp_path)
        assert result["action"] == "block"

    def test_quoted_redirect_ignored(self, tmp_path):
        """Redirect inside quotes is not flagged."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["secret.json"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        # The > secret.json is inside a string, so should be ignored
        result = check_bash('echo "redirect > secret.json"', tmp_path)
        assert result["action"] == "allow"

    def test_dev_special_files_ignored(self, tmp_path):
        """Writes to /dev/* are not guarded."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["*"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_bash("echo 'data' > /dev/null", tmp_path)
        assert result["action"] == "allow"

    def test_dash_special_file_ignored(self, tmp_path):
        """Writes to - (stdout) are not guarded."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["*"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_bash("tee -", tmp_path)
        assert result["action"] == "allow"

    def test_no_writes_allowed(self, tmp_path):
        """Command with no detected writes is allowed."""
        guards_file = tmp_path / ".ai" / "guards.json"
        guards_file.parent.mkdir(parents=True)
        guards_file.write_text(json.dumps({
            "guards": [{"id": "g1", "paths": ["*"]}],
            "session_unfreezes": []
        }), encoding="utf-8")

        result = check_bash("cat file.txt && echo 'ok'", tmp_path)
        assert result["action"] == "allow"
