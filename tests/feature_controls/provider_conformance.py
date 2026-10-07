import hashlib
import json
import socket
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from feature_controls import PROVIDER_SEMANTICS, FeatureRuntime, approval_message

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads(Path(__file__).with_name("golden.json").read_text())
NOW = GOLDEN["now"]

def bundle(payload=None, revision=1, scope=None):
    raw = json.dumps(GOLDEN["payload"] if payload is None else payload, separators=(",", ":"), ensure_ascii=False)
    return {"schema_version": 1, "scope": deepcopy(scope or GOLDEN["scope"]), "revision": revision,
            "catalog_revision": 1, "created_at": 0, "expires_at": GOLDEN["expires_at"],
            "provider_semantics": PROVIDER_SEMANTICS, "payload_bytes": raw,
            "payload_sha256": hashlib.sha256(raw.encode()).hexdigest(), "approval_ref": "local-fixture"}

def kill(generation=1, disabled=None, expires_at=None, scope=None):
    return {"schema_version": 1, "scope": deepcopy(scope or GOLDEN["scope"]), "generation": generation,
            "expires_at": expires_at or GOLDEN["expires_at"], "disabled": disabled or []}

class Assignments:
    durable = True
    def __init__(self):
        self.values, self.writes = {}, 0
    def read(self, key):
        return deepcopy(self.values.get(json.dumps(key, sort_keys=True)))
    def create_if_absent(self, key, variant):
        k = json.dumps(key, sort_keys=True)
        if k not in self.values:
            self.values[k] = {"assignment_id": f"assignment-{len(self.values)}", "variant": variant}
            self.writes += 1
        return deepcopy(self.values[k])

class Events:
    durable = True
    def __init__(self):
        self.values = {}
    def append_if_absent(self, event):
        if event["event_id"] in self.values:
            assert self.values[event["event_id"]] == event
            return False
        self.values[event["event_id"]] = deepcopy(event)
        return True

class Controls:
    durable = True
    test_only = True
    def __init__(self):
        self.values, self.writes = {}, 0
    def read(self, scope):
        return deepcopy(self.values.get(json.dumps(scope, sort_keys=True), {"bundle": None, "kill": None}))
    def compare_and_swap(self, scope, expected, next_state):
        if self.read(scope) != expected:
            return False
        self.values[json.dumps(scope, sort_keys=True)] = deepcopy(next_state)
        self.writes += 1
        return True

def setup(**options):
    r = FeatureRuntime(deepcopy(GOLDEN["catalog"]), trust_policy="local-test", **options)
    r.activate(bundle(), 0, NOW)
    r.update_kills(kill(), "local-fixture", NOW)
    return r

def event_for(d, kind="exposure", event_id="event-1"):
    return {"schema_version": 1, "scope": d["scope"], "event_id": event_id, "kind": kind,
            "decision_id": d["decision_id"], "feature_key": d["feature_key"],
            "assignment_id": d["assignment"]["assignment_id"], "configuration_revision": d["configuration_revision"],
            "allocation_epoch": d["allocation_epoch"], "variant": d["assignment"]["variant"], "timestamp": NOW,
            "evidence": {"source": "render" if kind == "exposure" else "business-transition", "unit_key": "render-unit"}}

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("offline evaluator attempted network")
    monkeypatch.setattr(socket.socket, "connect", blocked)

@pytest.mark.parametrize("case", GOLDEN["cases"])
def test_shared_golden(case):
    context = {**GOLDEN["context"], **{k: case[k] for k in ("excluded", "eligible", "authorized") if k in case}}
    d = setup().evaluate(case["key"], case["surface"], context, now=NOW)
    assert d["value"] == case["expected_value"]
    assert d["reason"] == case["expected_reason"]

def test_activation_approval_digest_and_replay():
    with pytest.raises(ValueError):
        FeatureRuntime(GOLDEN["catalog"]).activate(bundle(), 0, NOW)
    controls = Controls()
    with pytest.raises(ValueError):
        FeatureRuntime(GOLDEN["catalog"], controls=controls).activate(bundle(), 0, NOW)
    class Verifier:
        def verify(self, message, ref, purpose):
            return purpose == "release" and ref == "local-fixture" and message == approval_message(bundle())
    runtime = FeatureRuntime(GOLDEN["catalog"], controls=controls, verifier=Verifier())
    runtime.activate(bundle(), 0, NOW)
    for changed in (bundle(), bundle(revision=2), {**bundle(), "payload_sha256": "0" * 64}):
        with pytest.raises(ValueError):
            runtime.activate(changed, 0, NOW)
    assert controls.writes == 1

def test_local_trust_cannot_write_to_unmarked_store():
    controls = Controls()
    controls.test_only = False
    with pytest.raises(ValueError):
        FeatureRuntime(GOLDEN["catalog"], controls=controls, trust_policy="local-test")

@pytest.mark.parametrize("assignment_key", ["😀", "é", "x/y", "", "x" * 129, "x\n"])
def test_unicode_rejection(assignment_key):
    with pytest.raises(ValueError):
        setup().evaluate("release", "web", {**GOLDEN["context"], "assignment_key": assignment_key}, now=NOW)

