"""Source readiness, deliberate disables and authenticated release qualification."""
import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from .lifecycle import clock, kill_message
from .types import Catalog, ControlState, ReleaseBundle, ReleaseManifest, Scope, Snapshot
from .validation import _valid_json, compatible_release_manifest, release_catalog_features, release_manifest_message, validate, validate_release_manifest


def unfinished_release_features(manifest: ReleaseManifest) -> list[str]:
    validate_release_manifest(manifest)
    return [key for key, definition in manifest["features"].items() if definition["state"] == "development"]


def release_client_states(manifest: ReleaseManifest, application_id: str) -> dict[str, bool]:
    validate_release_manifest(manifest)
    return {key: definition["state"] == "ready" for key, definition in manifest["features"].items()
            if application_id in definition["applications"]}


def _provider_payload(value: Any) -> dict[str, Any]:
    if not _valid_json(value) or len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > 1048576 or type(value) is not dict or \
            set(value) != {"features", "experiments", "savedGroups", "contextualBandits"} or \
            any(type(value[field]) is not dict for field in ("features", "savedGroups", "contextualBandits")) or \
            type(value["experiments"]) is not list or value["experiments"] or value["savedGroups"] or value["contextualBandits"]:
        raise ValueError("unsupported release provider payload")
    return deepcopy(value)


def compile_release_payload(manifest: ReleaseManifest, provider: Any) -> dict[str, Any]:
    """Generate new bytes for signed publication; never change an old approval."""
    validate_release_manifest(manifest)
    payload = _provider_payload(provider)
    for key, definition in manifest["features"].items():
        if key in payload["features"]:
            raise ValueError("release/provider ownership collision; explicit migration required")
        payload["features"][key] = {"defaultValue": definition["state"] == "ready"}
    return _provider_payload(payload)


def _equal_json(left: Any, right: Any) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return bool(left == right)
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(_equal_json(value, right[key]) for key, value in left.items())
    if type(left) is list:
        return len(left) == len(right) and all(_equal_json(a, b) for a, b in zip(left, right))
    return bool(left == right)


def prepare_deployment_payload(manifest: ReleaseManifest, provider: Any, approved: ReleaseBundle, now: int) -> dict[str, Any]:
    """Caller first verifies the existing immutable approval and owning scope."""
    clock(now)
    validate("bundle", approved)
    if approved["created_at"] > now or approved["expires_at"] <= now or \
            hashlib.sha256(approved["payload_bytes"].encode()).hexdigest() != approved["payload_sha256"]:
        raise ValueError("invalid previous release")
    next_payload = compile_release_payload(manifest, provider)
    previous = _provider_payload(json.loads(approved["payload_bytes"]))
    previous_managed = approved.get("deployment", {}).get("manifest", {}).get("features", {})
    if "deployment" in approved:
        validate_release_manifest(approved["deployment"]["manifest"])
    for key in manifest["features"]:
        if key in previous["features"] and key not in previous_managed:
            raise ValueError("existing provider control needs explicit migration")
    for key in previous_managed:
        if key not in manifest["features"]:
            raise ValueError("release feature retirement needs explicit migration")
        if previous_managed[key]["state"] == "ready" and manifest["features"][key]["state"] != "ready":
            raise ValueError("ready feature regression needs explicit migration")
        if any(sorted(previous_managed[key].get(field, [])) != sorted(manifest["features"][key].get(field, []))
               for field in ("applications", "surfaces", "ancestors")):
            raise ValueError("release boundary change needs explicit migration")
        del previous["features"][key]
    expected_provider = deepcopy(next_payload)
    for key in manifest["features"]:
        del expected_provider["features"][key]
    if not _equal_json(previous, expected_provider):
        raise ValueError("provider policy changed; explicit publication required")
    return next_payload


def assert_release_catalog(manifest: ReleaseManifest, catalog: Catalog) -> None:
    for key, definition in release_catalog_features(manifest).items():
        if not _equal_json(catalog["features"].get(key), definition):
            raise ValueError("release catalogue does not match source policy")


