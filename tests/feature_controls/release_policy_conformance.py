import hashlib
import hmac
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from feature_controls import (FeatureRuntime, approval_message, assess_release, compile_release_payload,
                              kill_message, prepare_deployment_payload, release_catalog_features,
                              release_manifest_message, validate_release_manifest)
from tests.feature_controls.provider_conformance import Controls, GOLDEN, NOW, bundle, kill

MANIFEST = json.loads(Path(__file__).with_name("release-readiness.json").read_text())
SOURCE_A, SOURCE_B = "1" * 40, "2" * 40


def empty_provider():
    return {"features": {}, "experiments": [], "savedGroups": {}, "contextualBandits": {}}


def catalog(manifest=MANIFEST):
    return {"schema_version": 1, "scope": deepcopy(GOLDEN["scope"]), "applications": ["example-app"],
            "revision": 1, "features": release_catalog_features(manifest)}


def release(manifest=MANIFEST, revision=1, source=SOURCE_A, payload=None):
    result = bundle(compile_release_payload(manifest, empty_provider()) if payload is None else payload)
    result["revision"] = revision
    result["deployment"] = {"source_sha": source, "manifest": deepcopy(manifest)}
    return result


def setup(options=None, manifest=MANIFEST, source=SOURCE_A):
    options = options or {}
    controls = options.pop("controls", None) or Controls()
    runtime = FeatureRuntime(catalog(manifest), trust_policy="local-test", controls=controls,
                             release_manifest=manifest, release_source_sha=source, **options)
    runtime.activate(release(manifest, 1, source), 0, NOW)
    runtime.update_kills(kill(), "local-fixture", NOW)
    return runtime, controls


def snapshot(runtime, now=NOW):
    return runtime.snapshot(list(MANIFEST["features"]), "web", GOLDEN["context"], now=now)


def probe():
    runtime, controls = setup()
    initial = assess_release(MANIFEST, GOLDEN["scope"], controls.read(GOLDEN["scope"]), [snapshot(runtime)], NOW, SOURCE_A, SOURCE_A)
    disabled = {**kill(2, ["checkout"]), "exceptions": {
        "checkout": {"owner": "team.incident", "reason": "Synthetic incident investigation", "review_at": 90000}}}
    runtime.update_kills(disabled, "local-fixture", NOW)
    next_manifest = deepcopy(MANIFEST)
    next_manifest["features"]["next-checkout"]["state"] = "ready"
    next_manifest["features"]["next-checkout"]["acceptance"] = ["tests/feature_controls/release-policy.test.mjs"]
    previous = controls.read(GOLDEN["scope"])["bundle"]
    payload = prepare_deployment_payload(next_manifest, empty_provider(), previous, NOW)
    updated = FeatureRuntime(catalog(next_manifest), trust_policy="local-test", controls=controls,
                             release_manifest=next_manifest, release_source_sha=SOURCE_B)
    published = release(next_manifest, 2, SOURCE_B, payload)
    updated.activate(published, 1, NOW)
    after = assess_release(next_manifest, GOLDEN["scope"], controls.read(GOLDEN["scope"]), [snapshot(updated)], NOW, SOURCE_B, SOURCE_B)
    return {"initial": initial, "after": after, "kills_preserved": controls.read(GOLDEN["scope"])["kill"] == disabled,
            "kill_bytes": kill_message(disabled).decode(), "approval_bytes": approval_message(published).decode(),
            "manifest_bytes": release_manifest_message(next_manifest).decode()}


def test_auto_defaults_and_persistent_deliberate_off_have_cross_language_parity():
    result = probe()
    assert result["initial"]["status"] == result["after"]["status"] == "passed"
    assert [(row["actual"], row["status"]) for row in result["initial"]["rows"]] == [(True, "ready_on"), (False, "development_off")]
    assert [(row["actual"], row["status"]) for row in result["after"]["rows"]] == [(False, "deliberately_disabled"), (True, "ready_on")]
    assert result["kills_preserved"]
    process = subprocess.run(["node", "tests/feature_controls/release-readiness-parity.mjs"], cwd=Path(__file__).resolve().parents[2],
                             check=True, text=True, capture_output=True, timeout=60)
    assert result == json.loads(process.stdout)


def test_unexplained_off_and_overdue_or_undocumented_exceptions_fail_qualification():
    runtime, controls = setup({"local_disabled": ["checkout"]})
    report = assess_release(MANIFEST, GOLDEN["scope"], controls.read(GOLDEN["scope"]), [snapshot(runtime)], NOW, SOURCE_A, SOURCE_A)
    assert report["status"] == "failed" and report["rows"][0]["status"] == "unexpected_off"
    for exception in (None, {"owner": "team.ops", "reason": "Synthetic hold", "review_at": 1001}):
        runtime, controls = setup()
        disabled = kill(2, ["checkout"])
        if exception is not None:
            disabled["exceptions"] = {"checkout": exception}
        runtime.update_kills(disabled, "local-fixture", NOW)
        report = assess_release(MANIFEST, GOLDEN["scope"], controls.read(GOLDEN["scope"]), [snapshot(runtime, 1002)], 1002, SOURCE_A, SOURCE_A)
        assert report["rows"][0]["actual"] is False and report["rows"][0]["status"] == "needs_review"


