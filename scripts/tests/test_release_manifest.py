#!/usr/bin/env python3
"""Exercise actual manifest/action seams with synthetic product/provider inputs."""
import copy
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
                state.reserve(retained, proposal)

    def test_terminal_release_allows_later_subject(self):
        proposal = release.prepare(subject())
        for phase in ["success", "failed-before-writes", "recovered"]:
            retained = {"schema_version": 1, "active": {"release_id": "0" * 64, "phase": phase},
                        "continuations": {}}
            self.assertEqual(state.reserve(retained, proposal)["active"]["release_id"], proposal["release_id"])

    def test_exactly_one_continuation_can_claim_predecessor(self):
        fixture = ContinuationTests()
        fixture.setUp()
        child = release.activation_only(fixture.parent, **fixture.arguments)["manifest"]
        retained = {"schema_version": 1, "active": {"release_id": fixture.parent["release_id"],
                                                    "phase": "activation-held"}, "continuations": {}}
        reserved = state.reserve(retained, child)
        self.assertEqual(reserved["continuations"][fixture.parent["release_id"]], child["release_id"])
        with self.assertRaises(state.StateConflict):
            state.reserve(reserved, child)
        reserved["active"] = retained["active"]
        with self.assertRaises(state.StateConflict):
            state.reserve(reserved, child)

    def test_state_store_wire_uses_custom_ref_and_non_force_cas(self):
        from unittest.mock import Mock
        store = state.GitHubStateStore("example/product", "production", "fixture-token")
        store.request = Mock(side_effect=[{"object": {"sha": "a" * 40}},
                                          {"sha": "b" * 40}, {"sha": "c" * 40},
                                          {"sha": "d" * 40}, {"ref": "refs/release-state/production", "object": {"sha": "d" * 40}}])
        retained = {"schema_version": 1, "active": None, "continuations": {}}
        self.assertEqual(store.compare_and_swap("a" * 40, retained), "d" * 40)
        commit_call = store.request.call_args_list[3]
        self.assertEqual(commit_call.args[2]["parents"], ["a" * 40])
        last = store.request.call_args_list[-1]
        self.assertEqual(last.args, ("PATCH", "git/refs/release-state/production",
                                    {"sha": "d" * 40, "force": False}))
        store.request.reset_mock(side_effect=True)
        store.request.side_effect = [{"object": {"sha": "e" * 40}}]
        with self.assertRaises(state.StateConflict):
            store.compare_and_swap("a" * 40, retained)
        self.assertEqual(store.request.call_count, 1)

    def test_two_cas_children_cannot_both_commit(self):
        from unittest.mock import Mock
        store = state.GitHubStateStore("example/product", "production", "fixture-token")
        store.request = Mock(side_effect=[{"object": {"sha": "a" * 40}},
                                          {"sha": "b" * 40}, {"sha": "c" * 40},
                                          {"sha": "d" * 40}, state.StateConflict("non-fast-forward")])
        with self.assertRaises(state.StateConflict):
            store.compare_and_swap("a" * 40, {"schema_version": 1, "active": None, "continuations": {}})

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

    def test_state_commit_nonce_prevents_identical_concurrent_child_identity(self):
        from unittest.mock import Mock
        messages = []
        store = state.GitHubStateStore("example/product", "production", "fixture-token")
        for _ in range(2):
            store.request = Mock(side_effect=[{"object": {"sha": "a" * 40}},
                                              {"sha": "b" * 40}, {"sha": "c" * 40},
                                              {"sha": "d" * 40}, {"ref": "refs/release-state/production", "object": {"sha": "d" * 40}}])
            store.compare_and_swap("a" * 40, {"schema_version": 1, "active": None, "continuations": {}})
            messages.append(store.request.call_args_list[3].args[2]["message"])
        self.assertNotEqual(messages[0], messages[1])


if __name__ == "__main__":
    unittest.main()
