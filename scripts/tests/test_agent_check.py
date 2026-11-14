#!/usr/bin/env python3
"""Exercise local execution, invalidation and native-surface delivery."""
from __future__ import annotations

import concurrent.futures
import errno
from contextlib import contextmanager, nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ascii_environment():
    return load("agent-check").git_environment({
        **os.environ, "LC_ALL": "C", "LANG": "C",
        "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
    })


class AgentCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for directory in ("scripts", "config", "src"):
            (self.root / directory).mkdir()
        shutil.copyfile(SCRIPTS / "agent-check.py", self.root / "scripts/agent-check.py")
        (self.root / ".gitignore").write_text(".ai/agent-checks/\nnode_modules/\n")
        (self.root / "src/value.py").write_text("value = 1\n")
        (self.root / "lock.json").write_text("{}\n")
        self.config = {
            "schema_version": 1, "base": "origin/main", "environments": [],
            "checks": [{"id": "test", "paths": ["src/*", "lock.json", "config/agent-checks.json"],
                        "commands": [[sys.executable, "-c", "from pathlib import Path; assert 'value = 1' in Path('src/value.py').read_text()"]]}],
        }
        self.save_config()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE,
                                       env=load("agent-check").git_environment())

    def save_config(self):
        (self.root / "config/agent-checks.json").write_text(json.dumps(self.config))

    def run_check(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(self.root / "scripts/agent-check.py"),
                                 "--repo-root", str(self.root), "--provider", "codex", *args],
                                capture_output=True, text=True,
                                env=load("agent-check").git_environment() if env is None else env, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def receipt(self):
        return json.loads((self.root / ".ai/agent-checks/receipt.json").read_text())

    def change_source(self):
        (self.root / "src/value.py").write_text("value = 1\n# changed\n")

    def test_external_diff_cannot_hide_a_failing_edit_from_cached_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            driver = Path(temp) / "display-diff"
            driver.write_text("#!/bin/sh\nprintf 'display only\\n'\n")
            driver.chmod(0o755)
            env = {**os.environ, "GIT_EXTERNAL_DIFF": str(driver)}
            self.change_source()
            first = self.run_check(env=env)
            self.assertIn("AGENT-CHECK test:", first.stdout)
            (self.root / "src/value.py").write_text("value = 0\n# changed\n")
            self.run_check(expected=1, env=env)
            self.assertFalse(self.receipt()["cached"])

    def test_textconv_cannot_hide_a_failing_edit_from_cached_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            driver = Path(temp) / "textconv"
            driver.write_text("#!/bin/sh\nprintf 'display only\\n'\n")
            driver.chmod(0o755)
            (self.root / ".gitattributes").write_text("src/value.py diff=display\n")
            self.git("add", ".gitattributes")
            self.git("commit", "-qm", "fixture attributes")
            self.git("update-ref", "refs/remotes/origin/main", "HEAD")
            self.git("config", "diff.display.textconv", str(driver))
            self.change_source()
            first = self.run_check()
            self.assertIn("AGENT-CHECK test:", first.stdout)
            (self.root / "src/value.py").write_text("value = 0\n# changed\n")
            self.run_check(expected=1)
            self.assertFalse(self.receipt()["cached"])

    def test_git_color_configuration_does_not_invalidate_a_valid_cache(self):
        self.change_source()
        self.run_check()
        self.git("config", "color.ui", "always")
        self.run_check()
        self.assertTrue(self.receipt()["cached"])

    def test_clean_filter_cannot_hide_a_failing_working_file(self):
        driver = self.root / ".git/clean-source"
        driver.write_text("#!" + sys.executable + "\nimport sys\nbody=sys.stdin.read()\n"
                          "print('candidate' if '# changed' in body else 'base')\n")
        driver.chmod(0o755)
        (self.root / ".gitattributes").write_text("src/value.py filter=constant\n")
        self.git("config", "filter.constant.clean", str(driver))
        self.git("add", ".gitattributes", "src/value.py")
        self.git("commit", "-qm", "fixture filter")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.change_source()
        normalized = self.git("diff", "--binary", "HEAD")
        (self.root / "src/value.py").write_text("value = 0\n# changed\n")
        self.assertEqual(normalized, self.git("diff", "--binary", "HEAD"))
        actual = subprocess.run(self.config["checks"][0]["commands"][0], cwd=self.root,
                                capture_output=True)
        self.assertNotEqual(actual.returncode, 0)
        for args in ((), ("--no-cache",), ("--plan",)):
            with self.subTest(args=args):
                result = self.run_check(*args, expected=2)
                self.assertIn("Git filter attributes are unsupported", result.stderr)
                self.assertNotIn("AGENT-CHECK-CACHED", result.stdout)
                self.assertFalse((self.root / ".ai/agent-checks/receipt.json").exists())

    def test_enabling_local_filter_revokes_warm_validation_before_selection(self):
        self.change_source()
        self.run_check()
        (self.root / ".git/info/attributes").write_text("src/value.py filter=local\n")
        result = self.run_check(expected=2)
        self.assertIn("Git filter attributes are unsupported", result.stderr)
        self.assertNotIn("AGENT-CHECK test:", result.stdout)
        self.assertFalse((self.root / ".ai/agent-checks/receipt.json").exists())

    def test_filter_driver_names_cannot_masquerade_as_attribute_sentinels(self):
        for value in ("unspecified", "unset", "set"):
            with self.subTest(value=value):
                (self.root / ".git/info/attributes").write_text(f"src/value.py filter={value}\n")
                result = self.run_check(expected=2)
                self.assertIn("Git filter attributes are unsupported", result.stderr)

    def test_index_flags_revoke_warm_validation_before_cache_or_selection(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self.change_source()
                self.run_check()
                self.git("update-index", flag, "src/value.py")
                (self.root / "src/value.py").write_text("value = 0\n# changed\n")
                for args in ((), ("--no-cache",), ("--plan",)):
                    result = self.run_check(*args, expected=2)
                    self.assertIn("Git index flags hide", result.stderr)
                    self.assertNotIn("AGENT-CHECK-CACHED", result.stdout)
                    self.assertFalse((self.root / ".ai/agent-checks/receipt.json").exists())
                self.git("update-index", flag.replace("--", "--no-"), "src/value.py")

    def test_fsmonitor_valid_cannot_hide_failing_bytes_from_a_warm_cache(self):
        self.change_source()
        self.git("add", "src/value.py")
        self.git("commit", "-qm", "fixture candidate")
        hook = self.root / ".git/fsmonitor-hook"
        hook.write_text("#!/bin/sh\nprintf 'fixture-clock\\0'\n")
        hook.chmod(0o755)
        self.git("config", "core.fsmonitor", str(hook))
        self.git("config", "core.fsmonitorHookVersion", "2")
        self.run_check()
        self.git("update-index", "--fsmonitor", "--fsmonitor-valid", "src/value.py")
        (self.root / "src/value.py").write_text("value = 0\n# changed\n")
        self.assertEqual(self.git("diff", "--binary", "HEAD"), b"")
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertIn("src/value.py", plan["paths"])
        self.assertIn("test", [item["id"] for item in plan["checks"]])
        result = self.run_check(expected=1)
        self.assertNotIn("AGENT-CHECK-CACHED", result.stdout)
        self.assertFalse(self.receipt()["cached"])

    def test_relaxed_stat_hints_cannot_hide_same_size_failing_bytes(self):
        self.git("config", "core.trustctime", "false")
        self.git("config", "core.checkStat", "minimal")
        self.change_source()
        path = self.root / "src/value.py"
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        self.git("add", "src/value.py")
        self.git("commit", "-qm", "fixture old-mtime candidate")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.run_check()
        old = path.stat()
        path.write_text("value = 0\n# changed\n")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(self.git("diff", "--binary", "HEAD"), b"")
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertIn("src/value.py", plan["paths"])
        self.assertIn("test", [item["id"] for item in plan["checks"]])
        result = self.run_check(expected=1)
        self.assertNotIn("AGENT-CHECK-CACHED", result.stdout)
        self.assertFalse(self.receipt()["cached"])

    def test_tracked_submodule_requires_a_raw_input_adapter(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        (self.root / "module").mkdir()
        (self.root / "module/working-file").write_text("unvalidated content")
        result = self.run_check("--plan", expected=2)
        self.assertIn("tracked directories/submodules need a raw-input validation adapter", result.stderr)

    def test_empty_uninitialized_gitlink_binds_index_identity_and_selects_changes(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        (self.root / "module").mkdir()
        self.git("commit", "-qm", "empty gitlink")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        runner = load("agent-check")
        self.assertEqual(runner.raw_tracked_inputs(self.root)["module"], ("160000", head))
        self.git("config", "core.autocrlf", "true")
        self.assertNotIn("module", json.loads(self.run_check("--plan").stdout)["paths"])
        self.run_check()
        replacement = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--cacheinfo", "160000," + replacement + ",module")
        self.assertIn("module", json.loads(self.run_check("--plan").stdout)["paths"])
        self.assertNotIn("AGENT-CHECK-CACHED", self.run_check().stdout)

    def test_empty_gitlink_cache_is_revoked_when_module_gains_content(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        (self.root / "module").mkdir()
        self.run_check()
        (self.root / "module/.git").write_text("gitdir: fixture")
        self.run_check(expected=2)
        self.assertFalse((self.root / ".ai/agent-checks/receipt.json").exists())

    def test_empty_directory_replacing_a_regular_tracked_file_stays_blocked(self):
        (self.root / "src/value.py").unlink()
        (self.root / "src/value.py").mkdir()
        self.run_check("--plan", expected=2)

    def test_empty_gitlink_does_not_reuse_absent_module_receipt(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        self.run_check()
        (self.root / "module").mkdir()
        self.assertNotIn("AGENT-CHECK-CACHED", self.run_check().stdout)

    def test_unmerged_empty_gitlink_stays_blocked(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        subprocess.run(["git", "update-index", "--index-info"], cwd=self.root,
                       input=("160000 " + head + " 1\tmodule\n").encode(), check=True,
                       env=load("agent-check").git_environment())
        (self.root / "module").mkdir()
        self.run_check("--plan", expected=2)

    def test_gitlink_directory_must_remain_empty_and_identical_across_index_read(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        module = self.root / "module"
        module.mkdir()
        runner = load("agent-check")
        original = runner.git
        for change in ("populate", "replace"):
            with self.subTest(change=change):
                def concurrent_read(root, *args):
                    if args[:2] == ("ls-files", "--stage"):
                        if change == "populate":
                            (module / "unvalidated").write_text("changed during Git read")
                        else:
                            module.rename(self.root / "previous-module")
                            module.mkdir()
                    return original(root, *args)
                with mock.patch.object(runner, "git", side_effect=concurrent_read):
                    with self.assertRaisesRegex(ValueError, "raw-input validation adapter"):
                        runner.raw_tracked_inputs(self.root)
                if change == "populate":
                    (module / "unvalidated").unlink()

    def test_gitlink_scan_stays_bound_across_an_aba_name_swap(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        module = self.root / "module"
        module.mkdir()
        (module / "unvalidated").write_text("must not disappear from the scan")
        runner = load("agent-check")
        scandir, listdir = os.scandir, os.listdir
        saved = self.root / "original-module"
        def swap_scan(value):
            module.rename(saved)
            module.mkdir()
            try:
                with scandir(value) as scan:
                    entries = list(scan)
            finally:
                module.rmdir()
                saved.rename(module)
            return nullcontext(iter(entries))
        def swap_list(value):
            if Path(value) != module:
                return listdir(value)
            module.rename(saved)
            module.mkdir()
            try:
                return listdir(value)
            finally:
                module.rmdir()
                saved.rename(module)
        # Model coarse mutation timestamps: inode binding must independently
        # prevent scanning a replacement and accepting the restored original.
        with mock.patch.object(runner.os, "scandir", side_effect=swap_scan) as scan_mock, \
             mock.patch.object(runner.os, "listdir", side_effect=swap_list), \
             mock.patch.object(runner.os, "supports_fd", os.supports_fd | {scan_mock}), \
             mock.patch.object(runner, "directory_state", create=True,
                               side_effect=lambda s: (s.st_dev, s.st_ino, s.st_mode)):
            with self.assertRaisesRegex(ValueError, "raw-input validation adapter"):
                runner.raw_tracked_inputs(self.root)

    def test_gitlink_path_must_still_identify_scanned_directory(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        module = self.root / "module"
        module.mkdir()
        runner = load("agent-check")
        original = os.scandir
        def replace_after_scan(value):
            with original(value) as scan:
                entries = list(scan)
            module.rename(self.root / "original-module")
            module.mkdir()
            return nullcontext(iter(entries))
        with mock.patch.object(runner.os, "scandir", side_effect=replace_after_scan) as scan_mock, \
             mock.patch.object(runner.os, "supports_fd", os.supports_fd | {scan_mock}), \
             mock.patch.object(runner, "directory_state", create=True,
                               side_effect=lambda s: (s.st_dev, s.st_ino, s.st_mode)):
            with self.assertRaisesRegex(ValueError, "raw-input validation adapter"):
                runner.raw_tracked_inputs(self.root)

    def test_gitlink_access_time_changes_do_not_block_repeated_validation(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        (self.root / "module").mkdir()
        runner = load("agent-check")
        fstat, lstat = os.fstat, Path.lstat
        count = 0
        def access_only(details):
            nonlocal count
            count += 1
            return SimpleNamespace(**{name: (count if name in ("st_atime", "st_atime_ns") else getattr(details, name))
                                      for name in dir(details) if name.startswith("st_")})
        with mock.patch.object(runner.os, "fstat", side_effect=lambda fd: access_only(fstat(fd))), \
             mock.patch.object(Path, "lstat", autospec=True,
                               side_effect=lambda path: access_only(lstat(path))):
            for _ in range(3):
                self.assertEqual(runner.raw_tracked_inputs(self.root)["module"], ("160000", head))

    def test_gitlink_without_descriptor_enumeration_stays_blocked(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        (self.root / "module").mkdir()
        runner = load("agent-check")
        with mock.patch.object(runner.os, "supports_fd", set()), \
             mock.patch.object(runner.os, "open", side_effect=AssertionError("unsupported directory open")):
            with self.assertRaisesRegex(ValueError, "raw-input validation adapter"):
                runner.raw_tracked_inputs(self.root)

    def test_gitlink_descriptor_is_closed_on_success_and_git_timeout(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        (self.root / "module").mkdir()
        runner = load("agent-check")
        original = os.open
        for timeout in (False, True):
            descriptors = []
            def opened(*args, **kwargs):
                descriptor = original(*args, **kwargs)
                descriptors.append(descriptor)
                return descriptor
            try:
                with mock.patch.object(runner.os, "open", side_effect=opened):
                    if timeout:
                        with mock.patch.object(runner, "git", side_effect=subprocess.TimeoutExpired("git", 30)):
                            with self.assertRaises(subprocess.TimeoutExpired):
                                runner.empty_gitlink_identity(self.root, "module")
                    else:
                        self.assertEqual(runner.empty_gitlink_identity(self.root, "module"), head)
                self.assertEqual(len(descriptors), 1)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
            finally:
                for descriptor in descriptors:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def test_git_reads_are_bounded_and_timeout_revokes_old_evidence(self):
        runner = load("agent-check")
        calls = []
        def bounded(command, **kwargs):
            calls.append(command)
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 30)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with mock.patch.object(runner.subprocess, "run", side_effect=bounded):
            runner.git(self.root, "ls-files")
            runner.require_unfiltered_tracked_inputs(self.root)
        self.assertEqual(len(calls), 4)
        self.run_check()
        with mock.patch.object(runner, "plan_for", side_effect=subprocess.TimeoutExpired(["git"], 30)):
            self.assertEqual(runner.main(["--repo-root", str(self.root), "--provider", "codex"]), 2)
        self.assertFalse((self.root / ".ai/agent-checks/receipt.json").exists())

    def clean_conversion_fixture(self, attributes, *, encoding="utf-8"):
        path = self.root / "src/value.py"
        (self.root / ".gitattributes").write_text("src/value.py " + attributes + "\n")
        path.write_text("value = 1\n# $Id$\n", encoding=encoding)
        self.git("add", ".gitattributes", "src/value.py")
        self.git("commit", "-qm", "fixture checkout conversion")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        path.unlink()
        self.git("checkout", "HEAD", "--", "src/value.py")
        return path

    def test_text_auto_lf_checkout_avoids_per_path_filtered_git_processes(self):
        runner = load("agent-check")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.eol", "lf")
        (self.root / ".gitattributes").write_text("* text=auto\n")
        for index in range(80):
            (self.root / "src" / f"value-{index}.txt").write_text("synthetic LF content\n")
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic text auto source")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        with mock.patch.object(runner, "git", wraps=runner.git) as calls:
            self.assertEqual(runner.plan_for(self.root, self.config)["paths"], [])
        filtered = [c for c in calls.call_args_list if "cat-file" in c.args and "--filters" in c.args]
        self.assertEqual(filtered, [])
        self.git("config", "core.autocrlf", "true")
        with mock.patch.object(runner, "git", wraps=runner.git) as calls:
            plan = runner.plan_for(self.root, self.config)
        self.assertTrue(any("cat-file" in c.args and "--filters" in c.args for c in calls.call_args_list))
        self.assertIn("src/value.py", plan["paths"])

    def test_dotenv_limits_refuse_payload_read_and_disable_pass_reuse(self):
        runner = load("agent-check")
        plan = {"checks": [], "environments": []}
        large = self.root / ".env.large"
        large.write_bytes(b"synthetic-over-limit")
        with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 2), mock.patch.object(Path, "open", side_effect=AssertionError("oversized payload read")):
            self.assertIsNone(runner.dotenv_identity(self.root, plan, b"k" * 32)[".env.large"])
        self.change_source()
        with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 2), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(runner.main(["--repo-root", str(self.root)]), 0)
            self.assertFalse(self.receipt()["cache_reusable"])
            self.assertEqual(runner.main(["--repo-root", str(self.root)]), 0)
            self.assertFalse(self.receipt()["cached"])
            self.assertFalse(self.receipt()["cache_reusable"])

    def test_dotenv_entry_limit_stops_before_directory_payloads_and_shared_byte_limit(self):
        runner = load("agent-check")
        plan = {"checks": [], "environments": []}
        directory = self.root / ".env.d"
        directory.mkdir()
        for name in ("one", "two", "three"):
            (directory / name).write_text("synthetic")
        with mock.patch.object(runner, "READY_IDENTITY_MAX_ENTRIES", 2), mock.patch.object(Path, "open", side_effect=AssertionError("unbounded tree payload read")):
            self.assertIn(None, runner.dotenv_identity(self.root, plan, b"k" * 32).values())
            self.assertIsNone(runner.dotenv_bytes(directory))
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
        (self.root / ".env.a").write_text("12")
        (self.root / ".env.b").write_text("34")
        with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 3):
            result = runner.dotenv_identity(self.root, plan, b"k" * 32)
        self.assertIsInstance(result[".env.a"], str)
        self.assertIsNone(result[".env.b"])

    def test_clean_crlf_checkout_does_not_select_unchanged_checks(self):
        path = self.clean_conversion_fixture("text eol=crlf")
        self.assertIn(b"\r\n", path.read_bytes())
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertNotIn("src/value.py", plan["paths"])
        self.assertEqual(plan["checks"], [])

    def test_clean_encoded_checkout_does_not_select_unchanged_checks(self):
        self.clean_conversion_fixture("working-tree-encoding=UTF-16LE", encoding="utf-16-le")
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertNotIn("src/value.py", plan["paths"])
        self.assertEqual(plan["checks"], [])

    def test_ident_checkout_is_clean_but_an_edit_hidden_by_clean_conversion_is_checked(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert b'altered' not in Path('src/value.py').read_bytes()"]]
        self.save_config()
        self.git("add", "config/agent-checks.json")
        path = self.clean_conversion_fixture("ident")
        self.assertIn(b"$Id: ", path.read_bytes())
        self.assertNotIn("src/value.py", json.loads(self.run_check("--plan").stdout)["paths"])
        self.run_check()
        import re
        path.write_bytes(re.sub(rb"\$Id:.*?\$", b"$Id: altered $", path.read_bytes()))
        self.assertEqual(self.git("diff", "HEAD"), b"")
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertIn("src/value.py", plan["paths"])
        self.run_check(expected=1)
        self.assertFalse(self.receipt()["cached"])

    def test_unexpanded_ident_equal_to_canonical_blob_still_selects_owning_check(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert b'$Id: ' in Path('src/value.py').read_bytes()"]]
        self.save_config()
        self.git("add", "config/agent-checks.json")
        path = self.clean_conversion_fixture("ident")
        self.run_check()
        import re
        path.write_bytes(re.sub(rb"\$Id:.*?\$", b"$Id$", path.read_bytes()))
        self.assertEqual(self.git("diff", "HEAD"), b"")
        self.assertIn("src/value.py", json.loads(self.run_check("--plan").stdout)["paths"])
        self.run_check(expected=1)

    def test_autocrlf_clean_checkout_and_canonical_lf_edit_are_distinct_inputs(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert bytes([13,10]) in Path('src/value.py').read_bytes()"]]
        self.save_config()
        self.git("add", "config/agent-checks.json")
        self.git("commit", "-qm", "fixture CRLF owning check")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("config", "core.autocrlf", "true")
        # Recreate the checkout: checkout-index does not replace an existing
        # identical index entry merely because the checkout policy changed.
        (self.root / "src/value.py").unlink()
        self.git("checkout-index", "--force", "--all")
        path = self.root / "src/value.py"
        self.assertIn(b"\r\n", path.read_bytes())
        self.assertNotIn("src/value.py", json.loads(self.run_check("--plan").stdout)["paths"])
        self.run_check()
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
        self.assertEqual(self.git("diff", "HEAD"), b"")
        self.assertIn("src/value.py", json.loads(self.run_check("--plan").stdout)["paths"])
        self.run_check(expected=1)

    def test_ignored_executable_mode_is_not_an_extra_changed_path(self):
        self.git("config", "core.fileMode", "false")
        (self.root / "src/value.py").chmod(0o755)
        self.assertNotIn("src/value.py", json.loads(self.run_check("--plan").stdout)["paths"])

    def test_legacy_crlf_checkout_and_canonical_lf_edit_are_distinct_inputs(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert bytes([13,10]) in Path('src/value.py').read_bytes()"]]
        self.save_config()
        self.git("add", "config/agent-checks.json")
        self.git("config", "core.eol", "crlf")
        path = self.clean_conversion_fixture("crlf")
        self.assertIn(b"\r\n", path.read_bytes())
        self.assertNotIn("src/value.py", json.loads(self.run_check("--plan").stdout)["paths"])
        self.run_check()
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
        self.assertEqual(self.git("diff", "HEAD"), b"")
        self.assertIn("src/value.py", json.loads(self.run_check("--plan").stdout)["paths"])
        self.run_check(expected=1)

    def test_regular_checkout_of_symlink_is_not_an_extra_changed_path(self):
        path = self.root / "link.txt"
        path.symlink_to("target")
        self.git("add", "link.txt")
        self.git("commit", "-qm", "fixture link")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("config", "core.symlinks", "false")
        path.unlink()
        self.git("checkout", "HEAD", "--", "link.txt")
        self.assertFalse(path.is_symlink())
        self.assertNotIn("link.txt", json.loads(self.run_check("--plan").stdout)["paths"])

    def test_absent_skip_worktree_is_allowed_for_sparse_checkout(self):
        self.git("update-index", "--skip-worktree", "src/value.py")
        (self.root / "src/value.py").unlink()
        result = self.run_check("--plan")
        self.assertNotIn("Git index flags hide", result.stderr)

    def test_index_flag_paths_with_newlines_and_dangling_symlinks_are_checked(self):
        path = self.root / "src/hidden\nvalue.py"
        path.symlink_to("absent-target")
        self.git("add", str(path.relative_to(self.root)))
        self.git("commit", "-qm", "fixture symlink")
        self.git("update-index", "--skip-worktree", str(path.relative_to(self.root)))
        result = self.run_check("--plan", expected=2)
        self.assertIn("Git index flags hide", result.stderr)

    def test_concurrent_key_reader_waits_for_publisher_to_remove_temporary_link(self):
        runner = load("agent-check")
        published, release, waiting = threading.Event(), threading.Event(), threading.Event()
        original_link, original_sleep = runner.os.link, runner.time.sleep

        def paused_link(*args, **kwargs):
            original_link(*args, **kwargs)
            published.set()
            if not release.wait(5):
                raise RuntimeError("Fixture publisher was not released")

        def observed_wait(seconds):
            waiting.set()
            original_sleep(seconds)

        with mock.patch.object(runner.os, "link", side_effect=paused_link), mock.patch.object(
            runner, "time", wraps=runner.time
        ) as clock, concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            clock.sleep.side_effect = observed_wait
            writer = pool.submit(runner.private_cache_key, self.root)
            try:
                self.assertTrue(published.wait(5))
                reader = pool.submit(runner.private_cache_key, self.root)
                self.assertTrue(waiting.wait(1), "reader must observe the two-link publication window")
            finally:
                release.set()
            self.assertTrue(writer.result(timeout=5) == reader.result(timeout=5))
        path = self.root / ".ai/agent-checks/environment.key"
        self.assertEqual(len(path.read_bytes()), 32)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_persistent_cache_key_links_remain_rejected_with_a_bounded_wait(self):
        runner = load("agent-check")
        runner.private_cache_key(self.root)
        path = self.root / ".ai/agent-checks/environment.key"
        os.link(path, path.with_name("unexpected-link"))
        with mock.patch.object(runner, "time", wraps=runner.time) as clock:
            clock.sleep.return_value = None
            with self.assertRaisesRegex(ValueError, "private regular file"):
                runner.private_cache_key(self.root)
            self.assertLessEqual(clock.sleep.call_count, 20)
        self.assertEqual(path.stat().st_nlink, 2, "an unsafe key must not be repaired or replaced")

    def test_utf8_repository_and_receipts_ignore_default_locale(self):
        self.config["note"] = "Café validation"
        (self.root / "workflow.yml").write_text("# Café validation\n", encoding="utf-8")
        self.config["checks"][0]["ci_sources"] = [{"path": "workflow.yml", "contains": "Café validation"}]
        setup = ("from pathlib import Path; p=Path('node_modules'); p.mkdir(exist_ok=True); "
                 "c=p/'installs'; c.write_text(c.read_text()+'1' if c.exists() else '1')")
        self.config["environments"] = [{"id": "dependencies", "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        (self.root / "config/agent-checks.json").write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        env = ascii_environment()
        self.run_check("--provision", env=env)
        state = self.root / ".ai/agent-checks"
        for path in [*state.glob("environments/*.json"), *state.glob("cache/*.json")]:
            value = json.loads(path.read_text(encoding="utf-8"))
            value["note"] = "Café metadata"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.run_check("--provision", env=env)
        self.assertTrue(self.receipt()["cached"])
        self.assertEqual((self.root / "node_modules/installs").read_text(), "1")

    def test_invalid_utf8_environment_receipt_requires_fresh_setup(self):
        setup = ("from pathlib import Path; p=Path('node_modules'); p.mkdir(exist_ok=True); "
                 "c=p/'installs'; c.write_text(c.read_text()+'1' if c.exists() else '1')")
        self.config["environments"] = [{"id": "dependencies", "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        env = ascii_environment()
        self.run_check("--provision", env=env)
        receipt = next((self.root / ".ai/agent-checks/environments").glob("*.json"))
        receipt.write_bytes(b"\xff")
        self.run_check(expected=1, env=env)
        self.assertEqual(self.receipt()["outcome"], "failed")
        self.assertEqual((self.root / "node_modules/installs").read_text(), "1")
        self.run_check("--provision", env=env)
        self.assertEqual((self.root / "node_modules/installs").read_text(), "11")
        self.assertEqual(json.loads(receipt.read_text())["result"]["status"], "passed")

    def test_python_check_does_not_create_untracked_bytecode(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c", "import src.value"]]
        self.save_config()
        self.run_check()
        self.assertEqual(self.receipt()["outcome"], "passed")
        self.assertEqual(list((self.root / "src").rglob("*.pyc")), [])

    def test_execute_and_reuse_only_identical_candidate(self):
        self.change_source()
        self.run_check()
        first = self.receipt()
        self.assertFalse(first["cached"])
        self.assertFalse(first["merge_authority"])
        self.run_check()
        self.assertTrue(self.receipt()["cached"])
        (self.root / "src/value.py").write_text("value = 2\n")
        self.run_check(expected=1)
        self.assertNotEqual(first["digest"], self.receipt()["digest"])

    def test_inherited_execution_environment_invalidates_cached_pass_without_recording_values(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "import os; assert os.environ['AGENT_CHECK_FIXTURE_MODE'] == 'good'"]]
        self.save_config()
        env = {**load("agent-check").git_environment(), "AGENT_CHECK_FIXTURE_MODE": "good",
               "AGENT_CHECK_PRIVATE_FIXTURE": "private-fixture-value-not-a-credential"}
        self.run_check(env=env)
        before = self.receipt()["digest"]
        self.run_check(env=env)
        self.assertTrue(self.receipt()["cached"])
        env["AGENT_CHECK_FIXTURE_MODE"] = "bad"
        self.run_check(env=env, expected=1)
        self.assertNotEqual(before, self.receipt()["digest"])
        self.assertFalse(self.receipt()["cached"])
        for path in (self.root / ".ai/agent-checks").rglob("*.json"):
            self.assertNotIn(env["AGENT_CHECK_PRIVATE_FIXTURE"], path.read_text())

    def test_provisioning_must_create_every_declared_ready_path(self):
        self.config["environments"] = [{"id": "dependencies",
            "ready_paths": ["node_modules", "node_modules/installed"],
            "commands": [[sys.executable, "-c", "from pathlib import Path; Path('node_modules').mkdir(exist_ok=True)"]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check("--provision", expected=1)
        result = next(r for r in self.receipt()["results"] if r["id"] == "dependencies")
        self.assertEqual(result["status"], "failed")
        self.assertIn("ready", result["reason"])
        self.assertFalse((self.root / ".ai/agent-checks/environments").exists())

    def test_setup_cache_tracks_its_effective_inherited_environment(self):
        self.config["environments"] = [{"id": "dependencies", "ready_paths": ["node_modules/setting"],
            "commands": [[sys.executable, "-c", "import os; from pathlib import Path; "
                          "p=Path('node_modules'); p.mkdir(exist_ok=True); "
                          "(p/'setting').write_text(os.environ['AGENT_CHECK_FIXTURE_MODE'])"]]}]
        self.config["checks"][0].update(environment="dependencies", commands=[[sys.executable, "-c",
            "import os; from pathlib import Path; assert Path('node_modules/setting').read_text() == os.environ['AGENT_CHECK_FIXTURE_MODE']"]])
        self.save_config()
        env = {**load("agent-check").git_environment(), "AGENT_CHECK_FIXTURE_MODE": "first"}
        self.run_check("--provision", env=env)
        env["AGENT_CHECK_FIXTURE_MODE"] = "second"
        self.run_check(env=env, expected=1)
        self.run_check("--provision", env=env)
        self.assertEqual((self.root / "node_modules/setting").read_text(), "second")

    def test_pack_sast_only_change_requires_its_declared_release_python(self):
        installed = SCRIPTS.parent
        if not (installed / "scripts/build-plugin.py").is_file():
            self.skipTest("pack-maintainer SAST policy is not an installed consumer input")
        self.config = json.loads((SCRIPTS.parent / "config/agent-checks.json").read_text())
        self.assertTrue(any(item["id"] == "assessment-sast-failure-contract"
                            for item in self.config["checks"]),
                        "pack-maintainer recipe must declare assessment-sast-failure-contract")
        for check in self.config["checks"]:
            for source in check.get("ci_sources", []):
                path = self.root / source["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.read_text() + source["contains"] + "\n" if path.exists()
                                else source["contains"] + "\n")
        changed = self.root / "tests/test_routine_ci_repair_policy.py"
        changed.parent.mkdir()
        changed.write_text("# synthetic source-selection fixture\n")
        self.save_config()
        self.git("add", ".")
        self.git("commit", "-qm", "fixture production recipe")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        changed.write_text("# changed synthetic source-selection fixture\n")
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertEqual(plan["paths"], ["tests/test_routine_ci_repair_policy.py"])
        self.assertEqual([item["id"] for item in plan["checks"]],
                         ["assessment-sast-failure-contract"])
        self.assertEqual([item["id"] for item in plan["environments"]],
                         ["release-contract-python"])
        selected = plan["checks"][0]
        self.assertEqual({command[0] for command in selected["commands"]},
                         {".ai/agent-checks/release-venv/bin/python"})
        self.assertEqual(selected["timeout_seconds"], 180)
        # An ambient interpreter cannot satisfy an unprovisioned declared env.
        blocked = self.run_check(expected=1)
        self.assertNotIn("AGENT-CHECK assessment-sast-failure-contract:", blocked.stdout)
        self.assertTrue(any(item["id"] == "release-contract-python"
                            and item["status"] == "unavailable"
                            for item in self.receipt()["results"]))

    def test_pack_sast_budget_preserves_execution_context_and_failure(self):
        installed = SCRIPTS.parent
        if not (installed / "scripts/build-plugin.py").is_file():
            self.skipTest("pack-maintainer SAST policy is not an installed consumer input")
        config = json.loads((SCRIPTS.parent / "config/agent-checks.json").read_text())
        self.assertTrue(any(item["id"] == "assessment-sast-failure-contract"
                            for item in config["checks"]),
                        "pack-maintainer recipe must declare assessment-sast-failure-contract")
        check = next(item for item in config["checks"]
                     if item["id"] == "assessment-sast-failure-contract")
        runner = load("agent-check")
        control = {**check, "cwd": "src", "env": {"AGENT_CHECK_CONTROL": "retained"},
                   "commands": [[sys.executable, "-c",
                       "import os; assert os.environ['AGENT_CHECK_CONTROL']=='retained'; "
                       "assert os.path.basename(os.getcwd())=='src'; raise SystemExit(7)"],
                       [sys.executable, "-c", "raise AssertionError('later command ran')"]]}
        calls, commands = [], []
        native_run = runner.subprocess.run

        def observe(*args, **kwargs):
            if args[0] in control["commands"]:
                commands.append(args[0])
                calls.append(kwargs)
            return native_run(*args, **kwargs)

        with mock.patch.object(runner.subprocess, "run", side_effect=observe):
            result = runner.execute(self.root, control, runner.git_environment())
        self.assertEqual(result["id"], check["id"])
        self.assertEqual((result["status"], result["reason"]), ("failed", "exit=7"))
        self.assertEqual(commands, [control["commands"][0]])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["timeout"], 180)
        self.assertEqual(calls[0]["cwd"], self.root / "src")

    def test_later_setup_cannot_remove_an_earlier_readiness_postcondition(self):
        self.config["environments"] = [
            {"id": "first", "ready_paths": ["node_modules/first"], "commands": [[sys.executable, "-c",
                "from pathlib import Path; p=Path('node_modules'); p.mkdir(exist_ok=True); (p/'first').touch()"]]},
            {"id": "second", "ready_paths": ["node_modules/second"], "commands": [[sys.executable, "-c",
                "from pathlib import Path; p=Path('node_modules'); (p/'first').unlink(); (p/'second').touch()"]]},
        ]
        command = [[sys.executable, "-c", "from pathlib import Path; Path('.ai/agent-checks/application-ran').touch()"]]
        self.config["checks"] = [{"id": "check-first", "paths": ["config/*"], "environment": "first", "commands": command},
                                 {"id": "check-second", "paths": ["config/*"], "environment": "second", "commands": command}]
        self.save_config()
        self.run_check("--provision", expected=1)
        self.assertFalse((self.root / ".ai/agent-checks/application-ran").exists())
        setup_results = [json.loads(p.read_text()) for p in (self.root / ".ai/agent-checks/environments").glob("*.json")]
        self.assertFalse(any(x["result"]["id"] == "first" for x in setup_results))

    def test_private_environment_key_is_lazy_and_rejects_unsafe_permissions(self):
        state = self.root / ".ai/agent-checks"
        self.change_source()
        self.run_check("--plan")
        self.assertFalse(state.exists())
        self.run_check()
        key = state / "environment.key"
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(key.read_bytes()), 32)
        key.chmod(0o644)
        self.run_check(expected=2)
        self.assertFalse((state / "receipt.json").exists())



    def test_key_permissions_are_rechecked_after_creator_link_wait(self):
        runner = load("agent-check")
        runner.private_cache_key(self.root)
        path = self.root / ".ai/agent-checks/environment.key"
        extra = path.with_name("creator-temporary-link")
        os.link(path, extra)

        def finish_with_unsafe_permissions(_seconds):
            path.chmod(0o644)
            extra.unlink()

        with mock.patch.object(runner, "time", wraps=runner.time) as clock:
            clock.sleep.side_effect = finish_with_unsafe_permissions
            with self.assertRaisesRegex(ValueError, "owned private regular file"):
                runner.private_cache_key(self.root)
        clock.sleep.assert_called_once()
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_untracked_file_to_symlink_cannot_reuse_the_regular_file_pass(self):
        target = self.root / "node_modules/value.txt"
        target.parent.mkdir()
        target.write_text("bad")
        link = self.root / "src/link.txt"
        link.write_text("../node_modules/value.txt")
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert Path('src/link.txt').read_text() == '../node_modules/value.txt'"]]
        self.save_config()
        self.run_check()
        before = self.receipt()["snapshot"]
        link.unlink()
        link.symlink_to("../node_modules/value.txt")
        self.run_check(expected=1)
        self.assertNotEqual(before, self.receipt()["snapshot"])

    def test_untracked_symlink_target_changes_never_reuse_a_cached_pass(self):
        target = self.root / "node_modules/value.txt"
        target.parent.mkdir()
        target.write_text("good")
        (self.root / "src/link.txt").symlink_to("../node_modules/value.txt")
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert Path('src/link.txt').read_text() == 'good'"]]
        self.save_config()
        self.run_check()
        target.write_text("bad")
        self.run_check(expected=1)
        self.assertFalse(self.receipt()["cached"])

    def test_tracked_symlink_target_changes_never_reuse_a_cached_pass(self):
        target = self.root / "node_modules/value.txt"
        target.parent.mkdir()
        target.write_text("good")
        (self.root / "src/link.txt").symlink_to("../node_modules/value.txt")
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert Path('src/link.txt').read_text() == 'good'"]]
        self.save_config()
        self.git("add", "src/link.txt", "config/agent-checks.json")
        self.git("commit", "-qm", "linked fixture")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.change_source()
        self.run_check()
        self.assertFalse(self.receipt()["cache_reusable"])
        target.write_text("bad")
        self.run_check(expected=1)
        self.assertFalse(self.receipt()["cached"])

    def test_root_tool_requirement_still_applies_with_a_declared_environment(self):
        binaries = self.root / "node_modules/bin"
        binaries.mkdir(parents=True)
        binary = binaries / "node"
        binary.write_text(f"#!{sys.executable}\nimport os\nprint(os.environ['AGENT_CHECK_TOOL_VERSION'])\n")
        binary.chmod(0o755)
        self.config["checks"][0].update(tools=["node"], env={"AGENT_CHECK_TOOL_VERSION": "vfixture-wrong"})
        self.config["tool_requirements"] = {"node": "vfixture-required"}
        self.save_config()
        self.run_check(expected=1, env={**load("agent-check").git_environment(),
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"]})
        self.assertTrue(any(k.startswith("node@.#") for k in self.receipt()["tool_versions"]))
        self.assertTrue(any(r["id"].startswith("toolchain-node@.#") and r["status"] == "unavailable"
                            for r in self.receipt()["results"]))

    def test_staged_untracked_and_deleted_paths_count(self):
        self.change_source()
        self.git("add", "src/value.py")
        (self.root / "src/new.py").write_text("new = 1\n")
        (self.root / "lock.json").unlink()
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertEqual(plan["paths"], ["lock.json", "src/new.py", "src/value.py"])
        self.assertEqual(len(plan["checks"]), 1)

    def test_rename_out_of_scope_still_checks_removed_source(self):
        self.git("mv", "src/value.py", "archived.txt")
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertIn("src/value.py", plan["paths"])
        self.assertEqual([c["id"] for c in plan["checks"]], ["test"])

    def test_docs_do_not_select_application_suite(self):
        (self.root / "README.md").write_text("docs\n")
        self.assertEqual(json.loads(self.run_check("--plan").stdout)["checks"], [])

    def test_workflow_lint_only_receives_affected_workflows(self):
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / ".github/workflows/changed.yml").write_text("name: changed\n")
        self.config["checks"] = [{"id": "workflow", "paths": [".github/workflows/*"],
                                  "append_changed_paths": True, "commands": [["actionlint"]]}]
        self.save_config()
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertEqual(plan["checks"][0]["commands"], [["actionlint", ".github/workflows/changed.yml"]])

    def test_framework_change_does_not_implicitly_run_application_suite(self):
        self.config["checks"][0]["paths"] = ["src/*"]
        self.save_config()
        self.assertEqual(json.loads(self.run_check("--plan").stdout)["checks"], [])

    def test_deleted_workflow_alongside_edit_validates_remaining_callers(self):
        directory = self.root / ".github/workflows"
        directory.mkdir(parents=True)
        (directory / "reusable.yml").write_text("name: reusable\n")
        self.git("add", ".github")
        self.git("commit", "-qm", "workflow fixture")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        (directory / "reusable.yml").unlink()
        (directory / "unrelated.yml").write_text("name: unrelated\n")
        self.config["checks"] = [{"id": "workflow", "paths": [".github/workflows/*"],
                                  "append_changed_paths": True, "commands": [["actionlint"]]}]
        self.save_config()
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertEqual(plan["checks"][0]["commands"], [["actionlint"]])

    def test_manifest_lock_and_environment_inputs_revalidate(self):
        self.config["environments"] = [{
            "id": "dependencies", "inputs": ["lock.json"], "ready_paths": ["node_modules"],
            "commands": [[sys.executable, "-c", "from pathlib import Path; p=Path('node_modules'); p.mkdir(exist_ok=True); count=p/'installs'; count.write_text(count.read_text()+'1' if count.exists() else '1')"]],
        }]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check(expected=1)
        self.run_check("--provision")
        self.run_check("--provision")
        counter = self.root / "node_modules/installs"
        self.assertEqual(counter.read_text(), "1")
        (self.root / "lock.json").write_text('{"changed":true}')
        self.run_check("--provision")
        self.assertEqual(counter.read_text(), "11")
        shutil.rmtree(self.root / "node_modules")
        self.run_check("--provision")
        self.assertEqual(counter.read_text(), "1")

    def test_failed_install_never_runs_application_check(self):
        self.config["environments"] = [{"id": "dependencies", "commands": [[sys.executable, "-c", "raise SystemExit(4)"]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check("--provision", expected=1)
        self.assertEqual([r["id"] for r in self.receipt()["results"]], ["diff-check", "dependencies"])
        self.assertFalse((self.root / ".ai/agent-checks/environments").exists())

    def independent_tool_fixture(self, *, consumer_tools=(), requirements=None):
        state = self.root / '.ai/agent-checks'
        binaries = state / 'bin'
        binaries.mkdir(parents=True)
        binary = binaries / 'node'
        binary.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nimport sys\n'
                          "ready=Path('.ai/agent-checks/tool-ready').exists()\n"
                          "print('vfixture-1' if ready else 'not installed')\n"
                          'sys.exit(0 if ready else 1)\n')
        binary.chmod(0o755)
        environments = []
        checks = []
        for name in ('first', 'second'):
            setup = ('from pathlib import Path; '
                     f"p=Path('node_modules/{name}'); p.mkdir(parents=True,exist_ok=True); "
                     "c=p/'installs'; c.write_text(c.read_text()+'1' if c.exists() else '1')")
            if name == 'second':
                setup += "; Path('.ai/agent-checks/tool-ready').touch()"
            environments.append({'id': name, 'inputs': ['lock.json'],
                                 'tools': [] if name == 'first' else ['node'],
                                 'ready_paths': [f'node_modules/{name}'],
                                 'commands': [[sys.executable, '-c', setup]]})
            checks.append({**self.config['checks'][0], 'id': f'check-{name}',
                           'environment': name, 'tools': list(consumer_tools) if name == 'first' else ['node']})
        self.config.update(environments=environments, checks=checks)
        if requirements is not None:
            self.config['tool_requirements'] = requirements
        self.save_config()
        return {**load('agent-check').git_environment(),
                'PATH': str(binaries) + os.pathsep + os.environ['PATH']}

    def test_independent_later_tool_install_does_not_invalidate_earlier_setup(self):
        env = self.independent_tool_fixture()
        self.run_check('--provision', env=env)
        self.assertTrue(all(r['status'] == 'passed' for r in self.receipt()['results']))
        self.assertEqual((self.root / 'node_modules/first/installs').read_text(), '1')
        self.run_check('--provision', env=env)
        self.assertTrue(self.receipt()['cached'])
        self.assertEqual((self.root / 'node_modules/first/installs').read_text(), '1')

    def test_consuming_check_tool_still_invalidates_earlier_setup(self):
        env = self.independent_tool_fixture(consumer_tools=['node'])
        self.run_check('--provision', expected=1, env=env)
        first = next(r for r in self.receipt()['results'] if r['id'] == 'first')
        self.assertIn('later provisioning', first['reason'])
        self.assertFalse(any(r['id'].startswith('check-') for r in self.receipt()['results']))

    def test_global_required_tool_still_invalidates_earlier_setup(self):
        env = self.independent_tool_fixture(requirements={'node': 'vfixture-1'})
        self.run_check('--provision', expected=1, env=env)
        first = next(r for r in self.receipt()['results'] if r['id'] == 'first')
        self.assertIn('later provisioning', first['reason'])
        self.assertFalse(any(r['id'].startswith('check-') for r in self.receipt()['results']))

    def test_root_tool_requirement_is_probed_without_a_declared_consumer(self):
        binaries = self.root / 'node_modules/bin'
        binaries.mkdir(parents=True)
        binary = binaries / 'node'
        binary.write_text(f'#!{sys.executable}\nprint("vfixture-wrong")\n')
        binary.chmod(0o755)
        self.config['tool_requirements'] = {'node': 'vfixture-required'}
        self.save_config()
        self.run_check(expected=1, env={**load('agent-check').git_environment(),
            'PATH': str(binaries) + os.pathsep + os.environ['PATH']})
        self.assertEqual(self.receipt()['tool_versions']['node'], 'vfixture-wrong')
        self.assertTrue(any(r['id'] == 'toolchain-node' for r in self.receipt()['results']))

    def test_setup_tool_binding_keeps_root_and_exact_consuming_contexts(self):
        runner = load('agent-check')
        environment = {'id': 'first', 'tools': ['uv'], 'commands': [[sys.executable, '-c', 'pass']]}
        consumer = {'environment': 'first', 'tools': ['node'], 'cwd': 'app', 'env': {'MODE': 'selected'}}
        other = {'environment': 'second', 'tools': ['node'], 'cwd': 'other', 'env': {'MODE': 'unrelated'}}
        plan = {'checks': [consumer, other], 'environments': [environment]}
        selected = runner.tool_context_key('node', 'app', runner.encoded(consumer['env']).decode())
        unrelated = runner.tool_context_key('node', 'other', runner.encoded(other['env']).decode())
        tools = {'platform': 'fixture', 'python': 'Python fixture', 'uv': 'fixture-1',
                 selected: 'vfixture-1', unrelated: 'vfixture-1'}
        before = runner.environment_digest(self.root, environment, tools, b'fixture-key', {}, plan=plan)
        self.assertEqual(set(runner.environment_tools(environment, tools, plan)),
                         {'platform', 'python', 'uv', selected})
        for name in ('platform', 'python', 'uv', selected):
            with self.subTest(context=name):
                changed = {**tools, name: 'changed'}
                self.assertNotEqual(before, runner.environment_digest(
                    self.root, environment, changed, b'fixture-key', {}, plan=plan))
        self.assertEqual(before, runner.environment_digest(
            self.root, environment, {**tools, unrelated: 'changed'}, b'fixture-key', {}, plan=plan))
        plan['tool_requirements'] = {'node': 'vfixture-'}
        self.assertIn(unrelated, runner.environment_tools(environment, tools, plan))

    def test_legacy_global_tool_environment_receipt_is_revalidated(self):
        env = self.independent_tool_fixture()
        self.run_check('--provision', env=env)
        runner = load('agent-check')
        environment = self.config['environments'][0]
        state = self.root / '.ai/agent-checks'
        path = state / 'environments' / f"environment-{runner.sha(runner.encoded('first'))}.json"
        prior = json.loads(path.read_text())
        # Exact pre-migration digest structure; retain its valid result/readiness.
        prior['digest'] = runner.sha(runner.encoded({
            'recipe': environment,
            'inputs': {'lock.json': runner.sha((self.root / 'lock.json').read_bytes())},
            'tools': self.receipt()['tool_versions'],
            'execution_environment': runner.environment_fingerprint(
                runner.private_cache_key(self.root), runner.execution_environment(environment, env))}))
        path.write_text(json.dumps(prior))
        self.run_check('--provision', env=env)
        self.assertEqual((self.root / 'node_modules/first/installs').read_text(), '11')
        self.assertFalse(self.receipt()['cached'])

    def test_real_git_version_is_fingerprinted_and_changed_probe_invalidates_receipt(self):
        real_git = shutil.which('git')
        self.assertIsNotNone(real_git)
        actual_version = self.git('--version').decode().strip()
        state = self.root / '.ai/agent-checks'
        binaries = state / 'bin'
        binaries.mkdir(parents=True)
        suffix = state / 'git-version-suffix'
        suffix.write_text('')
        binary = binaries / 'git'
        # All Git operations use the real engine. Only the version response gets
        # a synthetic suffix to exercise an upgrade without installing software.
        binary.write_text('#!/bin/sh\n'
                          'if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then\n'
                          f'  version=$({shlex.quote(real_git)} --version) || exit "$?"\n'
                          f'  suffix=$(cat {shlex.quote(str(suffix))}) || exit "$?"\n'
                          '  printf "%s%s\\n" "$version" "$suffix"\n'
                          'else\n'
                          f'  exec {shlex.quote(real_git)} "$@"\n'
                          'fi\n')
        binary.chmod(0o755)
        self.config['checks'][0].update(tools=['git'], commands=[[sys.executable, '-c',
            "from pathlib import Path; p=Path('.ai/agent-checks/check-runs'); "
            "p.write_text(p.read_text()+'1' if p.exists() else '1')"]])
        self.save_config()
        env = {**load('agent-check').git_environment(),
               'PATH': str(binaries) + os.pathsep + os.environ['PATH']}
        self.run_check(env=env)
        first = self.receipt()
        self.assertEqual(first['tool_versions']['git'], actual_version)
        self.run_check(env=env)
        self.assertTrue(self.receipt()['cached'])
        self.assertEqual((state / 'check-runs').read_text(), '1')
        suffix.write_text(' fixture-upgrade')
        self.run_check(env=env)
        changed = self.receipt()
        self.assertEqual(changed['tool_versions']['git'], actual_version + ' fixture-upgrade')
        self.assertEqual(first['snapshot'], changed['snapshot'])
        self.assertNotEqual(first['digest'], changed['digest'])
        self.assertFalse(changed['cached'])
        self.assertEqual((state / 'check-runs').read_text(), '11')

    def test_real_git_context_and_required_prefix_use_the_selected_directory(self):
        (self.root / 'app').mkdir()
        declared = {'TOOL_SCOPE': 'nested-fixture'}
        self.config['checks'][0].update(tools=['git'], cwd='app', env=declared,
            commands=[[sys.executable, '-c',
                       "from pathlib import Path; assert 'value = 1' in Path('../src/value.py').read_text()"]])
        version = self.git('--version').decode().strip()
        self.config['tool_requirements'] = {'git': version}
        self.save_config()
        self.run_check()
        runner = load('agent-check')
        context = runner.tool_context_key('git', 'app', runner.encoded(declared).decode())
        self.assertEqual(self.receipt()['tool_versions'][context], version)
        self.assertNotIn('git', self.receipt()['tool_versions'])
        self.config['tool_requirements']['git'] = 'git version impossible-fixture'
        self.save_config()
        self.run_check(expected=1)
        self.assertTrue(any(r['id'] == 'toolchain-' + context and r['status'] == 'unavailable'
                            for r in self.receipt()['results']))

    def test_unregistered_tool_still_revokes_prior_receipt_before_checks(self):
        self.change_source()
        self.run_check()
        self.assertEqual(self.receipt()['outcome'], 'passed')
        self.config['checks'][0]['tools'] = ['unregistered-fixture-tool']
        self.save_config()
        result = self.run_check(expected=2)
        self.assertIn('unknown tool fingerprint: unregistered-fixture-tool', result.stderr)
        self.assertNotIn('AGENT-CHECK test:', result.stdout)
        self.assertFalse((self.root / '.ai/agent-checks/receipt.json').exists())

    def test_cli_preparation_reports_changed_tool_context_without_secret_values(self):
        env = self.independent_tool_fixture(consumer_tools=['node'])
        secret = 'synthetic-private-value-never-emit-42'
        declared = {'PRIVATE_FIXTURE_SECRET': secret}
        self.config['checks'][0]['env'] = declared
        self.save_config()
        result = self.run_check('--provision', expected=1, env=env)
        runner = load('agent-check')
        context = runner.tool_context_key('node', '.', runner.encoded(declared).decode())
        first = next(r for r in self.receipt()['results'] if r['id'] == 'first')
        self.assertEqual(first['reason'], 'provisioned environment invalidated by later provisioning or changed inputs; rerun with --provision')
        details = [json.loads(line.removeprefix('AGENT-CHECK-DIAGNOSTIC '))
                   for line in result.stderr.splitlines() if line.startswith('AGENT-CHECK-DIAGNOSTIC ')]
        self.assertIn({'id': 'first', 'changed': ['tool:' + context + '[unavailable-to-available]']}, details)
        self.assertFalse(any(r['id'].startswith('check-') for r in self.receipt()['results']))
        for text in (result.stdout, result.stderr, json.dumps(self.receipt())):
            self.assertNotIn(secret, text)
            self.assertNotIn('PRIVATE_FIXTURE_SECRET', text)

    def test_cli_final_context_reports_changed_tool_without_secret_values(self):
        state = self.root / '.ai/agent-checks'
        binaries = state / 'bin'
        binaries.mkdir(parents=True)
        version = state / 'tool-version'
        version.write_text('vfixture-1')
        binary = binaries / 'node'
        binary.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                          f'print(Path({str(version)!r}).read_text())\n')
        binary.chmod(0o755)
        secret = 'synthetic-private-value-never-emit-73'
        declared = {'PRIVATE_FIXTURE_SECRET': secret}
        self.config['checks'][0].update(tools=['node'], env=declared, commands=[
            [sys.executable, '-c', "from pathlib import Path; Path('.ai/agent-checks/tool-version').write_text('vfixture-2')"]])
        self.save_config()
        result = self.run_check(expected=1, env={**load('agent-check').git_environment(),
            'PATH': str(binaries) + os.pathsep + os.environ['PATH']})
        runner = load('agent-check')
        context = runner.tool_context_key('node', '.', runner.encoded(declared).decode())
        failure = next(r for r in self.receipt()['results'] if r['id'] == 'context-stability')
        self.assertEqual(failure['reason'], 'dotenv or tool context changed during validation; inspect setup and rerun')
        details = [json.loads(line.removeprefix('AGENT-CHECK-DIAGNOSTIC '))
                   for line in result.stderr.splitlines() if line.startswith('AGENT-CHECK-DIAGNOSTIC ')]
        self.assertIn({'id': 'context-stability', 'changed': ['tool:' + context + '[available-output-changed]']}, details)
        self.assertFalse(self.receipt()['cached'])
        for text in (result.stdout, result.stderr, json.dumps(self.receipt())):
            self.assertNotIn(secret, text)
            self.assertNotIn('PRIVATE_FIXTURE_SECRET', text)

    def test_cli_final_tool_unavailability_is_reported_without_raw_output(self):
        state = self.root / '.ai/agent-checks'
        binaries = state / 'bin'
        binaries.mkdir(parents=True)
        version = state / 'tool-version'
        version.write_text('vfixture-1')
        binary = binaries / 'node'
        binary.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nimport sys\n'
                          f'value=Path({str(version)!r}).read_text()\n'
                          "sys.exit(7) if value=='down' else print(value)\n")
        binary.chmod(0o755)
        self.config['checks'][0].update(tools=['node'], commands=[[sys.executable, '-c',
            "from pathlib import Path; Path('.ai/agent-checks/tool-version').write_text('down')"]])
        self.save_config()
        result = self.run_check(expected=1, env={**load('agent-check').git_environment(),
            'PATH': str(binaries) + os.pathsep + os.environ['PATH']})
        failure = next(r for r in self.receipt()['results'] if r['id'] == 'context-stability')
        self.assertEqual(failure['reason'], 'dotenv or tool context changed during validation; inspect setup and rerun')
        details = [json.loads(line.removeprefix('AGENT-CHECK-DIAGNOSTIC '))
                   for line in result.stderr.splitlines() if line.startswith('AGENT-CHECK-DIAGNOSTIC ')]
        self.assertIn({'id': 'context-stability', 'changed': ['tool:node[available-to-unavailable]']}, details)
        self.assertNotIn('vfixture-1', result.stderr)
        self.assertFalse(self.receipt()['cached'])

    def test_all_environment_receipts_use_final_provisioned_tool_versions(self):
        state = self.root / ".ai/agent-checks"
        binaries = state / "bin"
        binaries.mkdir(parents=True)
        binary = binaries / "node"
        binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
                          "ready=Path('.ai/agent-checks/tool-ready').exists()\n"
                          "print('vfixture-1' if ready else 'not installed')\n"
                          "sys.exit(0 if ready else 1)\n")
        binary.chmod(0o755)
        environments = []
        checks = []
        for name in ("first", "second"):
            setup = ("from pathlib import Path; "
                     f"p=Path('node_modules/{name}'); p.mkdir(parents=True,exist_ok=True); "
                     "counter=p/'installs'; counter.write_text(counter.read_text()+'1' if counter.exists() else '1')")
            if name == "second":
                setup += "; Path('.ai/agent-checks/tool-ready').touch()"
            environments.append({"id": name, "inputs": ["lock.json"], "tools": ["node"],
                                 "ready_paths": [f"node_modules/{name}"],
                                 "commands": [[sys.executable, "-c", setup]]})
            checks.append({**self.config["checks"][0], "id": f"check-{name}", "environment": name, "tools": ["node"]})
        self.config.update({"environments": environments, "checks": checks,
                            "tool_requirements": {"node": "vfixture-1"}})
        self.save_config()
        env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
        self.run_check("--provision", expected=1, env=env)
        self.assertIn("later provisioning", next(r for r in self.receipt()["results"] if r["id"] == "first")["reason"])
        self.assertFalse(self.receipt()["cached"])
        self.run_check("--provision", env=env)
        self.assertFalse(self.receipt()["cached"])
        self.run_check(env=env)
        self.assertTrue(self.receipt()["cached"])
        for name in ("first", "second"):
            self.assertEqual((self.root / f"node_modules/{name}/installs").read_text(), "11" if name == "first" else "1")

    def test_newly_provisioned_environment_is_not_rekeyed_after_later_tool_change(self):
        state = self.root / ".ai/agent-checks"
        binaries = state / "bin"
        binaries.mkdir(parents=True)
        (state / "tool-version").write_text("vfixture-1")
        binary = binaries / "node"
        binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                          "print(Path('.ai/agent-checks/tool-version').read_text())\n")
        binary.chmod(0o755)
        first = ("from pathlib import Path; import subprocess; "
                 "p=Path('node_modules/first'); p.mkdir(parents=True,exist_ok=True); "
                 "(p/'installed-tool-version').write_text(subprocess.check_output(['node','--version'],text=True).strip())")
        second = ("from pathlib import Path; p=Path('node_modules/second'); p.mkdir(parents=True,exist_ok=True); "
                  "Path('.ai/agent-checks/tool-version').write_text('vfixture-2')")
        self.config["environments"] = [
            {"id": "first", "tools": ["node"], "ready_paths": ["node_modules/first"], "commands": [[sys.executable, "-c", first]]},
            {"id": "second", "tools": ["node"], "ready_paths": ["node_modules/second"], "commands": [[sys.executable, "-c", second]]},
        ]
        self.config["checks"] = [{**self.config["checks"][0], "id": f"check-{name}", "environment": name, "tools": ["node"]}
                                 for name in ("first", "second")]
        self.save_config()
        env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
        self.run_check("--provision", expected=1, env=env)
        failed = next(r for r in self.receipt()["results"] if r["id"] == "first")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("later provisioning", failed["reason"])
        self.assertEqual((self.root / "node_modules/first/installed-tool-version").read_text(), "vfixture-1")
        self.assertFalse(any(r["id"].startswith("check-") for r in self.receipt()["results"]))
        self.run_check("--provision", env=env)
        self.assertEqual((self.root / "node_modules/first/installed-tool-version").read_text(), "vfixture-2")
        self.run_check(env=env)
        self.assertTrue(self.receipt()["cached"])

    def test_later_provisioning_invalidates_an_earlier_cached_environment(self):
        state = self.root / ".ai/agent-checks"
        binaries = state / "bin"
        binaries.mkdir(parents=True)
        version = state / "tool-version"
        version.write_text("vfixture-1")
        binary = binaries / "node"
        binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                          "print(Path('.ai/agent-checks/tool-version').read_text())\n")
        binary.chmod(0o755)
        (self.root / "second.lock").write_text("vfixture-1")
        environments = []
        checks = []
        for name in ("first", "second"):
            setup = ("from pathlib import Path; "
                     f"p=Path('node_modules/{name}'); p.mkdir(parents=True,exist_ok=True); "
                     "c=p/'installs'; c.write_text(c.read_text()+'1' if c.exists() else '1')")
            if name == "second":
                setup += "; Path('.ai/agent-checks/tool-version').write_text(Path('second.lock').read_text())"
            environments.append({"id": name, "inputs": ["second.lock" if name == "second" else "lock.json"],
                                 "tools": ["node"], "ready_paths": [f"node_modules/{name}"],
                                 "commands": [[sys.executable, "-c", setup]]})
            checks.append({**self.config["checks"][0], "id": f"check-{name}",
                           "environment": name, "tools": ["node"]})
        self.config.update({"environments": environments, "checks": checks})
        self.save_config()
        env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
        self.run_check("--provision", env=env)
        self.run_check("--provision", env=env)
        self.assertTrue(self.receipt()["cached"])

        (self.root / "second.lock").write_text("vfixture-2")
        self.run_check("--provision", expected=1, env=env)
        receipt = self.receipt()
        failed = next(r for r in receipt["results"] if r["id"] == "first")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("later provisioning", failed["reason"])
        self.assertFalse(any(r["id"].startswith("check-") for r in receipt["results"]))
        runner = load("agent-check")
        old_receipt = state / "environments" / f"environment-{runner.sha(runner.encoded('first'))}.json"
        self.assertFalse(old_receipt.exists())
        self.assertEqual((self.root / "node_modules/first/installs").read_text(), "1")
        self.assertEqual((self.root / "node_modules/second/installs").read_text(), "11")

        self.run_check("--provision", env=env)
        self.assertFalse(self.receipt()["cached"])
        self.assertEqual((self.root / "node_modules/first/installs").read_text(), "11")
        self.run_check(env=env)
        self.assertTrue(self.receipt()["cached"])

    def test_failed_reinstall_cannot_revive_receipt_for_restored_tool_version(self):
        state = self.root / ".ai/agent-checks"
        binaries = state / "bin"
        binaries.mkdir(parents=True)
        version = state / "tool-version"
        version.write_text("vfixture-1")
        binary = binaries / "node"
        binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                          "print(Path('.ai/agent-checks/tool-version').read_text())\n")
        binary.chmod(0o755)
        setup = ("from pathlib import Path; "
                 "p=Path('node_modules'); p.mkdir(exist_ok=True); "
                 "Path('.ai/agent-checks/tool-version').write_text('vfixture-1'); "
                 "raise SystemExit(4 if Path('.ai/agent-checks/fail-install').exists() else 0)")
        self.config["environments"] = [{"id": "dependencies", "tools": ["node"],
                                        "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0].update({"environment": "dependencies", "tools": ["node"]})
        self.save_config()
        env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
        self.run_check("--provision", env=env)
        version.write_text("vfixture-2")
        (state / "fail-install").touch()
        self.run_check("--provision", expected=1, env=env)
        self.assertEqual(version.read_text(), "vfixture-1")
        self.assertTrue((self.root / "node_modules").is_dir())
        self.run_check(expected=1, env=env)
        self.assertFalse(self.receipt()["cached"])

    def test_reprovisioning_cannot_reuse_pass_after_mutating_checked_source(self):
        setup = (
            "from pathlib import Path; "
            "marker=Path('.ai/agent-checks/setup-ran'); "
            "Path('src/value.py').write_text('value = 2') if marker.exists() else None; "
            "marker.parent.mkdir(parents=True,exist_ok=True); marker.touch(); "
            "Path('node_modules').mkdir(exist_ok=True)"
        )
        self.config["environments"] = [{"id": "dependencies", "inputs": ["lock.json"],
                                        "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check("--provision")
        self.run_check("--provision")
        self.assertTrue(self.receipt()["cached"])
        shutil.rmtree(self.root / "node_modules")
        self.run_check("--provision", expected=1)
        self.assertFalse(self.receipt()["cached"])
        self.assertIn("input-stability", [r["id"] for r in self.receipt()["results"]])

    def test_failed_reinstall_revokes_old_environment_pass(self):
        setup = (
            "from pathlib import Path; "
            "marker=Path('.ai/agent-checks/setup-ran'); "
            "Path('node_modules').mkdir(exist_ok=True); "
            "failed=marker.exists(); marker.parent.mkdir(parents=True,exist_ok=True); marker.touch(); "
            "raise SystemExit(4 if failed else 0)"
        )
        self.config["environments"] = [{"id": "dependencies", "inputs": ["lock.json"],
                                        "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check("--provision")
        shutil.rmtree(self.root / "node_modules")
        self.run_check("--provision", expected=1)
        # A partial install directory cannot resurrect the former success.
        self.assertTrue((self.root / "node_modules").is_dir())
        self.run_check(expected=1)
        self.run_check("--provision", expected=1)
        self.assertFalse(self.receipt()["cached"])

    def test_tool_version_changes_invalidate_cached_pass(self):
        directory = self.root / "node_modules/bin"
        directory.mkdir(parents=True)
        for tool, executable, old, new in (("node", "node", "v22.1.0", "v22.2.0"),
                                           ("swift", "swiftc", "Apple Swift version 6.2.0", "Apple Swift version 6.2.1")):
            with self.subTest(tool=tool):
                binary = directory / executable
                binary.write_text(f"#!{sys.executable}\nimport os\nprint(os.environ['FIXTURE_TOOL_VERSION'])\n")
                binary.chmod(0o755)
                self.config["checks"][0]["tools"] = [tool]
                self.save_config()
                env = {**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"], "FIXTURE_TOOL_VERSION": old}
                self.run_check(env=env)
                first = self.receipt()["digest"]
                self.run_check(env=env)
                self.assertTrue(self.receipt()["cached"])
                env["FIXTURE_TOOL_VERSION"] = new
                self.run_check(env=env)
                self.assertNotEqual(first, self.receipt()["digest"])
                self.assertFalse(self.receipt()["cached"])

        # A nested package's Corepack pin must be probed in that package,
        # with the same declared environment as the validation command.
        (self.root / "app").mkdir()
        pnpm = directory / "pnpm"
        pnpm.write_text(f"#!{sys.executable}\nimport os\nfrom pathlib import Path\n"
                        "print(os.environ['PINNED_VERSION'] if Path.cwd().name == 'app' else '11.17.0')\n")
        pnpm.chmod(0o755)
        self.config["checks"][0].update({"tools": ["pnpm"], "cwd": "app",
                                          "env": {"PINNED_VERSION": "11.5.2"},
                                          "commands": [[sys.executable, "-c",
                                                        "from pathlib import Path; assert 'value = 1' in Path('../src/value.py').read_text()"]]})
        self.config["tool_requirements"] = {"pnpm": "11.5.2"}
        self.save_config()
        self.run_check(env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"]})
        versions = self.receipt()["tool_versions"]
        self.assertEqual([v for k, v in versions.items() if k.startswith("pnpm@app#")], ["11.5.2"])

    def test_changed_inputs_during_execution_cannot_pass(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c", "from pathlib import Path; Path('src/value.py').write_text('mutated')"]]
        self.save_config()
        self.run_check(expected=1)
        self.assertIn("input-stability", [r["id"] for r in self.receipt()["results"]])

    def test_paths_cannot_escape_repo(self):
        self.config["checks"][0]["cwd"] = "../"
        self.save_config()
        self.run_check(expected=2)

    def test_missing_command_reports_unavailable_and_never_passes(self):
        self.config["checks"][0]["commands"] = [["agent-check-fixture-nonexistent"]]
        self.save_config()
        result = self.run_check(expected=1)
        self.assertIn("unavailable", [r["status"] for r in self.receipt()["results"]])
        self.assert_failure_summary(result)

    def assert_failure_summary(self, result):
        lines = result.stdout.splitlines()
        summaries = [json.loads(line.removeprefix("AGENT-CHECK-RESULT "))
                     for line in lines if line.startswith("AGENT-CHECK-RESULT ")]
        expected = [{key: item[key] for key in ("id", "status", "reason")}
                    for item in self.receipt()["results"] if item["status"] != "passed"]
        self.assertTrue(expected)
        self.assertEqual(summaries, expected)
        verdict = next(i for i, line in enumerate(lines) if line.startswith("AGENT-CHECK-FAILED "))
        self.assertTrue(all(i < verdict for i, line in enumerate(lines)
                            if line.startswith("AGENT-CHECK-RESULT ")))

    def test_all_failed_checks_are_summarised_without_passing_checks(self):
        self.config["checks"] = [
            {"id": name, "commands": [[sys.executable, "-c", f"raise SystemExit({code})"]]}
            for name, code in (("pass", 0), ("first", 3), ("second", 4))
        ]
        self.save_config()
        self.assert_failure_summary(self.run_check(expected=1))

    def test_receipts_must_be_ignored(self):
        (self.root / ".gitignore").write_text("")
        self.run_check(expected=2)

    def test_inherited_git_locations_do_not_redirect_validation(self):
        self.change_source()
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory).resolve()
            (other / "other.txt").write_text("another repository\n")
            for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                         ("config", "user.name", "Fixture"), ("add", "."),
                         ("commit", "-qm", "foreign fixture"),
                         ("update-ref", "refs/remotes/origin/main", "HEAD")):
                subprocess.run(["git", *args], cwd=other, check=True, capture_output=True,
                               env=load("agent-check").git_environment())
            index = (other / ".git/index").read_bytes()
            inherited = {**os.environ, "GIT_DIR": str(other / ".git"),
                         "GIT_COMMON_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                         "GIT_INDEX_FILE": str(other / ".git/index"),
                         "GIT_OBJECT_DIRECTORY": str(other / ".git/objects"),
                         "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git/objects")}
            plan = json.loads(self.run_check("--plan", env=inherited).stdout)
            self.assertEqual(plan["paths"], ["src/value.py"])
            self.assertEqual(plan["base"], self.git("rev-parse", "HEAD").decode().strip())
            self.run_check(env=inherited)
            self.assertEqual(self.receipt()["snapshot"]["head"], plan["base"])
            self.assertEqual((other / ".git/index").read_bytes(), index)

    def test_blocked_execution_revokes_old_receipt_but_plan_does_not(self):
        self.change_source()
        runner = load("agent-check")
        receipt = self.root / ".ai/agent-checks/receipt.json"
        for failure in ("config", "base", "ignore", "snapshot", "cache-write"):
            with self.subTest(failure=failure):
                self.save_config()
                (self.root / ".gitignore").write_text(".ai/agent-checks/\nnode_modules/\n")
                self.run_check()
                original = receipt.read_bytes()
                self.run_check("--plan")
                self.assertEqual(receipt.read_bytes(), original)
                if failure == "config":
                    (self.root / "config/agent-checks.json").write_text("{")
                    self.run_check("--plan", expected=2)
                    self.assertEqual(receipt.read_bytes(), original)
                    self.run_check(expected=2)
                elif failure == "base":
                    self.run_check("--base", "refs/heads/fixture-missing-base", expected=2)
                elif failure == "ignore":
                    (self.root / ".gitignore").write_text("")
                    self.run_check(expected=2)
                elif failure == "snapshot":
                    with mock.patch.object(runner, "snapshot", side_effect=OSError("fixture snapshot failure")):
                        self.assertEqual(runner.main(["--repo-root", str(self.root)]), 2)
                else:
                    real_write = runner.write_json

                    def fail_cache_write(path, value):
                        if path.parent.name == "cache":
                            raise OSError("fixture cache write failure")
                        real_write(path, value)

                    with mock.patch.object(runner, "write_json", side_effect=fail_cache_write):
                        self.assertEqual(runner.main(["--repo-root", str(self.root), "--no-cache"]), 2)
                self.assertFalse(receipt.exists(), "blocked execution retained an older PASS receipt")

    def test_ci_toolchain_mismatch_is_incomplete_validation(self):
        self.config["tool_requirements"] = {"python": "Python 0."}
        self.save_config()
        result = self.run_check(expected=1)
        self.assertIn("unavailable", [r["status"] for r in self.receipt()["results"]])
        self.assert_failure_summary(result)

    def test_changed_ci_command_requires_recipe_update(self):
        (self.root / "workflow.yml").write_text("run: new-command\n")
        self.config["checks"][0]["ci_sources"] = [{"path": "workflow.yml", "contains": "old-command"}]
        self.save_config()
        self.run_check(expected=2)


    @contextmanager
    def _frozen_dependency_metadata(self, runner, target):
        """Reproduce same-stat rewrites independently of filesystem precision."""
        before = target.stat()
        real = {name: getattr(runner.os, name) for name in ("stat", "lstat", "fstat")}

        def freeze(function):
            def read(*args, **kwargs):
                details = function(*args, **kwargs)
                return before if (details.st_dev, details.st_ino) == (before.st_dev, before.st_ino) else details
            return read

        with mock.patch.object(runner.os, "stat", side_effect=freeze(real["stat"])), \
                mock.patch.object(runner.os, "lstat", side_effect=freeze(real["lstat"])), \
                mock.patch.object(runner.os, "fstat", side_effect=freeze(real["fstat"])):
            yield

    def _assert_validated_setup_identity_is_not_recaptured(self, *, reused, unavailable):
        runner = load("agent-check")
        self._dependency_environment()
        # A changed qualification input must invalidate authority even when the
        # selected command does not happen to inspect that dependency itself.
        self.config["checks"][0]["commands"] = [[sys.executable, "-c", "pass"]]
        self.save_config()
        self.change_source()
        args = ["--repo-root", str(self.root), "--provider", "codex", "--provision"]
        if reused:
            self.assertEqual(runner.main(args), 0)
        real_ready, real_write = runner.ready_identity, runner.write_json
        changed, observations = False, 0

        def mutate():
            nonlocal changed
            (self.root / "node_modules/tool/bin.js").write_text("ko")
            changed = True

        def ready(*pos, **kwargs):
            nonlocal observations
            observations += 1
            if unavailable and changed:
                return None
            value = real_ready(*pos, **kwargs)
            # The second reused observation just validated the setup. Mutate
            # after that observation, before the candidate identity is built.
            if reused and observations == 2:
                mutate()
            return value

        def write(path, value):
            real_write(path, value)
            if not reused and path.parent.name == "environments" and not changed:
                mutate()

        with mock.patch.object(runner, "ready_identity", side_effect=ready), \
                mock.patch.object(runner, "write_json", side_effect=write):
            self.assertEqual(runner.main(args), 1)
        self.assertTrue(changed)
        receipt = self.receipt()
        self.assertFalse(receipt["cached"])
        failure = next(item for item in receipt["results"] if item["id"] == "dependency-stability")
        self.assertEqual(failure["status"], "unavailable" if unavailable else "failed")
        if unavailable:
            self.assertNotIn("changed", failure["reason"])
            self.assertFalse(receipt["cache_reusable"])
        record = self.root / ".ai/agent-checks/environments" / (
            "environment-" + runner.sha(runner.encoded("dependencies")) + ".json")
        self.assertFalse(record.exists(), "invalidated setup retained its qualification receipt")
        self.assertFalse(any((self.root / ".ai/agent-checks/cache").glob("*.json")))

    def test_fresh_setup_known_change_before_candidate_identity_is_refused(self):
        self._assert_validated_setup_identity_is_not_recaptured(reused=False, unavailable=False)

    def test_reused_setup_known_change_before_candidate_identity_is_refused(self):
        self._assert_validated_setup_identity_is_not_recaptured(reused=True, unavailable=False)

    def test_fresh_setup_unknown_candidate_identity_cannot_replace_validated_baseline(self):
        self._assert_validated_setup_identity_is_not_recaptured(reused=False, unavailable=True)

    def test_reused_setup_unknown_candidate_identity_cannot_replace_validated_baseline(self):
        self._assert_validated_setup_identity_is_not_recaptured(reused=True, unavailable=True)

    def test_internal_stability_ids_are_reserved_for_checks_and_environments(self):
        marker = self.root / ".ai/agent-checks/owner-executed"
        command = [sys.executable, "-c", "from pathlib import Path; p=Path('.ai/agent-checks/owner-executed'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('executed')"]
        for kind in ("checks", "environments"):
            for identifier in ("input-stability", "context-stability", "dependency-stability"):
                with self.subTest(kind=kind, identifier=identifier):
                    marker.unlink(missing_ok=True)
                    self.config["checks"] = [{"id": "test", "commands": [command]}]
                    self.config["environments"] = []
                    if kind == "checks":
                        self.config["checks"][0]["id"] = identifier
                    else:
                        self.config["environments"] = [{"id": identifier, "commands": [command]}]
                        self.config["checks"][0]["environment"] = identifier
                    self.save_config()
                    result = self.run_check("--provision", expected=2)
                    self.assertIn("reserved validation result", result.stderr)
                    self.assertFalse(marker.exists(), "reserved owner identity executed before rejection")
                    self.assertFalse((self.root / ".ai/agent-checks/receipt.json").exists())

    def _assert_owner_identity_cannot_mask_dotenv_change(self, *, environment):
        (self.root / ".gitignore").write_text(".ai/agent-checks/\n.env\n")
        dotenv = self.root / ".env"
        dotenv.write_text("FIXTURE_MODE=ok\n")
        self.change_source()
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; Path('.env').write_text('FIXTURE_MODE=ko\\n')"]]
        if environment:
            self.config["environments"] = [{"id": "context-stability", "commands": [[sys.executable, "-c", "pass"]]}]
            self.config["checks"][0]["environment"] = "context-stability"
        else:
            self.config["checks"][0]["id"] = "context-stability"
        self.save_config()
        result = self.run_check("--provision", expected=2)
        self.assertIn("reserved validation result", result.stderr)
        self.assertEqual(dotenv.read_text(), "FIXTURE_MODE=ok\n")
        self.assertFalse((self.root / ".ai/agent-checks/receipt.json").exists())

    def test_owner_check_id_cannot_mask_known_dotenv_failure(self):
        self._assert_owner_identity_cannot_mask_dotenv_change(environment=False)

    def test_owner_environment_id_cannot_mask_known_dotenv_failure(self):
        self._assert_owner_identity_cannot_mask_dotenv_change(environment=True)

    def test_directory_addition_during_content_walk_disables_reuse(self):
        runner = load("agent-check")
        for case, filename in (("direct", "index.js"), ("ancestor", "pkg/index.js")):
            with self.subTest(case=case):
                directory = self.root / "node_modules" / case
                target = directory / filename
                target.parent.mkdir(parents=True)
                target.write_text("ok")
                real_read = runner.os.read
                target_identity = (target.stat().st_dev, target.stat().st_ino)
                mutated = False

                def add_after_enumeration(descriptor, count):
                    nonlocal mutated
                    details = runner.os.fstat(descriptor)
                    if (details.st_dev, details.st_ino) == target_identity and not mutated:
                        (directory / "new.js").write_text("new input")
                        mutated = True
                    return real_read(descriptor, count)

                with mock.patch.object(runner.os, "read", side_effect=add_after_enumeration):
                    self.assertIsNone(runner.ready_identity(
                        self.root, {"ready_paths": [directory.relative_to(self.root).as_posix()]}))
                self.assertTrue(mutated)

    def test_entry_replacement_during_read_disables_reuse(self):
        runner = load("agent-check")
        directory = self.root / "node_modules"
        directory.mkdir()
        target = directory / "index.js"
        target.write_text("ok")
        real_read = runner.os.read
        target_identity = (target.stat().st_dev, target.stat().st_ino)
        replaced = False

        def replace_opened_entry(descriptor, count):
            nonlocal replaced
            details = runner.os.fstat(descriptor)
            if (details.st_dev, details.st_ino) == target_identity and not replaced:
                target.unlink()
                target.write_text("ko")
                replaced = True
            return real_read(descriptor, count)

        with mock.patch.object(runner.os, "read", side_effect=replace_opened_entry):
            self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))
        self.assertTrue(replaced)

    def test_owner_root_replacement_during_walk_disables_reuse(self):
        runner = load("agent-check")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "owner"
            target = root / "node_modules/index.js"
            target.parent.mkdir(parents=True)
            target.write_text("ok")
            target_identity = (target.stat().st_dev, target.stat().st_ino)
            real_read = runner.os.read
            replaced = False

            def replace_owner(descriptor, count):
                nonlocal replaced
                details = runner.os.fstat(descriptor)
                if (details.st_dev, details.st_ino) == target_identity and not replaced:
                    root.rename(root.with_name("original-owner"))
                    (root / "node_modules").mkdir(parents=True)
                    (root / "node_modules/index.js").write_text("ko")
                    replaced = True
                return real_read(descriptor, count)

            with mock.patch.object(runner.os, "read", side_effect=replace_owner):
                self.assertIsNone(runner.ready_identity(root, {"ready_paths": ["node_modules"]}))
            self.assertTrue(replaced)

    def test_changed_dependencies_after_planned_identity_cannot_return_cached_pass(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        args = ["--repo-root", str(self.root), "--provider", "codex"]
        real_dotenv = runner.dotenv_identity
        real_snapshot = runner.snapshot
        real_read = Path.read_text
        for window in ("dotenv", "snapshot", "cache-read"):
            with self.subTest(window=window):
                self.assertEqual(runner.main([*args, "--provision"]), 0)
                changed = False
                snapshot_calls = 0

                def mutate():
                    nonlocal changed
                    if not changed:
                        (self.root / "node_modules/tool/bin.js").write_text("ko")
                        changed = True

                def dotenv(*pos, **kwargs):
                    value = real_dotenv(*pos, **kwargs)
                    mutate()
                    return value

                def snapshot(*pos, **kwargs):
                    nonlocal snapshot_calls
                    value = real_snapshot(*pos, **kwargs)
                    snapshot_calls += 1
                    if snapshot_calls == 2:
                        mutate()
                    return value

                def read(path, *pos, **kwargs):
                    value = real_read(path, *pos, **kwargs)
                    if path.parent.name == "cache":
                        mutate()
                    return value

                patched = {"dotenv": mock.patch.object(runner, "dotenv_identity", side_effect=dotenv),
                           "snapshot": mock.patch.object(runner, "snapshot", side_effect=snapshot),
                           "cache-read": mock.patch.object(Path, "read_text", read)}[window]
                with patched:
                    self.assertEqual(runner.main(args), 1)
                self.assertTrue(changed)
                self.assertFalse(self.receipt()["cached"])
                self.assertTrue(any(item["id"] == "dependency-stability" and item["status"] == "failed"
                                    for item in self.receipt()["results"]))

    def test_unknown_postscan_cannot_reuse_cached_pass_or_claim_source_failure(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        args = ["--repo-root", str(self.root), "--provider", "codex"]
        real_ready, real_snapshot, real_read = runner.ready_identity, runner.snapshot, Path.read_text
        for window in ("snapshot", "cache-read"):
            with self.subTest(window=window):
                self.assertEqual(runner.main([*args, "--provision"]), 0)
                before = real_snapshot(self.root)
                lost, observations, snapshots = False, [], 0

                def ready(*pos, **kwargs):
                    with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 0) if lost else nullcontext():
                        value = real_ready(*pos, **kwargs)
                    observations.append(value)
                    return value

                def snapshot(*pos, **kwargs):
                    nonlocal lost, snapshots
                    value = real_snapshot(*pos, **kwargs)
                    snapshots += 1
                    if window == "snapshot" and snapshots == 2:
                        lost = True
                    return value

                def read(path, *pos, **kwargs):
                    nonlocal lost
                    value = real_read(path, *pos, **kwargs)
                    if window == "cache-read" and path.parent.name == "cache":
                        lost = True
                    return value

                output = io.StringIO()
                with mock.patch.object(runner, "ready_identity", side_effect=ready), \
                        mock.patch.object(runner, "snapshot", side_effect=snapshot), \
                        mock.patch.object(Path, "read_text", read), \
                        mock.patch("sys.stdout", output):
                    self.assertEqual(runner.main(args), 1)
                self.assertTrue(lost)
                self.assertTrue(any(value is not None for value in observations))
                self.assertIn(None, observations)
                receipt = self.receipt()
                self.assertEqual(receipt["schema"], "agent-check/v1")
                self.assertEqual(receipt["outcome"], "failed")
                self.assertFalse(receipt["cached"])
                failure = next(item for item in receipt["results"] if item["id"] == "dependency-stability")
                self.assertEqual(failure["status"], "unavailable")
                self.assertFalse(receipt["cache_reusable"])
                self.assertNotIn("changed", failure["reason"])
                self.assertFalse(any(item["id"] == "input-stability" for item in receipt["results"]))
                self.assertEqual(real_snapshot(self.root), before)
                self.assertFalse(any((self.root / ".ai/agent-checks/cache").glob("*.json")))
                self.assertNotIn("AGENT-CHECK-CACHED", output.getvalue())
                self.assert_failure_summary(SimpleNamespace(stdout=output.getvalue()))

    def test_unknown_postscan_after_fresh_check_cannot_save_pass(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        real_ready, real_execute = runner.ready_identity, runner.execute
        lost, checked = False, False

        def ready(*pos, **kwargs):
            with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 0) if lost else nullcontext():
                return real_ready(*pos, **kwargs)

        def execute(root, item, inherited):
            nonlocal lost, checked
            value = real_execute(root, item, inherited)
            if item["id"] == "test":
                checked = lost = True
            return value

        before = runner.snapshot(self.root)
        with mock.patch.object(runner, "ready_identity", side_effect=ready), \
                mock.patch.object(runner, "execute", side_effect=execute):
            self.assertEqual(runner.main(["--repo-root", str(self.root), "--provision"]), 1)
        self.assertTrue(checked)
        self.assertEqual(runner.snapshot(self.root), before)
        self.assertFalse(self.receipt()["cached"])
        self.assertFalse(self.receipt()["cache_reusable"])
        failure = next(item for item in self.receipt()["results"] if item["id"] == "dependency-stability")
        self.assertEqual(failure["status"], "unavailable")
        self.assertFalse(any((self.root / ".ai/agent-checks/cache").glob("*.json")))

    def test_unknown_setup_postscan_revokes_reused_and_fresh_setup_as_unavailable(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        args = ["--repo-root", str(self.root), "--provider", "codex", "--provision"]
        real_ready, real_versions = runner.ready_identity, runner.versions
        for mode in ("fresh", "reused"):
            with self.subTest(mode=mode):
                if mode == "reused":
                    self.assertEqual(runner.main(args), 0)
                lost, probes = False, 0

                def ready(*pos, **kwargs):
                    with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 0) if lost else nullcontext():
                        return real_ready(*pos, **kwargs)

                def versions(*pos, **kwargs):
                    nonlocal lost, probes
                    value = real_versions(*pos, **kwargs)
                    probes += 1
                    if probes == (3 if mode == "fresh" else 2):
                        lost = True
                    return value

                with mock.patch.object(runner, "ready_identity", side_effect=ready), \
                        mock.patch.object(runner, "versions", side_effect=versions):
                    self.assertEqual(runner.main(args), 1)
                self.assertTrue(lost)
                result = next(item for item in self.receipt()["results"] if item["id"] == "dependencies")
                self.assertEqual(result["status"], "unavailable")
                record = self.root / ".ai/agent-checks/environments" / (
                    "environment-" + runner.sha(runner.encoded("dependencies")) + ".json")
                self.assertFalse(record.exists())
                self.assertFalse(self.receipt()["cached"])

    def test_unknown_then_known_postscan_is_incomplete_without_asserting_changed_bytes(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        real_ready, real_snapshot = runner.ready_identity, runner.snapshot
        recovered, snapshots = False, 0

        def ready(*pos, **kwargs):
            with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 0) if not recovered else nullcontext():
                return real_ready(*pos, **kwargs)

        def snapshot(*pos, **kwargs):
            nonlocal recovered, snapshots
            value = real_snapshot(*pos, **kwargs)
            snapshots += 1
            if snapshots == 2:
                recovered = True
            return value

        with mock.patch.object(runner, "ready_identity", side_effect=ready), \
                mock.patch.object(runner, "snapshot", side_effect=snapshot):
            self.assertEqual(runner.main(["--repo-root", str(self.root), "--provision"]), 1)
        self.assertTrue(recovered)
        result = next(item for item in self.receipt()["results"] if item["id"] == "dependency-stability")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("changed", result["reason"])
        self.assertFalse(self.receipt()["cache_reusable"])

    def test_both_unknown_postscans_preserve_uncached_fresh_execution(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        args = ["--repo-root", str(self.root), "--provision"]
        with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 0):
            for _ in range(2):
                self.assertEqual(runner.main(args), 0)
                self.assertFalse(self.receipt()["cached"])
                self.assertFalse(self.receipt()["cache_reusable"])
        self.assertEqual((self.root / "node_modules/installs").read_text(), "11")

    def test_known_dependency_change_is_not_hidden_by_another_unknown_postscan(self):
        runner = load("agent-check")
        self._interacting_dependency_environments()
        (self.root / "b.lock").write_text("safe")
        real_ready, real_execute = runner.ready_identity, runner.execute
        checked = False

        def ready(root, item, **kwargs):
            with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 0) if checked and item["id"] == "b" else nullcontext():
                return real_ready(root, item, **kwargs)

        def execute(root, item, inherited):
            nonlocal checked
            result = real_execute(root, item, inherited)
            if item["id"] == "b-test":
                (self.root / "node_modules/a/value").write_text("ko")
                checked = True
            return result

        with mock.patch.object(runner, "ready_identity", side_effect=ready), \
                mock.patch.object(runner, "execute", side_effect=execute):
            self.assertEqual(runner.main(["--repo-root", str(self.root), "--provision"]), 1)
        self.assertTrue(checked)
        result = next(item for item in self.receipt()["results"] if item["id"] == "dependency-stability")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "dependency inputs changed during validation; inspect setup and rerun")

    def test_later_known_change_upgrades_an_unknown_postscan_to_source_failure(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        args = ["--repo-root", str(self.root), "--provision"]
        self.assertEqual(runner.main(args), 0)
        real_ready, real_snapshot = runner.ready_identity, runner.snapshot
        before = real_snapshot(self.root)
        snapshots, uncertain = 0, False

        def snapshot(*pos, **kwargs):
            nonlocal snapshots
            value = real_snapshot(*pos, **kwargs)
            snapshots += 1
            return value

        def ready(*pos, **kwargs):
            nonlocal uncertain
            if snapshots >= 2 and not uncertain:
                with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 0):
                    value = real_ready(*pos, **kwargs)
                self.assertIsNone(value)
                uncertain = True
                (self.root / "node_modules/tool/bin.js").write_text("ko")
                return value
            return real_ready(*pos, **kwargs)

        with mock.patch.object(runner, "ready_identity", side_effect=ready), \
                mock.patch.object(runner, "snapshot", side_effect=snapshot):
            self.assertEqual(runner.main(args), 1)
        self.assertTrue(uncertain)
        self.assertEqual(real_snapshot(self.root), before)
        failure = next(item for item in self.receipt()["results"] if item["id"] == "dependency-stability")
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["reason"], "dependency inputs changed during validation; inspect setup and rerun")

    def test_changed_dotenv_after_context_capture_cannot_return_cached_pass(self):
        runner = load("agent-check")
        (self.root / ".gitignore").write_text(".ai/agent-checks/\nnode_modules/\n.env\n")
        dotenv = self.root / ".env"
        dotenv.write_text("FIXTURE_MODE=ok\n")
        self.change_source()
        args = ["--repo-root", str(self.root), "--provider", "codex"]
        self.assertEqual(runner.main(args), 0)
        real_dotenv = runner.dotenv_identity
        changed = False

        def replace_after_capture(*pos, **kwargs):
            nonlocal changed
            value = real_dotenv(*pos, **kwargs)
            if not changed:
                dotenv.write_text("FIXTURE_MODE=ko\n")
                changed = True
            return value

        with mock.patch.object(runner, "dotenv_identity", side_effect=replace_after_capture):
            self.assertEqual(runner.main(args), 1)
        self.assertFalse(self.receipt()["cached"])
        self.assertTrue(any(item["id"] == "context-stability" for item in self.receipt()["results"]))

    def test_changed_tool_after_context_capture_cannot_return_cached_pass(self):
        runner = load("agent-check")
        directory = self.root / "node_modules"
        directory.mkdir()
        version = directory / "version"
        version.write_text("1")
        self.change_source()
        args = ["--repo-root", str(self.root), "--provider", "codex"]
        probe = [sys.executable, "-c", "from pathlib import Path; print(Path('node_modules/version').read_text())"]
        real_dotenv = runner.dotenv_identity
        changed = False

        def change_tool(*pos, **kwargs):
            nonlocal changed
            value = real_dotenv(*pos, **kwargs)
            if not changed:
                version.write_text("2")
                changed = True
            return value

        with mock.patch.dict(runner.TOOLS, {"python": probe}):
            self.assertEqual(runner.main(args), 0)
            with mock.patch.object(runner, "dotenv_identity", side_effect=change_tool):
                self.assertEqual(runner.main(args), 1)
        self.assertFalse(self.receipt()["cached"])
        self.assertTrue(any(item["id"] == "context-stability" for item in self.receipt()["results"]))

    def test_dotenv_mutation_during_fresh_checks_cannot_save_pass(self):
        runner = load("agent-check")
        (self.root / ".gitignore").write_text(".ai/agent-checks/\nnode_modules/\n.env\n")
        (self.root / ".env").write_text("FIXTURE_MODE=ok\n")
        self.change_source()
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; p=Path('.env'); assert 'ok' in p.read_text(); "
            "p.write_text('FIXTURE_MODE=ko\\n')"]]
        self.save_config()
        self.assertEqual(runner.main(["--repo-root", str(self.root), "--provider", "codex"]), 1)
        self.assertTrue(any(item["id"] == "context-stability" for item in self.receipt()["results"]))
        self.assertFalse(any((self.root / ".ai/agent-checks/cache").glob("*.json")))

    def test_readiness_contract_v3_partitions_previous_cache_identities(self):
        runner = load("agent-check")
        self.assertEqual(runner.READY_IDENTITY_CONTRACT, "agent-ready-content/v3")
        directory = self.root / "node_modules"
        directory.mkdir()
        (directory / "index.js").write_text("synthetic fixture")
        item = {"ready_paths": ["node_modules"]}
        current = runner.ready_identity(self.root, item, key=b"synthetic-key")
        self.assertIsNotNone(current)
        with mock.patch.object(runner, "READY_IDENTITY_CONTRACT", "agent-ready-content/v2"):
            previous = runner.ready_identity(self.root, item, key=b"synthetic-key")
        self.assertIsNotNone(previous)
        self.assertNotEqual(current, previous)

    def test_same_stat_dependency_rewrite_changes_ready_identity(self):
        runner = load("agent-check")
        target = self.root / "node_modules/tool.js"
        target.parent.mkdir()
        target.write_text("ok")
        with self._frozen_dependency_metadata(runner, target):
            first = runner.ready_identity(self.root, {"ready_paths": ["node_modules"]})
            target.write_text("ko")
            self.assertNotEqual(first, runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))

    def test_same_stat_linked_file_rewrite_changes_ready_identity(self):
        runner = load("agent-check")
        target = self.root / "owned-package.js"
        target.write_text("ok")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/package.js").symlink_to(target)
        with self._frozen_dependency_metadata(runner, target):
            first = runner.ready_identity(self.root, {"ready_paths": ["node_modules"]})
            target.write_text("ko")
            self.assertNotEqual(first, runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))

    def test_same_stat_linked_directory_rewrite_changes_ready_identity(self):
        runner = load("agent-check")
        package = self.root / "packages/shared"
        package.mkdir(parents=True)
        target = package / "index.js"
        target.write_text("ok")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/shared").symlink_to(package, target_is_directory=True)
        with self._frozen_dependency_metadata(runner, target):
            first = runner.ready_identity(self.root, {"ready_paths": ["node_modules"]})
            target.write_text("ko")
            self.assertNotEqual(first, runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))

    def test_same_stat_rewrite_cannot_reuse_environment_or_candidate_pass(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        args = ["--repo-root", str(self.root), "--provider", "codex"]
        self.assertEqual(runner.main([*args, "--provision"]), 0)
        target = self.root / "node_modules/tool/bin.js"
        with self._frozen_dependency_metadata(runner, target):
            target.write_text("ko")
            self.assertEqual(runner.main(args), 1)
            self.assertFalse(self.receipt()["cached"])
            self.assertTrue(any(item["id"] == "dependencies" and item["status"] == "unavailable"
                                for item in self.receipt()["results"]))

    def test_external_dependency_links_do_not_read_content_or_reuse_pass(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "package.js"
            outside.write_text("owner fixture")
            self.config["environments"][0]["commands"][0][-1] += (
                "; link=Path('node_modules/linked.js'); "
                "link.unlink(missing_ok=True); link.symlink_to(" + repr(str(outside)) + ")")
            self.save_config()
            args = ["--repo-root", str(self.root), "--provider", "codex", "--provision"]
            real_open = runner.os.open

            outside_identity = (outside.stat().st_dev, outside.stat().st_ino)

            def refuse_outside(path, *rest, **kwargs):
                descriptor = real_open(path, *rest, **kwargs)
                details = runner.os.fstat(descriptor)
                if (details.st_dev, details.st_ino) == outside_identity:
                    runner.os.close(descriptor)
                    self.fail("readiness hashing opened external file contents")
                return descriptor

            with mock.patch.object(runner.os, "open", side_effect=refuse_outside) as guarded_open, \
                    mock.patch.object(runner.os, "supports_dir_fd", runner.os.supports_dir_fd | {guarded_open}):
                self.assertEqual(runner.main(args), 0)
                self.assertFalse(self.receipt()["cache_reusable"])
                self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))
                self.assertEqual(runner.main(args), 0)
            self.assertFalse(self.receipt()["cached"])
            self.assertEqual((self.root / "node_modules/installs").read_text(), "11")

    def test_top_level_external_ready_link_allows_fresh_checks(self):
        runner = load("agent-check")
        self.change_source()
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "interpreter"
            outside.write_text("fixture")
            setup = ("from pathlib import Path; p=Path('node_modules/python'); "
                     "p.parent.mkdir(exist_ok=True); p.unlink(missing_ok=True); "
                     "p.symlink_to(" + repr(str(outside)) + ")")
            self.config["environments"] = [{"id": "tool", "ready_paths": ["node_modules/python"],
                                           "commands": [[sys.executable, "-c", setup]]}]
            self.config["checks"][0]["environment"] = "tool"
            self.save_config()
            self.assertEqual(runner.main(["--repo-root", str(self.root), "--provider", "codex", "--provision"]), 0)
            self.assertFalse(self.receipt()["cache_reusable"])
            self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": ["node_modules/python"]}))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO fixture")
    def test_special_ready_file_is_nonreusable_without_opening_it(self):
        runner = load("agent-check")
        (self.root / "node_modules").mkdir()
        os.mkfifo(self.root / "node_modules/pipe")
        self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))

    def test_readiness_scan_budgets_disable_reuse(self):
        runner = load("agent-check")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/tool.js").write_text("owned fixture")
        with mock.patch.object(runner, "READY_IDENTITY_MAX_ENTRIES", 1):
            self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))
        with mock.patch.object(runner, "READY_IDENTITY_MAX_BYTES", 1):
            self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))

    def test_unsupported_safe_directory_reads_disable_reuse(self):
        runner = load("agent-check")
        with mock.patch.object(runner.os, "supports_fd", set()):
            self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": []}))

    def test_parent_replaced_with_external_link_is_not_enumerated(self):
        runner = load("agent-check")
        package = self.root / "node_modules/pkg"
        package.mkdir(parents=True)
        (package / "index.js").write_text("owned fixture")
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (outside / "index.js").write_text("external fixture")
            outside_identity = (outside.stat().st_dev, outside.stat().st_ino)
            real_open, real_scandir = runner.os.open, runner.os.scandir
            swapped = False

            def swap_parent(path, *args, **kwargs):
                nonlocal swapped
                if path == "pkg" and kwargs.get("dir_fd") is not None and not swapped:
                    package.rename(package.with_name("pkg-original"))
                    package.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return real_open(path, *args, **kwargs)

            def owned_scandir(descriptor):
                details = runner.os.fstat(descriptor)
                self.assertNotEqual((details.st_dev, details.st_ino), outside_identity,
                                    "readiness walker enumerated an external directory")
                return real_scandir(descriptor)

            with mock.patch.object(runner.os, "open", side_effect=swap_parent) as guarded_open, \
                    mock.patch.object(runner.os, "scandir", side_effect=owned_scandir) as guarded_scandir, \
                    mock.patch.object(runner.os, "supports_dir_fd", runner.os.supports_dir_fd | {guarded_open}), \
                    mock.patch.object(runner.os, "supports_fd", runner.os.supports_fd | {guarded_scandir}):
                self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))
            self.assertTrue(swapped)

    def test_dependency_mutation_during_checks_cannot_create_a_cached_pass(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; p=Path('node_modules/tool/bin.js'); "
            "assert p.read_text() == 'ok'; p.write_text('ko')"]]
        self.save_config()
        self.assertEqual(runner.main(["--repo-root", str(self.root), "--provider", "codex", "--provision"]), 1)
        self.assertFalse(self.receipt()["cached"])
        self.assertTrue(any(item["id"] == "dependency-stability" and item["status"] == "failed"
                            for item in self.receipt()["results"]))

    def test_ready_name_preserves_parent_traversal_through_a_link(self):
        runner = load("agent-check")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/tool").write_text("inside")
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (outside / "package").mkdir()
            (self.root / "node_modules/alias").symlink_to(outside / "package", target_is_directory=True)
            name = "node_modules/alias/../tool"
            self.assertEqual(runner.missing_ready_paths(self.root, {"ready_paths": [name]}), [name])
            (outside / "tool").write_text("outside")
            self.assertEqual(runner.missing_ready_paths(self.root, {"ready_paths": [name]}), [])
            self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": [name]}))

    def test_persisted_readiness_identity_binds_the_private_key(self):
        runner = load("agent-check")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/tool.js").write_text("synthetic input")
        item = {"ready_paths": ["node_modules"]}
        first = runner.ready_identity(self.root, item, key=b"a" * 32)
        self.assertIsNotNone(first)
        self.assertNotEqual(first, runner.ready_identity(self.root, item, key=b"b" * 32))
        self.assertNotEqual(first, runner.ready_identity(self.root, item))

    def test_unsupported_root_descriptor_keeps_fresh_provision_and_checks(self):
        runner = load("agent-check")
        self._dependency_environment()
        self.change_source()
        real_open = runner.os.open

        def unsupported_root(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path) == self.root:
                raise OSError(errno.EOPNOTSUPP, "synthetic unsupported root descriptor")
            return real_open(path, *args, **kwargs)

        args = ["--repo-root", str(self.root), "--provider", "codex", "--provision"]
        with mock.patch.object(runner.os, "open", side_effect=unsupported_root) as guarded_open, \
                mock.patch.object(runner.os, "supports_dir_fd", runner.os.supports_dir_fd | {guarded_open}):
            self.assertIsNone(runner.ready_identity(self.root, {"ready_paths": ["node_modules"]}))
            self.assertEqual(runner.main(args), 0)
            self.assertFalse(self.receipt()["cache_reusable"])
            self.assertEqual(runner.main(args), 0)
        self.assertFalse(self.receipt()["cached"])
        self.assertEqual((self.root / "node_modules/installs").read_text(), "11")

    def _interacting_dependency_environments(self):
        setup_a = ("from pathlib import Path; p=Path('node_modules/a'); p.mkdir(parents=True, exist_ok=True); "
                   "(p/'value').write_text('ok'); c=Path('.ai/agent-checks/a-installs'); "
                   "c.write_text(c.read_text()+'1' if c.exists() else '1')")
        setup_b = ("from pathlib import Path; p=Path('node_modules/b'); p.mkdir(parents=True, exist_ok=True); "
                   "(p/'value').write_text('ready'); c=Path('.ai/agent-checks/b-installs'); "
                   "c.write_text(c.read_text()+'1' if c.exists() else '1'); "
                   "Path('node_modules/a/value').write_text('ko') if Path('b.lock').read_text()=='break' else None")
        self.config["environments"] = [
            {"id": "a", "ready_paths": ["node_modules/a"], "commands": [[sys.executable, "-c", setup_a]]},
            {"id": "b", "inputs": ["b.lock"], "ready_paths": ["node_modules/b"],
             "commands": [[sys.executable, "-c", setup_b]]},
        ]
        self.config["checks"][0].update(environment="a", commands=[[sys.executable, "-c",
            "from pathlib import Path; c=Path('.ai/agent-checks/check-executions'); "
            "c.write_text(c.read_text()+'1' if c.exists() else '1'); "
            "assert Path('node_modules/a/value').read_text()=='ok'"]])
        self.config["checks"].append({"id": "b-test", "paths": ["src/*", "b.lock", "config/agent-checks.json"],
                                      "environment": "b", "commands": [[sys.executable, "-c",
                "from pathlib import Path; assert Path('node_modules/b/value').read_text()=='ready'"]]})
        self.save_config()
        self.change_source()

    def test_later_provision_revokes_reused_dependency_identity(self):
        runner = load("agent-check")
        self._interacting_dependency_environments()
        (self.root / "b.lock").write_text("safe")
        args = ["--repo-root", str(self.root), "--provider", "codex", "--provision"]
        self.assertEqual(runner.main(args), 0)
        (self.root / "b.lock").write_text("break")
        self.assertEqual(runner.main(args), 1)
        record = self.root / ".ai/agent-checks/environments" / (
            "environment-" + runner.sha(runner.encoded("a")) + ".json")
        self.assertFalse(record.exists(), "later setup left stale A readiness evidence")
        self.assertTrue(any(item["id"] == "a" and "cached environment invalidated" in item.get("reason", "")
                            for item in self.receipt()["results"]))
        self.assertEqual((self.root / ".ai/agent-checks/check-executions").read_text(), "1")
        self.assertEqual(runner.main(args), 0)
        self.assertEqual((self.root / ".ai/agent-checks/a-installs").read_text(), "11")
        self.assertEqual((self.root / ".ai/agent-checks/b-installs").read_text(), "11")
        self.assertEqual((self.root / ".ai/agent-checks/check-executions").read_text(), "11")

    def test_later_provision_cannot_rekey_new_dependency_identity(self):
        runner = load("agent-check")
        self._interacting_dependency_environments()
        (self.root / "b.lock").write_text("break")
        args = ["--repo-root", str(self.root), "--provider", "codex", "--provision"]
        self.assertEqual(runner.main(args), 1)
        record = self.root / ".ai/agent-checks/environments" / (
            "environment-" + runner.sha(runner.encoded("a")) + ".json")
        self.assertFalse(record.exists(), "new A setup was re-keyed to B's unvalidated dependency bytes")
        self.assertTrue(any(item["id"] == "a" and "provisioned environment invalidated" in item.get("reason", "")
                            for item in self.receipt()["results"]))
        self.assertFalse((self.root / ".ai/agent-checks/check-executions").exists())
        self.assertEqual(runner.main(args), 0)
        self.assertEqual((self.root / ".ai/agent-checks/a-installs").read_text(), "11")
        self.assertEqual((self.root / ".ai/agent-checks/b-installs").read_text(), "1")
        self.assertEqual((self.root / ".ai/agent-checks/check-executions").read_text(), "1")

    def _dependency_environment(self):
        setup = ("from pathlib import Path; p=Path('node_modules/tool'); p.mkdir(parents=True, exist_ok=True); "
                 "(p/'bin.js').write_text('ok'); c=Path('node_modules/installs'); "
                 "c.write_text(c.read_text()+'1' if c.exists() else '1')")
        self.config["environments"] = [{"id": "dependencies", "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0].update(environment="dependencies", commands=[[sys.executable, "-c",
            "from pathlib import Path; assert Path('node_modules/tool/bin.js').read_text() == 'ok'"]])
        self.save_config()

    def test_deleted_dependency_is_not_reused_as_ready_setup(self):
        self._dependency_environment()
        self.change_source()
        self.run_check("--provision")
        shutil.rmtree(self.root / "node_modules/tool")
        self.run_check(expected=1)
        self.assertFalse(self.receipt()["cached"])
        self.assertTrue(any(r["id"] == "dependencies" and r["status"] == "unavailable"
                            for r in self.receipt()["results"]))
        self.run_check("--provision")
        self.assertEqual((self.root / "node_modules/installs").read_text(), "11")

    def test_edited_dependency_is_not_reused_as_ready_setup(self):
        self._dependency_environment()
        self.change_source()
        self.run_check("--provision")
        (self.root / "node_modules/tool/bin.js").write_text("broken but same size")
        self.run_check(expected=1)
        self.assertFalse(self.receipt()["cached"])

    def test_tool_cache_churn_keeps_ready_setup(self):
        self._dependency_environment()
        self.change_source()
        self.run_check("--provision")
        cache = self.root / "node_modules/.cache/lint"
        cache.mkdir(parents=True)
        (cache / "state").write_text("written by a check")
        (self.root / "node_modules/tool/__pycache__").mkdir()
        self.run_check("--provision")
        self.assertTrue(self.receipt()["cached"])
        self.assertEqual((self.root / "node_modules/installs").read_text(), "1")

    def test_dependency_replaced_with_same_size_and_mtime_is_not_reused(self):
        self._dependency_environment()
        self.change_source()
        self.run_check("--provision")
        target = self.root / "node_modules/tool/bin.js"
        before = target.stat()
        replacement = self.root / "node_modules/tool/bin.js.new"
        replacement.write_text("ko")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, target)
        self.assertEqual((target.stat().st_size, target.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.run_check(expected=1)
        self.assertFalse(self.receipt()["cached"])

    def test_symlinked_dependency_target_changes_are_not_reused(self):
        runner = load("agent-check")
        outside = self.root / "outside.js"
        outside.write_text("ok")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/linked.js").symlink_to(outside)
        item = {"ready_paths": ["node_modules"]}
        first = runner.ready_identity(self.root, item)
        outside.write_text("ko")
        self.assertNotEqual(first, runner.ready_identity(self.root, item))

    def test_replaced_dependency_directory_is_not_reused(self):
        runner = load("agent-check")
        (self.root / "node_modules/pkg").mkdir(parents=True)
        item = {"ready_paths": ["node_modules"]}
        first = runner.ready_identity(self.root, item)
        (self.root / "node_modules/pkg").rmdir()
        (self.root / "node_modules/spacer").mkdir()  # keep the old inode from being reused
        (self.root / "node_modules/pkg").mkdir()
        (self.root / "node_modules/spacer").rmdir()
        self.assertNotEqual(first, runner.ready_identity(self.root, item))

    def test_symlink_removed_during_the_walk_does_not_block(self):
        runner = load("agent-check")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/link.js").symlink_to(self.root / "src/value.py")
        def gone(path, *args, **kwargs):
            raise FileNotFoundError(path)
        with mock.patch.object(runner.os, "readlink", side_effect=gone):
            runner.ready_identity(self.root, {"ready_paths": ["node_modules"]})

    def test_symlinked_dependency_directory_contents_are_bound(self):
        runner = load("agent-check")
        package = self.root / "packages/shared"
        package.mkdir(parents=True)
        (package / "index.js").write_text("ok")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/shared").symlink_to(package, target_is_directory=True)
        (self.root / "node_modules/loop").symlink_to(self.root / "node_modules", target_is_directory=True)
        item = {"ready_paths": ["node_modules"]}
        first = runner.ready_identity(self.root, item)
        # In place: the package directory's own stat stays the same, so only a
        # walk through the link sees the change.
        (package / "index.js").write_text("ko")
        self.assertNotEqual(first, runner.ready_identity(self.root, item))

    def test_symlinked_dotenv_binds_the_target_contents(self):
        runner = load("agent-check")
        plan = {"checks": [], "environments": []}
        shared = self.root / "shared.env"
        shared.write_text("MODE=good")
        (self.root / ".env.local").symlink_to(shared)
        first = runner.dotenv_identity(self.root, plan, b"k" * 32)
        shared.write_text("MODE=bad")
        self.assertNotEqual(first, runner.dotenv_identity(self.root, plan, b"k" * 32))

    def test_reprovisioned_dependencies_never_reuse_a_cached_pass(self):
        self._dependency_environment()
        self.change_source()
        self.run_check("--provision")
        self.run_check("--provision")
        self.assertTrue(self.receipt()["cached"])
        shutil.rmtree(self.root / "node_modules/tool")
        self.run_check("--provision")
        self.assertFalse(self.receipt()["cached"])
        self.assertIn("AGENT-CHECK test:", self.run_check("--provision", "--no-cache").stdout)

    def test_vite_config_bundling_directory_keeps_ready_setup(self):
        self._dependency_environment()
        self.change_source()
        self.run_check("--provision")
        (self.root / "node_modules/.vite-temp").mkdir()
        (self.root / "node_modules/.vite-temp/vitest.config.mjs").write_text("bundled")
        self.run_check("--provision")
        self.assertTrue(self.receipt()["cached"])
        self.assertEqual((self.root / "node_modules/installs").read_text(), "1")

    def test_untracked_executable_bit_is_part_of_the_candidate(self):
        helper = self.root / "src/helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o755)
        self.config["checks"][0]["commands"] = [[str(helper)]]
        self.save_config()
        self.run_check()
        helper.chmod(0o644)
        # Without the mode in the snapshot this returned the cached PASS (exit 0).
        result = self.run_check(expected=2)
        self.assertIn("AGENT-CHECK test:", result.stdout)
        self.assertNotIn("AGENT-CHECK-CACHED", result.stdout)

    def test_ignored_dotenv_changes_invalidate_a_cached_pass_without_recording_values(self):
        with open(self.root / ".gitignore", "a") as stream:
            stream.write(".env.local\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore dotenv")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        dotenv = self.root / ".env.local"
        dotenv.write_text("MODE=good\n")
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert Path('.env.local').read_text() == 'MODE=good\\n'"]]
        self.save_config()
        self.run_check()
        self.run_check()
        self.assertTrue(self.receipt()["cached"])
        dotenv.write_text("MODE=bad\n")
        self.run_check(expected=1)
        self.assertFalse(self.receipt()["cached"])
        state = self.root / ".ai/agent-checks"
        stored = "".join(p.read_text() for p in state.rglob("*.json"))
        self.assertNotIn("MODE=", stored)

    def test_unreadable_or_directory_dotenv_binds_state_without_blocking(self):
        runner = load("agent-check")
        plan = {"checks": [], "environments": []}
        (self.root / ".env.d").mkdir()
        (self.root / ".env.d/one").write_text("a")
        first = runner.dotenv_identity(self.root, plan, b"k" * 32)
        (self.root / ".env.d/two").write_text("b")
        self.assertNotEqual(first, runner.dotenv_identity(self.root, plan, b"k" * 32))
        secret = self.root / ".env.local"
        secret.write_text("X=1")
        real_open = Path.open
        opened = []

        def deny_secret(path, *args, **kwargs):
            if path == secret:
                opened.append(path)
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=deny_secret):
            identity = runner.dotenv_identity(self.root, plan, b"k" * 32)
        self.assertTrue(opened, "the unreadable fallback was not exercised")
        self.assertIn(".env.local", identity)

    def test_dotenv_directory_contents_are_bound(self):
        runner = load("agent-check")
        plan = {"checks": [], "environments": []}
        (self.root / ".env.d/nested").mkdir(parents=True)
        value = self.root / ".env.d/nested/app"
        value.write_text("MODE=good")
        first = runner.dotenv_identity(self.root, plan, b"k" * 32)
        value.write_text("MODE=bad!")  # same names: only the contents changed
        self.assertNotEqual(first, runner.dotenv_identity(self.root, plan, b"k" * 32))

    def test_directory_link_inside_a_dotenv_directory_is_unknown(self):
        # A nested link to a directory cannot be bound by its link text alone:
        # a change under the target must never permit cached-PASS reuse.
        runner = load("agent-check")
        plan = {"checks": [], "environments": []}
        shared = self.root / "shared-env"
        shared.mkdir()
        (shared / "app").write_text("MODE=good")
        (self.root / ".env.d").mkdir()
        (self.root / ".env.d/linked").symlink_to(shared, target_is_directory=True)
        identity = runner.dotenv_identity(self.root, plan, b"k" * 32)
        self.assertIn(None, identity.values())

    def test_link_inside_a_dotenv_directory_binds_its_target_contents(self):
        runner = load("agent-check")
        plan = {"checks": [], "environments": []}
        shared = self.root / "shared.env"
        shared.write_text("MODE=good")
        (self.root / ".env.d").mkdir()
        (self.root / ".env.d/app").symlink_to(shared)
        first = runner.dotenv_identity(self.root, plan, b"k" * 32)
        shared.write_text("MODE=bad!")
        self.assertNotEqual(first, runner.dotenv_identity(self.root, plan, b"k" * 32))

    def test_unreadable_dotenv_replacement_is_bound(self):
        runner = load("agent-check")
        plan = {"checks": [], "environments": []}
        secret = self.root / ".env.local"
        secret.write_text("X=1")
        real_open = Path.open
        opened = []

        def deny_secret(path, *args, **kwargs):
            if path == secret:
                opened.append(path)
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=deny_secret):
            first = runner.dotenv_identity(self.root, plan, b"k" * 32)
            replacement = self.root / ".env.local.new"
            replacement.write_text("X=2")
            os.replace(replacement, secret)
            self.assertNotEqual(first, runner.dotenv_identity(self.root, plan, b"k" * 32))
        self.assertTrue(opened, "the unreadable fallback was not exercised")

    def test_tool_cache_inside_a_symlinked_dependency_keeps_the_identity(self):
        runner = load("agent-check")
        package = self.root / "packages/shared"
        package.mkdir(parents=True)
        (package / "index.js").write_text("ok")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/shared").symlink_to(package, target_is_directory=True)
        item = {"ready_paths": ["node_modules"]}
        first = runner.ready_identity(self.root, item)
        (package / ".vite").mkdir()  # excluded tool cache; moves the target's mtime and ctime
        self.assertEqual(first, runner.ready_identity(self.root, item))

    def test_ready_identity_tolerates_entries_removed_during_the_walk(self):
        runner = load("agent-check")
        (self.root / "node_modules/pkg").mkdir(parents=True)
        (self.root / "node_modules/pkg/a.js").write_text("a")
        real = os.lstat
        def vanishing(path, *args, **kwargs):
            if str(path).endswith("a.js"):
                raise FileNotFoundError(path)
            return real(path, *args, **kwargs)
        with mock.patch.object(runner.os, "lstat", side_effect=vanishing):
            runner.ready_identity(self.root, {"ready_paths": ["node_modules"]})

    def test_fresh_failure_revokes_the_cached_pass_for_the_same_candidate(self):
        flag = self.root / "node_modules/flag"
        flag.parent.mkdir()
        flag.write_text("pass")
        self.config["checks"][0]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; assert Path('node_modules/flag').read_text() == 'pass'"]]
        self.save_config()
        self.run_check()
        flag.write_text("fail")
        self.run_check("--no-cache", expected=1)
        self.run_check(expected=1)
        self.assertFalse(self.receipt()["cached"])