def test_unfinished_code_and_mismatched_source_cannot_enable():
    runtime, _ = setup()
    payload = compile_release_payload(MANIFEST, empty_provider())
    payload["features"]["next-checkout"]["defaultValue"] = True
    with pytest.raises(ValueError, match="release policy"):
        runtime.activate(release(MANIFEST, 2, SOURCE_A, payload), 1, NOW)
    assert runtime.evaluate("next-checkout", "web", GOLDEN["context"], now=NOW)["value"] is False
    incompatible = deepcopy(MANIFEST)
    incompatible["features"]["checkout"]["state"] = "development"
    with pytest.raises(ValueError, match="deployed source"):
        runtime.activate(release(incompatible, 2, SOURCE_B), 1, NOW)


def test_deployment_preserves_provider_policy_and_rejects_ambiguous_migrations():
    provider = empty_provider()
    provider["features"]["legacy"] = {"defaultValue": False}
    previous = bundle(provider)
    changed = deepcopy(provider)
    changed["features"]["legacy"]["defaultValue"] = True
    with pytest.raises(ValueError, match="provider policy changed"):
        prepare_deployment_payload(MANIFEST, changed, previous, NOW)
    collision = empty_provider()
    collision["features"]["checkout"] = {"defaultValue": False}
    with pytest.raises(ValueError, match="ownership collision"):
        compile_release_payload(MANIFEST, collision)
    with pytest.raises(ValueError, match="explicit migration"):
        prepare_deployment_payload(MANIFEST, empty_provider(), bundle(collision), NOW)
    with pytest.raises(ValueError, match="retirement"):
        prepare_deployment_payload({"schema_version": 1, "features": {}}, empty_provider(), release(), NOW)
    invalid = deepcopy(previous)
    invalid["payload_sha256"] = hashlib.sha256(b"different").hexdigest()
    with pytest.raises(ValueError, match="previous release"):
        prepare_deployment_payload(MANIFEST, provider, invalid, NOW)


def test_disable_metadata_is_bound_and_legacy_signatures_stay_unchanged():
    disabled = {**kill(2, ["checkout"]), "exceptions": {"checkout": {"owner": "team.ops", "reason": "Synthetic hold", "review_at": 90000}}}
    changed = deepcopy(disabled)
    changed["exceptions"]["checkout"]["reason"] = "Different synthetic hold"
    assert kill_message(disabled) != kill_message(changed)
    integral = deepcopy(disabled)
    integral["exceptions"]["checkout"]["review_at"] = 90000.0
    assert kill_message(disabled) == kill_message(integral)
    assert b"feature-controls/kill/v1" in kill_message(kill())
    assert approval_message(release()) != approval_message(release(source=SOURCE_B))
    with pytest.raises(ValueError, match="without disable"):
        kill_message({**kill(), "exceptions": disabled["exceptions"]})


@pytest.mark.parametrize("field,value", [("kill_generation", 9), ("configuration_revision", 9),
                                         ("environment_id", "other"), ("expires_at", NOW)])
def test_stale_or_mixed_runtime_evidence_is_not_green(field, value):
    runtime, controls = setup()
    observed = snapshot(runtime)
    observed[field] = value
    with pytest.raises(ValueError):
        assess_release(MANIFEST, GOLDEN["scope"], controls.read(GOLDEN["scope"]), [observed], NOW, SOURCE_A, SOURCE_A)


def test_missing_evidence_or_baseline_fallback_does_not_qualify():
    runtime, controls = setup()
    state = controls.read(GOLDEN["scope"])
    assert assess_release(MANIFEST, GOLDEN["scope"], state, [], NOW, SOURCE_A, SOURCE_A)["status"] == "failed"
    observed = snapshot(runtime)
    observed["decisions"]["checkout"]["status"] = "baseline"
    assert assess_release(MANIFEST, GOLDEN["scope"], state, [observed], NOW, SOURCE_A, SOURCE_A)["rows"][0]["status"] == "unverified"
    with pytest.raises(ValueError, match="duplicate"):
        assess_release(MANIFEST, GOLDEN["scope"], state, [observed, observed], NOW, SOURCE_A, SOURCE_A)


