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


def fixed_probe_tail(test, command, kwargs, real, *, full=False):
    """Fixed probes: HEAD listing, or Git's own HEAD^{commit} resolution and that id's listing."""
    if full:
        test.assertEqual(command[:4], ["git", "--no-replace-objects", "-c", "protocol.allow=never"])
    if command[-4:] == ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]:
        if full:
            test.assertEqual(len(command), 8)
        return
    if full:
        test.assertEqual(len(command), 9)
    tail = command[-5:]
    if tail[:4] == ["ls-tree", "-r", "-z", "--name-only"]:
        # Committed nested declarations: names only, of the same listed revision.
        if tail[4] != "HEAD":
            current = real(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=kwargs["cwd"], env=kwargs["env"],
                           capture_output=True, text=True, check=True).stdout.strip()
            test.assertEqual(tail[4], current)
        return
    test.assertEqual(tail[:2] + tail[3:], ["ls-tree", "-z", "--", ".ai/source-scope-policy.json"])
    if tail[2] != "HEAD":
        current = real(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=kwargs["cwd"], env=kwargs["env"],
                       capture_output=True, text=True, check=True).stdout.strip()
        test.assertEqual(tail[2], current)


def is_listing(command):
    return "ls-tree" in command and "-r" not in command


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
            # Inherited GIT_DIR/GIT_WORK_TREE must not route fixture writes elsewhere.
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True,
                           env=load("agent-check").git_environment())
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
            fixed_probe_tail(self, command, kwargs, real, full=True)
            self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "")
            return real(command, **kwargs)
        with patch.object(self.runner.subprocess, "run", side_effect=metadata_only), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("protected content read")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.plan_for(self.root, self.config)
        # Git resolves HEAD^{commit} itself, then that id is listed once.
        self.assertEqual(sum(map(is_listing, observed)), 1)
        self.assertEqual(len(observed), 2)

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

    def test_nested_dotenv_file_links_hold_before_scoped_owner_payload_reads(self):
        # An ordinary local link still contributes its target bytes. A link
        # encountered inside the dotenv directory walk must apply the same
        # lexical/resolved owner guard as a directly requested file.
        dotenv = self.root / ".env.d"
        dotenv.mkdir()
        local = dotenv / "local"
        local.symlink_to(self.root / "src/value.py")
        self.assertTrue(self.runner.dotenv_bytes(dotenv).startswith(b"directory\0"))
        local.unlink()
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory)
            (other / ".ai").mkdir()
            (other / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
            payload = other / "synthetic-value"
            payload.write_bytes(b"entirely synthetic separately owned payload\n")
            real_path_open, real_open = Path.open, open
            opened = []

            def path_open(path, *args, **kwargs):
                if path.resolve() == payload:
                    opened.append("Path.open")
                return real_path_open(path, *args, **kwargs)

            def builtin_open(path, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)).resolve() == payload:
                    opened.append("builtins.open")
                return real_open(path, *args, **kwargs)

            for relative in ("linked", "nested/linked"):
                link = dotenv / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(payload)
                try:
                    with self.subTest(relative=relative), \
                         patch.object(Path, "open", path_open), \
                         patch("builtins.open", builtin_open):
                        with self.assertRaises(self.runner.SourceScopeError):
                            self.runner.dotenv_bytes(dotenv)
                        self.assertEqual(opened, [], "cross-owner payload opened before the hold")
                finally:
                    link.unlink()

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
            fixed_probe_tail(self, command, kwargs, real)
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
        with patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}):
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
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True,
                                       env=self.runner.git_environment()).strip()
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
        observed = []
        def only_fixed_metadata(command, **kwargs):
            fixed_probe_tail(self, command, kwargs, real, full=True)
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
        # One listing per runner/gate call; the runner adds Git's HEAD^{commit}
        # resolution (re-checked after an undeclared listing before caching).
        # An undeclared root listing is followed by one names-only listing for
        # committed nested declarations in each of the runner and the gate.
        self.assertEqual(sum(map(is_listing, observed)), 4)
        self.assertEqual(len(observed), 9)

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
            fixed_probe_tail(self, command, kwargs, real)
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
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True,
                                       env=self.runner.git_environment()).strip()
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
                            "SOURCE_SCOPE_SYNTHETIC_VALUE": "synthetic", "SOURCE_SCOPE_PUBLIC_MARKER": "not-forwarded",
                            "GH_TOKEN": "synthetic-credential-canary", "DOPPLER_TOKEN": "synthetic-credential-canary",
                            "SSH_ASKPASS": "/synthetic/missing", "SSH_AUTH_SOCK": "/synthetic/missing"})
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
                              "GIT_GRAFT_FILE": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
            for name in ("SOURCE_SCOPE_PUBLIC_MARKER", "SOURCE_SCOPE_SYNTHETIC_VALUE", "GH_TOKEN",
                         "DOPPLER_TOKEN", "SSH_ASKPASS", "SSH_AUTH_SOCK"):
                self.assertNotIn(name, selected)
            fixed_probe_tail(self, command, kwargs, real, full=True)
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



