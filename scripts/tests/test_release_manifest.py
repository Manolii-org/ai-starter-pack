#!/usr/bin/env python3
"""Exercise actual manifest/action seams with synthetic product/provider inputs."""
import copy
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / ".github/actions/validate-release-manifest"
sys.path.insert(0, str(ACTION))
release = importlib.import_module("release_manifest")
state = importlib.import_module("release_state")
approval = importlib.import_module("github_approval")
ADMISSION_NOW = datetime(2026, 11, 1, 1, 15, tzinfo=timezone.utc)


def subject(*, database=True, provider="vercel"):
    return {
        "repo": "example/product", "environment": "production", "kind": "release",
        "source_sha": "a" * 40, "control_sha": "b" * 40, "capability_sha": "c" * 40,
        "profile_version": "1", "policy_sha256": "d" * 64,
        "decision_sequence": ["schema", "activation"],
        "preparation": {"run_id": 10, "run_attempt": 1, "job": "prepare"},
        "artifacts": [{"target": "web", "provider": provider, "artifact_id": "candidate",
                       "artifact_sha256": "e" * 64, "config_sha256": "f" * 64,
                       "before_artifact_id": "previous", "recovery_artifact_id": "previous",
                       "databases": ["primary"] if database else []}],
        "databases": [{"id": "primary", "adapter": "legacy-sequence", "baseline": ["001"],
                       "pending": ["002"], "expected_after": ["001", "002"],
                       "migration_sha256": "1" * 64, "backup_required": True}] if database else [],
        "qualification": {"source_sha": "a" * 40, "run_id": 9, "run_attempt": 2,
                          "artifact_id": 8, "artifact_sha256": "2" * 64,
                          "schema_source_sha": "a" * 40 if database else None,
                          "migration_sha256": "1" * 64 if database else None,
                          "fixture_version": "fixture-v1" if database else None},
        "timing": {"selected_at": "2026-11-01T00:00:00Z",
                   "approval_deadline": "2026-11-01T01:00:00Z",
                   "execute_before": "2026-11-01T02:00:00Z", "intermediate_seconds": 3600},
        "recovery": {"owner": "release-owner", "data_authority": "data-owner",
                     "allowed_actions": ["hold", "code-rollback"],
                     "automatic_data_loss_threshold": 0},
        "continuation": None,
    }


