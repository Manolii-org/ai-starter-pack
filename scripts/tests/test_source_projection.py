"""Real Git, synthetic opaque sentinels, no production/provider input."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

MODULE = Path(__file__).resolve().parents[1] / "lib/source_projection.py"
SPEC = importlib.util.spec_from_file_location("source_projection_tested", MODULE)
assert SPEC is not None and SPEC.loader is not None
projection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = projection
SPEC.loader.exec_module(projection)
GIT = "/usr/bin/git"


class SourceProjectionTests(unittest.TestCase):
    OBJECT_FORMAT = "sha1"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo, self.destination = self.root / "repo", self.root / "output"
        self.repo.mkdir(mode=0o700)
        self.destination.mkdir(mode=0o700)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "-q", "-b", "main", "--object-format=" + self.OBJECT_FORMAT)
        self.git("config", "user.name", "Synthetic Source")
        self.git("config", "user.email", "source@example.invalid")
        self.policy = {"schema_version": 1, "repository": "Example-org/service",
                       "scope": "source-only", "allowed_paths": ["src/", "config/", "scripts/", ".gitignore"],
                       "protected_paths": ["private/", "reports/"]}
        self.write(projection.POLICY_PATH, json.dumps(self.policy).encode())
        self.write("src/main.py", b"value = 1\n")
        self.write("src/run.sh", b"#!/bin/sh\nexit 0\n", 0o755)
        self.write("config/agent-checks.json", json.dumps({
            "schema_version": 1, "base": "HEAD", "checks": [{"id": "source", "paths": ["**"],
                "commands": [[sys.executable, "-c", "raise AssertionError('must not execute')"]]}],
        }).encode())
        self.write(".gitignore", b".ai/agent-checks/\n")
        self.write("private/opaque.bin", b"DO_NOT_READ_OPAQUE_SYNTHETIC_SENTINEL\n")
        self.commit()
        self.base, self.base_tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        self.pin = hashlib.sha256((self.repo / projection.POLICY_PATH).read_bytes()).hexdigest()
        self.opaque_oid = self.git("rev-parse", "HEAD:private/opaque.bin")
        self.write("src/main.py", b"value = 2\n")
        self.commit()
        self.selection = projection.Selection(self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}"),
                                               self.base, self.base_tree)

    def git(self, *args, input=None):
        result = subprocess.run([GIT, *args], cwd=self.repo, env=self.env, input=input,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode().strip()

    def write(self, name, value, mode=0o644):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(mode)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic source")

    def selected(self):
        return projection.Selection(self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}"),
                                    self.base, self.base_tree)

    def project(self, **kwargs):
        values = dict(selection=self.selection, repository_name=self.policy["repository"],
                      compiled_policy_sha256=self.pin, destination_parent=self.destination)
        return projection.project_committed_source(self.repo, **{**values, **kwargs})

    def test_real_git_ids_source_modes_and_non_authorizing_typed_record(self):
        result = self.project()
        self.assertEqual((result.source / "src/main.py").read_bytes(), b"value = 2\n")
        self.assertEqual((result.source / "src/run.sh").stat().st_mode & 0o777, 0o700)
        self.assertEqual(result.root.stat().st_mode & 0o777, 0o700)
        self.assertFalse((result.source / "private").exists())
        self.assertFalse((result.metadata / "objects" / self.opaque_oid[:2] / self.opaque_oid[2:]).exists())
        for oid in (self.selection.source, self.selection.base, self.selection.tree, self.selection.base_tree):
            value = subprocess.check_output([GIT, "--git-dir", str(result.metadata), "cat-file", "-t", oid], env=self.env)
            self.assertIn(value.strip(), (b"commit", b"tree"))
        raw = subprocess.check_output([GIT, "--git-dir", str(result.metadata), "cat-file", "commit", self.selection.source], env=self.env)
        original = subprocess.check_output([GIT, "cat-file", "commit", self.selection.source], cwd=self.repo, env=self.env)
        self.assertEqual(raw, original)
        self.assertEqual(result.record["schema"], "source-projection/v1")
        self.assertNotEqual(result.record["schema"], "agent-check/v1")
        for key in ("execution_authorized", "execution_closure_verified", "native_gate_qualified",
                    "full_repository_qualified", "cache_reusable"):
            self.assertIs(result.record[key], False)
        self.assertEqual(result.record["opaque_payloads_materialized"], 0)
        with self.assertRaises(projection.ProjectionError):
            result.require_execution_closure()
        with self.assertRaises(TypeError):
            result.record["execution_authorized"] = True

    def test_never_requests_opaque_objects_or_working_payloads_or_invokes_input_git(self):
        real_read, real_open = projection._LooseObjects.read, os.open
        requests = []
        def read(store, oid, kind, limit=projection.MAX_OBJECT):
            self.assertNotEqual(oid, self.opaque_oid, "opaque object request")
            requests.append((oid, kind))
            return real_read(store, oid, kind, limit)
        def opened(name, *args, **kwargs):
            self.assertNotEqual(os.fspath(name), self.opaque_oid[2:], "opaque object filesystem read")
            return real_open(name, *args, **kwargs)
        # The selected source comes from Git objects, never a mutable working
        # file that may point at a protected host file.
        working = self.repo / "src/main.py"
        working.unlink()
        working.symlink_to(self.repo / "private/opaque.bin")
        with patch.object(projection._LooseObjects, "read", read), patch.object(os, "open", opened), \
                patch.object(subprocess, "run", side_effect=AssertionError("no input Git subprocess")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no working payload scan")):
            result = self.project()
        self.assertEqual((result.source / "src/main.py").read_bytes(), b"value = 2\n")
        self.assertTrue(requests)

    def test_policy_self_grant_wrong_pin_and_unknown_input_hold_before_source_blobs(self):
        value = dict(self.policy, allowed_paths=[*self.policy["allowed_paths"], "private/"])
        self.write(projection.POLICY_PATH, json.dumps(value).encode())
        self.commit()
        with self.assertRaisesRegex(projection.ProjectionError, "candidate_policy_changed"):
            self.project(selection=self.selected())
        with self.assertRaisesRegex(projection.ProjectionError, "compiled_policy_digest_changed"):
            self.project(compiled_policy_sha256="0" * 64)
        with self.assertRaises(projection.ProjectionError):
            self.project(repository_name="Other-org/service")
        self.git("checkout", self.selection.source, "--", projection.POLICY_PATH)
        self.write("unclassified.bin", b"UNKNOWN_SENTINEL")
        self.commit()
        real = projection._LooseObjects.read
        def no_opaque(store, oid, kind, limit=projection.MAX_OBJECT):
            self.assertNotEqual(oid, self.opaque_oid)
            return real(store, oid, kind, limit)
        with patch.object(projection._LooseObjects, "read", no_opaque), \
                self.assertRaisesRegex(projection.ProjectionError, "unclassified_input"):
            self.project(selection=self.selected())

    def test_changed_opaque_input_and_ambiguous_object_scope_hold_without_payload(self):
        self.write("private/opaque.bin", b"CHANGED_PROTECTED_SENTINEL")
        self.commit()
        changed = self.git("rev-parse", "HEAD:private/opaque.bin")
        real = projection._LooseObjects.read
        def no_opaque(store, oid, kind, limit=projection.MAX_OBJECT):
            self.assertNotIn(oid, (self.opaque_oid, changed))
            return real(store, oid, kind, limit)
        with patch.object(projection._LooseObjects, "read", no_opaque), \
                self.assertRaisesRegex(projection.ProjectionError, "opaque_input_changed"):
            self.project(selection=self.selected())
        self.git("checkout", self.selection.source, "--", "private/opaque.bin")
        self.write("src/ambiguous.txt", b"DO_NOT_READ_OPAQUE_SYNTHETIC_SENTINEL\n")
        self.commit()
        with patch.object(projection._LooseObjects, "read", no_opaque), \
                self.assertRaisesRegex(projection.ProjectionError, "ambiguous_object_scope"):
            self.project(selection=self.selected())

    def test_compiled_policy_alias_is_held_before_ambiguous_payload_read(self):
        raw = (self.repo / projection.POLICY_PATH).read_bytes()
        self.write("private/policy-copy.json", raw)
        self.commit()
        policy_oid = self.git("rev-parse", "HEAD:" + projection.POLICY_PATH)
        real = projection._LooseObjects.read
        def no_policy_alias(store, oid, kind, limit=projection.MAX_OBJECT):
            self.assertNotEqual(oid, policy_oid)
            return real(store, oid, kind, limit)
        with patch.object(projection._LooseObjects, "read", no_policy_alias), \
                self.assertRaisesRegex(projection.ProjectionError, "compiled_policy_alias_ambiguous"):
            self.project(selection=self.selected())

    def test_refs_wrong_trees_noncommit_and_unrelated_base_are_refused(self):
        for value in ("HEAD", "main", "refs/heads/main", "A" * 40, self.selection.source + "\n"):
            with self.subTest(ref=value), self.assertRaises(projection.ProjectionError):
                self.project(selection=replace(self.selection, source=value))
        with self.assertRaisesRegex(projection.ProjectionError, "selected_tree_changed"):
            self.project(selection=replace(self.selection, tree=self.base_tree))
        with self.assertRaisesRegex(projection.ProjectionError, "object_type_changed"):
            self.project(selection=replace(self.selection, source=self.selection.tree))
        self.git("checkout", "--orphan", "unrelated")
        self.commit()
        with self.assertRaisesRegex(projection.ProjectionError, "base_not_selected_ancestor"):
            self.project(selection=replace(self.selection, base=self.git("rev-parse", "HEAD"),
                                            base_tree=self.git("rev-parse", "HEAD^{tree}")))

    def test_literal_policy_types_duplicates_unicode_and_traversal_refuse(self):
        variants = [dict(self.policy, schema_version=True), dict(self.policy, schema_version=1.0),
                    dict(self.policy, extra=True), dict(self.policy, scope="full"),
                    dict(self.policy, allowed_paths=[])]
        variants += [dict(self.policy, allowed_paths=[value]) for value in
                     ("../private/", "/private/", "src//", "src/*", "src\\x", "src/\n", "src/\ud800", ".git/")]
        for value in variants:
            raw = json.dumps(value).encode()
            with self.subTest(value=value), self.assertRaises(projection.ProjectionError):
                projection._policy(raw, hashlib.sha256(raw).hexdigest(), self.policy["repository"])
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"value":NaN}', b'\xff'):
            with self.assertRaises((projection.ProjectionError, UnicodeError)):
                projection._policy(raw, hashlib.sha256(raw).hexdigest(), self.policy["repository"])

    def test_policy_lists_match_nonempty_256_entry_guard_schema_boundary(self):
        for field in ("allowed_paths", "protected_paths"):
            boundary = dict(self.policy, **{field: ["scope/" + str(i) for i in range(256)]})
            raw = json.dumps(boundary).encode()
            self.assertEqual(projection._policy(raw, hashlib.sha256(raw).hexdigest(),
                                                self.policy["repository"]), boundary)
            for values in ([], ["scope/" + str(i) for i in range(257)]):
                raw = json.dumps(dict(self.policy, **{field: values})).encode()
                with self.subTest(field=field, count=len(values)), self.assertRaises(projection.ProjectionError):
                    projection._policy(raw, hashlib.sha256(raw).hexdigest(), self.policy["repository"])

    def test_policy_500_character_and_16KiB_boundaries_preserve_valid_utf8(self):
        for field in ("allowed_paths", "protected_paths"):
            for character in ("a", "\U0001f4da"):
                value = dict(self.policy, **{field: [character * 500]})
                raw = json.dumps(value, ensure_ascii=False).encode()
                self.assertEqual(projection._policy(raw, hashlib.sha256(raw).hexdigest(),
                                                    self.policy["repository"]), value)
                raw = json.dumps(dict(self.policy, **{field: [character * 501]}), ensure_ascii=False).encode()
                with self.subTest(field=field, character=character), self.assertRaises(projection.ProjectionError):
                    projection._policy(raw, hashlib.sha256(raw).hexdigest(), self.policy["repository"])
        raw = json.dumps(self.policy).encode()
        boundary = raw + b" " * (16 * 1024 - len(raw))
        self.assertEqual(projection._policy(boundary, hashlib.sha256(boundary).hexdigest(),
                                            self.policy["repository"]), self.policy)
        oversized = boundary + b" "
        with self.assertRaises(projection.ProjectionError):
            projection._policy(oversized, hashlib.sha256(oversized).hexdigest(), self.policy["repository"])

    def test_oversized_compiled_policy_stops_at_object_header_before_source_capture(self):
        raw = json.dumps(self.policy).encode()
        raw += b" " * (16 * 1024 + 1 - len(raw))
        self.write(projection.POLICY_PATH, raw)
        self.commit()
        self.base, self.base_tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        self.selection, self.pin = self.selected(), hashlib.sha256(raw).hexdigest()
        policy_oid = self.git("rev-parse", "HEAD:" + projection.POLICY_PATH)
        calls, original = [], projection._LooseObjects.read

        def observe(store, oid, kind, limit=projection.MAX_OBJECT):
            calls.append((oid, kind, limit))
            return original(store, oid, kind, limit)

        with patch.object(projection._LooseObjects, "read", observe):
            with self.assertRaisesRegex(projection.ProjectionError, "object_budget_exceeded"):
                self.project()
        self.assertIn((policy_oid, "blob", 16 * 1024), calls)
        self.assertEqual([oid for oid, kind, _ in calls if kind == "blob"], [policy_oid])
        self.assertFalse(list(self.destination.iterdir()))

    def test_source_symlink_and_missing_object_hold(self):
        link = self.root / "source-link"
        link.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(projection.ProjectionError):
            projection.project_committed_source(link, selection=self.selection, repository_name=self.policy["repository"],
                                                compiled_policy_sha256=self.pin, destination_parent=self.destination)
        path = self.repo / ".git/objects" / self.selection.source[:2] / self.selection.source[2:]
        path.unlink()
        with self.assertRaises(projection.ProjectionError):
            self.project()

    def test_source_object_symlink_hardlink_and_alternates_are_not_read(self):
        oid = self.git("rev-parse", "HEAD:src/main.py")
        path = self.repo / ".git/objects" / oid[:2] / oid[2:]
        saved = self.root / "saved-object"
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(projection.ProjectionError):
            self.project()
        path.unlink()
        saved.rename(path)
        os.link(path, self.root / "hardlinked-object")
        with self.assertRaisesRegex(projection.ProjectionError, "unsafe_object_file"):
            self.project()
        (self.root / "hardlinked-object").unlink()
        self.write_alternates(self.root / "opaque-external-store")
        with self.assertRaisesRegex(projection.ProjectionError, "packed_or_alternate_store_unqualified"):
            self.project()

    def write_alternates(self, outside):
        (self.repo / ".git/objects/info/alternates").write_text(str(outside) + "\n")

    def test_packs_and_worktree_descriptors_hold_without_opening_targets(self):
        pack = self.repo / ".git/objects/pack/opaque.pack"
        pack.write_bytes(b"PACK_OPAQUE_SENTINEL")
        with self.assertRaisesRegex(projection.ProjectionError, "packed_or_alternate_store_unqualified"):
            self.project()
        pack.unlink()
        original = self.repo / ".git"
        original.rename(self.root / "git-retained")
        original.write_text("gitdir: ../outside-private-directory\n")
        with self.assertRaises(projection.ProjectionError):
            self.project()

    def test_hostile_Git_config_environment_and_PATH_never_run_or_read(self):
        external = self.root / "external-config"
        external.write_text("OPAQUE_CONFIG_SENTINEL")
        with (self.repo / ".git/config").open("a") as stream:
            stream.write("\n[include]\npath = " + str(external) + "\n")
        with patch.dict(os.environ, {"GIT_DIR": "/missing", "GIT_OBJECT_DIRECTORY": "/missing",
                                     "GIT_CONFIG_GLOBAL": str(external), "PATH": "/malicious"}), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("must not execute Git")):
            result = self.project()
        self.assertTrue((result.source / "src/main.py").is_file())
        self.assertNotIn("include", (result.metadata / "config").read_text())
        self.assertFalse((result.metadata / "objects/info/alternates").exists())
        self.assertFalse((result.metadata / "objects/pack").exists())

    def test_allowed_symlinks_and_case_collisions_do_not_materialize(self):
        self.write("src/Case.py", b"a=1\n")
        self.write("src/case.py", b"a=2\n")
        self.commit()
        with self.assertRaisesRegex(projection.ProjectionError, "source_path_collision"):
            self.project(selection=self.selected())
        (self.repo / "src/Case.py").unlink()
        (self.repo / "src/case.py").unlink()
        (self.repo / "src/linked").symlink_to("../private/opaque.bin")
        self.commit()
        with self.assertRaisesRegex(projection.ProjectionError, "unsupported_source_mode"):
            self.project(selection=self.selected())

    def test_directory_case_and_unicode_aliases_refuse_before_materialization(self):
        for first, second in (("src/Case/a.py", "src/case/b.py"),
                              ("src/caf\u00e9/a.py", "src/cafe\u0301/b.py")):
            self.write(first, b"a=1\n")
            self.write(second, b"b=2\n")
            self.commit()
            with self.subTest(first=first), self.assertRaisesRegex(projection.ProjectionError, "source_path_collision"):
                self.project(selection=self.selected())
            (self.repo / first).unlink()
            (self.repo / second).unlink()
            self.commit()

    def test_object_TOCTOU_identical_rewrite_is_held(self):
        oid = self.git("rev-parse", "HEAD:src/main.py")
        path = self.repo / ".git/objects" / oid[:2] / oid[2:]
        identity = path.stat().st_ino
        real, changed = os.read, False
        def read(fd, count):
            nonlocal changed
            value = real(fd, count)
            if os.fstat(fd).st_ino == identity and not changed:
                changed = True
                path.chmod(0o600)
            return value
        with patch.object(os, "read", read), \
                self.assertRaisesRegex(projection.ProjectionError, "object_changed_during_capture"):
            self.project()
        self.assertTrue(changed)

    def test_object_directory_swap_never_follows_external_descriptor(self):
        oid = self.git("rev-parse", "HEAD:src/main.py")
        prefix = self.repo / ".git/objects" / oid[:2]
        outside = self.root / "external-object-directory"
        outside.mkdir(mode=0o700)
        sentinel = outside / oid[2:]
        sentinel.write_bytes(b"OPAQUE_OUTSIDE_SENTINEL")
        inode = sentinel.stat().st_ino
        original_read, original_object, changed = os.read, projection._LooseObjects.read, False
        def read(fd, count):
            self.assertNotEqual(os.fstat(fd).st_ino, inode, "outside payload read")
            return original_read(fd, count)
        def object_read(store, selected, kind, limit=projection.MAX_OBJECT):
            nonlocal changed
            if selected == oid and not changed:
                changed = True
                prefix.rename(self.root / "retained-prefix")
                prefix.symlink_to(outside, target_is_directory=True)
            return original_object(store, selected, kind, limit)
        with patch.object(projection._LooseObjects, "read", object_read), patch.object(os, "read", read), \
                self.assertRaises(projection.ProjectionError):
            self.project()
        self.assertTrue(changed)

    def test_destination_permissions_symlink_and_inside_source_hold(self):
        with self.assertRaisesRegex(projection.ProjectionError, "absolute_descriptor_path_required"):
            self.project(destination_parent=self.destination / ".." / self.destination.name)
        self.destination.chmod(0o755)
        with self.assertRaisesRegex(projection.ProjectionError, "destination_not_private"):
            self.project()
        self.destination.chmod(0o700)
        link = self.root / "destination-link"
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(projection.ProjectionError):
            self.project(destination_parent=link)
        private = self.repo / "output"
        private.mkdir(mode=0o700)
        with self.assertRaisesRegex(projection.ProjectionError, "destination_inside_source"):
            self.project(destination_parent=private)

    def test_destination_ancestor_swap_cannot_write_outside_anchor(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        original_write, changed = projection._write, False
        def write(*args, **kwargs):
            nonlocal changed
            original_write(*args, **kwargs)
            if not changed:
                changed = True
                self.destination.rename(self.root / "retained-output")
                self.destination.symlink_to(outside, target_is_directory=True)
        with patch.object(projection, "_write", write), self.assertRaises(projection.ProjectionError):
            self.project()
        self.assertEqual(list(outside.iterdir()), [])

    def test_destination_symlink_or_hardlink_leaf_never_touches_external_payload(self):
        outside = self.root / "protected-output"
        outside.write_bytes(b"UNCHANGED_OUTSIDE_SENTINEL")
        for link_kind in ("symlink", "hardlink"):
            original_write, planted = projection._write, False
            def write(root_fd, name, payload, mode=0o600):
                nonlocal planted
                if name == "src/main.py" and not planted:
                    planted = True
                    try:
                        os.mkdir("src", 0o700, dir_fd=root_fd)
                    except FileExistsError:
                        pass
                    directory = os.open("src", os.O_RDONLY | os.O_DIRECTORY, dir_fd=root_fd)
                    try:
                        if link_kind == "symlink":
                            os.symlink(str(outside), "main.py", dir_fd=directory)
                        else:
                            os.link(outside, "main.py", dst_dir_fd=directory)
                    finally:
                        os.close(directory)
                return original_write(root_fd, name, payload, mode)
            with self.subTest(kind=link_kind), patch.object(projection, "_write", write), \
                    self.assertRaises(projection.ProjectionError):
                self.project()
            self.assertTrue(planted)
            self.assertEqual(outside.read_bytes(), b"UNCHANGED_OUTSIDE_SENTINEL")

    def test_later_output_mutation_cannot_publish_a_materialization_record(self):
        original, changed = projection._write, False
        source_fd = None
        def write(root_fd, name, payload, mode=0o600):
            nonlocal source_fd, changed
            original(root_fd, name, payload, mode)
            if name == "src/main.py":
                source_fd = root_fd
            elif name == "projection.json" and source_fd is not None:
                directory = os.open("src", os.O_RDONLY | os.O_DIRECTORY, dir_fd=source_fd)
                try:
                    fd = os.open("main.py", os.O_WRONLY | os.O_TRUNC, dir_fd=directory)
                    try:
                        os.write(fd, b"tampered source\n")
                        changed = True
                    finally:
                        os.close(fd)
                finally:
                    os.close(directory)
        with patch.object(projection, "_write", write), \
                self.assertRaisesRegex(projection.ProjectionError, "destination_changed_after_capture"):
            self.project()
        self.assertTrue(changed)

    def test_old_full_reader_and_worktree_commands_refuse_bare_projection(self):
        # Use the actual public runner bytes, without running a planner against
        # this test's host checkout or any protected input repository.
        runner = Path(__file__).resolve().parents[1] / "agent-check.py"
        self.write("scripts/agent-check.py", runner.read_bytes())
        self.commit()
        selected = self.selected()
        result = self.project(selection=selected)
        command = [GIT, "-C", str(result.source), "diff", "--name-only", self.selection.base]
        failed = subprocess.run(command, env=self.env, capture_output=True, timeout=10)
        self.assertNotEqual(failed.returncode, 0)
        command = [sys.executable, "-B", str(result.source / "scripts/agent-check.py"),
                   "--repo-root", str(result.source), "--base", self.selection.base, "--plan"]
        failed = subprocess.run(command, env=self.env, cwd=result.source,
                                capture_output=True, timeout=10)
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn(b'"outcome": "passed"', failed.stdout)
        self.assertFalse((result.source / ".ai/agent-checks/receipt.json").exists())
        self.assertFalse(result.record["full_repository_qualified"])

    def test_malformed_object_header_digest_and_budget_hold(self):
        oid = self.git("rev-parse", "HEAD:src/main.py")
        path = self.repo / ".git/objects" / oid[:2] / oid[2:]
        path.chmod(0o600)
        for raw in (b"blob 100\0short", b"blob 1\0xx", b"blob 1\0x", b"tag 1\0x",
                    b"blob " + str(projection.MAX_OBJECT + 1).encode() + b"\0x"):
            path.write_bytes(zlib.compress(raw))
            with self.subTest(raw=raw), self.assertRaises(projection.ProjectionError):
                self.project()


class SourceProjectionSHA256Tests(SourceProjectionTests):
    OBJECT_FORMAT = "sha256"




class LiteralTreeEntryTests(unittest.TestCase):
    OBJECT_FORMAT = "sha1"
    setUp = SourceProjectionTests.setUp
    git = SourceProjectionTests.git
    write = SourceProjectionTests.write
    commit = SourceProjectionTests.commit
    selected = SourceProjectionTests.selected
    project = SourceProjectionTests.project

    def test_real_nextjs_and_glob_source_names_preserve_bytes_and_modes(self):
        for name in ("src/[id].tsx", "src/a*b?.py", "src/[...slug]/page.tsx"):
            self.write(name, (name + "\n").encode(), 0o755)
        self.commit()
        result = self.project(selection=self.selected())
        for name in ("src/[id].tsx", "src/a*b?.py", "src/[...slug]/page.tsx"):
            self.assertEqual((result.source / name).read_bytes(), (name + "\n").encode())
            self.assertEqual((result.source / name).stat().st_mode & 0o777, 0o700)

    def test_protected_glob_names_are_metadata_only_with_zero_content_reads(self):
        names = ("private/[id].tsx", "reports/a*b?.json", "private/[...slug]/item")
        for name in names:
            self.write(name, ("OPAQUE_GENERIC_" + name).encode())
        self.commit()
        self.base, self.base_tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        self.write("src/main.py", b"value = 3\n")
        self.commit()
        forbidden = {self.git("rev-parse", "HEAD:" + name) for name in names}
        read = projection._LooseObjects.read
        observed = []
        def guard(store, oid, kind, limit=projection.MAX_OBJECT):
            self.assertNotIn(oid, forbidden)
            observed.append(oid)
            return read(store, oid, kind, limit)
        with patch.object(projection._LooseObjects, "read", guard):
            result = self.project(selection=self.selected())
        self.assertTrue(observed)
        self.assertFalse((result.source / "private").exists())
        self.assertFalse((result.source / "reports").exists())
        for oid in forbidden:
            self.assertFalse((result.metadata / "objects" / oid[:2] / oid[2:]).exists())

    def test_literal_policy_stays_strict_and_tree_names_keep_unsafe_path_refusals(self):
        for name in ("src/[id].tsx", "src/a*b", "src/a?b"):
            with self.subTest(policy=name), self.assertRaises(projection.ProjectionError):
                projection._relative(name, directory_rule=True)
            self.assertEqual(projection._relative(name, tree_entry=True), name)
        for name in ("", ".", "..", ".git", "a/.GIT/b", "/absolute", "a//b", "a/../b", "a\x00b",
                     "a\nb", "a\x7fb", "a\\b", "x" * 1025, "\ud800"):
            with self.subTest(tree=name), self.assertRaises(projection.ProjectionError):
                projection._relative(name, tree_entry=True)

    def test_partial_failure_returns_only_its_created_private_cleanup_root(self):
        write = projection._write
        calls = []
        def fail(root, name, payload, mode=0o600):
            calls.append(name)
            if len(calls) == 2:
                raise OSError("synthetic destination failure")
            return write(root, name, payload, mode)
        with patch.object(projection, "_write", fail), self.assertRaises(projection.ProjectionError) as raised:
            self.project()
        root = raised.exception.cleanup_root
        self.assertEqual(root.parent, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [root])
        self.assertNotIn(str(root), str(raised.exception))
        recovered = self.project()
        self.assertNotEqual(recovered.root, root)
        self.assertTrue(root.is_dir())
        self.assertTrue(recovered.root.is_dir())


class LiteralTreeEntrySHA256Tests(LiteralTreeEntryTests):
    OBJECT_FORMAT = "sha256"


if __name__ == "__main__":
    unittest.main()