def test_declarations_require_acceptance_and_cannot_hide_unfinished_dependencies():
    for mutation in (lambda m: m["features"]["checkout"].update(acceptance=[]),
                     lambda m: m["features"]["checkout"].update(acceptance=["../outside"]),
                     lambda m: m["features"]["checkout"].update(ancestors=["next-checkout"]),
                     lambda m: m["features"]["checkout"].update(state="force_on")):
        invalid = deepcopy(MANIFEST)
        mutation(invalid)
        with pytest.raises(ValueError):
            validate_release_manifest(invalid)
    assert assess_release({"schema_version": 1, "features": {}}, GOLDEN["scope"],
                          {"bundle": None, "kill": None, "time_highwater": 0}, [], NOW, SOURCE_A, SOURCE_A)["status"] == "no_features"


def test_rolling_deployment_and_compatible_rollback_preserve_ready_features():
    old, controls = setup()
    next_manifest = deepcopy(MANIFEST)
    next_manifest["features"]["next-checkout"].update(state="ready", acceptance=["tests/feature_controls/release-policy.test.mjs"])
    next_manifest["features"]["receipt"] = deepcopy(next_manifest["features"]["checkout"])
    updated = FeatureRuntime(catalog(next_manifest), trust_policy="local-test", controls=controls,
                             release_manifest=next_manifest, release_source_sha=SOURCE_B)
    previous = updated.previous_for_deployment(NOW)
    updated.activate(release(next_manifest, 2, SOURCE_B, prepare_deployment_payload(next_manifest, empty_provider(), previous["bundle"], NOW)), 1, NOW)
    assert old.evaluate("checkout", "web", GOLDEN["context"], now=NOW)["value"] is True
    assert old.evaluate("next-checkout", "web", GOLDEN["context"], now=NOW)["value"] is False
    assert updated.evaluate("receipt", "web", GOLDEN["context"], now=NOW)["value"] is True
    evidence = updated.release_evidence([GOLDEN["context"]], NOW)
    assert assess_release(next_manifest, GOLDEN["scope"], evidence["controls"], evidence["snapshots"], NOW, SOURCE_B, evidence["source_sha"])["status"] == "passed"
    rollback = old.release_evidence([GOLDEN["context"]], NOW)
    assert assess_release(MANIFEST, GOLDEN["scope"], rollback["controls"], rollback["snapshots"], NOW, SOURCE_A, rollback["source_sha"], "rollback")["status"] == "passed"
    with pytest.raises(ValueError, match="retirement|regression"):
        prepare_deployment_payload(MANIFEST, empty_provider(), evidence["controls"]["bundle"], NOW)


def test_unsigned_stored_kill_cannot_qualify_but_remains_immediately_off():
    controls, receipts, references = Controls(), {}, {}

    def signature(message, purpose):
        return hmac.new(b"fixture-only-release-approval", purpose.encode() + b"\x00" + message, hashlib.sha256).hexdigest()

    def sign(message, purpose, reference=None):
        reference = reference or f"fixture-{len(receipts)}"
        receipts[reference] = signature(message, purpose)
        references[message] = reference
        return reference

    class Verifier:
        checked = 0

        def verify(self, message, reference, purpose):
            return hmac.compare_digest(signature(message, purpose), receipts.get(reference, ""))

        def verify_kill(self, value):
            self.checked += 1
            return value["scope"] == GOLDEN["scope"] and receipts.get(references.get(kill_message(value))) == signature(kill_message(value), "kill")

    verifier = Verifier()
    runtime = FeatureRuntime(catalog(), controls=controls, verifier=verifier,
                             release_manifest=MANIFEST, release_source_sha=SOURCE_A)
    approved = release()
    sign(approval_message(approved), "release", approved["approval_ref"])
    runtime.activate(approved, 0, NOW)
    initial = kill()
    runtime.update_kills(initial, sign(kill_message(initial), "kill"), NOW)
    assert runtime.release_evidence([GOLDEN["context"]], NOW)["controls"]["kill"]["generation"] == 1
    current = controls.read(GOLDEN["scope"])
    forged = {**kill(2, ["checkout"]), "exceptions": {
        "checkout": {"owner": "team.incident", "reason": "Synthetic hold", "review_at": NOW + 10000}}}
    assert controls.compare_and_swap(GOLDEN["scope"], current, {**current, "kill": forged})
    assert snapshot(runtime)["decisions"]["checkout"]["value"] is False
    with pytest.raises(ValueError, match="stored kill approval"):
        runtime.release_evidence([GOLDEN["context"]], NOW)
    with pytest.raises(ValueError, match="stored kill approval"):
        runtime.previous_for_deployment(NOW)
    sign(kill_message(forged), "kill")
    evidence = runtime.release_evidence([GOLDEN["context"]], NOW)
    assert assess_release(MANIFEST, GOLDEN["scope"], evidence["controls"], evidence["snapshots"], NOW, SOURCE_A, SOURCE_A)["status"] == "passed"
    assert verifier.checked >= 4
    del Verifier.verify_kill
    with pytest.raises(ValueError, match="stored kill approval"):
        runtime.inspect_for_release(NOW)
