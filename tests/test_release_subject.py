"""Exact release scope and rejection/invalidation regression tests."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lib.release_subject import SCHEMA, compare_approval, release_id, stage_key, validate

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def subject():
    return {"schema": SCHEMA, "repository": "sample/app", "target": "production",
            "source_sha": "a" * 40, "artifact_digest": "sha256:" + "b" * 64,
            "migrations": [{"id": "0001", "digest": "sha256:" + "c" * 64}],
            "compatibility_order": ["migrations", "artifact", "verification"],
            "verification": ["health", "tenant-isolation"],
            "recovery": {"action": "restore-artifact", "artifact_digest": "sha256:" + "d" * 64,
                         "migrations": [], "verification": ["health"]},
            "policy_sha": "e" * 40, "workflow_sha": "f" * 40}


def approval(value):
    return dict(instance_id="approval-1", release_id=release_id(value), decision="approved",
                actor="owner", issued_at="2026-10-23T11:00:00Z", expires_at="2026-10-23T13:00:00Z")


class ReleaseSubjectTests(unittest.TestCase):
    def test_identity_is_stable_across_mapping_order(self):
        value = subject()
        self.assertEqual(release_id(value), release_id(dict(reversed(list(value.items())))))

    def test_every_production_and_recovery_field_invalidates_approval(self):
        base = subject()
        variations = [dict(repository="sample/other"), dict(target="another-target"),
                      dict(source_sha="1" * 40), dict(artifact_digest="sha256:" + "2" * 64),
                      dict(migrations=[]), dict(compatibility_order=["artifact", "migrations", "verification"]),
                      dict(verification=["other-check"]), dict(policy_sha="3" * 40), dict(workflow_sha="4" * 40)]
        for recovery_field, replacement in [("action", "roll-forward"), ("artifact_digest", "sha256:" + "5" * 64),
                                             ("migrations", [{"id": "fix", "digest": "sha256:" + "6" * 64}]),
                                             ("verification", ["another-check"])]:
            recovery = copy.deepcopy(base["recovery"])
            recovery[recovery_field] = replacement
            variations.append(dict(recovery=recovery))
        for change in variations:
            with self.subTest(change=change):
                changed = dict(base, **change)
                self.assertEqual(compare_approval(changed, approval(base), now=NOW)["status"], "candidate-changed")

    def test_rejection_cancellation_expiry_and_future_approval_hold(self):
        value = subject()
        original = approval(value)
        for change, expected in [(dict(decision="rejected"), "decision-held"),
                                 (dict(decision="cancelled"), "decision-held"),
                                 (dict(issued_at="2026-10-23T09:00:00Z", expires_at="2026-10-23T12:00:00Z"), "expired"),
                                 (dict(issued_at="2026-10-23T12:01:00Z"), "not-yet-valid")]:
            with self.subTest(change=change):
                result = compare_approval(value, dict(original, **change), now=NOW)
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["execution_authorized"])

    def test_matching_local_document_cannot_authorize_execution(self):
        value = subject()
        result = compare_approval(value, approval(value), now=NOW)
        self.assertEqual(result["status"], "scope-matches")
        self.assertTrue(result["native_authority_required"])
        self.assertFalse(result["execution_authorized"])

    def test_missing_unknown_and_mutable_identity_fields_are_rejected(self):
        value = subject()
        for field in value:
            with self.subTest(field=field):
                incomplete = dict(value)
                incomplete.pop(field)
                with self.assertRaises(ValueError):
                    validate(incomplete)
        for change in [dict(extra="cannot-ignore"), dict(source_sha="main"), dict(artifact_digest="latest"),
                       dict(verification=[]), dict(compatibility_order=["artifact", "verification"])]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(dict(value, **change))

    def test_migration_content_order_and_recovery_are_bound(self):
        value = subject()
        other = copy.deepcopy(value)
        other["migrations"][0]["digest"] = "sha256:" + "9" * 64
        self.assertNotEqual(release_id(value), release_id(other))
        other["migrations"].append(dict(other["migrations"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate(other)
        other = copy.deepcopy(value)
        other["recovery"]["action"] = "none"
        with self.assertRaisesRegex(ValueError, "undeclared recovery"):
            validate(other)

    def test_idempotency_distinguishes_approval_instances_and_stages(self):
        value = subject()
        key = stage_key(value, "approval-1", "verification")
        self.assertEqual(key, stage_key(value, "approval-1", "verification"))
        self.assertNotEqual(key, stage_key(value, "approval-2", "verification"))
        self.assertNotEqual(key, stage_key(value, "approval-1", "recovery"))


if __name__ == "__main__":
    unittest.main()
