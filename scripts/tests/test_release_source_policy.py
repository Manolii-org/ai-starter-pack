#!/usr/bin/env python3
"""Exact external code provenance never expands database or native authority."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import ModuleType

from test_release_admission_git import GitFixture
from test_release_vector import release, state, subject

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
EXTERNAL = {"repo": "shared-fixture/runner-image", "sha": "7" * 40, "tree": "8" * 40}
# Exact public c4359cfdb2b9c85f10a2d4146660a186290e4ae9 reader; Apache-2.0 (LICENSE).
LEGACY_READER = Path("scripts/tests/fixtures/release-manifest.c435.py")
LEGACY_READER_SHA256 = "aa0afc19ff2581a70e7aceb99ee0e5ca16f4dfd6dccbea2bbcd320308fdea6c4"
LEGACY_SCHEMAS = {
    1: "6b4201a3fc07bc039d494d9f58bedc315b603d3aed65def7a1af3410ed99eb88",
    2: "940d0da59bcc0078d8ae758b72ba40d5a3039f9c1955c1f95975250e599d07f7",
}


def inputs(source=None):
    """Build a synthetic exact-code grant without extending data ownership."""
    value = subject()
    value["profile_version"] = "exact-code-v1"
    value["components"][0]["source"] = copy.deepcopy(EXTERNAL if source is None else source)
    policy = {"schema_version": 1, "repo": "example/product", "entity": "example",
              "control_sha": "b" * 40, "capability_sha": "c" * 40,
              "profile_version": "exact-code-v1", "allowed_code_sources": [copy.deepcopy(value["components"][0]["source"])]}
    return value, policy


def prepared(value, policy):
    """Bind matching policy and combined-input digests before preparation."""
    value = copy.deepcopy(value)
    value["policy_sha256"] = release.digest(policy)
    value["qualifications"][0]["tested_subject_sha256"] = release.digest(release.qualification_subject(value))
    return release.prepare(value, schema_version=2, trusted_source_policy=policy)


def context(record, policy):
    """Supply independent synthetic executor metadata, never approval history."""
    return {"repo": "example/product", "environment": "production", "controller_sha": "b" * 40,
            "release_id": record["release_id"], "preparation_run_id": 10, "preparation_run_attempt": 1,
            "execution_run_id": 10, "execution_run_attempt": 1, "execution_job": "execute",
            "policy_sha256": release.digest(policy), "profile_version": "exact-code-v1",
            "capability_sha": "c" * 40, "entity": "example"}


class ExactSourcePolicyTests(unittest.TestCase):
    def test_explicit_reader_policy_and_unchanged_component_code(self):
        value, policy = inputs()
        record = prepared(value, policy)
        self.assertEqual(release.validate(record, trusted_source_policy=policy), record)
        with self.assertRaises(release.ManifestError):
            release.validate(record)
        value["unchanged_components"] = [{"id": "shared-library", "source": copy.deepcopy(EXTERNAL),
                                           "artifact_id": "retained",
                                           "config_sha256": "8" * 64, "features_sha256": release.digest([])}]
        self.assertEqual(len(prepared(value, policy)["subject"]["unchanged_components"]), 1)

    def test_default_and_actual_old_reader_remain_closed(self):
        """Execute only the digest-pinned legacy reader with unchanged schemas."""
        value, policy = inputs()
        record = prepared(value, policy)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            action = root / ".github/actions/validate-release-manifest"
            action.mkdir(parents=True)
            (root / "schemas").mkdir()
            old = (ROOT / LEGACY_READER).read_bytes()
            self.assertEqual(hashlib.sha256(old).hexdigest(), LEGACY_READER_SHA256,
                             "legacy reader fixture digest differs")
            for version, checksum in LEGACY_SCHEMAS.items():
                path = Path(f"schemas/release-manifest.v{version}.schema.json")
                raw = (ROOT / path).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), checksum)
                (root / path).write_bytes(raw)
            legacy = ModuleType("old_exact_source_reader")
            legacy.__file__ = str(action / "reader.py")
            exec(compile(old, legacy.__file__, "exec"), legacy.__dict__)
            with self.assertRaises(legacy.ManifestError):
                legacy.validate(record)
            for version in (1, 2):
                fixture = json.loads((ROOT / f"scripts/tests/fixtures/release-manifest.v{version}.json").read_text())
                self.assertEqual(legacy.validate(fixture), release.validate(fixture))
            fixture = json.loads((ROOT / "scripts/tests/fixtures/release-manifest.v1.json").read_text())
            with self.assertRaises(release.ManifestError):
                release.validate(fixture, trusted_source_policy=policy)

    def test_old_reader_compatibility_survives_real_shallow_checkout(self):
        """Prove absent history works and tampered local reader bytes fail closed."""
        env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1",
               "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull}
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            cloned = subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet",
                                     "--no-tags", "--depth=1", ROOT.as_uri(), str(checkout)],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(cloned.returncode, 0, cloned.stdout + cloned.stderr)
            shallow = subprocess.check_output(["git", "rev-parse", "--is-shallow-repository"],
                                              cwd=checkout, env=env, text=True).strip()
            self.assertEqual(shallow, "true")
            historical = subprocess.run(["git", "cat-file", "-e",
                "c4359cfdb2b9c85f10a2d4146660a186290e4ae9^{commit}"],
                cwd=checkout, env=env, capture_output=True)
            self.assertNotEqual(historical.returncode, 0)
            # Overlay only this candidate's maintainer test inputs; never fetch history.
            for path in (LEGACY_READER, Path("scripts/tests/test_release_source_policy.py")):
                (checkout / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, checkout / path)
            command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "scripts/tests",
                       "-p", "test_release_source_policy.py", "-k", "default_and_actual_old_reader_remain_closed"]
            compatible = subprocess.run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(compatible.returncode, 0, compatible.stdout + compatible.stderr)
            with (checkout / LEGACY_READER).open("ab") as stream:
                stream.write(b'\nraise RuntimeError("unverified legacy fixture executed")\n')
            refused = subprocess.run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("legacy reader fixture digest differs", refused.stderr)
            self.assertNotIn("unverified legacy fixture executed", refused.stderr)
            for path in (Path(".github/actions/validate-release-manifest"), Path("scripts/tests")):
                self.assertFalse(list((checkout / path).rglob("__pycache__")))

    def test_exact_repository_commit_tree_no_broad_or_manifest_grants(self):
        for key, changed in (("repo", "shared-fixture/other"), ("sha", "0" * 40), ("tree", "0" * 40)):
            value, policy = inputs()
            value["components"][0]["source"][key] = changed
            with self.assertRaises(release.ManifestError):
                prepared(value, policy)
        for grant in ({"repo": "shared-fixture/*", "sha": "7" * 40, "tree": "8" * 40},
                      {"repo": "shared-fixture/runner-image"}, {**EXTERNAL, "database": True}):
            value, policy = inputs()
            policy["allowed_code_sources"] = [grant]
            with self.assertRaises(release.ManifestError):
                prepared(value, policy)
        value, policy = inputs()
        value["trusted_source_policy"] = policy
        with self.assertRaises(release.ManifestError):
            prepared(value, policy)

    def test_database_stays_local_even_when_exactly_allowed_as_code(self):
        value, policy = inputs()
        value["databases"][0]["source"] = copy.deepcopy(EXTERNAL)
        with self.assertRaisesRegex(release.ManifestError, "database source crosses"):
            prepared(value, policy)

    def test_exact_policy_rejects_newline_identities_and_noninteger_version(self):
        for key in ("repo", "sha", "tree"):
            value, policy = inputs()
            value["components"][0]["source"][key] += "\n"
            policy["allowed_code_sources"][0][key] += "\n"
            with self.assertRaisesRegex(release.ManifestError, "policy does not match schema"):
                prepared(value, policy)
        for key in ("repo", "control_sha", "capability_sha"):
            value, policy = inputs()
            value[key] += "\n"
            policy[key] += "\n"
            with self.assertRaises(release.ManifestError):
                prepared(value, policy)
        for version in (True, 1.0, "1"):
            value, policy = inputs()
            policy["schema_version"] = version
            with self.assertRaises(release.ManifestError):
                prepared(value, policy)

    def test_policy_digest_profile_owner_controller_and_capability_must_match(self):
        value, policy = inputs()
        record = prepared(value, policy)
        altered = copy.deepcopy(policy)
        altered["allowed_code_sources"].append({**EXTERNAL, "sha": "0" * 40})
        with self.assertRaisesRegex(release.ManifestError, "policy identity"):
            release.validate(record, trusted_source_policy=altered)
        for key, changed in (("repo", "example/other"), ("entity", "other"),
                             ("profile_version", "other"), ("control_sha", "0" * 40),
                             ("capability_sha", "0" * 40)):
            wrong = copy.deepcopy(policy)
            wrong[key] = changed
            with self.assertRaisesRegex(release.ManifestError, "owning release profile"):
                prepared(value, wrong)

    def test_native_context_requires_independent_controller_and_compiled_profile(self):
        value, policy = inputs()
        record = prepared(value, policy)
        args = {"expected_release_id": record["release_id"], "expected_controller_sha": "b" * 40,
                "execution_context": context(record, policy), "now": NOW, "trusted_source_policy": policy}
        self.assertEqual(release.validate_native_binding(record, **args), record)
        for key, changed in (("expected_controller_sha", "0" * 40), ("expected_controller_sha", None),
                             ("trusted_source_policy", None)):
            with self.assertRaises(release.ManifestError):
                release.validate_native_binding(record, **{**args, key: changed})
        for key in ("policy_sha256", "profile_version", "capability_sha", "entity", "controller_sha"):
            with self.assertRaises(release.ManifestError):
                release.validate_native_binding(record, **{**args, "execution_context": {**args["execution_context"], key: "wrong"}})
        value["execution"]["workflow_sha"] = "0" * 40
        changed = prepared(value, policy)
        with self.assertRaisesRegex(release.ManifestError, "workflow differs"):
            release.validate_native_binding(changed, **{**args, "expected_release_id": changed["release_id"],
                                                        "execution_context": context(changed, policy)})
        value, altered_policy = inputs()
        value["control_sha"] = altered_policy["control_sha"] = "0" * 40
        changed = prepared(value, altered_policy)
        with self.assertRaisesRegex(release.ManifestError, "controller differs"):
            release.validate_native_binding(changed, **{**args, "expected_release_id": changed["release_id"],
                                                        "trusted_source_policy": altered_policy,
                                                        "execution_context": context(changed, altered_policy)})

    def test_real_git_source_identity_and_admission_restart_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GitFixture(directory)
            first = store.initialize_empty_for_adoption(schema_version=2)
            tree = store.git("show", "-s", "--format=%T", first).stdout.strip()
            exact_source = {"repo": "shared-fixture/runner-image", "sha": first, "tree": tree}
            value, policy = inputs(exact_source)
            record = prepared(value, policy)
            _, empty = store.read()
            with self.assertRaises(release.ManifestError):
                state.reserve(empty, record, now=NOW)
            selected = state.reserve(empty, record, now=NOW, trusted_source_policy=policy)
            next_revision = store.compare_and_swap(first, selected)
            self.assertEqual(store.read(), (next_revision, selected))
            with self.assertRaises(state.StateConflict):
                store.compare_and_swap(first, empty)
            selected["active"]["phase"] = "success"
            store.compare_and_swap(next_revision, selected)
            _, restarted = store.read()
            with self.assertRaises(state.StateConflict):
                state.reserve(restarted, record, now=NOW, trusted_source_policy=policy)
            with self.assertRaises(state.StateConflict):
                state.reserve(empty, record, now=datetime(2026, 11, 2, tzinfo=timezone.utc), trusted_source_policy=policy)
            value["components"][0]["source"]["tree"] = "0" * 40
            with self.assertRaises(release.ManifestError):
                prepared(value, policy)


if __name__ == "__main__":
    unittest.main()
