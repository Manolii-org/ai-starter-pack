#!/usr/bin/env python3
"""Replay the state adapter over real Git objects and concurrent ref updates."""
import base64
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / ".github/actions/validate-release-manifest"
sys.path.insert(0, str(ACTION))
state = importlib.import_module("release_state")


class GitFixture(state.GitHubStateStore):
    def __init__(self, directory):
        super().__init__("example/product", "production", "unused-fixture-token")
        self.directory = directory
        self.barrier = None
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
                    "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}
        self.git("init", "--bare")

    def git(self, *args, data=None, check=True):
        result = subprocess.run(["git", "-C", self.directory, *args], input=data,
                                capture_output=True, text=True, env=self.env, timeout=10)
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def request(self, method, path, payload=None, *, missing=False):
        if path == "git/ref/" + self.ref:
            result = self.git("rev-parse", "--verify", "refs/" + self.ref, check=False)
            if result.returncode and missing:
                return None
            return {"object": {"sha": result.stdout.strip()}}
        if method == "POST" and path == "git/blobs":
            return {"sha": self.git("hash-object", "-w", "--stdin", data=payload["content"]).stdout.strip()}
        if method == "POST" and path == "git/trees":
            entry = payload["tree"][0]
            text = f'{entry["mode"]} {entry["type"]} {entry["sha"]}\t{entry["path"]}\n'
            return {"sha": self.git("mktree", data=text).stdout.strip()}
        if method == "POST" and path == "git/commits":
            args = ["commit-tree", payload["tree"]]
            for parent in payload["parents"]:
                args += ["-p", parent]
            return {"sha": self.git(*args, data=payload["message"]).stdout.strip()}
        if method == "GET" and path.startswith("git/commits/"):
            return {"tree": {"sha": self.git("show", "-s", "--format=%T", path.split("/")[-1]).stdout.strip()}}
        if method == "GET" and path.startswith("git/trees/"):
            line = self.git("ls-tree", path.split("/")[-1]).stdout.strip()
            meta, name = line.split("\t")
            mode, kind, sha = meta.split()
            return {"tree": [{"mode": mode, "type": kind, "sha": sha, "path": name}]}
        if method == "GET" and path.startswith("git/blobs/"):
            raw = self.git("cat-file", "blob", path.split("/")[-1]).stdout.encode()
            return {"encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode()}
        if method in {"PATCH", "POST"} and path in {"git/refs", "git/refs/" + self.ref}:
            if self.barrier:
                self.barrier.wait(timeout=10)
            ref = "refs/" + self.ref
            current = self.git("rev-parse", "--verify", ref, check=False)
            old = current.stdout.strip() if current.returncode == 0 else "0" * 40
            if method == "POST" and old != "0" * 40:
                raise state.StateConflict("already exists")
            if method == "PATCH":
                assert payload["force"] is False
                if self.git("merge-base", "--is-ancestor", old, payload["sha"], check=False).returncode:
                    raise state.StateConflict("not a fast-forward")
            result = self.git("update-ref", ref, payload["sha"], old, check=False)
            if result.returncode:
                raise state.StateConflict("another child won")
            return {"ref": ref, "object": {"sha": payload["sha"]}}
        raise AssertionError((method, path))


class GitAdmissionIntegrationTests(unittest.TestCase):
    def test_real_git_roundtrip_and_stale_parent_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GitFixture(directory)
            initial = {"schema_version": 1, "active": None, "continuations": {}}
            with self.assertRaises(state.StateUnavailable):
                store.read()
            first = store.initialize_empty_for_adoption()
            self.assertEqual(store.read(), (first, initial))
            changed = {"schema_version": 1, "active": {"release_id": "a" * 64, "phase": "selected"},
                       "continuations": {}}
            second = store.compare_and_swap(first, changed)
            self.assertEqual(store.read(), (second, changed))
            with self.assertRaises(state.StateConflict):
                store.compare_and_swap(first, initial)
            self.assertFalse(self.git_branches(store))

    @staticmethod
    def git_branches(store):
        return store.git("for-each-ref", "--format=%(refname)", "refs/heads/").stdout.strip()

    def test_identical_concurrent_proposals_have_exactly_one_cas_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GitFixture(directory)
            empty = {"schema_version": 1, "active": None, "continuations": {}}
            parent = store.initialize_empty_for_adoption()
            store.barrier = threading.Barrier(2)
            results = []

            def reserve():
                try:
                    results.append(("won", store.compare_and_swap(parent, empty)))
                except state.StateConflict:
                    results.append(("conflict", None))

            workers = [threading.Thread(target=reserve) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
                self.assertFalse(worker.is_alive())
            self.assertCountEqual([r[0] for r in results], ["won", "conflict"])
            self.assertEqual(store.read()[0], next(r[1] for r in results if r[0] == "won"))

    def test_near_limit_state_roundtrips_through_real_http_json_parser(self):
        from unittest.mock import patch
        from urllib.error import HTTPError

        class Response:
            def __init__(self, body):
                self.body = body

            def read(self, size):
                return self.body[:size]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as directory:
            fixture = GitFixture(directory)
            store = state.GitHubStateStore("example/product", "production", "fixture-token")

            def open_request(request, timeout):
                path = request.full_url.removeprefix("https://api.github.com/repos/example/product/")
                payload = json.loads(request.data) if request.data else None
                result = fixture.request(request.get_method(), path, payload, missing=True)
                if result is None:
                    raise HTTPError(request.full_url, 404, "missing", {}, None)
                return Response(json.dumps(result).encode())

            with patch.object(state, "build_opener") as opener:
                opener.return_value.open.side_effect = open_request
                first = store.initialize_empty_for_adoption()
                retained = {"schema_version": 1, "active": None,
                            "continuations": {f"{n:064x}": f"{n+1:064x}" for n in range(6000)}}
                size = len(json.dumps(retained, sort_keys=True, separators=(",", ":")).encode())
                self.assertGreater(size, 800000)
                self.assertLess(size, state.MAX_STATE_BYTES)
                final = store.compare_and_swap(first, retained)
                self.assertEqual(store.read(), (final, retained))


if __name__ == "__main__":
    unittest.main()