class ManifestTests(unittest.TestCase):
    def test_coordinated_adoption_schema_preserves_decisions_or_requires_explicit_combination(self):
        import json
        from jsonschema import Draft202012Validator
        fragment = json.loads((ROOT / "schemas/release-adoption.v1.schema.json").read_text())
        validator = Draft202012Validator(fragment)
        base = {"schema_version": 1, "mode": "coordinated", "profile_version": "fixture-v1",
                "policy_reference": "policy-v1", "qualification_reference": "qualification-v1",
                "release_owner": "release-owner", "recovery_owner": "recovery-owner",
                "data_authority": "data-owner", "admission_stop": "owner-stop",
                "approval_window_seconds": 300, "execution_budget_seconds": 1200,
                "intermediate_window_seconds": 1800}
        sequences = [[], ["activation"], ["schema", "activation"], ["release"]]
        for installed in sequences:
            for execution in sequences:
                row = {**base, "installed_decisions": installed, "execution_decisions": execution}
                self.assertEqual(not list(validator.iter_errors(row)), installed == execution)
                row["combined_scope_adoption"] = "explicit-owner-policy-adoption"
                expected = installed == execution or (installed == ["schema", "activation"] and execution == ["release"])
                self.assertEqual(not list(validator.iter_errors(row)), expected)

    def test_adoption_and_existing_lane_schema_are_distinct_valid_contracts(self):
        schema = json.loads((ROOT / "schemas/release-adoption.v1.schema.json").read_text())
        release.jsonschema.Draft202012Validator.check_schema(schema)
        validator = release.jsonschema.Draft202012Validator(schema)
        self.assertFalse(list(validator.iter_errors({"schema_version": 1, "mode": "legacy"})))
        self.assertTrue(list(validator.iter_errors({"schema_version": 1, "mode": "coordinated"})))
        lane = json.loads((ROOT / "schemas/deployment-contract.schema.json").read_text())
        release.jsonschema.Draft202012Validator.check_schema(lane)
        self.assertIn("repos", lane["required"])
        self.assertNotIn("repo", lane["required"])

    def test_schema_itself_rejects_invalid_activation_only_subjects(self):
        from jsonschema import Draft202012Validator
        schema = json.loads((ROOT / "schemas/release-manifest.v1.schema.json").read_text())
        validator = Draft202012Validator(schema)
        normal = json.loads((ROOT / "scripts/tests/fixtures/release-manifest.v1.json").read_text())
        self.assertEqual(release.validate(normal), normal)
        activation = copy.deepcopy(normal)
        activation["subject"].update(kind="activation-only", decision_sequence=["activation"],
                                     continuation={"parent_release_id": "a" * 64, "schema_evidence_sha256": "b" * 64})
        for db in activation["subject"]["databases"]:
            db["pending"] = []
            db["expected_after"] = list(db["baseline"])
        activation = release.prepare(activation["subject"])
        self.assertFalse(list(validator.iter_errors(activation)))
        for mutation in ["missing-evidence", "pending-sql", "no-protected-decision", "normal-with-parent"]:
            bad = copy.deepcopy(activation)
            if mutation == "missing-evidence":
                bad["subject"]["continuation"] = None
            elif mutation == "pending-sql":
                bad["subject"]["databases"][0]["pending"] = ["00002"]
            elif mutation == "no-protected-decision":
                bad["subject"]["decision_sequence"] = []
            else:
                bad["subject"]["kind"] = "release"
            self.assertTrue(list(validator.iter_errors(bad)), mutation)

    def test_invalid_timestamps_fail_with_sanitized_domain_error(self):
        for value in ["private-invalid-timeZ", "2026-13-01T00:00:00Z", "20261101T000000Z"]:
            changed = subject()
            changed["timing"]["selected_at"] = value
            with self.assertRaises(release.ManifestError):
                release.prepare(changed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            changed = subject()
            changed["timing"]["selected_at"] = "private-invalid-timeZ"
            path.write_text(json.dumps({"schema_version": 1, "release_id": release.digest(changed), "subject": changed}))
            result = subprocess.run([sys.executable, str(ACTION / "release_manifest.py"),
                                     "--manifest", str(path), "--dry-run"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("RELEASE_MANIFEST_INVALID:", result.stderr)
            self.assertNotIn("private-invalid-time", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_generated_comparison_survives_python_optimization(self):
        config = json.loads((ROOT / "config/agent-checks.json").read_text())
        recipe = next(r for r in config["checks"] if r["id"] == "release-manifest-contract")
        self.assertIn("config/agent-checks.json", recipe["paths"])
        self.assertIn("schemas/deployment-contract.schema.json", recipe["paths"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for target in ["schemas", "plugin/manolii-framework/schemas", "registry/platform/framework/schemas"]:
                (root / target).mkdir(parents=True)
                for name in ["release-adoption.v1.schema.json", "release-manifest.v1.schema.json", "release-manifest.v2.schema.json"]:
                    (root / target / name).write_text("fixture")
            comparisons = [command for command in recipe["commands"]
                           if len(command) == 3 and command[1] == "-c"
                           and "generated release schemas differ" in command[2]]
            self.assertEqual(len(comparisons), 1)
            command = [sys.executable, "-O", *comparisons[0][1:]]
            self.assertEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
            (root / "registry/platform/framework/schemas/release-manifest.v1.schema.json").write_text("corrupt")
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("generated release schemas differ", result.stderr)

    def test_deterministic_identity_preserves_meaning_and_array_order(self):
        original = subject()
        reversed_keys = dict(reversed(list(original.items())))
        self.assertEqual(release.prepare(original), release.prepare(reversed_keys))
        for key, value in [("source_sha", "0" * 40), ("policy_sha256", "0" * 64)]:
            altered = copy.deepcopy(original)
            altered[key] = value
            self.assertNotEqual(release.digest(original), release.digest(altered))

    def test_tampering_and_self_consistent_substitution_are_rejected(self):
        manifest = release.prepare(subject())
        trusted = manifest["release_id"]
        manifest["subject"]["artifacts"][0]["artifact_id"] = "substitute"
        with self.assertRaises(release.ManifestError):
            release.validate(manifest)
        manifest["release_id"] = release.digest(manifest["subject"])
        with self.assertRaises(release.ManifestError):
            release.validate(manifest, expected_release_id=trusted)

    def test_new_attempt_configuration_and_recovery_each_change_identity(self):
        original = subject()
        cases = []
        for node, key, value in [("preparation", "run_attempt", 2),
                                 ("qualification", "run_attempt", 3),
                                 ("recovery", "owner", "different-owner")]:
            changed = copy.deepcopy(original)
            changed[node][key] = value
            cases.append(changed)
        changed = copy.deepcopy(original)
        changed["artifacts"][0]["config_sha256"] = "0" * 64
        cases.append(changed)
        for changed in cases:
            self.assertNotEqual(release.prepare(original)["release_id"], release.prepare(changed)["release_id"])

    def test_qualification_binds_actual_candidate_and_schema_context(self):
        for field in ["source_sha", "schema_source_sha"]:
            changed = subject()
            changed["qualification"][field] = "0" * 40
            with self.assertRaises(release.ManifestError):
                release.prepare(changed)

    def test_qualification_digest_matches_single_and_mirrored_graphs(self):
        value = subject()
        value["qualification"]["migration_sha256"] = "0" * 64
        with self.assertRaises(release.ManifestError):
            release.prepare(value)
        value = subject()
        mirror = copy.deepcopy(value["databases"][0])
        mirror["id"] = "mirror"
        value["databases"].append(mirror)
        value["artifacts"][0]["databases"].append("mirror")
        release.prepare(value)
        mirror["migration_sha256"] = "0" * 64
        with self.assertRaises(release.ManifestError):
            release.prepare(value)

    def test_bad_ledger_graph_and_unknown_targets_fail(self):
        cases = []
        value = subject()
        value["databases"][0]["pending"] = ["001"]
        cases.append(value)
        value = subject()
        value["databases"][0]["expected_after"] = ["001"]
        cases.append(value)
        value = subject()
        value["artifacts"][0]["databases"] = ["unknown"]
        cases.append(value)
        value = subject()
        value["artifacts"].append(copy.deepcopy(value["artifacts"][0]))
        cases.append(value)
        for value in cases:
            with self.assertRaises(release.ManifestError):
                release.prepare(value)

    def test_no_database_automatic_image_profile_preserves_variation(self):
        value = subject(database=False, provider="fly")
        value["decision_sequence"] = []
        value["artifacts"][0]["artifact_id"] = "registry.example/app@sha256:" + "a" * 64
        self.assertEqual(release.prepare(value)["subject"]["databases"], [])

    def test_schema_rejects_extra_fields_nan_and_wrong_scalar_types(self):
        for mutate in [lambda s: s.update(approval="self-approved"),
                       lambda s: s["preparation"].update(run_id=True),
                       lambda s: s["timing"].update(intermediate_seconds=float("nan")),
                       lambda s: s["artifacts"][0].update(artifact_id="line\nbreak")]:
            value = subject()
            mutate(value)
            with self.assertRaises(release.ManifestError):
                release.prepare(value)

    def test_deadlines_cannot_be_reordered_or_malformed(self):
        for stamp in ["2026-10-31T23:00:00Z", "not-a-time", "2026-11-01T01:00:00+01:00"]:
            value = subject()
            value["timing"]["approval_deadline"] = stamp
            with self.assertRaises(release.ManifestError):
                release.prepare(value)

    def test_duplicate_json_key_is_not_silently_reinterpreted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"schema_version": 1, "schema_version": 2}')
            with self.assertRaises(release.ManifestError):
                release.load_json(path)

    def test_composite_cli_only_outputs_identity_and_never_authority(self):
        manifest = release.prepare(subject())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            output = Path(directory) / "outputs"
            path.write_text(json.dumps(manifest))
            argv = [sys.executable, str(ACTION / "release_manifest.py"), "--manifest", str(path),
                    "--expected-release-id", manifest["release_id"]]
            env = {**os.environ, "GITHUB_OUTPUT": str(output)}
            result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("authority=none", result.stdout)
            self.assertEqual(output.read_text(), "release_id=" + manifest["release_id"] + "\n")
            output.unlink()
            result = subprocess.run(argv + ["--dry-run"], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(output.exists())


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.parent = release.prepare(subject())
        self.evidence = {"release_id": self.parent["release_id"], "stage": "schema", "status": "success",
                         "completed_at": "2026-11-01T01:00:00Z", "databases": {"primary": ["001", "002"]}}
        self.current = {"policy_sha256": "d" * 64, "artifacts": {"web": "previous"},
                        "databases": {"primary": ["001", "002"]}}
        self.arguments = {"schema_evidence": self.evidence, "current": self.current,
                          "expected_evidence_sha256": release.digest(self.evidence),
                          "timing": {"selected_at": "2026-11-01T01:10:00Z",
                                     "approval_deadline": "2026-11-01T01:20:00Z",
                                     "execute_before": "2026-11-01T01:50:00Z", "intermediate_seconds": 3600},
                          "preparation": {"run_id": 12, "run_attempt": 1, "job": "prepare-continuation"},
                          "admission_revision": "old-shared-state-sha", "admitted_continuation": None}

    def test_activation_only_is_new_subject_empty_sql_and_explicit_cas_proposal(self):
        prepared = release.activation_only(self.parent, **self.arguments)
        child = prepared["manifest"]
        self.assertNotEqual(child["release_id"], self.parent["release_id"])
        self.assertEqual(child["subject"]["kind"], "activation-only")
        self.assertEqual(child["subject"]["databases"][0]["pending"], [])
        self.assertEqual(child["subject"]["databases"][0]["baseline"], ["001", "002"])
        self.assertEqual(child["subject"]["decision_sequence"], ["activation"])
        self.assertEqual(prepared["continuation_key"], self.parent["release_id"])
        self.assertEqual(prepared["expected_admission_revision"], "old-shared-state-sha")
        self.assertEqual(self.parent["subject"]["databases"][0]["pending"], ["002"])

    def test_changed_schema_artifact_policy_or_evidence_holds(self):
        for mutate in [lambda a: a["current"]["databases"].update(primary=["001", "002", "003"]),
                       lambda a: a["current"]["artifacts"].update(web="newer-live"),
                       lambda a: a["current"].update(policy_sha256="0" * 64),
                       lambda a: a.update(expected_evidence_sha256="0" * 64)]:
            args = copy.deepcopy(self.arguments)
            mutate(args)
            with self.assertRaises(release.ManifestError):
                release.activation_only(self.parent, **args)

    def test_combined_release_continuation_preserves_installed_decision_scope(self):
        parent_subject = copy.deepcopy(self.parent["subject"])
        parent_subject["decision_sequence"] = ["release"]
        parent = release.prepare(parent_subject)
        evidence = {**self.evidence, "release_id": parent["release_id"]}
        args = {**self.arguments, "schema_evidence": evidence,
                "expected_evidence_sha256": release.digest(evidence)}
        child = release.activation_only(parent, **args)["manifest"]
        self.assertEqual(child["subject"]["decision_sequence"], ["release"])
        self.assertEqual(child["subject"]["policy_sha256"], parent["subject"]["policy_sha256"])
        self.assertEqual(child["subject"]["databases"][0]["pending"], [])

    def test_duplicate_or_unfenced_continuation_is_not_admitted(self):
        for changes in [{"admission_revision": ""}, {"admitted_continuation": "existing-release"}]:
            with self.assertRaises(release.ManifestError):
                release.activation_only(self.parent, **{**self.arguments, **changes})

    def test_retry_cannot_extend_the_intermediate_window(self):
        self.arguments["timing"]["execute_before"] = "2026-11-01T04:00:00Z"
        with self.assertRaises(release.ManifestError):
            release.activation_only(self.parent, **self.arguments)

    def test_activation_only_cannot_hide_pending_sql(self):
        value = subject()
        value.update(kind="activation-only", continuation={"parent_release_id": "0" * 64,
                                                          "schema_evidence_sha256": "1" * 64})
        with self.assertRaises(release.ManifestError):
            release.prepare(value)


class AdmissionStateTests(unittest.TestCase):
    def test_newer_release_cannot_interleave_with_any_unresolved_phase(self):
        proposal = release.prepare(subject())
        for phase in ["selected", "schema-running", "schema-partial", "schema-applied",
                      "activation-held", "activation-running", "activation-unverified", "failed-unverified"]:
            retained = {"schema_version": 1, "active": {"release_id": "0" * 64, "phase": phase},
                        "continuations": {}}
            with self.subTest(phase=phase), self.assertRaises(state.StateConflict):
                state.reserve(retained, proposal, now=ADMISSION_NOW)

    def test_terminal_release_allows_later_subject(self):
        proposal = release.prepare(subject())
        for phase in ["success", "failed-before-writes", "recovered"]:
            retained = {"schema_version": 1, "active": {"release_id": "0" * 64, "phase": phase},
                        "continuations": {}}
            self.assertEqual(state.reserve(retained, proposal, now=ADMISSION_NOW)["active"]["release_id"], proposal["release_id"])

    def test_exactly_one_continuation_can_claim_predecessor(self):
        fixture = ContinuationTests()
        fixture.setUp()
        child = release.activation_only(fixture.parent, **fixture.arguments)["manifest"]
        retained = {"schema_version": 1, "active": {"release_id": fixture.parent["release_id"],
                                                    "phase": "activation-held"}, "continuations": {}}
        reserved = state.reserve(retained, child, now=ADMISSION_NOW)
        self.assertEqual(reserved["continuations"][fixture.parent["release_id"]], child["release_id"])
        with self.assertRaises(state.StateConflict):
            state.reserve(reserved, child, now=ADMISSION_NOW)
        reserved["active"] = retained["active"]
        with self.assertRaises(state.StateConflict):
            state.reserve(reserved, child, now=ADMISSION_NOW)

    def test_state_store_uses_verified_parent_and_git_wire_publication(self):
        from unittest.mock import Mock
        store = state.GitHubStateStore("example/product", "production", "fixture-token")
        store.request = Mock(return_value={"object": {"sha": "a" * 40}})
        store._write_state = Mock(return_value="d" * 40)
        retained = {"schema_version": 1, "active": None, "continuations": {}}
        self.assertEqual(store.compare_and_swap("a" * 40, retained), "d" * 40)
        store.request.assert_called_once_with("GET", "git/ref/release-state/production", missing=True)
        self.assertEqual(store._write_state.call_args.args[0], "a" * 40)
        self.assertEqual(json.loads(store._write_state.call_args.args[1]), retained)
        store._write_state.reset_mock()
        store.request.return_value = {"object": {"sha": "e" * 40}}
        with self.assertRaises(state.StateConflict):
            store.compare_and_swap("a" * 40, retained)
        store._write_state.assert_not_called()

    def test_ambiguous_git_publication_does_not_grant_reservation(self):
        from unittest.mock import Mock
        store = state.GitHubStateStore("example/product", "production", "fixture-token")
        store.request = Mock(return_value={"object": {"sha": "a" * 40}})
        store._write_state = Mock(side_effect=state.StateUnavailable("unknown write result"))
        with self.assertRaises(state.StateUnavailable):
            store.compare_and_swap("a" * 40, {"schema_version": 1, "active": None, "continuations": {}})
        self.assertEqual(store._write_state.call_count, 1)

    def test_denied_state_read_never_becomes_empty_legacy_state(self):
        from unittest.mock import Mock
        store = state.GitHubStateStore("example/product", "production", "fixture-token")
        store.request = Mock(side_effect=state.StateUnavailable("HTTP 403"))
        with self.assertRaises(state.StateUnavailable):
            store.read()

    def test_real_transport_404_is_not_empty_runtime_state(self):
        from unittest.mock import patch
        from urllib.error import HTTPError
        store = state.GitHubStateStore("example/product", "production", "fixture-token")
        with patch.object(state, "build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://api.github.com", 404, "concealed", {}, None)
            with self.assertRaises(state.StateUnavailable):
                store.read()
            self.assertEqual(opener.return_value.open.call_count, 1)
        with self.assertRaises(state.StateUnavailable):
            store.compare_and_swap(None, {"schema_version": 1, "active": None, "continuations": {}})


class ApprovalCapabilityTests(unittest.TestCase):
    def test_real_review_history_shape_does_not_claim_manifest_approval(self):
        history = [{"comment": "Ship it", "state": "approved", "user": {"id": 1, "type": "User"},
                    "environments": [{"id": 2, "name": "Production"}]}]
        result = approval.classify_history(history)
        self.assertEqual(result["status"], "unsupported")
        self.assertIs(result["execution_authorized"], False)
        history[0].update(subject_digest="a" * 64, decision_time="2026-11-01T00:00:00Z")
        self.assertIs(approval.classify_history(history)["execution_authorized"], False)

    def test_denial_and_redirect_do_not_rotate_identity_or_fall_back(self):
        from unittest.mock import patch
        from urllib.error import HTTPError
        for code in [302, 401, 403, 404]:
            with self.subTest(code=code), patch.object(approval, "build_opener") as opener:
                opener.return_value.open.side_effect = HTTPError("https://api.github.com", code, "refused", {}, None)
                result = approval.observe("example/product", 1, "fixture-token")
                self.assertEqual(result["status"], "blocked")
                self.assertIs(result["execution_authorized"], False)
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_git_publication_binds_lease_and_keeps_token_out_of_argv(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            output = "a" * 40 if argv[3] in {"hash-object", "mktree", "commit-tree"} else ""
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        store = state.GitHubStateStore("example/product", "production", "fixture-token")
        with patch.object(state.subprocess, "run", side_effect=run):
            store._write_state("b" * 40, "{}")
            store._write_state("b" * 40, "{}")
        push = calls[-1]
        self.assertIn("--force-with-lease=refs/release-state/production:" + "b" * 40, push[0])
        self.assertIn("--atomic", push[0])
        self.assertNotIn("fixture-token", " ".join(push[0]))
        self.assertEqual(push[1]["env"]["GIT_CONFIG_KEY_2"], "credential.helper")
        self.assertEqual(push[1]["env"]["GIT_CONFIG_VALUE_2"], "")
        messages = [options['input'] for argv, options in calls if argv[3] == 'commit-tree']
        self.assertNotEqual(messages[0], messages[1])

    def test_native_wire_conflict_requires_exact_ref_expected_old_and_different_live_oid(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        ref = 'refs/release-state/production'
        expected, actual, proposed = 'b' * 40, 'c' * 40, 'a' * 40
        message = f"[remote rejected] (cannot lock ref '{ref}': is at {actual} but expected {expected})"
        cases = [(message, ref, state.StateConflict),
                 ('[rejected] (stale info)', ref, state.StateConflict),
                 (message.replace(ref, ref + '-other'), ref, state.StateUnavailable),
                 (message.replace(actual, expected), ref, state.StateUnavailable),
                 (message.replace('expected ' + expected, 'expected ' + actual), ref, state.StateUnavailable),
                 (message.replace(actual, 'invalid'), ref, state.StateUnavailable),
                 (message, ref + '-other', state.StateUnavailable),
                 ('[remote rejected] (atomic transaction failed)', ref, state.StateUnavailable),
                 ('[remote rejected] (permission denied)', ref, state.StateUnavailable),
                 ('unexpected [rejected] (stale info)', ref, state.StateUnavailable)]
        for status, reported_ref, error in cases:
            def run(argv, **kwargs):
                if argv[3] == 'push':
                    return SimpleNamespace(returncode=1, stderr='unavailable',
                        stdout=f'To https://github.com/example/product.git\n!\t{proposed}:{reported_ref}\t{status}\nDone\n')
                return SimpleNamespace(returncode=0, stdout=proposed, stderr='')
            with self.subTest(status=status, reported_ref=reported_ref), patch.object(state.subprocess, 'run', side_effect=run):
                store = state.GitHubStateStore('example/product', 'production', 'fixture-token')
                with self.assertRaises(error):
                    store._write_state(expected, '{}')

    def test_native_wire_timeout_still_requires_reconciliation(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        def run(argv, **kwargs):
            if argv[3] == 'push':
                raise subprocess.TimeoutExpired(argv, 30)
            return SimpleNamespace(returncode=0, stdout='a' * 40, stderr='')
        with patch.object(state.subprocess, 'run', side_effect=run):
            store = state.GitHubStateStore('example/product', 'production', 'fixture-token')
            with self.assertRaises(state.StateUnavailable):
                store._write_state('b' * 40, '{}')




class AdmissionClockTests(unittest.TestCase):
    def test_expired_or_naive_clock_scope_cannot_be_selected(self):
        proposal = release.prepare(subject())
        retained = {"schema_version": 1, "active": None, "continuations": {}}
        for now in [datetime(2026, 11, 1, 2, tzinfo=timezone.utc),
                    datetime(2026, 11, 2, tzinfo=timezone.utc)]:
            with self.subTest(now=now), self.assertRaises(state.StateConflict):
                state.reserve(retained, proposal, now=now)
        with self.assertRaises(state.StateUnavailable):
            state.reserve(retained, proposal, now=datetime(2026, 11, 1))
        self.assertEqual(retained, {"schema_version": 1, "active": None, "continuations": {}})

    def test_unexpired_scope_can_be_proposed_without_execution_authority(self):
        proposal = release.prepare(subject())
        retained = {"schema_version": 1, "active": None, "continuations": {}}
        result = state.reserve(retained, proposal, now=ADMISSION_NOW)
        self.assertEqual(result["active"], {"release_id": proposal["release_id"], "phase": "selected"})
        self.assertIsNone(retained["active"])


if __name__ == "__main__":
    unittest.main()