@pytest.mark.parametrize("mutation", ["savedGroups", "child", "seed", "force", "condition", "segment-cycle"])
def test_complete_payload_and_retention(mutation):
    p = deepcopy(GOLDEN["payload"])
    if mutation == "savedGroups": del p["savedGroups"]
    elif mutation == "child": del p["features"]["child"]
    elif mutation == "seed": p["features"]["experiment"]["rules"][0]["seed"] = "😀"
    elif mutation == "force": p["features"]["release"]["rules"][0]["force"] = "wrong"
    elif mutation == "condition": p["features"]["release"]["rules"][0]["condition"] = {"id": "x"}
    else: p["savedGroups"] = {"a": {"references": ["b"]}, "b": {"references": ["a"]}}
    runtime = setup()
    with pytest.raises(ValueError): runtime.activate(bundle(p, 2), 1, NOW)
    assert runtime.evaluate("release", "web", GOLDEN["context"], now=NOW)["value"] is True

def test_independent_kills_scopes_expiry_and_local_disable():
    controls = Controls()
    r = setup(controls=controls)
    r.update_kills(kill(2, ["release"]), "local-fixture", NOW)
    r.activate(bundle(revision=2), 1, NOW)
    assert r.evaluate("child", "web", GOLDEN["context"], now=NOW)["reason"] == "disabled_or_excluded"
    with pytest.raises(ValueError): r.update_kills(kill(1), "local-fixture", NOW)
    scope = {**GOLDEN["scope"], "application": "other"}
    second = FeatureRuntime({**GOLDEN["catalog"], "scope": scope}, controls=controls, trust_policy="local-test")
    second.activate(bundle(scope=scope), 0, NOW)
    second.update_kills(kill(scope=scope), "local-fixture", NOW)
    assert second.evaluate("child", "web", GOLDEN["context"], now=NOW)["value"] == "enabled"
    r = setup(local_disabled=["release"])
    assert r.evaluate("child", "web", GOLDEN["context"], now=NOW)["status"] == "denied"
    r = setup()
    r.update_kills(kill(2, expires_at=2000), "local-fixture", NOW)
    assert r.evaluate("release", "web", GOLDEN["context"], now=2000)["reason"] == "kills_expired"
    r = setup(require_fresh_kills=False)
    assert r.evaluate("release", "web", {**GOLDEN["context"], "expires_at": 200000}, now=100000)["reason"] == "bundle_expired"

def test_signed_creation_time_skew_boundary():
    runtime = FeatureRuntime(GOLDEN["catalog"], trust_policy="local-test")
    runtime.activate({**bundle(), "created_at": NOW + 29999}, 0, NOW)
    with pytest.raises(ValueError):
        FeatureRuntime(GOLDEN["catalog"], trust_policy="local-test").activate({**bundle(), "created_at": NOW + 30001}, 0, NOW)

def test_preview_sticky_and_separate_event_idempotence():
    assignments, events = Assignments(), Events()
    r = setup(assignments=assignments, events=events)
    preview = r.evaluate("experiment", "web", GOLDEN["context"], preview=True, now=NOW)
    assert preview["reason"] == "preview"
    assert assignments.writes == 0 and not events.values
    assert setup().evaluate("experiment", "web", GOLDEN["context"], now=NOW)["reason"] == "assignment_store_required"
    d = r.evaluate("experiment", "web", GOLDEN["context"], now=NOW)
    assert d["reason"] == "experiment" and assignments.writes == 1 and not events.values
    p = deepcopy(GOLDEN["payload"])
    p["features"]["experiment"]["rules"][0]["weights"] = [0, 1] if d["value"] == "control" else [1, 0]
    r.activate(bundle(p, 2), 1, NOW)
    after = r.evaluate("experiment", "web", GOLDEN["context"], now=NOW)
    assert after["value"] == d["value"] and after["assignment"] == d["assignment"]
    with pytest.raises(ValueError): r.record_event(event_for(d, "outcome", "transition-1"), NOW)
    assert r.record_event(event_for(d), NOW) is True
    assert r.record_event(event_for(d), NOW) is False
    assert r.record_event(event_for(d, "outcome", "transition-1"), NOW) is True
    with pytest.raises(ValueError): r.record_event({**event_for(d), "variant": "forged"}, NOW)
    with pytest.raises(ValueError): r.record_event(event_for(d), d["expires_at"])

def test_wire_projection_and_schema_copy():
    snapshot = setup().snapshot(["release", "missing"], "web", GOLDEN["context"], now=NOW)
    assert set(snapshot) == {"schema_version", "application", "environment", "surface", "generated_at", "expires_at", "decisions"}
    for d in snapshot["decisions"].values():
        assert set(d) == {"value", "reason", "expires_at", "configuration_revision", "kill_generation"}
    assert (ROOT / "contracts/feature-controls/schema.json").read_bytes() == (ROOT / "packages/feature-controls/python/feature_controls/schema.json").read_bytes()

def test_ascii_cross_language_parity_1000_and_approval_bytes():
    process = subprocess.run(["node", str(Path(__file__).with_name("parity.mjs"))], cwd=ROOT, text=True, capture_output=True, check=True, timeout=60)
    expected = json.loads(process.stdout)
    runtime = setup()
    actual = []
    for i in range(1000):
        d = runtime.evaluate("experiment", "web", {**GOLDEN["context"], "assignment_key": f"unit-{i}"}, preview=True, now=NOW)
        actual.append({"value": d["value"], "reason": d["reason"], "configuration_revision": d["configuration_revision"], "kill_generation": d["kill_generation"]})
    assert actual == expected["decisions"]
    assert approval_message(bundle()).decode() == expected["approval_bytes"]
