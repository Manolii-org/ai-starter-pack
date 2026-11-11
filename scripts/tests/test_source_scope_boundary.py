"""Generic synthetic sentinels: a content-boundary hold must precede reads."""
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("/", "_").replace("-", "_"), SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceScopeBoundary(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("src", "config", "opaque", ".ai/agent-checks/cache"):
            (self.root / name).mkdir(parents=True)
        self.config = {"schema_version": 1, "base": "origin/main", "environments": [],
                       "checks": [{"id": "existing-check", "paths": ["src/*"],
                                   "commands": [[sys.executable, "-c", "raise SystemExit(17)"]]}]}
        (self.root / "config/agent-checks.json").write_text(json.dumps(self.config))
        (self.root / "src/value.py").write_text("value = 1\n")
        (self.root / "opaque/sentinel.bin").write_bytes(b"entirely synthetic opaque fixture\n")
        (self.root / ".gitignore").write_text(".ai/agent-checks/\n")
        for args in (("init", "-q"), ("config", "user.name", "Synthetic Fixture"),
                     ("config", "user.email", "fixture@example.invalid"), ("add", "."),
                     ("-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic source"),
                     ("update-ref", "refs/remotes/origin/main", "HEAD")):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        self.runner = load("agent-check")
        self.finalise = load("qa/finalise-gate")
        self.policy = {"schema_version": 1, "repository": "example/project", "scope": "source-only",
                       "allowed_paths": ["src/", "config/", "scripts/"], "protected_paths": ["opaque/"]}
        self.raw = json.dumps(self.policy).encode()

    def declare(self, raw=None):
        (self.root / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw if raw is None else raw)

    def test_compiled_policy_requires_independent_identity_and_strict_literal_fields(self):
        pin = hashlib.sha256(self.raw).hexdigest()
        self.assertEqual(self.runner.validate_source_scope_policy(self.raw, expected_sha256=pin,
                                                                 repository="example/project"), self.policy)
        mutations = [{"scope": "full-repository"}, {"schema_version": True}, {"repository": "other/project"},
                     {"projection_verified": True}, {"allowed_paths": []}, {"protected_paths": ["../outside"]},
                     {"allowed_paths": ["src/*"]}, {"protected_paths": ["opaque/", "opaque/"]}]
        for change in mutations:
            raw = json.dumps({**self.policy, **change}).encode()
            with self.subTest(change=change), self.assertRaises(self.runner.SourceScopeError):
                self.runner.validate_source_scope_policy(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                                         repository="example/project")
        for raw in (b'{"scope":1,"scope":2}', b'{"value":NaN}', b"not-json"):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.validate_source_scope_policy(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                                         repository="example/project")
        with self.assertRaises(self.runner.SourceScopeError):
            self.runner.validate_source_scope_policy(self.raw, expected_sha256="0" * 64, repository="example/project")

    def test_every_content_selection_cache_and_process_entry_refuses_before_io(self):
        # No declaration bytes, configuration, sentinel, receipt or setup path
        # may be opened. Even an absent/invalid independent pin is a hold.
        calls = [lambda: self.runner.raw_tracked_inputs(self.root), lambda: self.runner.snapshot(self.root),
                 lambda: self.runner.plan_for(self.root, self.config), lambda: self.runner.config_for(self.root),
                 lambda: self.runner.git(self.root, "diff", "--name-only"),
                 lambda: self.runner.git(self.root, "diff", "--binary"),
                 lambda: self.runner.private_cache_key(self.root),
                 lambda: self.runner.execute(self.root, self.config["checks"][0], {}),
                 lambda: self.runner.versions(self.root, {"checks": [], "environments": []}),
                 lambda: self.runner.ready_identity(self.root, {"ready_paths": ["opaque"]}),
                 lambda: self.runner.missing_ready_paths(self.root, {"ready_paths": ["opaque"]}),
                 lambda: self.runner.environment_digest(self.root, {"inputs": ["opaque/sentinel.bin"]}, {}, b"k" * 32, {}),
                 lambda: self.runner.dotenv_identity(self.root, {}, b"k" * 32),
                 lambda: self.runner.file_digest(self.root / "opaque/sentinel.bin"),
                 lambda: self.runner.dotenv_bytes(self.root / "opaque/sentinel.bin"),
                 lambda: self.runner.unreadable_state(self.root / "opaque/sentinel.bin"),
                 lambda: self.runner.write_json(self.root / ".ai/agent-checks/receipt.json", {"outcome": "passed"})]
        with patch.dict(os.environ, {self.runner.SOURCE_SCOPE_PIN: ""}), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("content read")), \
             patch.object(Path, "read_text", side_effect=AssertionError("text read")), \
             patch("builtins.open", side_effect=AssertionError("file open")), \
             patch.object(self.runner.os, "open", side_effect=AssertionError("descriptor open")), \
             patch.object(self.runner.subprocess, "run", side_effect=AssertionError("Git/process execution")):
            for call in calls:
                with self.subTest(call=call), self.assertRaises(self.runner.SourceScopeError):
                    call()

    def test_actual_cli_plan_and_execution_hold_without_opening_any_owner_file(self):
        self.declare()
        wrapper = r'''
import importlib.util,sys
from pathlib import Path
root=Path(sys.argv[1]); runner=Path(sys.argv[2]); arguments=sys.argv[3:]
attempts=[]
def audit(event,args):
 if event == 'open' and isinstance(args[0],(str,bytes)):
  path=Path(args[0]).absolute()
  if path.is_relative_to(root):
   attempts.append('owner-content-open')
   raise AssertionError('owner content opened before boundary')
sys.addaudithook(audit)
spec=importlib.util.spec_from_file_location('scoped_actual_runner',runner)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
code=module.main(['--repo-root',str(root),*arguments])
assert not attempts, attempts
assert code == 4, code
raise SystemExit(code)
'''
        for arguments in (["--plan"], ["--provision"], ["--no-cache"]):
            result = subprocess.run([sys.executable, "-B", "-c", wrapper, str(self.root),
                                     str(SCRIPTS / "agent-check.py"), *arguments],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertIn("SOURCE-SCOPE-HELD", result.stderr)
            self.assertNotIn("AGENT-CHECK-CACHED", result.stdout)

    def test_policy_symlink_alias_or_malformed_bytes_are_never_opened(self):
        policy = self.root / self.runner.SOURCE_SCOPE_POLICY
        for kind in ("symlink", "hardlink", "malformed"):
            if kind == "symlink":
                policy.symlink_to(self.root / "opaque/sentinel.bin")
            elif kind == "hardlink":
                os.link(self.root / "opaque/sentinel.bin", policy)
            else:
                policy.write_bytes(b"untrusted policy bytes must not be read")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("policy/data opened")), \
                 patch.object(self.runner.os, "open", side_effect=AssertionError("descriptor read")):
                with self.assertRaises(self.runner.SourceScopeError):
                    self.runner.snapshot(self.root)
            policy.unlink()

    def test_committed_declaration_cannot_be_removed_or_hidden_by_working_policy(self):
        self.declare()
        subprocess.run(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic scope"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        real = self.runner.subprocess.run
        observed = []
        def metadata_only(command, **kwargs):
            observed.append(command)
            self.assertEqual(command, ["git", "--no-replace-objects", "-c", "protocol.allow=never",
                                       "ls-tree", "-z", "HEAD", "--", self.runner.SOURCE_SCOPE_POLICY])
            self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "")
            return real(command, **kwargs)
        with patch.object(self.runner.subprocess, "run", side_effect=metadata_only), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("protected content read")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.plan_for(self.root, self.config)
        self.assertEqual(len(observed), 1)

    def test_symlinked_policy_parent_and_external_content_reader_stay_held(self):
        (self.root / ".ai").rename(self.root / ".ai-retained")
        (self.root / ".ai").symlink_to(self.root / "opaque", target_is_directory=True)
        (self.root / "opaque/source-scope-policy.json").symlink_to(self.root / "opaque/sentinel.bin")
        with patch.object(self.runner.os, "open", side_effect=AssertionError("alias descriptor opened")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.snapshot(self.root)
        with patch.dict(os.environ, {self.runner.SOURCE_SCOPE_PIN: ""}), \
             patch.object(Path, "open", side_effect=AssertionError("external content opened")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.file_digest(Path("/synthetic-outside/data"))

    def test_stale_pass_and_mode_bypass_never_materialize_or_authorize(self):
        self.declare()
        receipt = self.root / ".ai/agent-checks/receipt.json"
        receipt.write_text('{"schema":"agent-check/v1","outcome":"passed","cached":true}')
        for mode in ("off", "warn", "block"):
            with patch.dict(os.environ, {"AGENT_FINALISE_GATE": mode}), \
                 patch.object(self.finalise, "load_qa_config", side_effect=AssertionError("config read")), \
                 patch.object(self.finalise.subprocess, "Popen", side_effect=AssertionError("process materialization")), \
                 patch.object(Path, "read_text", side_effect=AssertionError("stale receipt read")):
                result = self.finalise.run_checks(self.root, "codex", 10)
                self.assertEqual(result["outcome"], "source-scope-held")
                self.assertEqual(self.finalise.gate_one(self.root, "codex", True, "push")[0], "deny")
        self.assertEqual(json.loads(receipt.read_text())["outcome"], "passed")

    def test_unavailable_existing_head_metadata_never_reaches_protected_content(self):
        def unavailable(command, **kwargs):
            operation = command[4]
            if operation == "ls-tree":
                return subprocess.CompletedProcess(command, 128, b"", b"synthetic missing tree")
            if operation == "rev-parse":
                return subprocess.CompletedProcess(command, 0, b"a" * 40 + b"\n", b"")
            if operation == "symbolic-ref":
                return subprocess.CompletedProcess(command, 0, b"refs/heads/main\n", b"")
            raise AssertionError("unavailable metadata must never trigger content/fetch commands")
        with patch.object(self.runner.subprocess, "run", side_effect=unavailable), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("unproven data read")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.file_digest(self.root / "opaque/sentinel.bin")

    def test_late_scope_exit_ignores_stale_receipts_and_is_not_warn_mode_success(self):
        with patch.object(self.finalise, "source_scope_declared", return_value=False), \
             patch.object(self.finalise.subprocess, "Popen") as process, \
             patch.object(Path, "read_text", side_effect=AssertionError("stale receipt read")), \
             patch.object(self.finalise, "environment_ids", side_effect=AssertionError("config read")):
            process.return_value.wait.return_value = 4
            self.assertEqual(self.finalise.run_checks(self.root, "codex", 10)["outcome"], "source-scope-held")
        with patch.object(self.finalise, "source_scope_declared", return_value=False), \
             patch.object(self.finalise, "load_qa_config", return_value=({"mode": "warn"}, True)), \
             patch.object(self.finalise, "run_checks", return_value={"outcome": "source-scope-held"}):
            self.assertEqual(self.finalise.gate_one(self.root, "codex", False, "push")[0], "deny")

    def test_installer_refuses_before_recipe_layout_or_owner_prose(self):
        self.declare()
        installer = load("install-agent-validation")
        arguments = type("Arguments", (), {"repo_root": self.root, "layout": None, "recipes": None})()
        with patch.object(installer, "OutputPlan", side_effect=AssertionError("owner materialization")):
            with self.assertRaises(ValueError):
                installer.build_plan(arguments)

    def test_default_v1_still_executes_and_real_failure_is_not_omitted(self):
        (self.root / "src/value.py").write_text("value = 2\n")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(self.runner.main(["--repo-root", str(self.root), "--no-cache"]), 1)
        receipt = json.loads((self.root / ".ai/agent-checks/receipt.json").read_text())
        self.assertEqual(receipt["schema"], "agent-check/v1")
        self.assertEqual(receipt["outcome"], "failed")
        self.assertTrue(any(item["id"] == "existing-check" and item["status"] == "failed"
                            for item in receipt["results"]))


    def test_enclosing_boundary_cannot_be_hidden_by_subdirectory_or_nested_git(self):
        nested = self.root / "opaque/subproject"
        (nested / "config").mkdir(parents=True)
        (nested / "config/agent-checks.json").write_text(json.dumps(self.config))
        (nested / "sentinel.bin").write_bytes(b"synthetic nested opaque bytes")
        subprocess.run(["git", "init", "-q", str(nested)], check=True, capture_output=True)
        self.declare()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("protected nested read")), \
             patch.object(Path, "read_text", side_effect=AssertionError("protected nested text")), \
             patch.object(self.runner.os, "open", side_effect=AssertionError("protected nested descriptor")), \
             patch.object(self.runner.subprocess, "run", side_effect=AssertionError("native Git before hold")):
            for root in (self.root / "opaque", nested):
                self.assertTrue(self.runner.source_scope_declared(root))
                self.assertTrue(self.finalise.source_scope_declared(root))
                with self.assertRaises(self.runner.SourceScopeError):
                    self.runner.config_for(root)
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.file_digest(nested / "sentinel.bin")
            with self.assertRaises(self.finalise.SourceScopeHeld):
                self.finalise.git_toplevel(nested)

    def test_native_git_config_includes_and_aliases_refuse_before_any_git_process(self):
        # The declaration exists only in the committed tree. Native Git must not
        # parse the opaque include before reporting that committed declaration.
        self.declare()
        subprocess.run(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Scope declaration"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        config = self.root / ".git/config"
        original = config.read_bytes()
        opaque = self.root / "opaque/git-control-sentinel"
        opaque.write_bytes(b"entirely synthetic invalid Git config\n")
        for kind in ("include", "includeIf", "symlink", "hardlink"):
            config.unlink()
            if kind == "symlink":
                config.symlink_to(opaque)
            elif kind == "hardlink":
                os.link(opaque, config)
            else:
                heading = '[include]' if kind == "include" else '[includeIf "gitdir:*"]'
                config.write_bytes(original + ("\n" + heading + "\n path = " + str(opaque) + "\n").encode())
            with self.subTest(kind=kind), \
                 patch.object(self.runner.subprocess, "run", side_effect=AssertionError("native Git before config hold")):
                with self.assertRaises(self.runner.SourceScopeError):
                    self.runner.require_source_scope(self.root)
                self.assertTrue(self.finalise.source_scope_declared(self.root))
            config.unlink()
            config.write_bytes(original)

    def test_standard_linked_worktree_metadata_is_compatible_but_indirection_is_held(self):
        linked = self.root / "linked"
        subprocess.run(["git", "worktree", "add", "--detach", str(linked), "HEAD"],
                       cwd=self.root, check=True, capture_output=True)
        self.assertFalse(self.runner.source_scope_declared(linked))
        self.assertFalse(self.finalise.source_scope_declared(linked))
        pointer = (linked / ".git").read_text().strip()[8:]
        config = Path(pointer) / "config.worktree"
        config.symlink_to(self.root / "opaque/sentinel.bin")
        real = subprocess.run
        def outer_metadata_only(command, **kwargs):
            if kwargs.get("cwd") == linked:
                raise AssertionError("Git through aliased worktree config")
            self.assertEqual(kwargs.get("cwd"), self.root)
            self.assertEqual(command[-5:], ["ls-tree", "-z", "HEAD", "--", self.runner.SOURCE_SCOPE_POLICY])
            return real(command, **kwargs)
        with patch.object(self.runner.subprocess, "run", side_effect=outer_metadata_only):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(linked)
            self.assertTrue(self.finalise.source_scope_declared(linked))

    def test_unqualified_git_pointer_refuses_before_opening_target_control_files(self):
        marker = self.root / ".git"
        marker.rename(self.root / ".git-original-synthetic")
        target = self.root / "opaque/unqualified"
        target.mkdir()
        (target / "commondir").write_bytes(b"synthetic opaque control-looking payload")
        marker.write_text("gitdir: " + str(target) + "\n")
        original = os.open
        def no_target_open(path, *args, **kwargs):
            if Path(path).is_relative_to(target):
                raise AssertionError("unqualified metadata target opened")
            return original(path, *args, **kwargs)
        with patch.object(self.runner.os, "open", side_effect=no_target_open), \
             patch.object(self.runner.subprocess, "run", side_effect=AssertionError("Git before pointer hold")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(self.root)
            self.assertTrue(self.finalise.source_scope_declared(self.root))

    def test_finalise_actual_query_and_hook_install_refuse_before_owner_reads_or_git(self):
        qa = self.root / "config/qa.json"
        opaque = self.root / "opaque/qa-sentinel.json"
        opaque.write_text(json.dumps({"schema_version": 1, "finalise_gate": {"replace_legacy_pre_push": True}}))
        os.link(opaque, qa)
        hook = self.root / ".git/hooks/pre-push"
        hook.symlink_to(self.root / "opaque/sentinel.bin")
        wrapper = r"""
import importlib.util,sys
from pathlib import Path
root,source=map(Path,sys.argv[1:3])
def audit(event,args):
    if event == 'open' and isinstance(args[0],(str,bytes)):
        if Path(args[0]).absolute().is_relative_to(root):
            raise AssertionError('owner content read before finalise source-scope hold')
    if event == 'subprocess.Popen':
        raise AssertionError('native Git/process before finalise source-scope hold')
sys.addaudithook(audit)
spec=importlib.util.spec_from_file_location('actual_finalise_boundary',source)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
raise SystemExit(module.main(['--repo-root',str(root),*sys.argv[3:]]))
"""
        for arguments in (["--query", "replace-legacy-pre-push"], ["--install-git-hook"]):
            with self.subTest(arguments=arguments):
                environment = {**os.environ, self.runner.SOURCE_SCOPE_PIN: ""}
                result = subprocess.run([sys.executable, "-B", "-c", wrapper, str(self.root),
                                         str(SCRIPTS / "qa/finalise-gate.py"), *arguments],
                                        env=environment, cwd=self.root, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("SOURCE-SCOPE-HELD", result.stderr)

    def test_inaccessible_policy_metadata_is_held_not_treated_as_absent(self):
        original = Path.lstat
        def unknown(path, *args, **kwargs):
            if path == self.root / self.runner.SOURCE_SCOPE_POLICY:
                raise PermissionError("synthetic metadata denial")
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", unknown), \
             patch.object(self.runner.subprocess, "run", side_effect=AssertionError("Git before metadata denial")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(self.root)
            self.assertTrue(self.finalise.source_scope_declared(self.root))

    def test_actual_installed_shim_does_not_bootstrap_with_native_git_before_hold(self):
        self.assertEqual(self.finalise.install_git_hook(self.root), 0)
        trap = self.root / "synthetic-tools"
        trap.mkdir()
        called = self.root / "native-git-called"
        git = trap / "git"
        git.write_text("#!/bin/sh\nprintf called > " + str(called) + "\nexit 99\n")
        git.chmod(0o700)
        self.declare()
        environment = {**os.environ, "PATH": str(trap) + os.pathsep + os.environ["PATH"],
                       self.runner.SOURCE_SCOPE_PIN: ""}
        result = subprocess.run([str(self.root / ".git/hooks/pre-push")], cwd=self.root,
                                env=environment, input="refs/heads/task " + "1" * 40 + " refs/heads/task " + "0" * 40,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SOURCE-SCOPE-HELD", result.stderr)
        self.assertFalse(called.exists(), "installed shim ran native Git before protected execution")

    def test_static_loose_ref_and_object_aliases_hold_before_native_git_or_payload_reads(self):
        self.declare()
        subprocess.run(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic committed scope"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        ref = (self.root / ".git/HEAD").read_text().strip()[5:]
        targets = [self.root / ".git" / ref, self.root / ".git/objects" / head[:2] / head[2:]]
        for target in targets:
            original = target.read_bytes()
            target.unlink()
            sentinel = self.root / "opaque/static-alias"
            sentinel.write_bytes(original)
            os.link(sentinel, target)
            inode, real_read = sentinel.stat().st_ino, os.read
            def no_opaque_read(fd, count):
                if os.fstat(fd).st_ino == inode:
                    raise AssertionError("static protected alias payload read")
                return real_read(fd, count)
            try:
                with self.subTest(target=target.parent.name), \
                     patch.object(self.runner.os, "read", side_effect=no_opaque_read), \
                     patch.object(self.runner.subprocess, "run", side_effect=AssertionError("Git before static alias hold")):
                    with self.assertRaises(self.runner.SourceScopeError):
                        self.runner.require_source_scope(self.root)
                    self.assertTrue(self.finalise.source_scope_declared(self.root))
            finally:
                target.unlink()
                target.write_bytes(original)
                sentinel.unlink()

    def test_real_packed_git_fixed_metadata_probe_preserves_default_and_committed_hold(self):
        real = subprocess.run
        expected = ["git", "--no-replace-objects", "-c", "protocol.allow=never",
                    "ls-tree", "-z", "HEAD", "--", self.runner.SOURCE_SCOPE_POLICY]
        observed = []
        def only_fixed_metadata(command, **kwargs):
            self.assertEqual(command, expected)
            self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "")
            observed.append(command)
            return real(command, **kwargs)
        for declared in (False, True):
            if declared:
                self.declare()
                real(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
                real(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic packed scope"],
                     cwd=self.root, check=True, capture_output=True)
                (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
            # All payloads are synthetic; this creates actual packed metadata
            # containing an opaque blob without asking the adapter to extract it.
            real(["git", "repack", "-a", "-d"], cwd=self.root, check=True, capture_output=True)
            self.assertTrue(list((self.root / ".git/objects/pack").glob("*.pack")))
            with patch.object(self.runner.subprocess, "run", side_effect=only_fixed_metadata), \
                 patch.object(Path, "read_bytes", side_effect=AssertionError("working payload read")):
                self.assertEqual(self.runner.source_scope_declared(self.root), declared)
                self.assertEqual(self.finalise.source_scope_declared(self.root), declared)
        self.assertEqual(len(observed), 4)

    def test_committed_enclosing_scope_is_checked_before_nested_git_control_bytes(self):
        self.declare()
        subprocess.run(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Enclosing synthetic scope"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        nested = self.root / "opaque/nested"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True, capture_output=True)
        inode = (nested / ".git/config").stat().st_ino
        original = os.read
        def no_nested_control_read(fd, count):
            if os.fstat(fd).st_ino == inode:
                raise AssertionError("nested protected control bytes before enclosing declaration")
            return original(fd, count)
        with patch.object(self.runner.os, "read", side_effect=no_nested_control_read):
            self.assertTrue(self.runner.source_scope_declared(nested))
            self.assertTrue(self.finalise.source_scope_declared(nested))

    def test_private_worktree_refs_and_controls_hold_before_native_git_or_alias_reads(self):
        self.declare()
        subprocess.run(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Private synthetic scope"],
                       cwd=self.root, check=True, capture_output=True)
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        linked = Path(other.name).resolve() / "linked"
        subprocess.run(["git", "worktree", "add", "--detach", str(linked), "HEAD"],
                       cwd=self.root, check=True, capture_output=True)
        (linked / self.runner.SOURCE_SCOPE_POLICY).unlink()
        gitdir = Path((linked / ".git").read_text().strip()[8:])
        head = (gitdir / "HEAD").read_text()
        private = gitdir / "refs/worktree/selected"
        private.parent.mkdir(parents=True)
        private.write_text(head)
        (gitdir / "HEAD").write_text("ref: refs/worktree/selected\n")
        for target in (private, gitdir / "packed-refs", gitdir / "shallow"):
            previous = target.read_bytes() if target.exists() else None
            if target.exists():
                target.unlink()
            sentinel = self.root / "opaque/private-control"
            sentinel.write_text(head)
            os.link(sentinel, target)
            inode, original = sentinel.stat().st_ino, os.read
            def no_alias_read(fd, count):
                if os.fstat(fd).st_ino == inode:
                    raise AssertionError("private worktree control alias bytes read")
                return original(fd, count)
            try:
                with self.subTest(control=target.name), patch.object(os, "read", side_effect=no_alias_read), \
                     patch.object(subprocess, "run", side_effect=AssertionError("Git through private control alias")):
                    with self.assertRaises(self.runner.SourceScopeError):
                        self.runner.require_source_scope(linked)
                    self.assertTrue(self.finalise.source_scope_declared(linked))
            finally:
                target.unlink()
                if previous is not None:
                    target.write_bytes(previous)
                sentinel.unlink()

    def test_graft_controls_are_not_interpreted_and_static_aliases_hold_without_reads(self):
        graft = self.root / ".git/info/grafts"
        graft.write_text("\n")
        real = subprocess.run
        def no_grafts(command, **kwargs):
            self.assertEqual(kwargs["env"]["GIT_GRAFT_FILE"], os.devnull)
            self.assertEqual(command[-5:], ["ls-tree", "-z", "HEAD", "--", self.runner.SOURCE_SCOPE_POLICY])
            return real(command, **kwargs)
        with patch.object(subprocess, "run", side_effect=no_grafts):
            self.assertFalse(self.runner.source_scope_declared(self.root))
            self.assertFalse(self.finalise.source_scope_declared(self.root))
        graft.unlink()
        sentinel = self.root / "opaque/graft-sentinel"
        sentinel.write_text("\n")
        for kind in ("hardlink", "symlink"):
            if kind == "hardlink":
                os.link(sentinel, graft)
            else:
                graft.symlink_to(sentinel)
            inode, original = sentinel.stat().st_ino, os.read
            def no_payload(fd, count):
                if os.fstat(fd).st_ino == inode:
                    raise AssertionError("graft alias bytes read")
                return original(fd, count)
            try:
                with self.subTest(kind=kind), patch.object(os, "read", side_effect=no_payload), \
                     patch.object(subprocess, "run", side_effect=AssertionError("Git before graft alias hold")):
                    with self.assertRaises(self.runner.SourceScopeError):
                        self.runner.require_source_scope(self.root)
                    self.assertTrue(self.finalise.source_scope_declared(self.root))
            finally:
                graft.unlink()

    def test_symbolic_head_route_stays_in_checked_refs_and_preserves_quote_hash_names(self):
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        reference = "refs/heads/quoted'branch#1"
        subprocess.run(["git", "update-ref", reference, head], cwd=self.root, check=True)
        (self.root / ".git/HEAD").write_text("ref: " + reference + "\n")
        self.assertFalse(self.runner.source_scope_declared(self.root))
        self.assertFalse(self.finalise.source_scope_declared(self.root))
        target = self.root / ".git" / reference
        target.write_text("ref: worktrees/unqualified/HEAD\n")
        with patch.object(subprocess, "run", side_effect=AssertionError("Git through external symbolic-ref route")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(self.root)
            self.assertTrue(self.finalise.source_scope_declared(self.root))

    def test_probe_strips_all_inherited_git_routing_without_changing_legacy_git_environment(self):
        self.declare()
        subprocess.run(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic trace scope"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        subprocess.run(["git", "repack", "-a", "-d"], cwd=self.root, check=True, capture_output=True)
        trace_names = ("GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF", "GIT_TRACE_PACKET",
                       "GIT_TRACE_PERFORMANCE", "GIT_TRACE_SETUP", "GIT_TRACE_PACK_ACCESS", "GIT_TRACE_CURL")
        sentinels = {name: self.root / "opaque" / name for name in trace_names}
        for path in sentinels.values():
            path.write_bytes(b"synthetic unchanged sentinel\n")
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
        environment.update({name: str(path) for name, path in sentinels.items()})
        environment.update({"GIT_TRACE2_ENV_VARS": "SOURCE_SCOPE_SYNTHETIC_VALUE",
                            "SOURCE_SCOPE_SYNTHETIC_VALUE": "synthetic", "SOURCE_SCOPE_PUBLIC_MARKER": "preserved"})
        # Real Git receives no trace routes, so it cannot append even before the
        # committed-only declaration is discovered. No real credentials exist in
        # this controlled process environment.
        with patch.dict(os.environ, environment, clear=True):
            self.assertTrue(self.runner.source_scope_declared(self.root))
            self.assertTrue(self.finalise.source_scope_declared(self.root))
        for path in sentinels.values():
            self.assertEqual(path.read_bytes(), b"synthetic unchanged sentinel\n")
        environment.update({"GIT_DIR": "/synthetic/missing", "GIT_COMMON_DIR": "/synthetic/missing",
                            "GIT_WORK_TREE": "/synthetic/missing", "GIT_NAMESPACE": "unqualified",
                            "GIT_OBJECT_DIRECTORY": "/synthetic/missing", "GIT_EXEC_PATH": "/synthetic/missing",
                            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "include.path",
                            "GIT_CONFIG_VALUE_0": str(sentinels["GIT_TRACE"]),
                            "GIT_CONFIG_PARAMETERS": "unqualified", "GIT_CONFIG_GLOBAL": "/synthetic/missing",
                            "GIT_ASKPASS": "/synthetic/missing", "GIT_SSH_COMMAND": "unqualified",
                            "GIT_EXTERNAL_DIFF": "/synthetic/missing", "GIT_FUTURE_WRITE_ROUTE": "unqualified"})
        # The existing legacy helper remains untouched; the restriction belongs
        # only to this non-authorizing fixed-argv metadata probe.
        self.assertEqual(self.runner.git_environment(environment)["GIT_TRACE"], environment["GIT_TRACE"])
        self.assertEqual(self.runner.git_environment(environment)["GIT_NAMESPACE"], "unqualified")
        real = subprocess.run
        def controlled_probe(command, **kwargs):
            selected = kwargs["env"]
            self.assertEqual({key: value for key, value in selected.items() if key.startswith("GIT_")},
                             {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
                              "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                              "GIT_GRAFT_FILE": os.devnull})
            self.assertEqual(selected["SOURCE_SCOPE_PUBLIC_MARKER"], "preserved")
            self.assertEqual(command, ["git", "--no-replace-objects", "-c", "protocol.allow=never",
                                       "ls-tree", "-z", "HEAD", "--", self.runner.SOURCE_SCOPE_POLICY])
            return real(command, **kwargs)
        with patch.dict(os.environ, environment, clear=True), patch.object(subprocess, "run", side_effect=controlled_probe):
            self.assertTrue(self.runner.source_scope_declared(self.root))
            self.assertTrue(self.finalise.source_scope_declared(self.root))
        for path in sentinels.values():
            self.assertEqual(path.read_bytes(), b"synthetic unchanged sentinel\n")

    def test_schema_literal_path_constraints_match_compiled_validator(self):
        schema = json.loads((SCRIPTS.parent / "schemas/source-scope-policy.v1.schema.json").read_text())
        pattern = schema["$defs"]["paths"]["items"]["pattern"]
        bad = ["../opaque/*", "src/../opaque", "/opaque", "src//", "src//value", ".", "..", "src/./x",
               "src\\value", "src/?", "src/[x]", "src/\n", "src/\x00", "src/\x7f", "src/\ud800"]
        good = ["src", "src/", "scripts/lib/reader.py", "schema.v1.json", "source files/value.py"]
        for value in bad + good:
            raw = json.dumps({**self.policy, "allowed_paths": [value]}).encode()
            with self.subTest(value=repr(value)):
                self.assertEqual(bool(re.search(pattern, value)), value in good)
                if value in bad:
                    with self.assertRaises(self.runner.SourceScopeError):
                        self.runner.validate_source_scope_policy(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                                                 repository="example/project")
                else:
                    self.runner.validate_source_scope_policy(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                                             repository="example/project")



if __name__ == "__main__":
    unittest.main()
