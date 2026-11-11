"""Real Linux namespace checks using generic source and synthetic host sentinels."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
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


if __name__ == "__main__":
    unittest.main()
