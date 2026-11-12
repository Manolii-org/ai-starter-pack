"""Real Linux namespace checks using generic source and synthetic host sentinels."""
from __future__ import annotations

import copy
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / "lib"
PACKAGE = "source_execution_tested"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(LIB)]
sys.modules[PACKAGE] = package
for name in ("source_projection", "source_executor"):
    spec = importlib.util.spec_from_file_location(PACKAGE + "." + name, LIB / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
executor = sys.modules[PACKAGE + ".source_executor"]
projection = sys.modules[PACKAGE + ".source_projection"]
spec = importlib.util.spec_from_file_location("executor_projection_fixture", Path(__file__).with_name("test_source_projection.py"))
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)


class SourceExecutorTests(unittest.TestCase):
    OBJECT_FORMAT = "sha1"
    setUp = fixture.SourceProjectionTests.setUp
    git = fixture.SourceProjectionTests.git
    write = fixture.SourceProjectionTests.write
    commit = fixture.SourceProjectionTests.commit
    selected = fixture.SourceProjectionTests.selected

    def prepare(self, script=b'IFS= read -r value < /source/src/main.py; test "$value" = "value = 2"\n'):
        self.write("scripts/check.sh", script)
        self.commit()
        self.selection = projection.Selection(*tuple(getattr(self.selected(), field)
            for field in ("source", "tree", "base", "base_tree")))
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        public = {"bin/sh": "/usr/bin/dash",
                  "lib/x86_64-linux-gnu/libc.so.6": "/usr/lib/x86_64-linux-gnu/libc.so.6",
                  "lib64/ld-linux-x86-64.so.2": "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"}
        self.profile = {"schema": executor.SCHEMA, "id": "synthetic-shell",
            "repository": self.policy["repository"], "source_sha": self.selection.source,
            "source_tree": self.selection.tree, "base_sha": self.selection.base,
            "base_tree": self.selection.base_tree, "compiled_policy_sha256": self.pin,
            "adapter_sha256": hashlib.sha256(executor.ADAPTER.read_bytes()).hexdigest(),
            "argv": ["/bin/sh", "/source/scripts/check.sh"], "runtime_files": [],
            "timeout_seconds": 5, "max_output_bytes": 4096}
        for name, path in public.items():
            self.add_runtime(name, Path(path).read_bytes(), executable=True)

    def add_runtime(self, name, value, executable=True):
        path = self.runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o700 if executable else 0o600)
        self.profile["runtime_files"].append({"path": name,
            "sha256": hashlib.sha256(value).hexdigest(), "executable": executable})

    def run_selected(self, *, raw=None, pin=None, **updates):
        encoded = json.dumps(self.profile, sort_keys=True).encode() if raw is None else raw
        values = dict(selection=self.selection, repository_name=self.policy["repository"],
            compiled_policy_sha256=self.pin, destination_parent=self.destination,
            profile_bytes=encoded, profile_sha256=pin or hashlib.sha256(encoded).hexdigest(),
            runtime_root=self.runtime)
        return executor.execute_committed_source(self.repo, **{**values, **updates})

    def test_actual_source_command_passes_without_legacy_or_admission_authority(self):
        self.prepare()
        result = self.run_selected()
        self.assertEqual(result["outcome"], "passed", result)
        self.assertTrue(result["source_execution_closure_verified"])
        self.assertEqual(result["source_sha"], self.selection.source)
        for key in ("full_repository_qualified", "native_gate_qualified", "admission_authorized",
                    "production_authorized", "cache_reusable"):
            self.assertIs(result[key], False)
        self.assertEqual(result["retained_output_bytes"], 0)
        self.assertFalse((self.repo / ".ai/agent-checks/receipt.json").exists())

    def test_real_omitted_input_is_a_failure_not_successful_evidence(self):
        self.prepare(b'read -r value < /source/private/opaque.bin\n')
        result = self.run_selected()
        self.assertEqual(result["outcome"], "command-or-isolation-failed")
        self.assertFalse(result["source_execution_closure_verified"])
        self.assertEqual(result["retained_output_bytes"], 0)

    def test_actual_host_paths_credentials_proc_and_source_writes_are_denied(self):
        sentinel = self.root / "host-sentinel"
        sentinel.write_text("PRIVATE_SYNTHETIC_SENTINEL")
        script = f'''test ! -e {sentinel} || exit 10
        test ! -e {self.repo}/src/main.py || exit 11
        test ! -e /proc/self/root{sentinel} || exit 12
        test ! -e /proc/1/environ || exit 13
        test -z "$SYNTHETIC_EXECUTOR_CREDENTIAL" || exit 14
        if (echo BAD > {sentinel}) 2>/dev/null; then exit 15; fi
        if (echo BAD > /source/src/main.py) 2>/dev/null; then exit 16; fi
        if (echo BAD > /bin/sh) 2>/dev/null; then exit 17; fi
        test -d /tmp || exit 18
        test ! -w /source || exit 19
        test ! -w /bin || exit 20
        '''.encode()
        self.prepare(script)
        with patch.dict(os.environ, {"SYNTHETIC_EXECUTOR_CREDENTIAL": "NEVER_RETAIN_SYNTHETIC_VALUE"}):
            result = self.run_selected()
        self.assertEqual(result["outcome"], "passed", result)
        self.assertEqual(sentinel.read_text(), "PRIVATE_SYNTHETIC_SENTINEL")
        encoded = json.dumps(result)
        for value in (str(sentinel), str(self.repo), "NEVER_RETAIN_SYNTHETIC_VALUE", "echo BAD"):
            self.assertNotIn(value, encoded)

    def test_real_network_namespace_and_host_unix_socket_are_unreachable(self):
        self.prepare()
        tcp = socket.socket()
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        self.addCleanup(tcp.close)
        unix = socket.socket(socket.AF_UNIX)
        socket_path = self.root / "host.sock"
        unix.bind(str(socket_path))
        unix.listen(1)
        self.addCleanup(unix.close)
        # Compiled generic synthetic probe uses no product/provider fixtures.
        source = self.root / "network-probe.c"
        source.write_text('''#include <sys/socket.h>
#include <sys/un.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdlib.h>
#include <string.h>
int main(int n,char **v){
 if(n!=3)return 10;
 int s=socket(AF_INET,SOCK_STREAM,0);
 struct sockaddr_in a={0};a.sin_family=AF_INET;a.sin_port=htons(atoi(v[1]));
 inet_pton(AF_INET,"127.0.0.1",&a.sin_addr);
 if(connect(s,(struct sockaddr*)&a,sizeof(a))==0)return 11;
 int u=socket(AF_UNIX,SOCK_STREAM,0);struct sockaddr_un b={0};b.sun_family=AF_UNIX;
 strncpy(b.sun_path,v[2],sizeof(b.sun_path)-1);
 if(connect(u,(struct sockaddr*)&b,sizeof(b))==0)return 12;
 return 0;}
''')
        binary = self.root / "network-probe"
        subprocess.run(["/usr/bin/cc", str(source), "-o", str(binary)], check=True,
                       capture_output=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
        # Same actual probe must reach the known host listeners outside sandbox.
        control = subprocess.run([str(binary), str(tcp.getsockname()[1]), str(socket_path)],
                                 capture_output=True, timeout=3)
        self.assertEqual(control.returncode, 11)
        unix_control = subprocess.run([str(binary), "0", str(socket_path)],
                                      capture_output=True, timeout=3)
        self.assertEqual(unix_control.returncode, 12)
        self.add_runtime("bin/network-probe", binary.read_bytes())
        self.profile["argv"] = ["/bin/network-probe", str(tcp.getsockname()[1]), str(socket_path)]
        result = self.run_selected()
        self.assertEqual(result["outcome"], "passed", result)

    def test_pinned_argv_profile_source_policy_and_adapter_changes_refuse(self):
        self.prepare()
        raw = json.dumps(self.profile, sort_keys=True).encode()
        pin = hashlib.sha256(raw).hexdigest()
        for key, value in (("argv", ["/bin/sh", "-c", "exit 0"]),
                           ("source_sha", "1" * 40), ("base_tree", "2" * 40),
                           ("compiled_policy_sha256", "3" * 64), ("adapter_sha256", "4" * 64)):
            changed = {**self.profile, key: value}
            with self.subTest(key=key), self.assertRaisesRegex(executor.ExecutionError, "profile_digest_changed"):
                self.run_selected(raw=json.dumps(changed, sort_keys=True).encode(), pin=pin)
        self.profile["base_sha"] = "3" * 40
        with self.assertRaisesRegex(executor.ExecutionError, "profile_source_binding_changed"):
            self.run_selected()

    def test_no_caller_boolean_or_unknown_adapter_can_claim_closure(self):
        self.prepare()
        for key in ("execution_closure_verified", "admission_authorized", "trusted", "env", "mounts"):
            self.profile[key] = True
            with self.subTest(key=key), self.assertRaisesRegex(executor.ExecutionError, "invalid_profile"):
                self.run_selected()
            del self.profile[key]
        self.profile["adapter_sha256"] = "1" * 64
        with self.assertRaisesRegex(executor.ExecutionError, "runtime_digest_changed"):
            self.run_selected()

    def test_runtime_mutation_symlink_and_hardlink_are_not_inputs(self):
        self.prepare()
        selected = self.runtime / "bin/sh"
        original = selected.read_bytes()
        selected.write_bytes(b"changed runtime")
        with self.assertRaisesRegex(executor.ExecutionError, "runtime_digest_changed"):
            self.run_selected()
        selected.unlink()
        outside = self.root / "synthetic-tool"
        outside.write_bytes(original)
        outside.chmod(0o700)
        selected.symlink_to(outside)
        with self.assertRaises(executor.ExecutionError):
            self.run_selected()
        selected.unlink()
        os.link(outside, selected)
        with self.assertRaisesRegex(executor.ExecutionError, "unsafe_runtime_file"):
            self.run_selected()

    def test_runtime_ancestor_symlink_is_refused_without_external_payload_read(self):
        self.prepare()
        original = self.runtime / "bin"
        original.rename(self.runtime / "kept")
        outside = self.root / "external-runtime"
        outside.mkdir()
        sentinel = outside / "sh"
        sentinel.write_bytes(b"DO_NOT_READ_EXTERNAL_TOOL")
        inode = sentinel.stat().st_ino
        original.symlink_to(outside, target_is_directory=True)
        real = os.read
        def read(fd, amount):
            self.assertNotEqual(os.fstat(fd).st_ino, inode)
            return real(fd, amount)
        with patch.object(os, "read", read), self.assertRaises(executor.ExecutionError):
            self.run_selected()

    def test_runtime_and_source_bytes_are_sealed_before_execution(self):
        self.prepare()
        original = executor._collect
        def collected(argv, fds, timeout, limit):
            for fd in fds:
                with self.assertRaises(PermissionError):
                    os.write(fd, b"MUTATION")
            (self.runtime / "bin/sh").write_bytes(b"changed after sealed capture")
            for selected in self.destination.glob("source-projection-*/source/src/main.py"):
                selected.write_bytes(b"changed projected working file")
            return original(argv, fds, timeout, limit)
        with patch.object(executor, "_collect", collected):
            result = self.run_selected()
        self.assertEqual(result["outcome"], "passed", result)

    def test_output_deadline_and_private_sentinel_have_bounded_receipts(self):
        self.prepare(b'while :; do echo PRIVATE_SYNTHETIC_OUTPUT; done\n')
        self.profile["max_output_bytes"] = 128
        result = self.run_selected()
        self.assertEqual(result["outcome"], "output-limit")
        self.assertFalse(result["source_execution_closure_verified"])
        self.assertLessEqual(result["output_bytes"], 65536 + 128)
        self.assertNotIn("PRIVATE_SYNTHETIC_OUTPUT", json.dumps(result))
        self.profile["max_output_bytes"] = 1048576
        self.profile["timeout_seconds"] = 0.08
        self.profile["argv"] = ["/bin/sh", "-c", "while :; do :; done"]
        result = self.run_selected()
        self.assertEqual(result["outcome"], "timeout")
        self.assertLess(result["elapsed_ms"], 1000)
        self.assertFalse(result["source_execution_closure_verified"])

    def test_missing_runtime_and_real_adapter_failure_never_fall_back_to_host(self):
        self.prepare()
        (self.runtime / "bin/sh").unlink()
        with self.assertRaisesRegex(executor.ExecutionError, "execution_input_or_adapter_unavailable"):
            self.run_selected()
        self.add_runtime("bin/other", b"not an executable")
        self.profile["runtime_files"] = [row for row in self.profile["runtime_files"] if row["path"] != "bin/sh"]
        self.profile["argv"] = ["/bin/other"]
        result = self.run_selected()
        self.assertEqual(result["outcome"], "command-or-isolation-failed")
        self.assertFalse(result["source_execution_closure_verified"])

    def test_invalid_profiles_are_strict_and_bounded(self):
        self.prepare()
        baseline = copy.deepcopy(self.profile)
        variants = [{"timeout_seconds": True}, {"timeout_seconds": float("nan")},
            {"timeout_seconds": 301}, {"max_output_bytes": True}, {"max_output_bytes": 1048577},
            {"argv": []}, {"argv": ["/usr/bin/git"]}, {"argv": ["/bin/sh", "bad\narg"]},
            {"runtime_files": []}, {"runtime_files": [baseline["runtime_files"][0]] * 2},
            {"runtime_files": [{"path": "bin/../../private", "sha256": "1" * 64, "executable": True}]},
            {"runtime_files": [{"path": "proc/self/root/bin/sh", "sha256": "1" * 64, "executable": True}]}]
        for change in variants:
            self.profile = {**baseline, **change}
            with self.subTest(change=change), self.assertRaises(executor.ExecutionError):
                self.run_selected()
        with self.assertRaisesRegex(executor.ExecutionError, "profile_digest_changed"):
            self.run_selected(raw=b" " * 65537)
        duplicate = json.dumps(baseline).replace('"id": "synthetic-shell"', '"id": "a", "id": "b"').encode()
        with self.assertRaisesRegex(executor.ExecutionError, "invalid_profile"):
            self.run_selected(raw=duplicate)

    def test_timeout_and_terminal_cleanup_preserve_unrelated_known_neighbor(self):
        neighbor = subprocess.Popen(["/usr/bin/sleep", "10"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, env={})
        try:
            self.prepare(b"while :; do :; done\n")
            self.profile["timeout_seconds"] = 0.05
            timed = self.run_selected()
            self.assertEqual(timed["outcome"], "timeout")
            self.assertIsNone(neighbor.poll())
            # Background shell remains in the isolated PID namespace; either
            # terminal command verdict must settle without killing the neighbor.
            for code in (0, 7):
                self.profile["timeout_seconds"] = 2
                self.profile["argv"] = ["/bin/sh", "-c", f"(while :; do :; done) >/dev/null 2>&1 & exit {code}"]
                result = self.run_selected()
                self.assertEqual(result["returncode"], code)
                self.assertEqual(result["outcome"], "passed" if code == 0 else "command-or-isolation-failed")
                self.assertLess(result["elapsed_ms"], 1000)
                self.assertIsNone(neighbor.poll())
        finally:
            neighbor.kill()
            neighbor.wait(timeout=3)

    def test_pinned_source_and_profile_bytes_changes_are_refused(self):
        self.prepare()
        self.write("src/main.py", b"new selected source\n")
        self.commit()
        changed = self.selected()
        changed_selection = projection.Selection(changed.source, changed.tree, changed.base, changed.base_tree)
        with self.assertRaisesRegex(executor.ExecutionError, "profile_source_binding_changed"):
            self.run_selected(selection=changed_selection)
        raw = json.dumps(self.profile, sort_keys=True).encode()
        with patch.object(executor, "_collect", side_effect=AssertionError("must not execute")):
            with self.assertRaisesRegex(executor.ExecutionError, "profile_digest_changed"):
                self.run_selected(raw=raw + b" ", pin=hashlib.sha256(raw).hexdigest())

    def test_candidate_cannot_expand_the_pinned_base_policy(self):
        self.prepare()
        self.write(projection.POLICY_PATH, json.dumps({**self.policy, "allowed_paths": ["private/"]}).encode())
        self.commit()
        changed = self.selected()
        self.selection = projection.Selection(changed.source, changed.tree, changed.base, changed.base_tree)
        self.profile["source_sha"] = self.selection.source
        self.profile["source_tree"] = self.selection.tree
        with patch.object(executor, "_collect", side_effect=AssertionError("must not execute")), \
                self.assertRaisesRegex(executor.ExecutionError, "execution_input_or_adapter_unavailable"):
            self.run_selected()

    def test_cleanup_keeps_the_own_leader_unreaped_until_group_signal(self):
        self.prepare()
        real = os.killpg
        observed = []
        def owned(pid, sig):
            status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.assertIsNotNone(status)
            self.assertEqual(status.si_pid, pid)
            self.assertEqual(status.si_status, 0)
            observed.append(pid)
            return real(pid, sig)
        with patch.object(os, "killpg", owned):
            result = self.run_selected()
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(len(observed), 1)

    def test_cleanup_denial_cannot_be_reported_as_pass(self):
        self.prepare()
        # Real command/process lifecycle; only signal injection is synthetic.
        real = os.killpg
        def denied(pid, sig):
            try:
                real(pid, sig)
            except ProcessLookupError:
                pass
            raise PermissionError("SYNTHETIC_PRIVATE_DIAGNOSTIC")
        with patch.object(os, "killpg", denied), self.assertRaisesRegex(executor.ExecutionError,
                                                                         "owned_group_cleanup_failed"):
            self.run_selected()



class AdapterDescriptorTests(unittest.TestCase):
    """Actual original-inode checks and explicitly synthetic negative controls."""

    OBJECT_FORMAT = "sha1"
    setUp = SourceExecutorTests.setUp
    git = SourceExecutorTests.git
    write = SourceExecutorTests.write
    commit = SourceExecutorTests.commit
    selected = SourceExecutorTests.selected
    prepare = SourceExecutorTests.prepare
    add_runtime = SourceExecutorTests.add_runtime
    run_selected = SourceExecutorTests.run_selected

    def adapter_pin(self):
        return hashlib.sha256(executor.ADAPTER.read_bytes()).hexdigest()

    def test_actual_original_is_readonly_nonsealed_and_payload_is_sealed(self):
        with executor._adapter_scope(self.adapter_pin()) as adapter, executor.ExitStack() as stack:
            self.assertEqual(os.fstat(adapter.fd).st_uid, 0)
            self.assertEqual(executor.fcntl.fcntl(adapter.fd, executor.fcntl.F_GETFL) & os.O_ACCMODE,
                             os.O_RDONLY)
            self.assertTrue(executor.fcntl.fcntl(adapter.fd, executor.fcntl.F_GETFD)
                            & executor.fcntl.FD_CLOEXEC)
            with self.assertRaises(OSError) as written:
                os.write(adapter.fd, b"MUTATION")
            self.assertEqual(written.exception.errno, errno.EBADF)
            with self.assertRaises(OSError) as sealed:
                executor.fcntl.fcntl(adapter.fd, getattr(executor.fcntl, "F_GET_SEALS", 1034))
            self.assertEqual(sealed.exception.errno, errno.EINVAL)
            payload = executor._sealed(stack, b"synthetic sealed payload")
            with self.assertRaises(PermissionError):
                os.write(payload, b"MUTATION")
            command = executor._AdapterCommand([f"/proc/{adapter.owner_pid}/fd/{adapter.fd}"], adapter)
            self.assertEqual(executor._execution_fds(command, (payload,)), (payload,))
            self.assertNotIn(adapter.fd, executor._execution_fds(command, (payload,)))

    def test_actual_parent_descriptor_exec_works_without_adapter_inheritance(self):
        # A host adapter --version observation proves only the proc-FD exec
        # mechanism, not namespace isolation or source-execution closure.
        with executor._adapter_scope(self.adapter_pin()) as adapter:
            command = executor._AdapterCommand(
                [f"/proc/{adapter.owner_pid}/fd/{adapter.fd}", "--version"], adapter)
            self.assertEqual(executor._execution_fds(command, ()), ())
            result = executor._collect(command, (), 3, 1024)
            self.assertEqual(result["outcome"], "passed", result)
            self.assertEqual(result["retained_output_bytes"], 0)
            self.assertTrue(executor.fcntl.fcntl(adapter.fd, executor.fcntl.F_GETFD)
                            & executor.fcntl.FD_CLOEXEC)
            executor._verify_adapter(adapter)

    def test_parent_pid_binding_refuses_foreign_or_forked_owners_before_fd_reads(self):
        from dataclasses import replace
        with executor._adapter_scope(self.adapter_pin()) as adapter:
            foreign = replace(adapter, owner_pid=adapter.owner_pid + 1)
            command = executor._AdapterCommand(
                [f"/proc/{foreign.owner_pid}/fd/{foreign.fd}"], foreign)
            with patch.object(executor, "_anchor_identity", side_effect=AssertionError("must not read FD")), \
                    self.assertRaisesRegex(executor.ExecutionError, "invalid_adapter_owner"):
                executor._execution_fds(command, ())
            with patch.object(os, "getpid", return_value=adapter.owner_pid + 1), \
                    patch.object(executor, "_anchor_identity", side_effect=AssertionError("must not read FD")), \
                    self.assertRaisesRegex(executor.ExecutionError, "invalid_adapter_owner"):
                executor._verify_adapter(adapter)

    def test_actual_proc_parent_exec_keeps_executable_and_unrelated_fds_out(self):
        # Public synthetic ELF control for the kernel exec route. The fixed
        # adapter's metadata trust and isolated payload remain separate tests.
        source = self.root / "parent-fd-probe.c"
        source.write_text("""#include <fcntl.h>
#include <errno.h>
int main(void) {
 for (int fd=3;fd<1024;fd++) {
  errno=0;
  if (fcntl(fd,F_GETFD)!=-1 || errno!=EBADF) return 31;
 }
 return 0;
}
""")
        binary = self.root / "parent-fd-probe"
        subprocess.run(["/usr/bin/cc", str(source), "-o", str(binary)], check=True,
                       capture_output=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
        executable = os.open(binary, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        unrelated = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        os.set_inheritable(unrelated, True)
        try:
            old_route = subprocess.run([f"/proc/self/fd/{executable}"], pass_fds=(executable,),
                                       close_fds=True, capture_output=True, timeout=3, env={})
            self.assertEqual(old_route.returncode, 31)
            parent_route = subprocess.run([f"/proc/{os.getpid()}/fd/{executable}"], pass_fds=(),
                                          close_fds=True, capture_output=True, timeout=3, env={})
            self.assertEqual(parent_route.returncode, 0)
            self.assertTrue(executor.fcntl.fcntl(executable, executor.fcntl.F_GETFD)
                            & executor.fcntl.FD_CLOEXEC)
        finally:
            os.close(unrelated)
            os.close(executable)

    def test_capability_unknown_or_present_metadata_holds_before_launch(self):
        for error in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EACCES, errno.EPERM, errno.EIO):
            with self.subTest(error=error), patch.object(os, "getxattr", side_effect=OSError(error, "")), \
                    patch.object(executor.subprocess, "Popen") as launch, \
                    self.assertRaisesRegex(executor.ExecutionError, "adapter_capability_metadata_unavailable"):
                with executor._adapter_scope(self.adapter_pin()):
                    self.fail("unknown capability metadata admitted")
            launch.assert_not_called()
        for value in (b"", b"synthetic capability"):
            with self.subTest(value=value), patch.object(os, "getxattr", return_value=value), \
                    self.assertRaisesRegex(executor.ExecutionError, "unsafe_adapter_capabilities"):
                with executor._adapter_scope(self.adapter_pin()):
                    self.fail("present capability metadata admitted")

    def test_wrong_hash_never_launches_or_retries_another_subject(self):
        with patch.object(executor.subprocess, "Popen") as launch, \
                self.assertRaisesRegex(executor.ExecutionError, "runtime_digest_changed"):
            with executor._adapter_scope("1" * 64):
                self.fail("changed adapter admitted")
        launch.assert_not_called()

    def test_plain_or_noncanonical_argv_cannot_establish_fd_trust(self):
        with executor._adapter_scope(self.adapter_pin()) as adapter:
            valid = f"/proc/{adapter.owner_pid}/fd/{adapter.fd}"
            for name in (valid + "/x", valid + " ", valid.replace("/fd/", "/fd/0"),
                         "/proc/self/fd/0", "/usr/bin/bwrap", f"/dev/fd/{adapter.fd}",
                         f"/proc/self/fd/{adapter.fd}",
                         f"/proc/{adapter.owner_pid + 1}/fd/{adapter.fd}"):
                with self.subTest(name=name), self.assertRaisesRegex(executor.ExecutionError,
                                                                      "invalid_adapter_descriptor"):
                    executor._execution_fds(executor._AdapterCommand([name], adapter), ())
            with self.assertRaisesRegex(executor.ExecutionError, "invalid_adapter_descriptor"):
                executor._execution_fds([valid], ())

    def test_payloads_exclude_adapter_anchors_duplicates_and_unsealed_files(self):
        with executor._adapter_scope(self.adapter_pin()) as adapter, executor.ExitStack() as stack:
            command = executor._AdapterCommand([f"/proc/{adapter.owner_pid}/fd/{adapter.fd}"], adapter)
            sealed = executor._sealed(stack, b"payload")
            for payloads in ((adapter.fd,), (adapter.anchors[0][0],), (sealed, sealed),
                             (True,), [sealed]):
                with self.subTest(payloads=payloads), self.assertRaisesRegex(executor.ExecutionError,
                                                                            "invalid_payload_descriptor"):
                    executor._execution_fds(command, payloads)
            unsealed = os.memfd_create("synthetic-unsealed", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            stack.callback(os.close, unsealed)
            with self.assertRaisesRegex(executor.ExecutionError, "unsealed_payload_descriptor"):
                executor._execution_fds(command, (unsealed,))

    def test_writable_or_noncloexec_descriptor_is_refused(self):
        fd = os.memfd_create("synthetic-writable", os.MFD_CLOEXEC)
        try:
            with self.assertRaisesRegex(executor.ExecutionError, "adapter_descriptor_not_readonly"):
                executor._adapter_bytes(fd, self.adapter_pin())
        finally:
            os.close(fd)
        with executor._adapter_scope(self.adapter_pin()) as adapter:
            flags = executor.fcntl.fcntl(adapter.fd, executor.fcntl.F_GETFD)
            try:
                executor.fcntl.fcntl(adapter.fd, executor.fcntl.F_SETFD, 0)
                with self.assertRaisesRegex(executor.ExecutionError, "adapter_descriptor_not_readonly"):
                    executor._verify_adapter(adapter)
            finally:
                executor.fcntl.fcntl(adapter.fd, executor.fcntl.F_SETFD, flags)

    def test_unsafe_file_metadata_refuses_before_reading_bytes(self):
        with executor._adapter_scope(self.adapter_pin()) as adapter:
            actual = os.fstat(adapter.fd)
            fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                      "st_size", "st_mtime_ns", "st_ctime_ns")
            changes = ({"st_uid": 1000}, {"st_nlink": 2}, {"st_size": 0},
                       {"st_size": executor.MAX_ADAPTER + 1},
                       *({"st_mode": actual.st_mode | bits} for bits in (0o002, 0o020, 0o4000, 0o2000)),
                       *({"st_mode": actual.st_mode & ~bits} for bits in (0o004, 0o001)),
                       {"st_mode": stat.S_IFDIR | 0o755})
            for change in changes:
                synthetic = types.SimpleNamespace(**{**{k: getattr(actual, k) for k in fields}, **change})
                with self.subTest(change=change), patch.object(os, "fstat", return_value=synthetic), \
                        patch.object(os, "pread") as read, \
                        self.assertRaisesRegex(executor.ExecutionError, "unsafe_adapter_file"):
                    executor._adapter_bytes(adapter.fd, self.adapter_pin())
                read.assert_not_called()

    def test_named_inode_and_retained_ancestor_drift_refuse(self):
        with executor._adapter_scope(self.adapter_pin()) as adapter:
            real_stat = os.stat
            def changed(name, *args, **kwargs):
                actual = real_stat(name, *args, **kwargs)
                if name != "bwrap" or "dir_fd" not in kwargs:
                    return actual
                fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                          "st_size", "st_mtime_ns", "st_ctime_ns")
                return types.SimpleNamespace(**{**{k: getattr(actual, k) for k in fields},
                                                "st_ino": actual.st_ino + 1})
            with patch.object(os, "stat", changed), \
                    self.assertRaisesRegex(executor.ExecutionError, "adapter_path_changed"):
                executor._verify_adapter(adapter)
            original = executor._anchor_identity
            def ancestor(fd):
                identity = original(fd)
                return (identity[0], identity[1] + 1, *identity[2:])
            with patch.object(executor, "_anchor_identity", ancestor), \
                    self.assertRaisesRegex(executor.ExecutionError, "adapter_ancestor_changed"):
                executor._verify_adapter(adapter)

    def test_postcheck_detects_drift_preserving_primary_failure_or_timeout(self):
        real = executor._verify_adapter
        for primary in (None, executor.ExecutionError("synthetic_command_failed"), TimeoutError()):
            count = 0
            def verify(adapter):
                nonlocal count
                count += 1
                if count == 2:
                    raise executor.ExecutionError("adapter_identity_changed")
                return real(adapter)
            with self.subTest(primary=type(primary).__name__), patch.object(executor, "_verify_adapter", verify):
                if primary is None:
                    with self.assertRaisesRegex(executor.ExecutionError, "adapter_identity_changed"):
                        with executor._adapter_scope(self.adapter_pin()):
                            pass
                else:
                    with self.assertRaises(type(primary)) as raised:
                        with executor._adapter_scope(self.adapter_pin()):
                            raise primary
                    self.assertIs(raised.exception, primary)
                    self.assertIn("adapter_postcheck_failed", raised.exception.__notes__)
                self.assertEqual(count, 2)

    def test_actual_command_rechecks_and_passes_only_required_fds(self):
        self.prepare(b"exit 7\n")
        unrelated = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        os.set_inheritable(unrelated, True)
        self.addCleanup(os.close, unrelated)
        launch = executor.subprocess.Popen
        verify = executor._verify_adapter
        calls = []
        reobserved = []
        def checked(adapter):
            reobserved.append(adapter.fd)
            return verify(adapter)
        def process(argv, *args, **kwargs):
            if isinstance(argv, executor._AdapterCommand):
                adapter = argv.adapter
                payloads = tuple(int(argv[i + 1]) for i, arg in enumerate(argv) if arg == "--ro-bind-data")
                self.assertEqual(kwargs["pass_fds"], payloads)
                self.assertNotIn(adapter.fd, kwargs["pass_fds"])
                self.assertEqual(argv[0], f"/proc/{os.getpid()}/fd/{adapter.fd}")
                self.assertTrue(executor.fcntl.fcntl(adapter.fd, executor.fcntl.F_GETFD)
                                & executor.fcntl.FD_CLOEXEC)
                self.assertTrue(kwargs["close_fds"])
                self.assertEqual(kwargs["env"], {})
                self.assertNotIn(unrelated, kwargs["pass_fds"])
                self.assertTrue(set(kwargs["pass_fds"]).isdisjoint(fd for fd, _ in adapter.anchors))
                calls.append(adapter.fd)
            return launch(argv, *args, **kwargs)
        with patch.object(executor, "_verify_adapter", checked), \
                patch.object(executor.subprocess, "Popen", process):
            result = self.run_selected()
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(len(reobserved), 3)
        self.assertFalse(result["source_execution_closure_verified"])
        self.assertEqual(result["retained_output_bytes"], 0)

    def test_actual_cleanup_error_remains_primary_if_postcheck_also_fails(self):
        self.prepare()
        verify = executor._verify_adapter
        signal_group = os.killpg
        count = 0
        def checked(adapter):
            nonlocal count
            count += 1
            if count == 3:
                raise executor.ExecutionError("adapter_identity_changed")
            return verify(adapter)
        def denied(pid, sig):
            try:
                signal_group(pid, sig)
            except ProcessLookupError:
                pass
            raise PermissionError("synthetic cleanup denial")
        with patch.object(executor, "_verify_adapter", checked), patch.object(os, "killpg", denied), \
                self.assertRaisesRegex(executor.ExecutionError, "owned_group_cleanup_failed") as raised:
            self.run_selected()
        self.assertEqual(count, 3)
        self.assertIn("adapter_postcheck_failed", raised.exception.__notes__)

    def test_actual_isolated_payload_cannot_inherit_host_anchors_or_unrelated_fds(self):
        self.prepare()
        source = self.root / "fd-probe.c"
        source.write_text("""#include <fcntl.h>
#include <errno.h>
int main(void) {
 for (int fd=3;fd<1024;fd++) {
  errno=0;
  if (fcntl(fd,F_GETFD)!=-1 || errno!=EBADF) return 31;
 }
 return 0;
}
""")
        binary = self.root / "fd-probe"
        subprocess.run(["/usr/bin/cc", str(source), "-o", str(binary)], check=True,
                       capture_output=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
        self.add_runtime("bin/fd-probe", binary.read_bytes())
        self.profile["argv"] = ["/bin/fd-probe"]
        unrelated = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        os.set_inheritable(unrelated, True)
        try:
            host_control = subprocess.run([str(binary)], pass_fds=(unrelated,),
                                          capture_output=True, timeout=3, env={})
            self.assertEqual(host_control.returncode, 31)
            result = self.run_selected()
        finally:
            os.close(unrelated)
        self.assertEqual(result["outcome"], "passed", result)


    def test_real_capture_uses_nofollow_anchors_and_never_reopens_exec_leaf(self):
        opened = []
        real = os.open
        def observed(path, flags, *args, **kwargs):
            opened.append((path, flags))
            return real(path, flags, *args, **kwargs)
        with patch.object(os, "open", observed):
            with executor._adapter_scope(self.adapter_pin()) as adapter:
                executor._verify_adapter(adapter)
        self.assertEqual(sum(path == "bwrap" for path, _ in opened), 1)
        for path, flags in opened:
            self.assertIn(path, ("/", "usr", "bin", "bwrap"))
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertTrue(flags & os.O_CLOEXEC)
            if path != "bwrap":
                self.assertTrue(flags & os.O_DIRECTORY)

    def test_changed_content_or_new_capability_is_refused_during_reobservation(self):
        with executor._adapter_scope(self.adapter_pin()) as adapter:
            real_pread = os.pread
            def changed(fd, amount, offset):
                raw = real_pread(fd, amount, offset)
                return bytes([raw[0] ^ 1]) + raw[1:] if raw else raw
            with patch.object(os, "pread", changed), \
                    self.assertRaisesRegex(executor.ExecutionError, "runtime_digest_changed"):
                executor._adapter_bytes(adapter.fd, self.adapter_pin())
            observed = 0
            def capability(fd, name):
                nonlocal observed
                observed += 1
                if observed == 1:
                    raise OSError(errno.ENODATA, "")
                return b""
            with patch.object(os, "getxattr", capability), \
                    self.assertRaisesRegex(executor.ExecutionError, "unsafe_adapter_capabilities"):
                executor._adapter_bytes(adapter.fd, self.adapter_pin())
            self.assertEqual(observed, 2)

    def test_unsafe_or_replaced_root_ancestor_is_refused(self):
        with executor._adapter_scope(self.adapter_pin()) as adapter:
            observed = os.fstat(adapter.anchors[0][0])
            for change in ({"st_uid": 1000}, {"st_mode": observed.st_mode | 0o002},
                           {"st_mode": observed.st_mode | 0o020},
                           {"st_mode": stat.S_IFREG | 0o755}):
                metadata = types.SimpleNamespace(**{**{key: getattr(observed, key)
                    for key in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")}, **change})
                with self.subTest(change=change), patch.object(os, "fstat", return_value=metadata), \
                        self.assertRaisesRegex(executor.ExecutionError, "unsafe_adapter_ancestor"):
                    executor._anchor_identity(adapter.anchors[0][0])



if __name__ == "__main__":
    unittest.main()
