"""Portable release semantics, including installed-client and activation failures."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("coordinated_release", ROOT / ".github/actions/qualify-coordinated-release/validate.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def bundle():
    subject = {
        "entity": "example", "release_group": "portal", "target": "production",
        "sources": {"backend": "a" * 40, "web": "b" * 40},
        "artifacts": {"web": "c" * 64}, "migration_digest": "d" * 64,
        "policy_digest": "e" * 64, "client_policy_digest": "f" * 64,
        "baseline": {"web": "deployment_previous"},
        "recovery": {"mode": "fix-forward", "artifact_digest": None, "migration_digest": None},
    }
    clients = ["web:installed", "ios:older", "ios:candidate"]
    return {
        "schema_version": 1, "subject": subject, "supported_clients": clients,
        "pending_migrations": [{"id": "202610220001_expand", "sha256": "1" * 64,
                                "effect": "schema-preparation", "feature": None}],
        "features": [], "required_checks": ["contracts"],
        "evidence": {"subject": copy.deepcopy(subject), "run_id": 123, "run_attempt": 1,
                     "completed_at": NOW.isoformat(), "ready_components": {},
                     "checks": [{"id": "contracts", "result": "success", "clients": clients.copy()}]},
    }


def feature(data, *, activate=True, active=False, requires=None):
    data["features"] = [{"id": "messages", "active": active, "activate": activate,
                         "requires": requires or [], "client_policy": "all-supported",
                         "required_checks": ["push-route", "badge"]}]
    data["evidence"]["checks"] += [{"id": name, "result": "success", "clients": data["supported_clients"].copy()}
                                   for name in ("push-route", "badge")]


class CoordinatedReleaseTests(unittest.TestCase):
    def validate(self, data, phase="qualify", subject=None, *, seal=True, approved=None):
        if seal:
            data["evidence"]["release_id"] = release.release_identity(data)
        return release.validate(data, subject or data["subject"], phase, now=NOW,
                                approved_release_id=approved or release.release_identity(data))

    def test_inert_database_preparation_is_independent_of_app_readiness(self):
        result = self.validate(bundle(), "prepare")
        self.assertTrue(result["qualified"])
        self.assertFalse(result["approval_granted"])

    def test_no_database_and_azure_revision_variations_use_same_contract(self):
        for sources, artifacts in (({"worker": "a" * 40}, {"image": "c" * 64}),
                                   ({"api": "a" * 40, "portal": "b" * 40}, {"revision": "c" * 64})):
            with self.subTest(components=list(sources)):
                data = bundle()
                data["subject"].update(sources=sources, artifacts=artifacts)
                data["evidence"]["subject"] = copy.deepcopy(data["subject"])
                data["pending_migrations"] = []
                self.validate(data, "prepare")

    def test_new_behavior_requires_every_installed_cohort_even_with_new_client_ready(self):
        data = bundle()
        feature(data)
        data["evidence"]["ready_components"] = {"web": data["subject"]["sources"]["web"]}
        for result, covered in (("failure", data["supported_clients"]), ("skipped", data["supported_clients"]),
                                ("success", ["web:installed", "ios:candidate"])):
            with self.subTest(result=result, covered=covered), self.assertRaises(release.Held):
                data["evidence"]["checks"][-1].update(result=result, clients=covered)
                self.validate(data)

    def test_already_active_feature_needs_behavior_evidence_too(self):
        data = bundle()
        feature(data, activate=False, active=True)
        data["evidence"]["checks"][-1]["result"] = "failure"
        with self.assertRaises(release.Held):
            self.validate(data)

    def test_activation_sql_cannot_hide_in_schema_preparation(self):
        data = bundle()
        feature(data)
        data["pending_migrations"][0].update(effect="feature-activation", feature="messages")
        self.validate(data)
        with self.assertRaisesRegex(release.Held, "separated"):
            self.validate(data, "prepare")
        self.validate(data, "activate")

    def test_behavior_dependent_on_new_app_cannot_run_in_preparation(self):
        data = bundle()
        feature(data, requires=["web"])
        data["pending_migrations"][0].update(effect="behavior-change", feature="messages")
        with self.assertRaisesRegex(release.Held, "readiness"):
            self.validate(data, "prepare")

    def test_activation_waits_for_exact_dependent_components(self):
        data = bundle()
        feature(data, requires=["web"])
        with self.assertRaisesRegex(release.Held, "readiness"):
            self.validate(data, "activate")
        data["evidence"]["ready_components"] = {"web": "9" * 40}
        with self.assertRaises(release.Held):
            self.validate(data, "activate")
        data["evidence"]["ready_components"] = {"web": data["subject"]["sources"]["web"]}
        self.validate(data, "activate")

    def test_active_feature_behavior_change_also_waits_for_dependent_app(self):
        data = bundle()
        feature(data, activate=False, active=True, requires=["web"])
        data["pending_migrations"][0].update(effect="behavior-change", feature="messages")
        with self.assertRaisesRegex(release.Held, "readiness"):
            self.validate(data, "activate")
        data["evidence"]["ready_components"] = {"web": data["subject"]["sources"]["web"]}
        self.validate(data, "activate")

    def test_evidence_and_approval_bind_cohorts_classification_checks_and_intent(self):
        original = bundle()
        self.validate(original)
        approved = release.release_identity(original)
        for field, value in (("supported_clients", ["ios:candidate"]), ("required_checks", ["different"]),
                             ("pending_migrations", []), ("features", [{"id": "new"}])):
            data = copy.deepcopy(original)
            data[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(release.Held, "another release plan"):
                self.validate(data, seal=False)
        data = copy.deepcopy(original)
        data["pending_migrations"] = []
        data["evidence"]["release_id"] = release.release_identity(data)
        with self.assertRaisesRegex(release.Held, "approved release identity"):
            self.validate(data, "prepare", seal=False, approved=approved)

    def test_version_policy_requires_enforcement_and_unknown_client_tests(self):
        for mode in ("verified-targeting", "verified-minimum"):
            data = bundle()
            feature(data)
            data["features"][0]["client_policy"] = mode
            with self.assertRaises(release.Held):
                self.validate(data)
            for check_id in ("messages:version-policy", "messages:unknown-client-safe"):
                data["evidence"]["checks"].append({"id": check_id, "result": "success", "clients": data["supported_clients"]})
            self.validate(data)

    def test_unclassified_unknown_duplicate_and_unapproved_changes_hold(self):
        for key, value in (("effect", "unreviewed"), ("effect", "behavior-change"),
                           ("feature", "messages"), ("sha256", "latest")):
            data = bundle()
            data["pending_migrations"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(release.Held):
                self.validate(data)
        data = bundle()
        data["pending_migrations"] *= 2
        with self.assertRaises(release.Held):
            self.validate(data)

    def test_exact_scope_policy_artifact_baseline_and_recovery_are_bound(self):
        for key in ("entity", "target", "policy_digest", "client_policy_digest", "migration_digest", "sources", "artifacts", "baseline", "recovery"):
            data = bundle()
            expected = copy.deepcopy(data["subject"])
            if isinstance(expected[key], str):
                expected[key] = "9" * 64 if key.endswith("digest") else "other"
            elif key == "recovery":
                expected[key]["mode"] = "none"
            else:
                expected[key] = {}
            with self.subTest(key=key), self.assertRaises(release.Held):
                self.validate(data, subject=expected)

    def test_stale_future_duplicate_evidence_and_boolean_versions_hold(self):
        for delta in (timedelta(hours=-25), timedelta(minutes=6)):
            data = bundle()
            data["evidence"]["completed_at"] = (NOW + delta).isoformat()
            with self.assertRaises(release.Held):
                self.validate(data)
        for key in ("run_id", "run_attempt"):
            data = bundle()
            data["evidence"][key] = True
            with self.assertRaises(release.Held):
                self.validate(data)
        data = bundle()
        data["evidence"]["checks"] *= 2
        with self.assertRaises(release.Held):
            self.validate(data)
        for text in ('{"a":1,"a":2}', '{"a":NaN}', '[]'):
            with self.assertRaises(release.Held):
                release.strict_json(text)

    def test_release_identity_changes_with_intent_but_not_requalification(self):
        data = bundle()
        identity = release.release_identity(data)
        data["evidence"]["run_attempt"] = 2
        self.assertEqual(identity, release.release_identity(data))
        feature(data)
        self.assertNotEqual(identity, release.release_identity(data))

    def test_cli_dry_run_is_read_only(self):
        data = bundle()
        data["evidence"]["completed_at"] = datetime.now(timezone.utc).isoformat()
        data["evidence"]["release_id"] = release.release_identity(data)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bundle.json").write_text(json.dumps(data))
            (root / "subject.json").write_text(json.dumps(data["subject"]))
            self.assertEqual(release.main(["--bundle", str(root / "bundle.json"), "--expected-subject", str(root / "subject.json"),
                                           "--phase", "prepare", "--approved-release-id", data["evidence"]["release_id"],
                                           "--output", str(root / "result.json"), "--dry-run"]), 0)
            self.assertFalse((root / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