class ScopeControlCompatibility(unittest.TestCase):
    """Real synthetic Git/alias inputs cover safe defaults and early holds."""

    setUp = SourceScopeBoundary.setUp
    declare = SourceScopeBoundary.declare

    def other_root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def init_source(self, root):
        root.mkdir(parents=True, exist_ok=True)
        (root / "fixture.txt").write_text("synthetic source\n")
        for arguments in (("init", "-q"), ("config", "user.name", "Synthetic Fixture"),
                          ("config", "user.email", "fixture@example.invalid"), ("add", "."),
                          ("-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic source")):
            subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)

    def alias_root(self):
        wrapper = self.other_root()
        prefix = wrapper / "ambient-prefix"
        prefix.symlink_to(self.root.parent, target_is_directory=True)
        return prefix / self.root.name, wrapper

    def test_ambient_alias_prefix_does_not_alias_git_control_metadata(self):
        lexical, _ = self.alias_root()
        self.assertNotEqual(lexical, lexical.resolve())
        self.assertFalse(self.runner.source_scope_declared(lexical))
        self.assertFalse(self.finalise.source_scope_declared(lexical))
        # The explicit install scope probe must pass too; its independent
        # strict destination-layout validation retains its separate policy.
        installer = load("install-agent-validation")
        observed = []
        original = installer.OutputPlan.regular_path
        def inspect_path(plan, path, **kwargs):
            observed.append(path)
            raise RuntimeError("synthetic stop after successful scope probe")
        from argparse import Namespace
        with patch.object(installer.OutputPlan, "regular_path", inspect_path):
            with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
                installer.build_plan(Namespace(repo_root=lexical, layout=Path("layout.json"), recipes=None))
        self.assertEqual(observed, [self.root])

    def test_lexical_enclosing_working_and_committed_policies_survive_alias_resolution(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                lexical, wrapper = self.alias_root()
                (wrapper / ".ai").mkdir()
                policy = wrapper / self.runner.SOURCE_SCOPE_POLICY
                policy.write_bytes(self.raw)
                if committed:
                    self.init_source(wrapper)
                    policy.unlink()
                real_open = os.open
                def no_inner_controls(path, *args, **kwargs):
                    if Path(path).is_relative_to(self.root / ".git"):
                        raise AssertionError("inner controls read before enclosing boundary")
                    return real_open(path, *args, **kwargs)
                with patch.object(os, "open", side_effect=no_inner_controls):
                    with self.assertRaises(self.runner.SourceScopeError):
                        self.runner.require_source_scope(lexical)
                    self.assertTrue(self.finalise.source_scope_declared(lexical))

    def test_physical_enclosing_policy_is_not_hidden_by_a_lexical_alias(self):
        nested = self.root / "nested-source"
        self.init_source(nested)
        self.declare()
        subprocess.run(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic outer boundary"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        wrapper = self.other_root()
        lexical = wrapper / "source"
        lexical.symlink_to(nested, target_is_directory=True)
        real_open = os.open
        def no_inner_controls(path, *args, **kwargs):
            if Path(path).is_relative_to(nested / ".git"):
                raise AssertionError("inner controls read before physical parent boundary")
            return real_open(path, *args, **kwargs)
        with patch.object(os, "open", side_effect=no_inner_controls):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(lexical)
            self.assertTrue(self.finalise.source_scope_declared(lexical))

    def test_git_marker_alias_remains_held_under_an_ordinary_alias_prefix(self):
        lexical, _ = self.alias_root()
        marker = self.root / ".git"
        original = self.root / "synthetic-controls.git"
        marker.rename(original)
        marker.symlink_to(original, target_is_directory=True)
        with patch.object(subprocess, "run", side_effect=AssertionError("native Git through marker alias")):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(lexical)
            self.assertTrue(self.finalise.source_scope_declared(lexical))

    def test_relative_and_absolute_worktree_links_are_resolved_by_their_owner(self):
        linked = self.root / "linked"
        subprocess.run(["git", "worktree", "add", "--detach", str(linked), "HEAD"],
                       cwd=self.root, check=True, capture_output=True)
        gitdir = Path((linked / ".git").read_text().strip()[8:])
        for relative in (False, True):
            with self.subTest(relative=relative):
                (linked / ".git").write_text("gitdir: " + (os.path.relpath(gitdir, linked) if relative else str(gitdir)) + "\n")
                (gitdir / "gitdir").write_text(os.path.relpath(linked / ".git", gitdir) if relative else str(linked / ".git"))
                # An actual native Git observation confirms the fixture is a
                # supported worktree, rather than a fabricated control layout.
                actual = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=linked, text=True)
                self.assertEqual(Path(actual.strip()), linked)
                self.assertFalse(self.runner.source_scope_declared(linked))
                self.assertFalse(self.finalise.source_scope_declared(linked))
        lexical_owner, _ = self.alias_root()
        lexical_linked = lexical_owner / "linked-through-ambient-prefix"
        subprocess.run(["git", "worktree", "add", "--detach", str(lexical_linked), "HEAD"],
                       cwd=lexical_owner, check=True, capture_output=True)
        self.assertFalse(self.runner.source_scope_declared(lexical_linked))
        self.assertFalse(self.finalise.source_scope_declared(lexical_linked))

    def test_static_hold_reasons_are_useful_without_relaxing_the_hold(self):
        for reason in ("Git metadata aliases require a qualified adapter",
                       "Git object alternates require a qualified metadata adapter",
                       "unqualified linked-worktree control namespace"):
            with self.subTest(reason=reason), patch.object(self.runner, "_source_scope_git_controls", side_effect=ValueError(reason)):
                with self.assertRaises(self.runner.SourceScopeError) as held:
                    self.runner.require_source_scope(self.root)
                self.assertIn(reason, str(held.exception))
                self.assertIn("content reads held", str(held.exception))

    def test_dynamic_and_unicode_errors_never_reveal_paths_or_payloads(self):
        class ExternalValueError(ValueError):
            pass
        private = "SYNTHETIC_PRIVATE_SENTINEL"
        errors = [ValueError(private), OSError(13, private, "/synthetic/private/path"),
                  UnicodeDecodeError("utf-8", private.encode() + b"\xff", len(private), len(private) + 1, "invalid bytes"),
                  ExternalValueError("Git object alternates require a qualified metadata adapter")]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(self.runner, "_source_scope_git_controls", side_effect=error):
                with self.assertRaises(self.runner.SourceScopeError) as held:
                    self.runner.require_source_scope(self.root)
                self.assertEqual(str(held.exception), "source-scope metadata unavailable; content reads held")
                self.assertNotIn(private, str(held.exception))
                self.assertNotIn("/synthetic/private", str(held.exception))

    def test_actual_cli_hold_keeps_only_the_closed_static_diagnostic(self):
        reason = "Git object alternates require a qualified metadata adapter"
        with patch.object(self.runner, "_source_scope_git_controls", side_effect=ValueError(reason)), \
             patch.object(sys, "stderr", io.StringIO()) as output:
            self.assertEqual(self.runner.main(["--repo-root", str(self.root), "--plan"]), 4)
            self.assertIn(reason, output.getvalue())
        with patch.object(self.runner, "_source_scope_git_controls", side_effect=ValueError("SYNTHETIC_PRIVATE_SENTINEL")), \
             patch.object(sys, "stderr", io.StringIO()) as output:
            self.assertEqual(self.runner.main(["--repo-root", str(self.root), "--plan"]), 4)
            self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", output.getvalue())

    def test_external_recipe_owner_holds_before_any_plan_watch_or_input_read(self):
        from argparse import Namespace
        for linked in ("direct", "leaf", "parent"):
            for committed in (False, True):
                for explicit in (False, True):
                    with self.subTest(linked=linked, committed=committed, explicit=explicit):
                        external = self.other_root()
                        self.init_source(external)
                        (external / ".ai").mkdir()
                        policy = external / self.runner.SOURCE_SCOPE_POLICY
                        policy.write_bytes(self.raw)
                        recipe = external / "recipes.json"
                        recipe.write_text(json.dumps(self.config))
                        if committed:
                            subprocess.run(["git", "add", "."], cwd=external, check=True)
                            subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic boundary"],
                                           cwd=external, check=True, capture_output=True)
                            policy.unlink()
                        if linked == "leaf":
                            alias = self.other_root() / "recipe-link.json"
                            alias.symlink_to(recipe)
                            recipe = alias
                        elif linked == "parent":
                            alias = self.other_root() / "recipe-directory"
                            alias.symlink_to(external, target_is_directory=True)
                            recipe = alias / "recipes.json"
                        installer = load("install-agent-validation")
                        original_read = Path.read_bytes
                        with patch.object(installer.OutputPlan, "watch", side_effect=AssertionError("plan snapshot before input scope")), \
                             patch.object(Path, "read_bytes", autospec=True, side_effect=lambda path: (_ for _ in ()).throw(AssertionError("recipe payload read"))
                                          if path == external / "recipes.json" else original_read(path)):
                            with self.assertRaises(ValueError) as held:
                                installer.build_plan(Namespace(repo_root=self.root, layout=Path("layout.json") if explicit else None,
                                                              recipes=recipe))
                            self.assertIn("source-scope", str(held.exception))

    def test_unscoped_legacy_linked_recipe_input_remains_supported(self):
        from argparse import Namespace
        external = self.other_root()
        self.init_source(external)
        recipe = external / "recipes.json"
        recipe.write_text(json.dumps(self.config))
        alias = self.other_root() / "recipe-link.json"
        alias.symlink_to(recipe)
        installer = load("install-agent-validation")
        plan, config, owned = installer.build_plan(Namespace(repo_root=self.root, layout=None, recipes=alias))
        self.assertEqual(config, self.config)
        self.assertEqual(owned, self.root / "config/agent-checks.json")
        self.assertEqual(plan.outputs[owned], recipe.read_bytes())
        plan.revalidate()
        self.assertFalse((self.root / ".ai/agent-validation-source.json").exists())

    def test_gate_and_runner_qualify_the_same_real_unborn_repository(self):
        unborn = self.other_root()
        subprocess.run(["git", "init", "-q", str(unborn)], check=True, capture_output=True)
        self.assertFalse(self.runner.source_scope_declared(unborn))
        self.assertFalse(self.finalise.source_scope_declared(unborn))
        self.finalise.require_source_scope(unborn)
        self.assertEqual(self.finalise.git_toplevel(unborn), unborn)
        with patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(self.finalise.main(["--query", "replace-legacy-pre-push", "--repo-root", str(unborn)]), 1)
            with patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}):
                self.assertEqual(self.finalise.main(["--install-git-hook", "--repo-root", str(unborn)]), 0)
        self.assertTrue((unborn / ".git/hooks/pre-push").is_file())
        # A broken or detached missing HEAD is not an unborn branch grant.
        (unborn / ".git/HEAD").write_text("ref: refs/invalid/synthetic\n")
        with self.assertRaises(self.runner.SourceScopeError):
            self.runner.require_source_scope(unborn)
        self.assertTrue(self.finalise.source_scope_declared(unborn))

    def test_cursor_ordinary_commands_classify_before_scope_or_root_probes(self):
        for command in ("ls", "pytest -q", "git status", "echo 'git push'", "python3 -m pytest", "pwd"):
            with self.subTest(command=command), patch.object(self.finalise, "git_toplevel", side_effect=AssertionError("ordinary command root probe")), \
                 patch.object(self.finalise, "source_scope_declared", side_effect=AssertionError("ordinary command scope probe")):
                trigger, _starts, _bypass, _command = self.finalise.hook_target("cursor", {
                    "cwd": str(self.root), "workspace_roots": [str(self.root)], "command": command})
                self.assertIsNone(trigger)

    def test_cursor_real_handoffs_remain_held_even_with_legacy_off_or_inline_bypass(self):
        self.declare()
        for command in ("git push", "gh pr create", "AGENT_FINALISE_GATE=off git push", "ls; git push"):
            with self.subTest(command=command), patch.dict(os.environ, {"AGENT_FINALISE_GATE": "off"}), \
                 patch.object(sys, "stdin", io.StringIO(json.dumps({"cwd": str(self.root), "workspace_roots": [str(self.root)],
                                                                   "command": command}))), \
                 patch.object(sys, "stdout", io.StringIO()) as output:
                self.assertEqual(self.finalise.main(["--hook", "cursor"]), 0)
                self.assertEqual(json.loads(output.getvalue())["permission"], "deny")


    def test_final_leaf_links_hold_every_payload_reader_before_external_scope_bytes(self):
        external = self.other_root()
        self.init_source(external)
        (external / ".ai").mkdir()
        (external / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        payload = external / "synthetic-private.bin"
        payload.write_bytes(b"synthetic opaque fixture, never opened")
        lexical = self.root / "src/linked-input"
        lexical.symlink_to(payload)
        actual_open = open
        actual_read = Path.read_bytes
        def no_open(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path) in (lexical, payload):
                raise AssertionError("external scoped payload open")
            return actual_open(path, *args, **kwargs)
        def no_read(path):
            if path in (lexical, payload):
                raise AssertionError("external scoped payload read")
            return actual_read(path)
        for operation in (lambda: self.runner.file_digest(lexical), lambda: self.runner.dotenv_bytes(lexical),
                          lambda: self.runner.unreadable_state(lexical),
                          lambda: self.runner.write_json(lexical, {"synthetic": True})):
            with self.subTest(operation=operation), patch("builtins.open", side_effect=no_open), \
                 patch.object(Path, "read_bytes", no_read), \
                 patch.object(Path, "write_bytes", side_effect=AssertionError("scoped write")):
                with self.assertRaises(self.runner.SourceScopeError):
                    operation()

    def test_directory_input_checks_its_own_boundary_before_payload_or_enumeration(self):
        external = self.other_root()
        self.init_source(external)
        (external / ".ai").mkdir()
        (external / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        lexical = self.root / "src/linked-directory"
        lexical.symlink_to(external, target_is_directory=True)
        for argument in (external, lexical):
            with self.subTest(argument=argument.name), \
                 patch.object(Path, "iterdir", side_effect=AssertionError("scoped directory enumerated")), \
                 patch.object(Path, "read_bytes", side_effect=AssertionError("scoped directory payload read")):
                with self.assertRaises(self.runner.SourceScopeError):
                    self.runner.dotenv_bytes(argument)

    def test_dotenv_nested_repository_boundary_holds_before_child_enumeration(self):
        folder = self.root / ".env.synthetic"
        nested = folder / "nested"
        self.init_source(nested)
        (nested / ".ai").mkdir()
        (nested / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        actual_iterdir = Path.iterdir
        def no_nested_entries(path):
            if path == nested:
                raise AssertionError("scoped child directory enumerated")
            return actual_iterdir(path)
        with patch.object(Path, "iterdir", no_nested_entries):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.dotenv_bytes(folder)

    def test_unscoped_linked_and_missing_inputs_keep_legacy_observations(self):
        external = self.other_root()
        self.init_source(external)
        payload = external / "fixture.bin"
        raw = b"synthetic unscoped input\n"
        payload.write_bytes(raw)
        lexical = self.root / "src/linked-input"
        lexical.symlink_to(payload)
        self.assertEqual(self.runner.file_digest(lexical), hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.runner.dotenv_bytes(lexical),
                         b"regular-sha256\0" + hashlib.sha256(raw).hexdigest().encode("ascii"))
        self.assertTrue(self.runner.unreadable_state(lexical).startswith(b"unreadable\0"))
        missing = self.root / "src/missing-link"
        missing.symlink_to(external / "missing.bin")
        self.assertTrue(self.runner.file_digest(missing))
        self.assertEqual(self.runner.dotenv_bytes(missing), b"other\0")

    def test_link_cycles_and_denied_owner_metadata_hold_without_private_diagnostics(self):
        first, second = self.root / "src/cycle-a", self.root / "src/cycle-b"
        first.symlink_to(second)
        second.symlink_to(first)
        for reader in (self.runner.file_digest, self.runner.dotenv_bytes, self.runner.unreadable_state):
            with self.subTest(reader=reader.__name__), patch("builtins.open", side_effect=AssertionError("cycle payload read")):
                with self.assertRaises(self.runner.SourceScopeError) as held:
                    reader(first)
                self.assertEqual(str(held.exception), "source-scope metadata unavailable; content reads held")
        external = self.other_root()
        self.init_source(external)
        payload = external / "fixture.bin"
        payload.write_bytes(b"synthetic private bytes")
        link = self.root / "src/denied-owner"
        link.symlink_to(payload)
        actual_lstat = Path.lstat
        def denied(path, *args, **kwargs):
            if path == external / self.runner.SOURCE_SCOPE_POLICY:
                raise PermissionError(13, "SYNTHETIC_PRIVATE_SENTINEL", str(path))
            return actual_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", denied), patch("builtins.open", side_effect=AssertionError("denied-owner payload read")):
            with self.assertRaises(self.runner.SourceScopeError) as held:
                self.runner.file_digest(link)
            self.assertEqual(str(held.exception), "source-scope metadata unavailable; content reads held")


    def test_readonly_root_discovery_ignores_foreign_git_selectors_and_trace_writes(self):
        foreign = self.other_root()
        self.init_source(foreign)
        (foreign / ".ai").mkdir()
        (foreign / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        # These are synthetic private-control canaries: the requested owner is
        # valid, whereas following inherited foreign Git metadata would fail.
        (foreign / ".git/config").write_text("SYNTHETIC_PRIVATE_INVALID_CONFIG\n")
        trace = foreign / "private-trace.json"
        trace.write_bytes(b"synthetic unchanged trace\n")
        context = {"GIT_DIR": str(foreign / ".git"), "GIT_COMMON_DIR": str(foreign / ".git"),
                   "GIT_WORK_TREE": str(foreign), "GIT_OBJECT_DIRECTORY": str(foreign / ".git/objects"),
                   "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign / ".git/objects"),
                   "GIT_TRACE2_EVENT": str(trace), "GIT_CONFIG_COUNT": "1",
                   "GIT_CONFIG_KEY_0": "include.path", "GIT_CONFIG_VALUE_0": str(foreign / "private.cfg"),
                   "GIT_CONFIG_GLOBAL": str(foreign / "private.cfg"),
                   "GIT_ASKPASS": "/synthetic/no-askpass", "SSH_ASKPASS": "/synthetic/no-askpass",
                   "SSH_AUTH_SOCK": "/synthetic/no-agent", "SOURCE_SCOPE_PUBLIC_MARKER": "not-forwarded",
                   "GH_TOKEN": "synthetic-credential-canary", "DOPPLER_TOKEN": "synthetic-credential-canary"}
        actual_run = subprocess.run
        calls = []
        def observed(command, **kwargs):
            calls.append(command)
            selected = kwargs.get("env", {})
            self.assertEqual(kwargs.get("cwd"), self.root)
            self.assertEqual(command[:4], ["git", "--no-replace-objects", "-c", "protocol.allow=never"])
            for name in context:
                if name not in ("GIT_CONFIG_GLOBAL",):
                    self.assertNotIn(name, selected)
            self.assertEqual(selected["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(selected["GIT_TERMINAL_PROMPT"], "0")
            return actual_run(command, **kwargs)
        with patch.dict(os.environ, context), patch.object(subprocess, "run", side_effect=observed):
            self.assertEqual(self.finalise.git_toplevel(self.root), self.root)
        self.assertEqual(len(calls), 3)  # root listing, nested names listing, toplevel
        self.assertEqual(trace.read_bytes(), b"synthetic unchanged trace\n")
        self.assertEqual((foreign / ".git/config").read_text(), "SYNTHETIC_PRIVATE_INVALID_CONFIG\n")

    def test_hook_install_queries_cannot_route_a_write_to_an_inherited_foreign_owner(self):
        foreign = self.other_root()
        self.init_source(foreign)
        (foreign / ".ai").mkdir()
        (foreign / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        before = (foreign / ".git/hooks/pre-push").exists()
        trace = foreign / "private-trace.json"
        trace.write_bytes(b"synthetic unchanged trace\n")
        with patch.dict(os.environ, {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                                     "GIT_TRACE2_EVENT": str(trace), "GH_TOKEN": "synthetic-credential-canary",
                                     "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}), \
             patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(self.finalise.install_git_hook(self.root), 0)
        self.assertTrue((self.root / ".git/hooks/pre-push").is_file())
        self.assertEqual((foreign / ".git/hooks/pre-push").exists(), before)
        self.assertEqual(trace.read_bytes(), b"synthetic unchanged trace\n")



    def test_global_manager_route_holds_without_false_installed_or_payload_read(self):
        home = self.other_root()
        manager = home / "native-manager"
        manager.mkdir()
        marker = manager / "pre-push"
        marker.write_text("# synthetic existing native manager\n")
        config = home / "global.gitconfig"
        config.write_text("[core]\n\thooksPath = " + str(manager) + "\n")
        environment = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config)}
        native = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=self.root,
                                env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(native.stdout.strip(), str(manager))
        expected = marker.read_bytes()
        output, error = io.StringIO(), io.StringIO()
        original_run = subprocess.run
        def sanitized(command, **kwargs):
            self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")
            return original_run(command, **kwargs)
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(subprocess, "run", side_effect=sanitized), \
             patch.object(Path, "write_text", side_effect=AssertionError("hook write before qualified native routing")), \
             patch.object(sys, "stdout", output), patch.object(sys, "stderr", error):
            self.assertEqual(self.finalise.install_git_hook(self.root), 3)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), "finalise-gate: native hook routing is unqualified; no hook installed or changed\n")
        self.assertNotIn(str(config), error.getvalue())
        self.assertNotIn("managed elsewhere", error.getvalue())
        self.assertEqual(marker.read_bytes(), expected)
        self.assertFalse((self.root / ".git/hooks/pre-push").exists())

    def test_hook_install_unknown_routes_hold_with_metadata_only_and_static_diagnostic(self):
        home = self.other_root()
        home.mkdir(exist_ok=True)
        global_config = home / ".gitconfig"
        global_config.write_text("SYNTHETIC_PRIVATE_INVALID_CONFIG\n")
        xdg = home / "xdg"
        (xdg / "git").mkdir(parents=True)
        (xdg / "git/config").write_text("SYNTHETIC_PRIVATE_INVALID_CONFIG\n")
        actual_run = subprocess.run
        cases = [
            {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"},
            {"HOME": str(home / "absent-home"), "XDG_CONFIG_HOME": str(xdg), "GIT_CONFIG_NOSYSTEM": "1"},
            {"GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"},
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": str(global_config)},
            {"GIT_CONFIG_GLOBAL": os.devnull},  # Unknown compiled system path.
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "invalid-private-value"},
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "1"},
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_PARAMETERS": "private-value"},
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG": str(global_config)},
        ]
        def sanitized(command, **kwargs):
            self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")
            return actual_run(command, **kwargs)
        for context in cases:
            error = io.StringIO()
            with self.subTest(context=list(context)), patch.dict(os.environ, {"PATH": os.defpath, **context}, clear=True), \
                 patch.object(subprocess, "run", side_effect=sanitized), \
                 patch.object(Path, "read_text", side_effect=AssertionError("external config payload read")), \
                 patch.object(Path, "write_text", side_effect=AssertionError("hook write on unknown native routing")), \
                 patch.object(sys, "stderr", error):
                self.assertEqual(self.finalise.install_git_hook(self.root), 3)
            self.assertEqual(error.getvalue(), "finalise-gate: native hook routing is unqualified; no hook installed or changed\n")
        self.assertFalse((self.root / ".git/hooks/pre-push").exists())

    def test_hook_install_known_local_manager_wins_over_unknown_lower_precedence(self):
        for configured in (".synthetic-manager", ""):
            subprocess.run(["git", "config", "core.hooksPath", configured], cwd=self.root, check=True)
            output = io.StringIO()
            with self.subTest(configured=configured), patch.dict(os.environ, {"PATH": os.defpath}, clear=True), \
                 patch.object(sys, "stdout", output):
                self.assertEqual(self.finalise.install_git_hook(self.root), 0)
            self.assertIn("local core.hooksPath is managed elsewhere", output.getvalue())
            self.assertFalse((self.root / ".git/hooks/pre-push").exists())
        # Higher-precedence injected config cannot be silently ignored.
        with patch.dict(os.environ, {"PATH": os.defpath, "GIT_CONFIG_COUNT": "1"}, clear=True), \
             patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.finalise.install_git_hook(self.root), 3)

    def test_hook_install_allows_only_proven_absent_or_disabled_nonlocal_sources(self):
        home = self.other_root()
        contexts = [
            {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"},
            {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"), "GIT_CONFIG_NOSYSTEM": "true"},
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
            {"GIT_CONFIG_GLOBAL": str(home / "absent-global"), "GIT_CONFIG_SYSTEM": str(home / "absent-system")},
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "yes", "GIT_CONFIG_COUNT": "0", "GIT_CONFIG_PARAMETERS": ""},
        ]
        for context in contexts:
            with self.subTest(context=list(context)), patch.dict(os.environ, {"PATH": os.defpath, **context}, clear=True), \
                 patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(self.finalise.install_git_hook(self.root), 0)
            shim = self.root / ".git/hooks/pre-push"
            self.assertIn(self.finalise.SHIM_MARKER, shim.read_text())
            shim.unlink()

    def test_hook_install_aliased_or_inaccessible_nonlocal_config_is_unknown(self):
        config = self.root / "synthetic-dangling-config"
        config.symlink_to(self.root / "missing-config")
        with patch.dict(os.environ, {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config)}, clear=True), \
             patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.finalise.install_git_hook(self.root), 3)
        actual_lstat = Path.lstat
        def denied(path, *arguments, **kwargs):
            if path == config:
                raise PermissionError("SYNTHETIC_PRIVATE_DENIED")
            return actual_lstat(path, *arguments, **kwargs)
        with patch.dict(os.environ, {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config)}, clear=True), \
             patch.object(Path, "lstat", denied), patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.finalise.install_git_hook(self.root), 3)
        self.assertFalse((self.root / ".git/hooks/pre-push").exists())

    def test_actual_hook_install_cli_unknown_route_is_incomplete_not_success(self):
        config = self.root / "synthetic-global-config"
        config.write_text("SYNTHETIC_PRIVATE_INVALID_CONFIG\n")
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "qa/finalise-gate.py"),
                                 "--install-git-hook", "--repo-root", str(self.root)], cwd=self.root,
                                env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config)},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "finalise-gate: native hook routing is unqualified; no hook installed or changed\n")
        self.assertFalse((self.root / ".git/hooks/pre-push").exists())



    def dotdot_source_fixture(self, *, committed=False, scoped=True):
        container = self.other_root()
        safe, owner = container / "safe", container / "physical-owner"
        safe.mkdir()
        (owner / "inner").mkdir(parents=True)
        payload = owner / "payload.bin"
        payload.write_bytes(b"synthetic physical owner input\n")
        if scoped:
            (owner / ".ai").mkdir()
            (owner / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        self.init_source(owner)
        if scoped and committed:
            (owner / self.runner.SOURCE_SCOPE_POLICY).unlink()
        alias = safe / "alias"
        alias.symlink_to(owner / "inner", target_is_directory=True)
        raw_root = alias / ".."
        raw_payload = raw_root / "payload.bin"
        self.assertEqual(raw_payload.resolve(), payload)
        self.assertNotEqual(Path(os.path.abspath(raw_payload)), payload)
        return safe, owner, raw_root, raw_payload

    def test_symlink_parent_components_preserve_raw_physical_root_scope(self):
        for committed in (False, True):
            safe, owner, absolute, _ = self.dotdot_source_fixture(committed=committed)
            previous = Path.cwd()
            try:
                os.chdir(safe)
                for root in (absolute, absolute.relative_to(safe)):
                    with self.subTest(committed=committed, absolute=root.is_absolute()):
                        with self.assertRaises(self.runner.SourceScopeError):
                            self.runner.require_source_scope(root)
                        with self.assertRaises(self.finalise.SourceScopeHeld):
                            self.finalise.require_source_scope(root)
                        with self.assertRaises(self.finalise.SourceScopeHeld):
                            self.finalise.git_toplevel(root)
            finally:
                os.chdir(previous)

    def test_symlink_parent_components_hold_payload_readers_before_any_payload_open(self):
        for committed in (False, True):
            safe, owner, _, absolute = self.dotdot_source_fixture(committed=committed)
            previous = Path.cwd()
            try:
                os.chdir(safe)
                for path in (absolute, absolute.relative_to(safe)):
                    for reader in (self.runner.file_digest, self.runner.dotenv_bytes, self.runner.unreadable_state):
                        with self.subTest(committed=committed, absolute=path.is_absolute(), reader=reader.__name__), \
                             patch.object(Path, "read_bytes", side_effect=AssertionError("protected payload read")), \
                             patch.object(Path, "read_text", side_effect=AssertionError("protected text read")), \
                             patch("builtins.open", side_effect=AssertionError("protected input opened")):
                            with self.assertRaises(self.runner.SourceScopeError):
                                reader(path)
            finally:
                os.chdir(previous)

    def test_symlink_parent_components_hold_actual_cli_and_installer_before_plan_or_recipe_reads(self):
        from argparse import Namespace
        installer = load("install-agent-validation")
        for committed in (False, True):
            safe, owner, absolute_root, absolute_payload = self.dotdot_source_fixture(committed=committed)
            previous = Path.cwd()
            try:
                os.chdir(safe)
                for root, recipe in ((absolute_root, absolute_payload),
                                     (absolute_root.relative_to(safe), absolute_payload.relative_to(safe))):
                    with self.subTest(committed=committed, absolute=root.is_absolute()), \
                         patch.object(self.runner, "config_for", side_effect=AssertionError("owner config read")), \
                         patch.object(self.finalise, "load_qa_config", side_effect=AssertionError("QA config read")), \
                         patch.object(installer, "OutputPlan", side_effect=AssertionError("installer plan before scope")), \
                         patch.object(sys, "stderr", io.StringIO()):
                        self.assertEqual(self.runner.main(["--repo-root", str(root), "--plan", "--select-only"]), 4)
                        for mode in ("--query", "--install-git-hook"):
                            args = [mode, "replace-legacy-pre-push"] if mode == "--query" else [mode]
                            self.assertEqual(self.finalise.main([*args, "--repo-root", str(root)]), 4)
                        with self.assertRaisesRegex(ValueError, "source-scope"):
                            installer.build_plan(Namespace(repo_root=root, layout=None, recipes=None))
                        with self.assertRaisesRegex(ValueError, "source-scope"):
                            installer.build_plan(Namespace(repo_root=self.root, layout=None, recipes=recipe))
            finally:
                os.chdir(previous)

    def test_unscoped_symlink_parent_and_dot_paths_keep_real_owner_observations(self):
        safe, owner, absolute_root, absolute_payload = self.dotdot_source_fixture(scoped=False)
        previous = Path.cwd()
        try:
            os.chdir(safe)
            for root, payload in ((absolute_root, absolute_payload),
                                  (absolute_root.relative_to(safe), absolute_payload.relative_to(safe)),
                                  (owner / "inner/..", owner / "inner/../payload.bin")):
                with self.subTest(absolute=root.is_absolute()):
                    self.assertFalse(self.runner.source_scope_declared(root))
                    self.assertFalse(self.finalise.source_scope_declared(root))
                    self.assertEqual(self.finalise.git_toplevel(root), owner)
                    self.assertEqual(self.runner.file_digest(payload), hashlib.sha256(b"synthetic physical owner input\n").hexdigest())
                    self.assertEqual(self.runner.dotenv_bytes(payload), b"regular-sha256\0"
                                     + hashlib.sha256(b"synthetic physical owner input\n").hexdigest().encode("ascii"))
        finally:
            os.chdir(previous)

    def test_symlink_parent_cycles_are_held_and_dangling_routes_keep_missing_observations(self):
        first, second = self.root / "first-cycle", self.root / "second-cycle"
        first.symlink_to(second)
        second.symlink_to(first)
        cycle = first / "../input.bin"
        with self.assertRaises(self.runner.SourceScopeError):
            self.runner.source_scope_declared(first / "..")
        self.assertTrue(self.finalise.source_scope_declared(first / ".."))
        for reader in (self.runner.file_digest, self.runner.dotenv_bytes, self.runner.unreadable_state):
            with self.subTest(reader=reader.__name__), patch("builtins.open", side_effect=AssertionError("cyclic payload open")):
                with self.assertRaises(self.runner.SourceScopeError):
                    reader(cycle)
        dangling = self.root / "dangling-parent"
        dangling.symlink_to(self.root / "missing-directory", target_is_directory=True)
        missing = dangling / "../missing-input.bin"
        self.assertEqual(self.runner.dotenv_bytes(missing), b"other\0")
        self.assertTrue(self.runner.file_digest(missing))



    def test_control_pointer_parent_aliases_hold_before_target_bytes_or_native_git(self):
        linked = self.other_root() / "linked"
        subprocess.run(["git", "worktree", "add", "--detach", str(linked), "HEAD"],
                       cwd=self.root, check=True, capture_output=True)
        marker = linked / ".git"
        gitdir = Path(marker.read_text().strip()[8:])
        common = self.root / ".git"
        protected = self.other_root()
        (protected / "inner").mkdir()
        (protected / ".ai").mkdir()
        (protected / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        for field, target in ((marker, gitdir), (gitdir / "commondir", common), (gitdir / "gitdir", marker)):
            alias = target.parent / ("synthetic-pointer-alias-" + field.name.replace(".", ""))
            alias.symlink_to(protected / "inner", target_is_directory=True)
            raw = alias / ".." / target.name
            original = field.read_bytes()
            field.write_text(("gitdir: " if field == marker else "") + str(raw) + "\n")
            self.assertEqual(Path(os.path.abspath(raw)), target)
            self.assertNotEqual(raw.resolve(), target)
            actual_open = os.open
            def no_protected_open(path, *arguments, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)) and Path(path).resolve().is_relative_to(protected):
                    raise AssertionError("protected control target opened")
                return actual_open(path, *arguments, **kwargs)
            try:
                for module in (self.runner, self.finalise):
                    with self.subTest(field=field.name, module=module.__name__), \
                         patch.object(os, "open", side_effect=no_protected_open), \
                         patch.object(subprocess, "run", side_effect=AssertionError("native Git before pointer admission")):
                        with self.assertRaisesRegex(ValueError, "unqualified Git metadata alias"):
                            module._source_scope_git_controls(linked)
            finally:
                field.write_bytes(original)
                alias.unlink()
        for module in (self.runner, self.finalise):
            self.assertFalse(module.source_scope_declared(linked))



    def install_known_hook(self, context=None):
        environment = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        if context:
            environment.update(context)
        output, error = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch.object(sys, "stdout", output), patch.object(sys, "stderr", error):
            code = self.finalise.install_git_hook(self.root)
        return code, output.getvalue(), error.getvalue()

    def test_relative_native_config_metadata_uses_requested_git_cwd(self):
        manager = self.root / "synthetic-manager"
        manager.mkdir()
        explicit = self.root / "synthetic-relative.cfg"
        explicit.write_text("[core]\n hooksPath = " + str(manager) + "\n")
        home, xdg = self.root / "synthetic-home", self.root / "synthetic-xdg"
        home.mkdir()
        (home / ".gitconfig").write_bytes(explicit.read_bytes())
        (xdg / "git").mkdir(parents=True)
        (xdg / "git/config").write_bytes(explicit.read_bytes())
        contexts = [
            {"GIT_CONFIG_GLOBAL": explicit.name},
            {"GIT_CONFIG_SYSTEM": explicit.name, "GIT_CONFIG_NOSYSTEM": "0"},
            {"HOME": home.name, "GIT_CONFIG_GLOBAL": None},
            {"HOME": "synthetic-missing-home", "XDG_CONFIG_HOME": xdg.name, "GIT_CONFIG_GLOBAL": None},
        ]
        self.assertNotEqual(Path.cwd(), self.root)
        for context in contexts:
            env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, **context}
            env = {k: v for k, v in env.items() if v is not None}
            native = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=self.root,
                                    env=env, capture_output=True, text=True, check=True)
            self.assertEqual(native.stdout.strip(), str(manager))
            with self.subTest(context=list(context)), patch.dict(os.environ, env, clear=True), \
                 patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(self.finalise.install_git_hook(self.root), 3)
            self.assertFalse((self.root / ".git/hooks/pre-push").exists())
        # A file in the Python process cwd cannot make an absent native input
        # appear present. No copy of that unrelated file is read by the probe.
        self.assertTrue((SCRIPTS.parent / "CLAUDE.md").is_file())
        self.assertFalse((self.root / "CLAUDE.md").exists())
        code, _, _ = self.install_known_hook({"GIT_CONFIG_GLOBAL": "CLAUDE.md"})
        self.assertEqual(code, 0)
        self.assertTrue((self.root / ".git/hooks/pre-push").is_file())

    def test_existing_hooks_are_conserved_and_differing_marked_refresh_is_held(self):
        target = self.root / ".git/hooks/pre-push"
        self.assertEqual(self.install_known_hook()[0], 0)
        expected = target.read_bytes()
        with patch.object(os, "write", side_effect=AssertionError("existing hook rewritten")), \
             patch.object(os, "fchmod", side_effect=AssertionError("existing mode changed")):
            self.assertEqual(self.install_known_hook()[0], 0)
            for raw, mode, wanted in ((b"# synthetic unmanaged hook\n", 0o755, 0),
                                      (self.finalise.SHIM_MARKER.encode() + b"\n# synthetic old hook\n", 0o755, 3),
                                      (expected, 0o600, 3), (expected, 0o644, 3), (expected, 0o700, 3)):
                target.write_bytes(raw)
                target.chmod(mode)
                code, output, error = self.install_known_hook()
                self.assertEqual(code, wanted, output + error)
                self.assertEqual(target.read_bytes(), raw)
                self.assertEqual(target.stat().st_mode & 0o777, mode)
                self.assertNotIn("installed", output)
                if wanted:
                    self.assertIn("marked-hook refresh requires a qualified replacement", error)

    def test_hook_target_scope_alias_hardlink_and_special_files_hold_before_payload(self):
        target = self.root / ".git/hooks/pre-push"
        hooks = target.parent
        external = self.other_root()
        outside = external / "pre-push"
        original = self.finalise.SHIM_MARKER.encode() + b"\n# synthetic outside control\n"
        outside.write_bytes(original)
        outside.chmod(0o755)
        cases = ("scoped-parent", "scoped-leaf", "unscoped-parent", "unscoped-leaf", "hardlink", "fifo", "directory", "scoped-directory", "loop")
        native_read = os.read
        outside_identity = (outside.stat().st_dev, outside.stat().st_ino)
        def no_outside_read(descriptor, count):
            details = os.fstat(descriptor)
            if (details.st_dev, details.st_ino) == outside_identity:
                raise AssertionError("outside hook payload read")
            return native_read(descriptor, count)
        for case in cases:
            policy = external / self.runner.SOURCE_SCOPE_POLICY
            policy.parent.mkdir(exist_ok=True)
            if case.startswith("scoped-") and case != "scoped-directory":
                policy.write_bytes(self.raw)
            moved = None
            if case.endswith("parent"):
                moved = hooks.with_name("synthetic-hooks-saved")
                hooks.rename(moved)
                hooks.symlink_to(external, target_is_directory=True)
            elif case.endswith("leaf"):
                target.symlink_to(outside)
            elif case == "hardlink":
                os.link(outside, target)
            elif case == "fifo":
                os.mkfifo(target)
            elif case in ("directory", "scoped-directory"):
                target.mkdir()
                if case == "scoped-directory":
                    (target / ".ai").mkdir()
                    (target / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
            else:
                target.symlink_to(target)
            try:
                with self.subTest(case=case), patch.object(os, "read", side_effect=no_outside_read):
                    if case.startswith("scoped-"):
                        with self.assertRaises(self.finalise.SourceScopeHeld):
                            self.install_known_hook()
                    else:
                        code, output, error = self.install_known_hook()
                        self.assertEqual(code, 3, output + error)
                        self.assertNotIn("installed", output)
                self.assertEqual(outside.read_bytes(), original)
                self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
            finally:
                if moved is not None:
                    hooks.unlink()
                    moved.rename(hooks)
                elif target.is_dir() and not target.is_symlink():
                    import shutil
                    shutil.rmtree(target)
                else:
                    target.unlink()
                policy.unlink(missing_ok=True)

    def test_hook_target_metadata_and_read_denials_are_static_incomplete(self):
        target = self.root / ".git/hooks/pre-push"
        target.write_bytes(self.finalise.SHIM_MARKER.encode() + b"\n# synthetic existing control\n")
        target.chmod(0o755)
        expected = target.read_bytes()
        actual_lstat = Path.lstat
        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError(13, "SYNTHETIC_PRIVATE_DENIAL", str(target))
            return actual_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", denied):
            code, output, error = self.install_known_hook()
        self.assertEqual(code, 3)
        self.assertNotIn("SYNTHETIC_PRIVATE", error)
        self.assertNotIn(str(target), error)
        self.assertEqual(target.read_bytes(), expected)
        actual_read = os.read
        def denied_read(descriptor, count):
            if os.fstat(descriptor).st_ino == target.stat().st_ino:
                raise OSError(5, "SYNTHETIC_PRIVATE_IO")
            return actual_read(descriptor, count)
        with patch.object(os, "read", side_effect=denied_read):
            code, output, error = self.install_known_hook()
        self.assertEqual(code, 3)
        self.assertNotIn("installed", output)
        self.assertNotIn("SYNTHETIC_PRIVATE", error)
        self.assertEqual(target.read_bytes(), expected)

    def test_hook_native_query_failure_and_timeout_never_prove_absence(self):
        actual_run = subprocess.run
        for failure in ("failure", "timeout", "os-error"):
            def unknown(command, **kwargs):
                if command[4:7] == ["config", "--get", "core.hooksPath"]:
                    if failure == "timeout":
                        raise subprocess.TimeoutExpired(command, 10, output="SYNTHETIC_PRIVATE_OUTPUT")
                    if failure == "os-error":
                        raise PermissionError("SYNTHETIC_PRIVATE_DENIAL")
                    return subprocess.CompletedProcess(command, 2, "SYNTHETIC_PRIVATE_OUTPUT", "SYNTHETIC_PRIVATE_ERROR")
                return actual_run(command, **kwargs)
            with self.subTest(failure=failure), patch.object(subprocess, "run", side_effect=unknown):
                code, output, error = self.install_known_hook()
            self.assertEqual(code, 3)
            self.assertNotIn("installed", output)
            self.assertNotIn("SYNTHETIC_PRIVATE", error)
            self.assertFalse((self.root / ".git/hooks/pre-push").exists())

    def test_fresh_hook_io_failures_leave_inactive_partial_state_without_false_success(self):
        target = self.root / ".git/hooks/pre-push"
        for operation in ("write", "zero-write", "fsync", "fchmod"):
            actual_write = os.write
            writes = []
            def failed_write(descriptor, raw):
                if not writes:
                    writes.append(True)
                    return actual_write(descriptor, raw[:max(1, len(raw) // 2)])
                raise OSError(5, "SYNTHETIC_PRIVATE_PARTIAL_IO")
            function = {"write": "write", "zero-write": "write", "fsync": "fsync", "fchmod": "fchmod"}[operation]
            replacement = failed_write if operation == "write" else (lambda *args: 0) if operation == "zero-write" else None
            with self.subTest(operation=operation):
                with patch.object(os, function, side_effect=replacement or OSError(5, "SYNTHETIC_PRIVATE_IO")):
                    code, output, error = self.install_known_hook()
                self.assertEqual(code, 3)
                self.assertNotIn("installed", output)
                self.assertNotIn("SYNTHETIC_PRIVATE", error)
                self.assertTrue(target.is_file())
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertFalse(os.access(target, os.X_OK))
                target.unlink()  # This test owns the observed inactive fixture.

    def test_raced_new_hook_alias_is_never_overwritten_or_truncated(self):
        target = self.root / ".git/hooks/pre-push"
        external = self.other_root()
        payload = external / "pre-push"
        payload.write_bytes(b"synthetic conserved external control\n")
        actual_open = os.open
        def race(path, flags, *args, **kwargs):
            if path == "pre-push" and flags & os.O_CREAT:
                target.symlink_to(payload)
            return actual_open(path, flags, *args, **kwargs)
        with patch.object(os, "open", side_effect=race):
            code, output, error = self.install_known_hook()
        self.assertEqual(code, 3)
        self.assertNotIn("installed", output)
        self.assertTrue(target.is_symlink())
        self.assertEqual(payload.read_bytes(), b"synthetic conserved external control\n")

    def test_raced_existing_hook_alias_is_held_before_payload_read(self):
        target = self.root / ".git/hooks/pre-push"
        original = self.finalise.SHIM_MARKER.encode() + b"\n# synthetic original\n"
        target.write_bytes(original)
        target.chmod(0o755)
        saved = target.with_name("synthetic-original-saved")
        external = self.other_root() / "outside-control"
        external.write_bytes(b"synthetic conserved external control\n")
        actual_open = os.open
        def race(path, flags, *args, **kwargs):
            if path == "pre-push" and not flags & os.O_CREAT:
                target.rename(saved)
                target.symlink_to(external)
            return actual_open(path, flags, *args, **kwargs)
        with patch.object(os, "open", side_effect=race):
            code, output, error = self.install_known_hook()
        self.assertEqual(code, 3)
        self.assertNotIn("installed", output)
        self.assertEqual(saved.read_bytes(), original)
        self.assertEqual(external.read_bytes(), b"synthetic conserved external control\n")
        self.assertTrue(target.is_symlink())

    def test_raced_hook_parent_alias_is_held_before_external_target_creation(self):
        hooks = self.root / ".git/hooks"
        saved = hooks.with_name("synthetic-parent-saved")
        external = self.other_root()
        actual_open = os.open
        def race(path, flags, *args, **kwargs):
            if path == "hooks" and flags & os.O_DIRECTORY:
                hooks.rename(saved)
                hooks.symlink_to(external, target_is_directory=True)
            return actual_open(path, flags, *args, **kwargs)
        with patch.object(os, "open", side_effect=race):
            code, output, error = self.install_known_hook()
        self.assertEqual(code, 3)
        self.assertNotIn("installed", output)
        self.assertFalse((external / "pre-push").exists())
        self.assertTrue(hooks.is_symlink())
        self.assertTrue(saved.is_dir())
        self.assertFalse((saved / "pre-push").exists())

    def test_hook_activation_checks_late_leaf_parent_body_and_hardlink_changes(self):
        target = self.root / ".git/hooks/pre-push"
        external = self.other_root()
        payload = external / "conserved-control"
        payload.write_bytes(b"synthetic conserved outside bytes\n")
        payload.chmod(0o644)
        hooks = target.parent
        actual_fsync = os.fsync
        for change in ("leaf", "parent", "body", "hardlink"):
            held = self.root / ".git" / ("synthetic-held-" + change)
            linked = external / "synthetic-late-link"
            def changed(descriptor):
                actual_fsync(descriptor)
                if change == "leaf":
                    target.rename(held)
                    target.symlink_to(payload)
                elif change == "parent":
                    hooks.rename(held)
                    hooks.symlink_to(external, target_is_directory=True)
                elif change == "body":
                    os.pwrite(descriptor, b"X", 0)
                else:
                    os.link(target, linked)
            try:
                with self.subTest(change=change), patch.object(os, "fsync", side_effect=changed):
                    code, output, error = self.install_known_hook()
                self.assertEqual(code, 3)
                self.assertNotIn("installed", output)
                self.assertEqual(payload.read_bytes(), b"synthetic conserved outside bytes\n")
                self.assertEqual(payload.stat().st_mode & 0o777, 0o644)
                inactive = held / "pre-push" if change == "parent" else held if change == "leaf" else target
                self.assertEqual(inactive.stat().st_mode & 0o777, 0o600)
            finally:
                if change == "parent":
                    hooks.unlink()
                    held.rename(hooks)
                    target.unlink()
                elif change == "leaf":
                    target.unlink()
                    held.unlink()
                else:
                    target.unlink()
                linked.unlink(missing_ok=True)

    def test_hook_late_postactivation_alias_never_claims_installed_or_modifies_external(self):
        target = self.root / ".git/hooks/pre-push"
        held = target.with_name("synthetic-complete-held")
        external = self.other_root() / "outside-control"
        external.write_bytes(b"synthetic conserved outside bytes\n")
        external.chmod(0o644)
        actual_chmod = os.fchmod
        def changed(descriptor, mode):
            actual_chmod(descriptor, mode)
            target.rename(held)
            target.symlink_to(external)
        with patch.object(os, "fchmod", side_effect=changed):
            code, output, error = self.install_known_hook()
        self.assertEqual(code, 3)
        self.assertNotIn("installed", output)
        self.assertTrue(target.is_symlink())
        self.assertEqual(external.read_bytes(), b"synthetic conserved outside bytes\n")
        self.assertEqual(external.stat().st_mode & 0o777, 0o644)
        self.assertEqual(held.stat().st_mode & 0o777, 0o755)



    def test_hook_close_failure_is_incomplete_and_closes_other_owned_descriptors(self):
        target = self.root / ".git/hooks/pre-push"
        actual_open, actual_close = os.open, os.close
        created, directories, injected = [], [], []
        def observe(path, flags, *args, **kwargs):
            descriptor = actual_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                created.append(descriptor)
            if flags & os.O_DIRECTORY:
                directories.append(descriptor)
            return descriptor
        def failed_close(descriptor):
            actual_close(descriptor)
            if descriptor in created and not injected:
                injected.append(True)
                raise OSError(5, "SYNTHETIC_PRIVATE_CLOSE_IO")
        with patch.object(os, "open", side_effect=observe), patch.object(os, "close", side_effect=failed_close):
            code, output, error = self.install_known_hook()
        self.assertEqual(len(created), 1)
        self.assertEqual(len(injected), 1)
        self.assertEqual(code, 3)
        self.assertNotIn("installed", output)
        self.assertNotIn("SYNTHETIC_PRIVATE", error)
        for descriptor in set(directories):
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertIn(self.finalise.SHIM_MARKER.encode(), target.read_bytes())



    def test_hook_postactivation_body_mutation_cannot_rebind_expected_bytes(self):
        target = self.root / ".git/hooks/pre-push"
        actual_chmod = os.fchmod
        for restore_mtime in (False, True):
            mutations = []
            def mutate(descriptor, mode):
                original = os.fstat(descriptor)
                actual_chmod(descriptor, mode)
                os.pwrite(descriptor, b"X", 0)
                if restore_mtime:
                    os.utime(descriptor, ns=(original.st_atime_ns, original.st_mtime_ns))
                mutations.append(True)
            with self.subTest(restore_mtime=restore_mtime), patch.object(os, "fchmod", side_effect=mutate):
                code, output, error = self.install_known_hook()
            self.assertEqual(len(mutations), 1)
            self.assertEqual(code, 3)
            self.assertNotIn("installed", output)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertTrue(target.read_bytes().startswith(b"X"))
            target.unlink()  # Only this test owns the new mutated fixture.

    def test_hook_postactivation_readback_also_rechecks_held_fd_and_entry(self):
        target = self.root / ".git/hooks/pre-push"
        actual_chmod, actual_read = os.fchmod, os.read
        activated, mutations = [], []
        def activated_hook(descriptor, mode):
            actual_chmod(descriptor, mode)
            activated.append(descriptor)
        def mutate_after_read(descriptor, count):
            raw = actual_read(descriptor, count)
            if descriptor in activated and not mutations:
                os.pwrite(descriptor, b"X", 0)
                mutations.append(True)
            return raw
        with patch.object(os, "fchmod", side_effect=activated_hook), patch.object(os, "read", side_effect=mutate_after_read):
            code, output, error = self.install_known_hook()
        self.assertEqual(len(mutations), 1)
        self.assertEqual(code, 3)
        self.assertNotIn("installed", output)
        self.assertTrue(target.read_bytes().startswith(b"X"))

    def test_memoized_undeclared_tree_never_survives_a_changed_observation(self):
        real = subprocess.run
        probes = []
        def counting(command, **kwargs):
            if command[:4] == ["git", "--no-replace-objects", "-c", "protocol.allow=never"]:
                probes.append(command)
            return real(command, **kwargs)
        def git(*args):
            real(["git", "-c", "commit.gpgsign=false", *args], cwd=self.root, check=True, capture_output=True)
        with patch.object(self.runner.subprocess, "run", side_effect=counting):
            self.assertFalse(self.runner.source_scope_declared(self.root))
            self.assertFalse(self.runner.source_scope_declared(self.root))
            # Identical observation: Git resolved, listed (root and nested names)
            # and re-resolved once only.
            self.assertEqual(sum(map(is_listing, probes)), 1)
            self.assertEqual(len(probes), 4)
            # Every other hold is still observed fresh on a memoized route.
            with patch.dict(os.environ, {self.runner.SOURCE_SCOPE_PIN: ""}):
                self.assertTrue(self.runner.source_scope_declared(self.root))
            self.declare()
            self.assertTrue(self.runner.source_scope_declared(self.root))
            git("add", self.runner.SOURCE_SCOPE_POLICY)
            git("commit", "-qm", "Synthetic mid-run committed scope")
            (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
            # A declaration committed mid-run changes the route and is held.
            self.assertTrue(self.runner.source_scope_declared(self.root))
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(self.root)
            git("update-ref", "refs/heads/declared", "HEAD")
            git("reset", "-q", "--hard", "HEAD~1")
            self.assertFalse(self.runner.source_scope_declared(self.root))
            # Moving back to the declared commit through a rewritten ref holds again.
            git("reset", "-q", "--hard", "declared")
            self.assertTrue(self.runner.source_scope_declared(self.root))
            git("reset", "-q", "--hard", "HEAD~1")
            self.assertFalse(self.runner.source_scope_declared(self.root))
            # Alias/control checks still run on a memoized route.
            outside = self.other_root() / "outside-ref"
            outside.write_bytes(b"0" * 40 + b"\n")
            (self.root / ".git/refs/heads/planted").symlink_to(outside)
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(self.root)
            (self.root / ".git/refs/heads/planted").unlink()
            config = self.root / ".git/config"
            config.write_bytes(config.read_bytes() + b"[include]\n\tpath = elsewhere\n")
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.require_source_scope(self.root)

    def memo_git(self, *args):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def memo_fixture(self):
        """Cache an undeclared answer at U, with a declared commit D on branch 'declared'."""
        undeclared = self.memo_git("rev-parse", "HEAD")
        initial = self.memo_git("symbolic-ref", "--short", "HEAD")
        self.memo_git("checkout", "-q", "-b", "declared")
        self.declare()
        self.memo_git("add", self.runner.SOURCE_SCOPE_POLICY)
        self.memo_git("commit", "-qm", "Synthetic declared")
        declared = self.memo_git("rev-parse", "HEAD")
        self.memo_git("checkout", "-q", initial)
        self.assertFalse((self.root / self.runner.SOURCE_SCOPE_POLICY).exists())
        self.assertFalse(self.runner.source_scope_declared(self.root))
        self.assertTrue(self.runner._SOURCE_SCOPE_UNDECLARED_TREES)
        return undeclared, declared, self.memo_git("symbolic-ref", "HEAD")

    def test_cached_answer_yields_to_a_new_commit_adding_the_declaration(self):
        self.memo_fixture()
        self.memo_git("merge", "-q", "--ff-only", "declared")
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        self.assertTrue(self.runner.source_scope_declared(self.root))
        self.assertTrue(self.runner.source_scope_declared(self.root))

    def test_cached_answer_yields_to_a_packed_refs_only_switch(self):
        _, declared, branch = self.memo_fixture()
        self.memo_git("update-ref", branch, declared)
        self.memo_git("pack-refs", "--all", "--prune")
        self.assertFalse((self.root / ".git" / branch).exists())
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink(missing_ok=True)
        self.assertTrue(self.runner.source_scope_declared(self.root))

    def test_cached_answer_yields_to_a_symbolic_head_moved_to_another_branch(self):
        self.memo_fixture()
        self.memo_git("symbolic-ref", "HEAD", "refs/heads/declared")
        self.assertTrue(self.runner.source_scope_declared(self.root))

    def test_cached_answer_yields_to_a_same_size_ref_rewrite_with_restored_mtime(self):
        _, declared, branch = self.memo_fixture()
        ref = self.root / ".git" / branch
        before = ref.stat()
        with open(ref, "r+b") as stream:  # in place: same inode and size
            stream.write(declared.encode() + b"\n")
        os.utime(ref, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = ref.stat()
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                         (before.st_ino, before.st_size, before.st_mtime_ns))
        self.assertTrue(self.runner.source_scope_declared(self.root))

    def test_refs_directory_swapped_during_the_probe_never_caches_undeclared(self):
        undeclared, declared, branch = self.memo_fixture()
        self.memo_git("merge", "-q", "--ff-only", "declared")
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        heads, parked = self.root / ".git/refs/heads", self.root / ".git/refs/heads.parked"
        real = subprocess.run
        def racer(command, **kwargs):
            if "ls-tree" not in command:
                return real(command, **kwargs)
            # Reroute the whole ref directory to an undeclared commit only while
            # Git runs, then restore every original file identity.
            heads.rename(parked)
            heads.mkdir()
            (self.root / ".git" / branch).write_text(undeclared + "\n")
            try:
                return real(command, **kwargs)
            finally:
                for child in heads.iterdir():
                    child.unlink()
                heads.rmdir()
                parked.rename(heads)
        with patch.object(self.runner.subprocess, "run", side_effect=racer):
            self.assertTrue(self.runner.source_scope_declared(self.root))
        for _ in range(2):
            self.assertTrue(self.runner.source_scope_declared(self.root))
        self.assertEqual(self.memo_git("ls-tree", "HEAD", "--", self.runner.SOURCE_SCOPE_POLICY) != "", True)

    def test_commondir_in_a_plain_git_directory_holds_instead_of_local_resolution(self):
        # Repository A's branch is declared; B's own branch names an undeclared
        # commit but B/.git/commondir routes Git to A's refs, objects and config.
        undeclared, declared, branch = self.memo_fixture()
        self.memo_git("update-ref", branch, declared)
        other = self.other_root()
        (other / ".git/refs/heads").mkdir(parents=True)
        (other / ".git/objects").mkdir()
        (other / ".git/HEAD").write_text("ref: " + branch + "\n")
        (other / ".git" / branch).write_text(undeclared + "\n")
        (other / ".git/commondir").write_text(str(self.root / ".git") + "\n")
        (other / ".git/config").write_bytes((self.root / ".git/config").read_bytes())
        truth = subprocess.run(["git", "ls-tree", "HEAD", "--", self.runner.SOURCE_SCOPE_POLICY], cwd=other,
                               capture_output=True, text=True)
        self.assertTrue(truth.stdout)  # native Git follows commondir to the declaration
        for _ in range(2):
            with self.assertRaises(self.runner.SourceScopeError) as held:
                self.runner.require_source_scope(other)
            self.assertIn("Git common-directory routing requires a qualified adapter", str(held.exception))
        self.assertEqual(len(self.runner._SOURCE_SCOPE_UNDECLARED_TREES), 1)  # only the fixture's own entry

    def test_inexact_loose_ref_forms_are_never_an_observation_or_undeclared(self):
        undeclared, _, branch = self.memo_fixture()
        self.runner._SOURCE_SCOPE_UNDECLARED_TREES.clear()
        ref = self.root / ".git" / branch
        # Git rejects leading whitespace: hold, as the uncached guard did.
        for form in (b"  " + undeclared.encode() + b"\n", b"\t" + undeclared.encode()):
            with self.subTest(form=form):
                ref.write_bytes(form)
                with self.assertRaises(self.runner.SourceScopeError):
                    self.runner.require_source_scope(self.root)
                self.assertEqual(self.runner._SOURCE_SCOPE_UNDECLARED_TREES, set())
        # Git accepts extra trailing whitespace: answer per Git, but never cache it.
        for form in (undeclared.encode() + b"\n\n", undeclared.encode() + b" \n"):
            with self.subTest(form=form):
                ref.write_bytes(form)
                self.assertFalse(self.runner.source_scope_declared(self.root))
                self.assertEqual(self.runner._SOURCE_SCOPE_UNDECLARED_TREES, set())
        head = self.root / ".git/HEAD"
        ref.write_bytes(undeclared.encode() + b"\n")
        head.write_bytes(b"ref: " + branch.encode() + b"\n")
        self.assertFalse(self.runner.source_scope_declared(self.root))
        self.assertEqual(len(self.runner._SOURCE_SCOPE_UNDECLARED_TREES), 1)  # exact forms still cache

    def test_git_and_observation_disagreement_is_never_cached(self):
        undeclared, declared, branch = self.memo_fixture()
        self.runner._SOURCE_SCOPE_UNDECLARED_TREES.clear()
        # A real disagreement: the loose ref names an annotated tag, Git peels it.
        self.memo_git("tag", "-a", "-m", "Synthetic tag", "synthetic-tag", undeclared)
        tag = self.memo_git("rev-parse", "synthetic-tag")
        (self.root / ".git" / branch).write_text(tag + "\n")
        for _ in range(2):
            self.assertFalse(self.runner.source_scope_declared(self.root))
        self.assertEqual(self.runner._SOURCE_SCOPE_UNDECLARED_TREES, set())
        # A simulated disagreement: Git resolves a different commit than observed.
        (self.root / ".git" / branch).write_text(undeclared + "\n")
        real = subprocess.run
        tree = self.memo_git("rev-parse", undeclared + "^{tree}")
        distinct = self.memo_git("commit-tree", tree, "-m", "Synthetic distinct undeclared")
        for when in ("before", "after"):
            calls = []
            def disagree(command, **kwargs):
                if command[-1:] == ["HEAD^{commit}"]:
                    calls.append(command)
                    if (when == "before") == (len(calls) == 1):
                        return subprocess.CompletedProcess(command, 0, distinct.encode() + b"\n", b"")
                return real(command, **kwargs)
            with self.subTest(when=when), patch.object(self.runner.subprocess, "run", side_effect=disagree):
                self.assertFalse(self.runner.source_scope_declared(self.root))
                self.assertEqual(len(calls), 1 if when == "before" else 2)  # early mismatch stops
                self.assertEqual(self.runner._SOURCE_SCOPE_UNDECLARED_TREES, set())
        self.assertFalse(self.runner.source_scope_declared(self.root))
        self.assertEqual(len(self.runner._SOURCE_SCOPE_UNDECLARED_TREES), 1)

    def test_head_moved_during_probe_is_not_memoized(self):
        real = subprocess.run
        subprocess.run(["git", "branch", "other"], cwd=self.root, check=True)
        self.declare()
        subprocess.run(["git", "add", self.runner.SOURCE_SCOPE_POLICY], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic declared"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / self.runner.SOURCE_SCOPE_POLICY).unlink()
        head = self.root / ".git/HEAD"
        original = head.read_bytes()
        def racing(command, **kwargs):
            # HEAD flips to an undeclared branch around every Git process and
            # back afterwards (an ABA route change). Git answers for what it
            # actually saw, and that mismatch with the observation is never kept.
            head.write_bytes(b"ref: refs/heads/other\n")
            try:
                return real(command, **kwargs)
            finally:
                head.unlink()
                head.write_bytes(original)
        with patch.object(self.runner.subprocess, "run", side_effect=racing):
            self.assertFalse(self.runner.source_scope_declared(self.root))
        self.assertEqual(self.runner._SOURCE_SCOPE_UNDECLARED_TREES, set())
        self.assertTrue(self.runner.source_scope_declared(self.root))

    def test_config_changed_during_probe_is_not_memoized(self):
        real = subprocess.run
        config = self.root / ".git/config"
        def racing(command, **kwargs):
            try:
                return real(command, **kwargs)
            finally:
                config.write_bytes(config.read_bytes() + b"[core]\n\tsynthetic = true\n")
        with patch.object(self.runner.subprocess, "run", side_effect=racing):
            self.assertFalse(self.runner.source_scope_declared(self.root))
        self.assertEqual(self.runner._SOURCE_SCOPE_UNDECLARED_TREES, set())

class NestedScopeAndCommonDirectory(unittest.TestCase):
    """Review regressions: nested owners in ready trees and finalise commondir."""

    setUp = SourceScopeBoundary.setUp
    other_root = ScopeControlCompatibility.other_root
    init_source = ScopeControlCompatibility.init_source

    def scoped_owner(self, owner):
        self.init_source(owner)
        (owner / ".ai").mkdir()
        (owner / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        (owner / "opaque.bin").write_bytes(b"synthetic nested opaque payload\n")

    def guarded_open(self, forbidden):
        # Patching os.open would remove dir_fd support and short-circuit the
        # walk; guard the payload read itself through the descriptor's name.
        original = os.read
        def no_payload(descriptor, *args, **kwargs):
            if os.readlink(f"/proc/self/fd/{descriptor}").endswith("/" + forbidden):
                raise AssertionError("nested scoped payload read")
            return original(descriptor, *args, **kwargs)
        return patch.object(self.runner.os, "read", side_effect=no_payload)

    def test_nested_scoped_checkout_in_ready_tree_holds_before_payload_read(self):
        self.scoped_owner(self.root / "deps/owned")
        with self.assertRaises(self.runner.SourceScopeError):
            self.runner.require_content_path_scope(self.root / "deps/owned/opaque.bin")
        with self.guarded_open("opaque.bin"):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.ready_identity(self.root, {"ready_paths": ["deps"]})

    def test_ready_root_inside_a_nested_scoped_owner_holds(self):
        self.scoped_owner(self.root / "vendor")
        (self.root / "vendor/lib").mkdir()
        (self.root / "vendor/lib/opaque.bin").write_bytes(b"synthetic payload\n")
        with self.guarded_open("opaque.bin"):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.ready_identity(self.root, {"ready_paths": ["vendor/lib"]})

    def test_link_into_a_scoped_owner_outside_the_walked_tree_holds(self):
        self.scoped_owner(self.root / "scoped")
        (self.root / "scoped/inner").mkdir()
        (self.root / "scoped/inner/opaque.bin").write_bytes(b"synthetic payload\n")
        (self.root / "deps").mkdir()
        (self.root / "deps/link").symlink_to(Path("../scoped/inner"), target_is_directory=True)
        with self.guarded_open("opaque.bin"):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.ready_identity(self.root, {"ready_paths": ["deps"]})

    def test_unscoped_nested_trees_and_plain_nested_git_keep_a_usable_identity(self):
        (self.root / "deps/plain/sub").mkdir(parents=True)
        (self.root / "deps/plain/sub/file.txt").write_text("synthetic\n")
        self.init_source(self.root / "deps/repo")  # nested Git without a declaration
        first = self.runner.ready_identity(self.root, {"ready_paths": ["deps"]})
        self.assertIsNotNone(first)
        self.assertEqual(first, self.runner.ready_identity(self.root, {"ready_paths": ["deps"]}))
        (self.root / "deps/plain/sub/file.txt").write_text("changed\n")
        self.assertNotEqual(first, self.runner.ready_identity(self.root, {"ready_paths": ["deps"]}))

    def test_finalise_commondir_in_a_plain_git_directory_holds_before_native_git(self):
        other = self.other_root()
        self.init_source(other)
        include = self.other_root() / "external.config"
        include.write_text("[core]\n\tsynthetic = true\n")
        common = self.other_root() / "common.git"
        common.mkdir()
        (common / "config").write_text("[include]\n\tpath = " + str(include) + "\n")
        (other / ".git/commondir").write_text(str(common) + "\n")
        original = os.open
        def no_external(path, *args, **kwargs):
            if Path(os.fspath(path)).is_absolute() and Path(os.fspath(path)).is_relative_to(include.parent):
                raise AssertionError("external common config read")
            return original(path, *args, **kwargs)
        with patch.object(self.finalise.subprocess, "run", side_effect=AssertionError("native Git before hold")), \
             patch.object(self.finalise.os, "open", side_effect=no_external):
            self.assertTrue(self.finalise.source_scope_declared(other))
            with self.assertRaises(self.runner.SourceScopeError) as held:
                self.runner.require_source_scope(other)
        self.assertIn("Git common-directory routing requires a qualified adapter", str(held.exception))

    def nested_payload(self, owner):
        (owner / "opaque.bin").write_bytes(b"synthetic nested payload\n")

    def test_committed_only_nested_declaration_holds_before_payload_read(self):
        owner = self.root / "deps/owned"
        self.scoped_owner(owner)
        for args in (("add", "."), ("-c", "commit.gpgsign=false", "commit", "-qm", "Declare")):
            subprocess.run(["git", *args], cwd=owner, check=True, capture_output=True,
                           env=self.runner.git_environment())
        (owner / self.runner.SOURCE_SCOPE_POLICY).unlink()
        with self.guarded_open("opaque.bin"):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.ready_identity(self.root, {"ready_paths": ["deps"]})

    def test_unmodelled_nested_git_metadata_holds_like_the_root_and_is_unread(self):
        # Submodule-style gitfile pointer and a shared clone's alternates.
        modules = self.root / ".git/modules/vendor/dep"
        modules.mkdir(parents=True)
        pointer = self.root / "vendor/dep"
        pointer.mkdir(parents=True)
        (pointer / ".git").write_text("gitdir: ../../.git/modules/vendor/dep\n")
        self.nested_payload(pointer)
        shared = self.root / "deps/pkg"
        self.init_source(self.other_root() / "origin")
        subprocess.run(["git", "clone", "-q", "--shared", str(self.other_root() / "origin"), str(shared)],
                       check=False, capture_output=True, env=self.runner.git_environment())
        if not (shared / ".git").exists():
            self.init_source(shared)
            (shared / ".git/objects/info").mkdir(parents=True, exist_ok=True)
            (shared / ".git/objects/info/alternates").write_text(str(self.other_root()) + "\n")
        self.nested_payload(shared)
        for ready in ("vendor", "deps"):
            with self.subTest(ready=ready), self.guarded_open("opaque.bin"):
                with self.assertRaises(self.runner.SourceScopeError):
                    self.runner.ready_identity(self.root, {"ready_paths": [ready]})

    def test_actual_cli_holds_before_setup_or_checks_for_unqualified_nested_metadata(self):
        pointer = self.root / "vendor/dep"
        pointer.mkdir(parents=True)
        (self.root / ".git/modules/vendor/dep").mkdir(parents=True)
        (pointer / ".git").write_text("gitdir: ../../.git/modules/vendor/dep\n")
        self.nested_payload(pointer)
        marker = self.other_root()
        write = lambda name: [sys.executable, "-c", f"open({str(marker / name)!r}, 'w').close()"]
        config = {"schema_version": 1, "base": "origin/main",
                  "environments": [{"id": "deps", "ready_paths": ["vendor"], "commands": [write("setup")]}],
                  "checks": [{"id": "uses-deps", "paths": ["src/*"], "environment": "deps",
                              "commands": [write("check")]}]}
        (self.root / "config/agent-checks.json").write_text(json.dumps(config))
        (self.root / "src/value.py").write_text("value = 2\n")
        for arguments in (["--provision"], ["--provision", "--no-cache"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "agent-check.py"),
                                         "--repo-root", str(self.root), *arguments],
                                        capture_output=True, text=True, timeout=60,
                                        env=self.runner.git_environment())
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("SOURCE-SCOPE-HELD", result.stderr)
                self.assertEqual(sorted(path.name for path in marker.iterdir()), [])

    def test_declared_file_paths_through_intermediate_links_hold_before_read(self):
        owner = self.root / "vendor"
        self.scoped_owner(owner)
        (owner / "deep").mkdir()
        (owner / "deep/f.bin").write_bytes(b"synthetic deep payload\n")
        (self.root / "l").symlink_to(Path("vendor/deep"), target_is_directory=True)
        for ready, name in (("l/../opaque.bin", "opaque.bin"), ("l/f.bin", "f.bin"),
                            ("vendor/opaque.bin", "opaque.bin")):
            with self.subTest(ready=ready), self.guarded_open(name):
                with self.assertRaises(self.runner.SourceScopeError):
                    self.runner.ready_identity(self.root, {"ready_paths": [ready]})

    def test_tracked_nested_declaration_holds_tracked_and_snapshot_readers(self):
        owner = self.root / "src/vendored"
        owner.mkdir()
        (owner / ".ai").mkdir()
        (owner / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        self.nested_payload(owner)
        for args in (("add", "."), ("-c", "commit.gpgsign=false", "commit", "-qm", "Nested scope")):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True,
                           env=self.runner.git_environment())
        for working_policy in (True, False):
            if not working_policy:
                (owner / self.runner.SOURCE_SCOPE_POLICY).unlink()  # committed only
            self.runner._SOURCE_SCOPE_UNDECLARED_TREES.clear()
            self.runner._SOURCE_SCOPE_UNDECLARED_WORKTREES.clear()
            with self.subTest(working_policy=working_policy), self.guarded_open("opaque.bin"):
                for reader in (self.runner.raw_tracked_inputs, self.runner.snapshot):
                    with self.assertRaises(self.runner.SourceScopeError):
                        reader(self.root)
                self.assertTrue(self.finalise.source_scope_declared(self.root))

    def test_qa_config_linked_into_a_scoped_owner_holds_before_read(self):
        external = self.other_root()
        self.init_source(external)
        (external / ".ai").mkdir()
        (external / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        (external / "qa.json").write_text(json.dumps({"schema_version": 1, "finalise_gate": {"mode": "off"}}))
        (external / "configs").mkdir()
        (external / "configs/qa.json").write_text(json.dumps({"schema_version": 1, "finalise_gate": {"mode": "off"}}))
        target = self.root / "config/qa.json"
        for route in ("leaf", "directory"):
            with self.subTest(route=route):
                if route == "leaf":
                    target.symlink_to(external / "qa.json")
                else:
                    target.unlink()
                    (self.root / "config").rename(self.root / "config-original")
                    (self.root / "config").symlink_to(external / "configs", target_is_directory=True)
                with patch.object(Path, "read_text", side_effect=AssertionError("scoped QA config read")):
                    with self.assertRaises(self.finalise.SourceScopeHeld):
                        self.finalise.load_qa_config(self.root)

    def test_actual_cli_holds_a_repository_with_any_declared_subfolder(self):
        owner = self.root / "src/vendored"
        owner.mkdir()
        (owner / ".ai").mkdir()
        (owner / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        self.nested_payload(owner)
        marker = self.other_root()
        write = lambda name: [sys.executable, "-c", f"open({str(marker / name)!r}, 'w').close()"]
        config = {"schema_version": 1, "base": "origin/main",
                  "environments": [{"id": "deps", "ready_paths": ["src"], "commands": [write("setup")]}],
                  "checks": [{"id": "uses-deps", "paths": ["src/*"], "environment": "deps",
                              "commands": [write("check")]}]}
        (self.root / "config/agent-checks.json").write_text(json.dumps(config))
        for arguments in (["--plan"], ["--provision"], ["--provision", "--no-cache"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "agent-check.py"),
                                         "--repo-root", str(self.root), *arguments],
                                        capture_output=True, text=True, timeout=60,
                                        env=self.runner.git_environment())
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("SOURCE-SCOPE-HELD", result.stderr)
                self.assertEqual(sorted(path.name for path in marker.iterdir()), [])

    def test_nested_ai_link_to_an_external_policy_holds_the_owner(self):
        external = self.other_root() / "ext_ai"
        external.mkdir()
        (external / "source-scope-policy.json").write_bytes(self.raw)
        sub = self.root / "src/sub"
        sub.mkdir()
        (sub / ".ai").symlink_to(external, target_is_directory=True)
        self.nested_payload(sub)
        with self.guarded_open("opaque.bin"):
            self.assertTrue(self.runner.source_scope_declared(self.root))
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.raw_tracked_inputs(self.root)
        self.assertTrue(self.finalise.source_scope_declared(self.root))

    def test_owner_created_by_a_recipe_command_holds_later_reads_in_process(self):
        self.assertFalse(self.runner.source_scope_declared(self.root))  # cached undeclared
        package = self.root / "node_modules/pkg"
        install = ("import pathlib,sys; p=pathlib.Path(sys.argv[1]); (p/'.ai').mkdir(parents=True); "
                   "(p/'.ai/source-scope-policy.json').write_text('{}'); (p/'opaque.bin').write_text('x')")
        result = self.runner.execute(self.root, {"id": "install", "commands": [[sys.executable, "-c", install, str(package)]]})
        self.assertEqual(result["status"], "passed")
        with self.guarded_open("opaque.bin"):
            with self.assertRaises(self.runner.SourceScopeError):
                self.runner.ready_identity(self.root, {"ready_paths": ["node_modules"]})

    def test_recipe_command_after_one_that_creates_an_owner_is_never_launched(self):
        package = self.root / "node_modules/pkg"
        marker = self.other_root() / "second-ran"
        install = ("import pathlib,sys; p=pathlib.Path(sys.argv[1]); (p/'.ai').mkdir(parents=True); "
                   "(p/'.ai/source-scope-policy.json').write_text('{}')")
        second = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
        with self.assertRaises(self.runner.SourceScopeError):
            self.runner.execute(self.root, {"id": "install", "commands": [
                [sys.executable, "-c", install, str(package)], second]})
        self.assertFalse(marker.exists())

    def test_hard_linked_qa_config_holds_before_read(self):
        other = self.other_root() / "qa.json"
        other.write_text(json.dumps({"schema_version": 1, "finalise_gate": {"mode": "off"}}))
        os.link(other, self.root / "config/qa.json")
        with patch.object(Path, "read_text", side_effect=AssertionError("hard-linked QA config read")):
            with self.assertRaises(self.finalise.SourceScopeHeld):
                self.finalise.load_qa_config(self.root)

    def test_walk_refuses_inside_an_over_bound_directory(self):
        (self.root / "wide").mkdir()
        for index in range(50):
            (self.root / f"wide/f{index}").write_text("")
        for module in (self.runner, self.finalise):
            module._SOURCE_SCOPE_UNDECLARED_WORKTREES.clear()
            with self.subTest(module=module.__name__), patch.object(module, "SOURCE_SCOPE_WALK_MAX_ENTRIES", 20):
                with self.assertRaises(ValueError):
                    module._source_scope_nested_working(self.root, {}, 30)

    def test_gate_evidence_routed_outside_the_owner_holds_before_io(self):
        scoped = self.other_root()
        self.init_source(scoped)
        (scoped / ".ai").mkdir()
        (scoped / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        (scoped / "state").mkdir()
        (scoped / "state/receipt.json").write_text("{}")
        plain = self.other_root()
        state = self.root / ".ai/agent-checks"
        state.rename(self.root / ".ai/agent-checks-original")
        for target in (scoped / "state", plain):
            with self.subTest(target=target.name):
                if state.is_symlink():
                    state.unlink()
                state.symlink_to(target, target_is_directory=True)
                before = sorted(path.name for path in target.iterdir())
                with patch.object(self.finalise, "load_qa_config", return_value=({"mode": "block"}, True)), \
                     patch.object(self.finalise, "agent_check_path", side_effect=AssertionError("agent-check located")), \
                     patch.object(Path, "read_text", side_effect=AssertionError("external receipt read")):
                    decision, message = self.finalise.gate_one(self.root, "codex", False, "push")
                    self.assertEqual(decision, "deny")
                    self.assertIn("SOURCE-SCOPE-HELD", message)
                    self.assertEqual(self.finalise.run_checks(self.root, "codex", 10)["outcome"], "source-scope-held")
                    self.finalise.record(self.root, {"probe": True})
                self.assertEqual(sorted(path.name for path in target.iterdir()), before)

    def test_core_worktree_redirection_holds_before_content_git(self):
        external = self.other_root()
        self.init_source(external)
        (external / ".ai").mkdir()
        (external / self.runner.SOURCE_SCOPE_POLICY).write_bytes(self.raw)
        config = self.root / ".git/config"
        original = config.read_bytes()
        for form in (b"\tworktree = " + str(external).encode() + b"\n", b"\tWorkTree=" + str(external).encode() + b"\n"):
            with self.subTest(form=form):
                config.write_bytes(original.replace(b"[core]\n", b"[core]\n" + form, 1))
                self.runner._SOURCE_SCOPE_UNDECLARED_TREES.clear()
                with self.assertRaises(self.runner.SourceScopeError) as held:
                    self.runner.git(self.root, "diff", "--binary", "HEAD")
                self.assertIn("work-tree redirection", str(held.exception))
                self.assertTrue(self.finalise.source_scope_declared(self.root))
        config.write_bytes(original.replace(b"[core]\n", b"[core]\n\tworktreeConfig = false\n", 1))
        self.assertFalse(self.runner.source_scope_declared(self.root))  # other keys stay compatible

    def test_link_heavy_trees_check_each_directory_once_without_per_link_git(self):
        subprocess.run(["git", "pack-refs", "--all"], cwd=self.root, check=True, capture_output=True,
                       env=self.runner.git_environment())
        store = self.root / "deps/.store"
        for index in range(200):
            (store / f"pkg{index}").mkdir(parents=True)
            (store / f"pkg{index}/index.js").write_text("synthetic\n")
            (self.root / f"deps/pkg{index}").symlink_to(Path(f".store/pkg{index}"), target_is_directory=True)
        original, calls = subprocess.run, []
        def counting(command, *args, **kwargs):
            calls.append(command)
            return original(command, *args, **kwargs)
        with patch.object(self.runner.subprocess, "run", side_effect=counting):
            self.assertIsNotNone(self.runner.ready_identity(self.root, {"ready_paths": ["deps"]}))
        self.assertLess(len(calls), 20)


if __name__ == "__main__":
    unittest.main()
