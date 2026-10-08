import hashlib
import json
import socket
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from feature_controls import (
    PROVIDER_SEMANTICS,
    FeatureRuntime,
    approval_message,
    kill_message,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads(Path(__file__).with_name("golden.json").read_text())
NOW = GOLDEN["now"]
STICKY_NUMERICS = json.loads(Path(__file__).with_name("sticky-numerics.json").read_text())

def numeric_bundle(epoch, version):
    catalog, payload = deepcopy(GOLDEN["catalog"]), deepcopy(GOLDEN["payload"])
    catalog["features"]["experiment"]["experiment"]["epoch"] = "EPOCH_LITERAL"
    catalog = json.loads(json.dumps(catalog, separators=(",", ":")).replace('"EPOCH_LITERAL"', epoch))
    payload["features"]["experiment"]["rules"][0]["bucketVersion"] = "VERSION_LITERAL"
    release = bundle(payload)
    release["payload_bytes"] = release["payload_bytes"].replace('"VERSION_LITERAL"', version)
    release["payload_sha256"] = hashlib.sha256(release["payload_bytes"].encode()).hexdigest()
    return catalog, release

def numeric_sticky_probe(epoch, version):
    catalog, release = numeric_bundle(epoch, version)
    integers = []
    def initialize(assignments=None):
        controls = Controls()
        runtime = FeatureRuntime(catalog, trust_policy="local-test", controls=controls, assignments=assignments)
        runtime.activate(release, 0, NOW)
        runtime.update_kills(kill(), "local-fixture", NOW)
        integers.append(type(runtime.catalog["features"]["experiment"]["experiment"]["epoch"]) is int)
        provider, calls = runtime._provider, []
        def capture(*args):
            rule = args[0]["features"]["experiment"]["rules"][0]
            integers.append(type(rule["bucketVersion"]) is int)
            result = provider(*args)
            calls.append({"key": result.experimentResult.key, "sticky": result.experimentResult.stickyBucketUsed})
            return result
        runtime._provider = capture
        return runtime, calls, controls
    fresh, natural_calls, _ = initialize()
    fresh.evaluate("experiment", "web", GOLDEN["context"], preview=True, now=NOW)
    natural = natural_calls[-1]["key"]
    saved = "treatment" if natural == "control" else "control"
    reads, creates = [], []
    class ExistingAssignments:
        durable = True
        def read(self, key):
            integers.append(type(key["allocation_epoch"]) is int)
            reads.append(deepcopy(key))
            return {"assignment_id": "persisted-choice", "variant": saved}
        def create_if_absent(self, key, variant):
            integers.append(type(key["allocation_epoch"]) is int)
            creates.append({"key": deepcopy(key), "variant": variant})
            return {"assignment_id": "persisted-choice", "variant": saved}
    runtime, calls, controls = initialize(ExistingAssignments())
    preview = runtime.evaluate("experiment", "web", GOLDEN["context"], preview=True, now=NOW)
    preview_creates = len(creates)
    live = runtime.evaluate("experiment", "web", GOLDEN["context"], now=NOW)
    assert catalog == numeric_bundle(epoch, version)[0]
    return {"epoch": epoch, "version": version, "saved": saved, "natural": natural, "preview": preview["value"],
            "preview_creates": preview_creates, "live": live["value"], "status": live["status"], "reason": live["reason"], "calls": calls,
            "epochs": [key["allocation_epoch"] for key in reads] + [item["key"]["allocation_epoch"] for item in creates],
            "payload_unchanged": controls.read(GOLDEN["scope"])["bundle"] == release,
            "integer_metadata": all(integers), "payload_hash": release["payload_sha256"]}

@pytest.mark.parametrize("epoch,version", STICKY_NUMERICS)
def test_actual_provider_sticky_numeric_tokens(epoch, version):
    if any(token in ("true", "false") for token in (epoch, version)):
        with pytest.raises(ValueError):
            numeric_sticky_probe(epoch, version)
        return
    row = numeric_sticky_probe(epoch, version)
    assert row["saved"] != row["natural"]
    assert row["preview"] == row["saved"] and row["preview_creates"] == 0
    assert (row["live"], row["status"], row["reason"]) == (row["saved"], "resolved", "experiment")
    assert all(call == {"key": row["saved"], "sticky": True} for call in row["calls"])
    assert all(value == json.loads(epoch) for value in row["epochs"])
    assert row["payload_unchanged"] and row["integer_metadata"]

def test_semantic_epoch_representations_preserve_one_assignment_identity():
    assignments, ids = Assignments(), []
    for token in ("1.0", "1", "1e0"):
        catalog, release = numeric_bundle(token, token)
        runtime = FeatureRuntime(catalog, trust_policy="local-test", assignments=assignments)
        runtime.activate(release, 0, NOW)
        runtime.update_kills(kill(), "local-fixture", NOW)
        d = runtime.evaluate("experiment", "web", GOLDEN["context"], now=NOW)
        assert d["status"] == "resolved"
        ids.append(d["assignment"]["assignment_id"])
    assert len(set(ids)) == len(assignments.values) == assignments.writes == 1

def test_owned_native_integer_metadata_normalizes_without_changing_approved_bytes():
    release = bundle()
    release["payload_bytes"] = release["payload_bytes"].replace('"hashVersion":2', '"hashVersion":2.0')
    release["payload_sha256"] = hashlib.sha256(release["payload_bytes"].encode()).hexdigest()
    controls = Controls()
    runtime = FeatureRuntime(GOLDEN["catalog"], trust_policy="local-test", controls=controls, assignments=Assignments())
    runtime.activate(release, 0, NOW)
    runtime.update_kills(kill(), "local-fixture", NOW)
    provider = runtime._provider
    def capture(*args):
        assert type(args[0]["features"]["experiment"]["rules"][0]["hashVersion"]) is int
        return provider(*args)
    runtime._provider = capture
    assert runtime.evaluate("experiment", "web", GOLDEN["context"], now=NOW)["status"] == "resolved"
    assert controls.read(GOLDEN["scope"])["bundle"] == release
    for field in ("hashVersion", "bucketVersion"):
        for token in (True, False, -1, 1.5, "1", 9007199254740992):
            payload = deepcopy(GOLDEN["payload"])
            payload["features"]["experiment"]["rules"][0][field] = token
            with pytest.raises(ValueError):
                FeatureRuntime(GOLDEN["catalog"], trust_policy="local-test").activate(bundle(payload), 0, NOW)

def test_dense_ancestor_dag_traversal_is_bounded_and_preserves_shared_exclusions():
    catalog, template = deepcopy(GOLDEN["catalog"]), deepcopy(GOLDEN["catalog"]["features"]["child"])
    catalog["features"] = {
        "other": {**deepcopy(template), "ancestors": []},
        "leaf": {**deepcopy(template), "ancestors": ["f15", "f16"]},
        **{
            f"f{i}": {**deepcopy(template), "ancestors": [f"f{j}" for j in range(i)]}
            for i in range(18)
        },
    }

    def evaluate(runtime, context=GOLDEN["context"], key="f17"):
        calls = 0
        excluded = runtime._excluded

        def counted(key, exclusions, visited=None):
            nonlocal calls
            calls += 1
            return excluded(key, exclusions) if visited is None else excluded(key, exclusions, visited)

        runtime._excluded = counted
        decision = runtime.evaluate(key, "web", context, now=NOW)
        assert calls <= 36, calls
        return decision

    plain = FeatureRuntime(catalog, trust_policy="local-test")
    assert evaluate(plain)["reason"] == "kills_expired"
    unrelated = FeatureRuntime(catalog, trust_policy="local-test")
    assert evaluate(unrelated, {**GOLDEN["context"], "excluded": ["other"]})["reason"] == "kills_expired"
    contextual = FeatureRuntime(catalog, trust_policy="local-test")
    assert evaluate(contextual, {**GOLDEN["context"], "excluded": ["f0"]}, "leaf")["reason"] == "disabled_or_excluded"
    local = FeatureRuntime(catalog, trust_policy="local-test", local_disabled=["f0"])
    assert evaluate(local, key="leaf")["reason"] == "disabled_or_excluded"
    killed = FeatureRuntime(catalog, trust_policy="local-test")
    killed.update_kills(kill(1, ["f0"]), "local-fixture", NOW)
    assert evaluate(killed, key="leaf")["reason"] == "disabled_or_excluded"

def test_scope_context_surface_and_projection_fences():
    r = setup(assignments=Assignments())
    for field in GOLDEN["scope"]:
        ctx = deepcopy(GOLDEN["context"])
        ctx["scope"][field] = "other"
        d = r.evaluate("release", "web", ctx, now=NOW)
        assert d["status"] == "denied" and d["value"] is False
    ctx = {**GOLDEN["context"], "surface_id": "mobile"}
    assert r.evaluate("release", "web", ctx, now=NOW)["status"] == "denied"

def test_disabled_boolean_baseline_cannot_reenable_client_control():
    catalog = deepcopy(GOLDEN["catalog"])
    catalog["features"]["release"]["baseline"] = True
    catalog["features"]["release"]["disabled_value"] = True
    with pytest.raises(ValueError):
        FeatureRuntime(catalog, trust_policy="local-test")
    catalog["features"]["release"]["disabled_value"] = False
    r = FeatureRuntime(catalog, trust_policy="local-test")
    r.activate(bundle(), 0, NOW)
    r.update_kills(kill(disabled=["release"]), "local-fixture", NOW)
    d = r.snapshot(["release"], "web", GOLDEN["context"], now=NOW)["decisions"]["release"]
    assert d["status"] == "denied" and d["value"] is False

def test_native_nested_group_targeting_and_ancestor_exclusion():
    payload = deepcopy(GOLDEN["payload"])
    payload["features"]["release"]["rules"][0]["condition"] = {"groups": {"$in": ["root"]}, "roles": {"$in": ["reader"]}, "tenant": {"$eq": "demo-tenant"}}
    r = setup()
    r.activate(bundle(payload, 2), 1, NOW)
    ctx = {**GOLDEN["context"], "groups": ["leaf"], "roles": ["reader"], "group_ancestors": {"leaf": ["branch"], "branch": ["root"], "root": []}}
    assert r.evaluate("release", "web", ctx, now=NOW)["value"] is True
    assert r.evaluate("release", "web", {**ctx, "excluded_groups": ["root"]}, now=NOW)["value"] is False

def test_value_schema_and_allowed_values_reject_invalid_baselines():
    catalog = deepcopy(GOLDEN["catalog"])
    feature = catalog["features"]["child"]
    feature["allowed_values"] = ["enabled", "disabled"]
    with pytest.raises(ValueError):
        FeatureRuntime(catalog, trust_policy="local-test")

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
    test_only = True
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
        return deepcopy(self.values.get(json.dumps(scope, sort_keys=True), {"bundle": None, "kill": None, "time_highwater": 0}))
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
    return {"schema_version": 1, "scope": d["scope"], "application_id": d["application_id"], "surface_id": d["surface_id"], "context_scope": d["context_scope"], "event_id": event_id, "kind": kind,
            "decision_id": d["decision_id"], "feature_key": d["feature_key"],
            "assignment_id": d["assignment"]["assignment_id"], "configuration_revision": d["configuration_revision"],
            "allocation_epoch": d["allocation_epoch"], "variant": d["assignment"]["variant"], "timestamp": NOW,
            "evidence": {"source": "render" if kind == "exposure" else "business-transition", "unit_key": d["unit_key"], **({"transition_key": event_id} if kind == "outcome" else {})}}

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("offline evaluator attempted network")
    monkeypatch.setattr(socket.socket, "connect", blocked)

@pytest.mark.parametrize("case", GOLDEN["cases"])
def test_shared_golden(case):
    context = {**GOLDEN["context"], "surface_id": case["surface_id"], **{k: case[k] for k in ("excluded", "eligible", "authorized") if k in case}}
    d = setup().evaluate(case["key"], case["surface_id"], context, now=NOW)
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
    scope = {**GOLDEN["scope"], "environment_id": "other"}
    second = FeatureRuntime({**GOLDEN["catalog"], "scope": scope}, controls=controls, trust_policy="local-test")
    second.activate(bundle(scope=scope), 0, NOW)
    second.update_kills(kill(scope=scope), "local-fixture", NOW)
    assert second.evaluate("child", "web", {**GOLDEN["context"], "scope": scope}, now=NOW)["value"] == "enabled"
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
    assert set(snapshot) == {*GOLDEN["scope"], "application_id", "context_scope", "configuration_revision", "kill_generation", "time_highwater", "schema_version", "surface_id", "generated_at", "expires_at", "decisions"}
    for d in snapshot["decisions"].values():
        assert set(d) == {"value", "reason", "status", "expires_at", "configuration_revision", "kill_generation"}
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
    actual_sticky = []
    for epoch, version in STICKY_NUMERICS:
        if any(token in ("true", "false") for token in (epoch, version)):
            with pytest.raises(ValueError):
                numeric_sticky_probe(epoch, version)
            actual_sticky.append({"epoch": epoch, "version": version, "status": "rejected"})
        else:
            actual_sticky.append(numeric_sticky_probe(epoch, version))
    assert actual_sticky == expected["sticky"]

HARDENING = json.loads(Path(__file__).with_name("hardening.json").read_text())

def custom(catalog, **options):
    r = FeatureRuntime(catalog, trust_policy="local-test", **options)
    r.activate(bundle(), 0, NOW)
    r.update_kills(kill(), "local-fixture", NOW)
    return r

def test_catalog_public_view_and_original_are_defensive():
    c = deepcopy(GOLDEN["catalog"])
    r = custom(c)
    c["features"]["child"]["ancestors"] = []
    r.catalog["features"]["child"]["ancestors"] = []
    r.update_kills(kill(2, ["release"]), "local-fixture", NOW)
    assert r.evaluate("child", "web", GOLDEN["context"], now=NOW)["status"] == "denied"

def test_time_floor_blocks_expiry_resurrection_after_restart():
    controls = Controls()
    r = setup(controls=controls)
    r.update_kills(kill(2, expires_at=NOW + 10), "local-fixture", NOW)
    assert r.evaluate("release", "web", GOLDEN["context"], now=NOW + 10)["status"] == "denied"
    restarted = FeatureRuntime(GOLDEN["catalog"], controls=controls, trust_policy="local-test")
    for runtime in (r, restarted):
        assert runtime.evaluate("release", "web", GOLDEN["context"], now=NOW + 1)["reason"] == "controls_unavailable"
    assert controls.read(GOLDEN["scope"])["time_highwater"] == NOW + 10
    with pytest.raises(ValueError): r.activate(bundle(revision=2), 1, NOW + 1)

@pytest.mark.parametrize("token", ["1000", "1000.0", "1e3"])
@pytest.mark.parametrize("failure", ["baseline", "deny"])
def test_persisted_integer_floor_preserves_active_kill_fences(token, failure):
    catalog = deepcopy(GOLDEN["catalog"])
    catalog["features"]["release"].update(baseline=True, failure=failure)
    controls = Controls()
    original = FeatureRuntime(catalog, controls=controls, trust_policy="local-test")
    original.activate(bundle(), 0, NOW)
    original.update_kills(kill(2, ["release"]), "local-fixture", NOW)
    old = controls.read(GOLDEN["scope"])
    serialized = json.dumps(old, separators=(",", ":"))
    assert '"time_highwater":1000' in serialized
    state = json.loads(serialized.replace('"time_highwater":1000', f'"time_highwater":{token}'))
    assert controls.compare_and_swap(GOLDEN["scope"], old, state)
    restarted = FeatureRuntime(catalog, controls=controls, trust_policy="local-test")
    d = restarted.evaluate("release", "web", GOLDEN["context"], now=NOW)
    assert (d["value"], d["status"], d["reason"]) == (False, "denied", "disabled_or_excluded")
    snapshot = restarted.snapshot(["release"], "web", GOLDEN["context"], now=NOW)
    assert [snapshot["configuration_revision"], snapshot["kill_generation"], snapshot["time_highwater"]] == [1, 2, 1000]
    assert snapshot["decisions"]["release"]["value"] is False
    assert snapshot["decisions"]["release"]["status"] == "denied"
    assert type(restarted._state()["time_highwater"]) is int
    assert type(controls.read(GOLDEN["scope"])["time_highwater"]) is type(state["time_highwater"])
    assert restarted.evaluate("release", "web", GOLDEN["context"], now=NOW + 1)["value"] is False
    assert controls.read(GOLDEN["scope"])["time_highwater"] == NOW + 1
    assert type(controls.read(GOLDEN["scope"])["time_highwater"]) is int
    assert restarted.evaluate("release", "web", GOLDEN["context"], now=NOW)["reason"] == "controls_unavailable"
    with pytest.raises(ValueError):
        restarted.activate(bundle(revision=2), 1, NOW)

def test_invalid_persisted_floor_representations_reject_before_observing_control_fences():
    for failure in ("baseline", "deny"):
        for token in ("true", "false", "1000.5", "-1", "9007199254740992", "1e400"):
            catalog = deepcopy(GOLDEN["catalog"])
            catalog["features"]["release"].update(baseline=True, failure=failure)
            controls = Controls()
            original = FeatureRuntime(catalog, controls=controls, trust_policy="local-test")
            original.activate(bundle(), 0, NOW)
            original.update_kills(kill(2, ["release"]), "local-fixture", NOW)
            old = controls.read(GOLDEN["scope"])
            state = json.loads(json.dumps(old, separators=(",", ":")).replace('"time_highwater":1000', f'"time_highwater":{token}'))
            assert controls.compare_and_swap(GOLDEN["scope"], old, state)
            restarted = FeatureRuntime(catalog, controls=controls, trust_policy="local-test")
            assert restarted.evaluate("release", "web", GOLDEN["context"], now=NOW)["reason"] == "controls_unavailable"
            snapshot = restarted.snapshot(["release"], "web", GOLDEN["context"], now=NOW)
            assert [snapshot["configuration_revision"], snapshot["kill_generation"], snapshot["time_highwater"]] == [0, 0, 0]
            assert snapshot["decisions"]["release"]["value"] is False

def test_approval_api_clocks_remain_strict_native_integers():
    runtime = setup()
    for now in (float(NOW), NOW + 0.5, True, float("inf"), -1, 9007199254740992):
        with pytest.raises(ValueError):
            runtime.activate(bundle(revision=2), 1, now)
        with pytest.raises(ValueError):
            runtime.update_kills(kill(2), "local-fixture", now)

def test_snapshot_uses_one_atomic_state_and_cap():
    controls = Controls()
    r = setup(controls=controls)
    first = controls.read(GOLDEN["scope"])
    second = {**deepcopy(first), "bundle": bundle(revision=2), "kill": kill(2, ["release"])}
    reads = []
    def read(scope):
        reads.append(scope)
        return deepcopy(first if len(reads) == 1 else second)
    controls.read = read
    s = r.snapshot(["release", "child"], "web", GOLDEN["context"], now=NOW)
    assert len(reads) == 1 and s["configuration_revision"] == s["kill_generation"] == 1
    assert all(d["status"] == "resolved" and d["configuration_revision"] == d["kill_generation"] == 1 for d in s["decisions"].values())
    assert r.evaluate("child", "web", GOLDEN["context"], now=NOW)["status"] == "denied"
    with pytest.raises(ValueError): r.snapshot(["release"] * 65, "web", GOLDEN["context"], now=NOW)

def test_context_is_captured_before_adapter_mutation():
    controls = Controls()
    r = setup(controls=controls, assignments=Assignments())
    ctx = deepcopy(GOLDEN["context"])
    read = controls.read
    def mutate(scope):
        ctx["assignment_key"] = "different-account"
        ctx["application_id"] = "different-app"
        return read(scope)
    controls.read = mutate
    assert r.evaluate("experiment", "web", ctx, now=NOW)["unit_key"] == GOLDEN["context"]["assignment_key"]

def test_assignment_default_isolation_shared_continuity_distinct_boundaries():
    c = deepcopy(GOLDEN["catalog"])
    c["applications"] += ["second-app", "outside-app"]
    assignments = Assignments()
    r = custom(c, assignments=assignments)
    ctx = GOLDEN["context"]
    a = r.evaluate("experiment", "web", ctx, now=NOW)
    b = r.evaluate("experiment", "web", {**ctx, "application_id": "second-app"}, now=NOW)
    assert a["assignment"]["assignment_id"] != b["assignment"]["assignment_id"]
    c["features"]["experiment"]["experiment"]["assignment_boundary"] = {"key": "shared-cohort", "applications": [ctx["application_id"], "second-app"]}
    r = custom(c, assignments=assignments)
    x = r.evaluate("experiment", "web", ctx, now=NOW)
    y = r.evaluate("experiment", "web", {**ctx, "application_id": "second-app"}, now=NOW)
    z = r.evaluate("experiment", "web", {**ctx, "application_id": "outside-app"}, now=NOW)
    assert x["assignment"] == y["assignment"] and x["value"] == y["value"]
    assert x["assignment"] != a["assignment"] and x["assignment"] != z["assignment"]
    c["features"]["experiment"]["experiment"]["assignment_boundary"]["key"] = "distinct-cohort"
    assert custom(c, assignments=assignments).evaluate("experiment", "web", ctx, now=NOW)["assignment"] != x["assignment"]
    assert assignments.writes == 5

@pytest.mark.parametrize("v", HARDENING["targeting"])
def test_shared_targeting_vectors(v):
    p = deepcopy(GOLDEN["payload"])
    p["features"]["release"]["rules"][0]["condition"] = v["condition"]
    r = setup()
    r.activate(bundle(p, 2), 1, NOW)
    ctx = {**GOLDEN["context"], "groups": ["leaf"], "roles": ["reader"], "group_ancestors": {"leaf": ["branch"], "branch": ["root"], "root": []}}
    assert r.evaluate("release", "web", ctx, now=NOW)["value"] is v["expected"]
    if v["expected"]:
        for group in ("leaf", "branch", "root"):
            assert r.evaluate("release", "web", {**ctx, "excluded_groups": [group]}, now=NOW)["status"] == "denied"

@pytest.mark.parametrize("condition", HARDENING["invalid_conditions"])
def test_shared_unsupported_native_conditions(condition):
    p = deepcopy(GOLDEN["payload"])
    p["features"]["release"]["rules"][0]["condition"] = condition
    with pytest.raises(ValueError): setup().activate(bundle(p, 2), 1, NOW)

def test_membership_rejects_missing_cycle_depth():
    r = setup()
    chain = {f"n{i}": [f"n{i+1}"] if i < 34 else [] for i in range(35)}
    for graph in ({"leaf": ["missing"]}, {"leaf": ["root"], "root": ["leaf"]}, chain):
        with pytest.raises(ValueError):
            r.evaluate("release", "web", {**GOLDEN["context"], "groups": ["n0" if graph is chain else "leaf"], "group_ancestors": graph}, now=NOW)

@pytest.mark.parametrize("v", HARDENING["values"])
def test_shared_value_constraints_all_value_positions(v):
    c = deepcopy(GOLDEN["catalog"])
    c["features"]["release"].update(v["feature"])
    invalid = deepcopy(c)
    invalid["features"]["release"]["baseline"] = v["invalid"]
    with pytest.raises(ValueError): FeatureRuntime(invalid)
    for position in ("default", "force", "variation"):
        catalog, p = deepcopy(c), deepcopy(GOLDEN["payload"])
        p["features"]["release"] = {"defaultValue": v["feature"]["baseline"], "rules": [{"force": v["feature"]["baseline"]}]}
        if position == "default": p["features"]["release"]["defaultValue"] = v["invalid"]
        if position == "force": p["features"]["release"]["rules"][0]["force"] = v["invalid"]
        if position == "variation":
            catalog["features"]["release"]["experiment"] = deepcopy(catalog["features"]["experiment"]["experiment"])
            p["features"]["release"]["rules"] = deepcopy(p["features"]["experiment"]["rules"])
            p["features"]["release"]["rules"][0]["variations"] = [v["feature"]["baseline"], v["invalid"]]
        with pytest.raises(ValueError): FeatureRuntime(catalog, trust_policy="local-test").activate(bundle(p), 0, NOW)

def test_local_measurement_and_forged_attribution_fences():
    with pytest.raises(ValueError): FeatureRuntime(GOLDEN["catalog"], events=Events())
    events, assignments = Events(), Assignments()
    r = setup(events=events, assignments=assignments)
    d = r.evaluate("experiment", "web", GOLDEN["context"], now=NOW)
    event = event_for(d)
    for changes in ({"evidence": {**event["evidence"], "unit_key": "forged"}}, {"application_id": "other"}, {"surface_id": "other"}, {"context_scope": "other"}):
        with pytest.raises(ValueError): r.record_event({**event, **changes}, NOW)
    r.record_event(event, NOW)
    outcome = event_for(d, "outcome", "transition-1")
    outcome["evidence"]["transition_key"] = "unrelated"
    with pytest.raises(ValueError): r.record_event(outcome, NOW)
    with pytest.raises(ValueError): setup(events=events, assignments=assignments).record_event(event_for(d, "outcome", "transition-2"), NOW)

@pytest.mark.parametrize("mode", ["absent", "expired", "invalid", "unavailable"])
def test_known_kill_dominates_true_baseline_without_usable_bundle(mode):
    c = deepcopy(GOLDEN["catalog"])
    c["features"]["release"].update({"failure": "baseline", "baseline": True})
    controls = Controls()
    r = custom(c, controls=controls)
    r.update_kills(kill(2, ["release"]), "local-fixture", NOW)
    state = controls.read(GOLDEN["scope"])
    if mode == "absent": state["bundle"] = None
    if mode == "expired": state["bundle"]["expires_at"] = NOW
    if mode == "invalid": state["bundle"]["payload_sha256"] = "0" * 64
    controls.read = lambda scope: deepcopy(state)
    def unavailable(*args, **kwargs):
        raise AssertionError("provider must not be reached for a known kill")
    r._provider = unavailable
    d = r.evaluate("release", "web", GOLDEN["context"], now=NOW)
    assert d["value"] is False and d["status"] == "denied"
    s = r.snapshot(["release"], "web", GOLDEN["context"], now=NOW)
    assert s["decisions"]["release"]["value"] is False
    assert s["decisions"]["release"]["kill_generation"] == s["kill_generation"] == 2

@pytest.mark.parametrize("bad", HARDENING["invalid_identifiers"])
def test_ascii_identity_seed_namespace_rejections(bad):
    r = setup()
    p = deepcopy(GOLDEN["payload"])
    p["features"]["experiment"]["rules"][0]["seed"] = bad
    with pytest.raises(ValueError): r.activate(bundle(p, 2), 1, NOW)
    with pytest.raises(ValueError): r.evaluate("release", "web", {**GOLDEN["context"], "assignment_key": bad}, now=NOW)
    c = deepcopy(GOLDEN["catalog"])
    c["scope"]["feature_namespace"] = bad
    with pytest.raises(ValueError): FeatureRuntime(c)

@pytest.mark.parametrize("error", [RuntimeError, OSError])
@pytest.mark.parametrize("mode", ["killed", "baseline", "deny"])
def test_verifier_availability_outage_preserves_kill_and_ordinary_fallbacks(error, mode):
    catalog = deepcopy(GOLDEN["catalog"])
    catalog["features"]["release"].update({"baseline": True, "failure": "deny" if mode == "deny" else "baseline"})
    release, kills = bundle(), kill(disabled=["release"] if mode == "killed" else [])
    class Verifier:
        outage = False
        calls = 0
        def verify(self, message, ref, purpose):
            self.calls += 1
            if self.outage:
                raise error("verifier unavailable")
            return ref == "local-fixture" and message == (approval_message(release) if purpose == "release" else kill_message(kills))
    verifier = Verifier()
    runtime = FeatureRuntime(catalog, controls=Controls(), verifier=verifier)
    runtime.activate(release, 0, NOW)
    runtime.update_kills(kills, "local-fixture", NOW)
    verifier.outage = True
    def blocked(*args, **kwargs):
        raise AssertionError("provider must not run with unavailable approval")
    runtime._provider = blocked
    expected = {"release": (False, "denied") if mode != "baseline" else (True, "baseline"), "child": ("disabled", "denied")}
    reason = "disabled_or_excluded" if mode == "killed" else "invalid_bundle"
    for key, (value, status) in expected.items():
        decision = runtime.evaluate(key, "web", GOLDEN["context"], now=NOW)
        assert (decision["value"], decision["status"], decision["reason"]) == (value, status, reason)
    before = verifier.calls
    snapshot = runtime.snapshot(list(expected), "web", GOLDEN["context"], now=NOW)
    assert verifier.calls == before + 1
    assert snapshot["configuration_revision"] == snapshot["kill_generation"] == 1
    for key, (value, status) in expected.items():
        decision = snapshot["decisions"][key]
        assert (decision["value"], decision["status"], decision["reason"]) == (value, status, reason)
        assert decision["configuration_revision"] == decision["kill_generation"] == 1

@pytest.mark.parametrize("error", [AssertionError, AttributeError])
def test_unexpected_verifier_programmer_errors_propagate(error):
    class Verifier:
        outage = False
        def verify(self, message, ref, purpose):
            if self.outage:
                raise error("verifier programming defect")
            return ref == "local-fixture" and message == (approval_message(bundle()) if purpose == "release" else kill_message(kill()))
    verifier = Verifier()
    runtime = FeatureRuntime(GOLDEN["catalog"], controls=Controls(), verifier=verifier)
    runtime.activate(bundle(), 0, NOW)
    runtime.update_kills(kill(), "local-fixture", NOW)
    verifier.outage = True
    with pytest.raises(error, match="verifier programming defect"):
        runtime.evaluate("release", "web", GOLDEN["context"], now=NOW)
    with pytest.raises(error, match="verifier programming defect"):
        runtime.snapshot(["release"], "web", GOLDEN["context"], now=NOW)

def test_shared_unsupported_value_schemas():
    for schema in HARDENING["invalid_schemas"]:
        c = deepcopy(GOLDEN["catalog"])
        c["features"]["child"]["value_schema"] = schema
        with pytest.raises(ValueError): FeatureRuntime(c)

def test_json_declaration_cannot_disguise_enabled_boolean_disabled_value():
    c = deepcopy(GOLDEN["catalog"])
    c["features"]["release"].update({"value_type": "json", "baseline": True, "disabled_value": True})
    with pytest.raises(ValueError): FeatureRuntime(c)

def test_explicit_payload_bytes_preserve_unicode_fraction_semantics_without_generic_digest():
    spec = HARDENING["unicode_numeric"]
    c = deepcopy(GOLDEN["catalog"])
    c["features"] = {"data": {"value_type": "json", "baseline": None, "disabled_value": None,
                              "allowed_values": [None, spec["value"]], "failure": "deny",
                              "surfaces": ["web"], "ancestors": []}}
    b = bundle()
    b["payload_bytes"] = spec["payload_bytes"]
    b["payload_sha256"] = hashlib.sha256(b["payload_bytes"].encode()).hexdigest()
    assert approval_message(b).decode() == spec["approval_message"]
    integral = deepcopy(b)
    for field in ("revision", "catalog_revision", "created_at", "expires_at"):
        integral[field] = float(integral[field])
    assert approval_message(integral).decode() == spec["approval_message"]
    with pytest.raises(ValueError): approval_message({**b, "revision": 1.5})
    assert kill_message(kill(1)) == kill_message({**kill(1), "generation": 1.0})
    r = FeatureRuntime(c, trust_policy="local-test")
    r.activate(b, 0, NOW)
    r.update_kills(kill(1), "local-fixture", NOW)
    d = r.evaluate("data", "web", GOLDEN["context"], now=NOW)
    assert d["value"]["é"] == "café" and d["value"]["𝄞"] == "music"
    assert d["value"]["one"] == 1 and d["value"]["tiny"] == 1e-7
    bad = bundle()
    bad["payload_bytes"] = spec["unsafe_payload_bytes"]
    bad["payload_sha256"] = hashlib.sha256(bad["payload_bytes"].encode()).hexdigest()
    unrestricted = deepcopy(c)
    unrestricted["features"]["data"].pop("allowed_values")
    with pytest.raises(ValueError): FeatureRuntime(unrestricted, trust_policy="local-test").activate(bad, 0, NOW)
    unsafe = deepcopy(unrestricted)
    unsafe["features"]["data"]["baseline"] = spec["unsafe_integer"]
    with pytest.raises(ValueError): FeatureRuntime(unsafe)
    unsafe_enum = deepcopy(c)
    unsafe_enum["features"]["data"]["allowed_values"].append(spec["unsafe_integer"])
    with pytest.raises(ValueError): FeatureRuntime(unsafe_enum)