def assess_release(manifest: ReleaseManifest, scope: Scope, controls: ControlState,
                   snapshots: list[Snapshot], now: int, source_sha: str, observed_source_sha: str, operation: str = "deploy") -> dict[str, Any]:
    """Use trusted store state and authenticated evidence, never client input."""
    validate_release_manifest(manifest)
    clock(now)
    validate("control_state", controls)
    kill, bundle = controls["kill"], controls["bundle"]
    if not manifest["features"]:
        return {"status": "no_features", "rows": [], "configuration_revision": bundle["revision"] if bundle else None,
                "kill_generation": kill["generation"] if kill else 0}
    if operation not in {"deploy", "rollback"}:
        raise ValueError("invalid release operation")
    if not kill or not bundle or "deployment" not in bundle or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None or source_sha != observed_source_sha or \
            operation == "deploy" and bundle["deployment"]["source_sha"] != source_sha or \
            not (compatible_release_manifest(manifest, bundle["deployment"]["manifest"]) if operation == "rollback" else
                 release_manifest_message(bundle["deployment"]["manifest"]) == release_manifest_message(manifest)) or \
            bundle["scope"] != scope or bundle["created_at"] > now or bundle["expires_at"] <= now or controls["time_highwater"] > now or \
            hashlib.sha256(bundle["payload_bytes"].encode()).hexdigest() != bundle["payload_sha256"]:
        raise ValueError("release does not match deployed policy")
    kill_message(kill)
    if kill["scope"] != scope or kill["expires_at"] <= now or len(snapshots) > 128:
        raise ValueError("invalid release-check binding")
    captured: dict[tuple[str, str], Snapshot] = {}
    revision = None
    for snapshot in snapshots:
        validate("snapshot", snapshot)
        if any(snapshot[key] != value for key, value in scope.items()) or snapshot["kill_generation"] != kill["generation"] or \
                snapshot["configuration_revision"] != bundle["revision"] or snapshot["generated_at"] > now or \
                snapshot["expires_at"] <= now or snapshot["time_highwater"] > now or \
                revision is not None and snapshot["configuration_revision"] != revision:
            raise ValueError("stale or mixed runtime evidence")
        revision = snapshot["configuration_revision"]
        key = (snapshot["application_id"], snapshot["surface_id"])
        if key in captured:
            previous = captured[key]
            if previous["context_scope"] != snapshot["context_scope"] or set(previous["decisions"]) & set(snapshot["decisions"]):
                raise ValueError("duplicate runtime evidence")
            previous["decisions"].update(deepcopy(snapshot["decisions"]))
            previous["expires_at"] = min(previous["expires_at"], snapshot["expires_at"])
        else:
            captured[key] = deepcopy(snapshot)
    disabled, rows = set(kill["disabled"]), []
    def blockers(key: str, visited: set[str]) -> set[str]:
        if key in visited:
            return set()
        visited.add(key)
        found = {key} if key in disabled else set()
        for parent in manifest["features"][key].get("ancestors", []):
            found |= blockers(parent, visited)
        return found
    for key, definition in manifest["features"].items():
        disabling = sorted(blockers(key, set()))
        exceptions = {parent: deepcopy(kill["exceptions"][parent]) for parent in disabling if parent in kill.get("exceptions", {})}
        needs_review = any(parent not in exceptions or exceptions[parent]["review_at"] <= now for parent in disabling)
        for app in definition["applications"]:
            for surface in definition["surfaces"]:
                snapshot = captured.get((app, surface))
                decision = snapshot["decisions"].get(key) if snapshot else None
                expected = definition["state"] == "ready" and not disabling
                status = "unverified"
                coherent = decision and snapshot and decision["expires_at"] > now and \
                    decision["configuration_revision"] == snapshot["configuration_revision"] and decision["kill_generation"] == kill["generation"]
                if coherent and type(decision["value"]) is bool and (decision["status"] == "resolved" and decision["reason"] == "released" or
                                                                  decision["status"] == "denied" and decision["reason"] == "disabled_or_excluded"):
                    if decision["value"] != expected:
                        status = "unexpected_on" if decision["value"] else "unexpected_off"
                    elif needs_review:
                        status = "needs_review"
                    elif disabling and decision["status"] == "denied" and decision["reason"] == "disabled_or_excluded":
                        status = "deliberately_disabled"
                    elif not disabling and decision["status"] == "resolved" and decision["reason"] == "released":
                        status = "ready_on" if expected else "development_off"
                    elif not disabling and definition["state"] == "development" and decision["status"] == "denied" and decision["reason"] == "disabled_or_excluded":
                        status = "development_off"
                rows.append({"feature_key": key, "application_id": app, "surface_id": surface, "state": definition["state"],
                             "expected": bool(expected), "actual": decision["value"] if decision and type(decision["value"]) is bool else None,
                             "status": status, "disabling_features": disabling, "exceptions": exceptions})
    return {"status": "no_features" if not rows else "passed" if all(row["status"] in {"ready_on", "development_off", "deliberately_disabled"} for row in rows) else "failed",
            "rows": rows, "configuration_revision": revision, "kill_generation": kill["generation"]}