class SurfaceTests(unittest.TestCase):
    def test_installer_rejects_malformed_checks_and_paths_without_writes_or_traceback(self):
        cases = [None, {}, "checks", [None], ["check"], [{"paths": None}],
                 [{"paths": "src/*"}], [{"paths": {}}], [{"paths": [None]}]]
        for checks in cases:
            with self.subTest(checks=checks), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "config").mkdir()
                recipe = root / "config/agent-checks.json"
                recipe.write_text(json.dumps({"schema_version": 1, "base": "origin/main", "checks": checks}))
                before = recipe.read_bytes()
                result = subprocess.run([sys.executable, str(SCRIPTS / "install-agent-validation.py"),
                    "--repo-root", str(root), "--check"], capture_output=True, text=True, timeout=30,
                    env=load("agent-check").git_environment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("AGENT-VALIDATION-INSTALL-BLOCKED", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(recipe.read_bytes(), before)
                self.assertFalse((root / "scripts").exists())

    def test_pack_generated_runners_and_contracts_select_existing_validation(self):
        installed = SCRIPTS.parent
        if not (installed / "scripts/build-plugin.py").is_file():
            self.skipTest("pack-maintainer generated surfaces are not installed consumer inputs")
        runner = load("agent-check")
        config = json.loads((installed / "config/agent-checks.json").read_text())
        plugin_name = load("build-plugin").PLUGIN_NAME
        paths = [prefix + suffix
                 for prefix in (f"plugin/{plugin_name}/", "registry/platform/framework/")
                 for suffix in ("scripts/agent-check.py", "scripts/tests/test_agent_check.py")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unchanged synthetic runner input\n")
            for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                         ("config", "user.email", "fixture@example.invalid"), ("add", "."),
                         ("commit", "-qm", "synthetic generated validation baseline")):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                               env=runner.git_environment())
            for name in paths:
                with self.subTest(path=name):
                    path = root / name
                    path.write_text("changed synthetic runner input\n")
                    plan = runner.plan_for(root, config, "HEAD")
                    self.assertEqual(plan["paths"], [name])
                    self.assertIn("agent-validation-contract", {item["id"] for item in plan["checks"]})
                    path.write_text("unchanged synthetic runner input\n")

    def test_replacement_recipe_cannot_unguard_the_existing_owner_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "config").mkdir()
            original = json.dumps({"schema_version": 1, "base": "origin/trunk", "checks": []})
            (root / "config/agent-checks.json").write_text(original)
            replacement = root / "replacement.json"
            replacement.write_text(json.dumps({"schema_version": 1, "base": "origin/main", "checks": []}))
            env = load("agent-check").git_environment()
            for cmd in [("init", "-q"), ("checkout", "-qb", "trunk")]:
                subprocess.run(["git", *cmd], cwd=root, env=env, check=True, capture_output=True)
            result = subprocess.run([sys.executable, str(SCRIPTS / "install-agent-validation.py"),
                "--repo-root", str(root), "--recipes", str(replacement)], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual((root / "config/agent-checks.json").read_text(), original)
            self.assertFalse((root / "scripts").exists())

    def test_installer_rejects_owner_integration_and_protected_branches_before_writes(self):
        for branch, base, extra in [("trunk", "origin/trunk", []),
                                   ("trunk", "team/upstream/trunk", []),
                                   ("trunk", "refs/remotes/team/upstream/trunk", []),
                                   ("release/stable", "refs/remotes/upstream/release/stable", []),
                                   ("production", "refs/heads/trunk", ["production"])]:
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / "config").mkdir()
                original = json.dumps({"schema_version": 1, "base": base, "checks": [],
                                       "protected_branches": extra})
                (root / "config/agent-checks.json").write_text(original)
                (root / "AGENTS.md").write_text("Owner prose\n")
                env = load("agent-check").git_environment()
                for cmd in [("init", "-q"), ("checkout", "-qb", branch),
                            ("remote", "add", "team/upstream", "https://example.invalid/fixture.git")]:
                    subprocess.run(["git", *cmd], cwd=root, env=env, check=True, capture_output=True)
                result = subprocess.run([sys.executable, str(SCRIPTS / "install-agent-validation.py"),
                    "--repo-root", str(root)], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual((root / "AGENTS.md").read_text(), "Owner prose\n")
                self.assertEqual((root / "config/agent-checks.json").read_text(), original)
                self.assertFalse((root / "scripts").exists())

    def test_installer_resolves_symbolic_bases_and_refuses_revision_or_tag_bases_without_writes(self):
        for base in ["HEAD", "origin/HEAD", "HEAD~0", "integration-tag", "refs/tags/integration-tag"]:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / "config").mkdir()
                original = json.dumps({"schema_version": 1, "base": base, "checks": []})
                (root / "config/agent-checks.json").write_text(original)
                (root / "AGENTS.md").write_text("Owner prose\n")
                env = load("agent-check").git_environment()
                commands = [("init", "-q"), ("checkout", "-qb", "trunk"),
                            ("config", "user.email", "fixture@example.invalid"),
                            ("config", "user.name", "Fixture"), ("add", "."),
                            ("commit", "-qm", "fixture"), ("tag", "integration-tag"),
                            ("update-ref", "refs/remotes/origin/trunk", "HEAD"),
                            ("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")]
                for command in commands:
                    subprocess.run(["git", *command], cwd=root, env=env, check=True, capture_output=True)
                result = subprocess.run([sys.executable, str(SCRIPTS / "install-agent-validation.py"),
                    "--repo-root", str(root)], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual((root / "AGENTS.md").read_text(), "Owner prose\n")
                self.assertEqual((root / "config/agent-checks.json").read_text(), original)
                self.assertFalse((root / "scripts").exists())

    def test_sync_updates_previously_managed_claude_surface_after_root_file_appears(self):
        module = load("sync-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "docs").mkdir()
            (root / "docs/agent-validation.md").write_text("Previous policy\n")
            self.assertEqual(module.main(["--repo-root", str(root)]), 0)
            previous = root / ".claude/CLAUDE.md"
            previous.write_text(previous.read_text() + "\nOwner nested prose\n")
            (root / "CLAUDE.md").write_text("New root owner prose\n")
            (root / "docs/agent-validation.md").write_text("Current policy\n")
            self.assertEqual(module.main(["--repo-root", str(root), "--check"]), 1)
            self.assertEqual(module.main(["--repo-root", str(root)]), 0)
            self.assertIn("Current policy", previous.read_text())
            self.assertNotIn("Previous policy", previous.read_text())
            self.assertIn("Owner nested prose", previous.read_text())
            self.assertIn("New root owner prose", (root / "CLAUDE.md").read_text())
            self.assertEqual(module.main(["--repo-root", str(root), "--check"]), 0)

    def test_primary_claude_alias_updates_without_writing_through_link(self):
        module = load("sync-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "docs").mkdir()
            (root / ".claude").mkdir()
            (root / "docs/agent-validation.md").write_text("Current policy\n")
            primary = root / "CLAUDE.md"
            primary.write_text("Owner prose\n")
            alias = root / ".claude/CLAUDE.md"
            alias.symlink_to("../CLAUDE.md")
            planned = module.outputs(root)
            self.assertIn(primary, planned)
            self.assertNotIn(alias, planned)
            self.assertEqual(module.main(["--repo-root", str(root)]), 0)
            self.assertTrue(alias.is_symlink())
            self.assertEqual(os.readlink(alias), "../CLAUDE.md")
            self.assertIn("Current policy", alias.read_text())
            self.assertIn("Owner prose", primary.read_text())
            self.assertEqual(module.main(["--repo-root", str(root), "--check"]), 0)
            (root / "config").mkdir()
            (root / "config/agent-checks.json").write_text(json.dumps(
                {"schema_version": 1, "base": "origin/main", "checks": []}))
            for args in (("init", "-q"), ("checkout", "-qb", "codex/alias-fixture")):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                               env=load("agent-check").git_environment())
            installer = load("install-agent-validation")
            self.assertEqual(installer.main(["--repo-root", str(root)]), 0)
            self.assertTrue(alias.is_symlink())
            self.assertEqual(os.readlink(alias), "../CLAUDE.md")
            self.assertIn("Owner prose", primary.read_text())
            self.assertEqual(installer.main(["--repo-root", str(root), "--check"]), 0)

    def test_other_claude_links_still_refuse_entire_write_batch(self):
        module = load("sync-agent-validation")
        for kind in ("other-local", "external", "linked-primary", "dangling", "linked-parent"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                root = base / "repo"
                (root / "docs").mkdir(parents=True)
                (root / ".claude").mkdir()
                (root / "docs/agent-validation.md").write_text("Current policy\n")
                target = base / "outside.md" if kind == "external" else root / "other.md"
                if kind != "dangling":
                    target.write_text("Unchanged target\n")
                primary = root / "CLAUDE.md"
                if kind == "linked-primary":
                    primary.symlink_to(target)
                else:
                    primary.write_text("Owner prose\n")
                if kind == "linked-parent":
                    (root / ".claude").rename(root / "actual-claude")
                    (root / ".claude").symlink_to("actual-claude", target_is_directory=True)
                    target = primary
                (root / ".claude/CLAUDE.md").symlink_to(target)
                self.assertEqual(module.main(["--repo-root", str(root)]), 2)
                if kind == "dangling":
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(target.read_text(), "Owner prose\n" if kind == "linked-parent"
                                     else "Unchanged target\n")
                self.assertFalse((root / "AGENTS.md").exists())
                self.assertFalse((root / ".gitignore").exists())

    def test_installer_resolves_utf8_owner_branch_refs_in_an_ascii_locale(self):
        for base in ["origin/HEAD", "origin/intégration"]:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / "config").mkdir()
                (root / "config/agent-checks.json").write_text(json.dumps(
                    {"schema_version": 1, "base": base, "checks": []}), encoding="utf-8")
                env = load("agent-check").git_environment()
                commands = [("init", "-q"), ("checkout", "-qb", "intégration"),
                            ("config", "user.email", "fixture@example.invalid"),
                            ("config", "user.name", "Fixture"), ("add", "."),
                            ("commit", "-qm", "fixture"),
                            ("update-ref", "refs/remotes/origin/intégration", "HEAD"),
                            ("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/intégration"),
                            ("checkout", "-qb", "codex/isolated")]
                for command in commands:
                    subprocess.run(["git", *command], cwd=root, env=env, check=True, capture_output=True)
                result = subprocess.run([sys.executable, str(SCRIPTS / "install-agent-validation.py"),
                    "--repo-root", str(root)], env=ascii_environment(), capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((root / "scripts/agent-check.py").is_file())

    def test_new_root_claude_does_not_hide_a_malformed_previously_managed_surface(self):
        module = load("sync-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".claude").mkdir()
            (root / ".claude/CLAUDE.md").write_text(module.BEGIN + "\nUnclosed old block\n")
            (root / "CLAUDE.md").write_text("New root owner prose\n")
            with self.assertRaises(ValueError):
                module.outputs(root, "Current policy")
            self.assertEqual((root / "CLAUDE.md").read_text(), "New root owner prose\n")

    def test_installer_uses_target_branch_under_foreign_git_context(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, foreign = base / "target", base / "foreign"
            (root / "config").mkdir(parents=True)
            foreign.mkdir()
            recipe = json.dumps({"schema_version": 1, "base": "origin/main", "checks": []})
            (root / "config/agent-checks.json").write_text(recipe)
            (root / "AGENTS.md").write_text("Local owner rules\n")
            clean_env = load("agent-check").git_environment()
            for repo, branch in ((root, "main"), (foreign, "codex/foreign-task")):
                for args in (("init", "-q"), ("checkout", "-qb", branch)):
                    subprocess.run(["git", *args], cwd=repo, env=clean_env, check=True, capture_output=True)
            env = {**clean_env, "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.transport", "GIT_CONFIG_VALUE_0": "preserved"}
            command = [sys.executable, str(SCRIPTS / "install-agent-validation.py"), "--repo-root", str(root)]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse((root / "scripts").exists())
            self.assertFalse((root / ".ai/agent-validation-source.json").exists())
            self.assertEqual((root / "AGENTS.md").read_text(), "Local owner rules\n")
            self.assertEqual((root / "config/agent-checks.json").read_text(), recipe)
            for repo, branch in ((root, "codex/target-task"), (foreign, "main")):
                subprocess.run(["git", "checkout", "-qb", branch], cwd=repo, env=clean_env,
                               check=True, capture_output=True)
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / "scripts/agent-check.py").is_file())
            self.assertIn("Local owner rules", (root / "AGENTS.md").read_text())

    def test_utf8_adoption_and_surfaces_preserve_owner_prose_in_ascii_locale(self):
        installer = load("install-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            pack, root = base / "pack", base / "repo"
            for rel in installer.FILES:
                original = SCRIPTS.parent / rel
                if not original.is_file():
                    original = SCRIPTS.parent / rel.removeprefix(".claude/")
                destination = pack / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
            policy = "# Café validation\n\nExecute the local check.\n"
            (pack / "docs/agent-validation.md").write_text(policy, encoding="utf-8")
            (root / "config").mkdir(parents=True)
            (root / "config/agent-checks.json").write_text(json.dumps({"schema_version": 1, "base": "origin/main", "checks": []}))
            owner = "---\ntitle: Café owner\n---\n\nLocal résumé rules.\n"
            (root / "AGENTS.md").write_text(owner, encoding="utf-8")
            (root / ".gitignore").write_text("# Café local ignores\n", encoding="utf-8")
            env = ascii_environment()
            for args in (("init", "-q"), ("checkout", "-qb", "codex/utf8-fixture")):
                subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
            def run(script, *args):
                result = subprocess.run([sys.executable, str(script), "--repo-root", str(root), *args],
                                        env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run(pack / "scripts/install-agent-validation.py")
            run(pack / "scripts/install-agent-validation.py", "--check")
            self.assertIn("Local résumé rules.", (root / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertTrue((root / "AGENTS.md").read_text(encoding="utf-8").startswith("---\ntitle: Café owner\n---\n"))
            self.assertTrue((root / ".gitignore").read_text(encoding="utf-8").startswith("# Café local ignores\n"))
            new_policy = "# Updated café validation\n"
            (root / "docs/agent-validation.md").write_text(new_policy, encoding="utf-8")
            run(root / "scripts/sync-agent-validation.py")
            run(root / "scripts/sync-agent-validation.py", "--check")
            for rel in ("AGENTS.md", ".claude/CLAUDE.md", ".cursor/rules/agent-validation.mdc"):
                self.assertIn(new_policy.strip(), (root / rel).read_text(encoding="utf-8"))
            self.assertIn("Local résumé rules.", (root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_installer_names_managed_inputs_no_recipe_check_selects(self):
        installer = load("install-agent-validation")
        root = Path("/fixture")
        managed = [*installer.FILES, "config/agent-checks.json"]
        covered = {"checks": [{"id": "all", "paths": managed}]}
        self.assertEqual(installer.uncovered_managed_inputs(root, covered, False), [])
        partial = {"checks": [{"id": "some", "paths": [p for p in managed if not p.endswith("SKILL.md")]}]}
        self.assertEqual(installer.uncovered_managed_inputs(root, partial, False),
                         [p for p in managed if p.endswith("SKILL.md")])

    def test_installer_coverage_uses_the_runner_default_for_checks_without_paths(self):
        installer = load("install-agent-validation")
        self.assertEqual(installer.uncovered_managed_inputs(Path("/fixture"), {"checks": [{"id": "all"}]}, False), [])

    def test_installer_provenance_accepts_sha1_and_sha256_object_ids(self):
        import re
        pattern = load("install-agent-validation").GIT_OBJECT_ID
        self.assertTrue(re.fullmatch(pattern, "a" * 40))
        self.assertTrue(re.fullmatch(pattern, "b" * 64))
        for invalid in ("a" * 39, "a" * 50, "A" * 40, "g" * 64):
            self.assertIsNone(re.fullmatch(pattern, invalid))

    def test_each_managed_input_selects_an_owning_check(self):
        installed = SCRIPTS.parent
        config_path = installed / "config/agent-checks.json"
        if not config_path.is_file():
            self.skipTest("plugin-only bundle has no repository-owned recipe")
        config = json.loads(config_path.read_text())
        runner = load("agent-check")
        paths = (*load("install-agent-validation").FILES, "config/agent-checks.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for rel in paths:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original\n")
            for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                         ("config", "user.name", "Fixture"), ("add", "."),
                         ("commit", "-qm", "managed input fixture")):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                               env=runner.git_environment())
            for rel in paths:
                with self.subTest(path=rel):
                    path = root / rel
                    path.write_text("changed\n")
                    plan = runner.plan_for(root, config, "HEAD")
                    self.assertEqual(plan["paths"], [rel])
                    self.assertTrue(plan["checks"], f"no owning check covers {rel}")
                    renamed = {**config, "checks": [{**c, "id": f"owner-check-{i}"}
                                                     for i, c in enumerate(config["checks"])]}
                    owner_plan = runner.plan_for(root, renamed, "HEAD")
                    self.assertTrue(owner_plan["checks"], f"custom owner check ids lost coverage for {rel}")
                    self.assertEqual([c["commands"] for c in owner_plan["checks"]],
                                     [c["commands"] for c in plan["checks"]])
                    path.write_text("original\n")

    def test_installed_repository_surfaces_are_current(self):
        root = SCRIPTS.parent
        if not (root / "config/agent-checks.json").is_file():
            self.skipTest("plugin-only bundle has no repository-owned recipe")
        for path, expected in load("sync-agent-validation").outputs(root).items():
            self.assertTrue(path.is_file(), str(path))
            self.assertEqual(path.read_text(), expected, str(path))

    def test_same_policy_preserves_local_instructions_and_detects_drift(self):
        module = load("sync-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "docs").mkdir()
            (root / "docs/agent-validation.md").write_text("Shared command and policy\n")
            (root / "AGENTS.md").write_text("Local ownership rules\n")
            self.assertEqual(module.main(["--repo-root", str(root), "--dry-run"]), 0)
            self.assertEqual((root / "AGENTS.md").read_text(), "Local ownership rules\n")
            self.assertEqual(module.main(["--repo-root", str(root)]), 0)
            for rel in ("AGENTS.md", ".claude/CLAUDE.md", ".cursor/rules/agent-validation.mdc"):
                self.assertIn("Shared command and policy", (root / rel).read_text())
                self.assertFalse((root / rel).read_text().endswith("\n\n"))
            self.assertTrue((root / "AGENTS.md").read_text().endswith("Local ownership rules\n"))
            self.assertEqual(module.main(["--repo-root", str(root), "--check"]), 0)
            (root / "docs/agent-validation.md").write_text("New policy\n")
            self.assertEqual(module.main(["--repo-root", str(root), "--check"]), 1)

    def test_symlink_destination_refused_before_any_write(self):
        module = load("sync-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "docs").mkdir()
            (root / "docs/agent-validation.md").write_text("policy")
            (root / "real.md").write_text("local")
            (root / "AGENTS.md").symlink_to(root / "real.md")
            self.assertEqual(module.main(["--repo-root", str(root)]), 2)
            self.assertFalse((root / ".claude/CLAUDE.md").exists())


if __name__ == "__main__":
    unittest.main()
