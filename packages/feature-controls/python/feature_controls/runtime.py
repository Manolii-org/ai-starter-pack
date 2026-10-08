import uuid
from copy import deepcopy
from typing import Any

from growthbook import GrowthBook, InMemoryStickyBucketService

from .lifecycle import Lifecycle, clock
from .types import (
    Assignment,
    AssignmentKey,
    AssignmentStore,
    Catalog,
    Context,
    Decision,
    Event,
    EventSink,
    Feature,
    Snapshot,
)
from .validation import (
    allocation_id,
    ascii_id,
    attributes_of,
    boundary_of,
    matches,
    payload_of,
    validate,
)


class ReadOnlyStickyFixture(InMemoryStickyBucketService):
    def __init__(self, doc: dict[str, Any] | None):
        super().__init__()
        if doc:
            self.docs[self.get_key(doc["attributeName"], doc["attributeValue"])] = deepcopy(doc)

    def save_assignments(self, doc: dict[str, Any]) -> None:
        pass

class FeatureRuntime(Lifecycle):
    def __init__(self, catalog: Catalog, *, assignments: AssignmentStore | None = None,
                 events: EventSink | None = None, **kwargs: Any):
        super().__init__(catalog, **kwargs)
        if events and (self.trust_policy != "local-test" or getattr(events, "test_only", False) is not True):
            raise ValueError("measurement is local-test only")
        self.assignments, self.events = assignments, events
        self._issued: dict[str, Decision] = {}
        self._exposed: set[str] = set()

    def evaluate(self, key: str, surface_id: str, context: Context, *, preview: bool = False, now: int | None = None) -> Decision:
        return self._evaluate(key, surface_id, deepcopy(context), preview=preview, now=now)

    def _evaluate(self, key: str, surface_id: str, context: Context, *, preview: bool = False, now: int | None = None,
                  capture: dict[str, Any] | None = None) -> Decision:
        now = clock(now)
        ascii_id(key)
        ascii_id(surface_id)
        validate("context", context)
        ascii_id(context["assignment_key"])
        attributes = attributes_of(context)
        feature = self._catalog["features"].get(key)
        base: Decision = {"decision_id": str(uuid.uuid4()), "feature_key": key, "scope": deepcopy(self._catalog["scope"]),
                          "application_id": context["application_id"], "context_scope": context["context_scope"], "surface_id": surface_id, "value": deepcopy(feature["disabled_value"] if feature else None), "reason": "unknown",
                          "status": "denied", "preview": preview, "expires_at": min(context["expires_at"], now + 60000),
                          "configuration_revision": None, "kill_generation": 0}
        if feature is None:
            return base
        def fail(reason: str) -> Decision:
            return {**base, "reason": reason, "value": deepcopy(feature["baseline"] if feature["failure"] == "baseline" else feature["disabled_value"]),
                    "status": "baseline" if feature["failure"] == "baseline" else "denied"}
        if context["scope"] != self._catalog["scope"] or context["surface_id"] != surface_id:
            return {**base, "reason": "context_binding_mismatch"}
        if not context["authorized"]:
            return {**base, "reason": "unauthorized"}
        if context["application_id"] not in feature.get("applications", self._catalog["applications"]):
            return {**base, "reason": "application_ineligible"}
        if set(context["excluded_groups"]) & set(attributes["groups"]):
            return {**base, "reason": "group_excluded"}
        if context["expires_at"] <= now:
            return {**base, "reason": "context_expired"}
        if surface_id not in feature["surfaces"]:
            return {**base, "reason": "surface_ineligible"}
        if self._excluded(key, self.local_disabled | set(context["excluded"])):
            return {**base, "reason": "disabled_or_excluded"}
        if capture and capture.get("unavailable"):
            return {**base, "reason": "controls_unavailable"}
        try:
            state = capture["state"] if capture else self._observed_state(now, preview)
        except (ValueError, TypeError, KeyError, RuntimeError, OSError):
            return {**base, "reason": "controls_unavailable"}
        kill = state["kill"]
        if kill:
            try:
                validate("kill", kill)
                if kill["scope"] != self._catalog["scope"]:
                    raise ValueError("kill scope mismatch")
            except ValueError:
                return {**base, "reason": "invalid_kills"}
            base["kill_generation"] = kill["generation"]
        disabled = self.local_disabled | set(kill["disabled"] if kill else []) | set(context["excluded"])
        if self._excluded(key, disabled):
            return {**base, "reason": "disabled_or_excluded"}
        if self.require_fresh_kills and (kill is None or kill["expires_at"] <= now):
            return {**base, "reason": "kills_expired"}
        if not context["eligible"]:
            return {**base, "reason": "ineligible"}
        bundle = state["bundle"]
        if bundle is None:
            return fail("bundle_unavailable")
        base["configuration_revision"] = bundle["revision"]
        base["expires_at"] = min(base["expires_at"], bundle["expires_at"], kill["expires_at"] if kill else base["expires_at"])
        if bundle["expires_at"] <= now or bundle["created_at"] > now:
            return fail("bundle_expired")
        if capture and capture.get("invalid_bundle"):
            return fail("invalid_bundle")
        try:
            if capture and capture.get("payload") is not None:
                payload = capture["payload"]
            else:
                payload = payload_of(bundle, self._catalog)
                self._approved(bundle)
        except (ValueError, TypeError, KeyError, RuntimeError, OSError):
            return fail("invalid_bundle")
        if "experiment" in feature and (self.assignments is None or self.assignments.durable is not True) and not preview:
            return fail("assignment_store_required")
        try:
            assignment_boundary = boundary_of(feature, context)
            attributes["id"] = allocation_id(context, assignment_boundary)
            assignment_key: AssignmentKey | None = None
            if "experiment" in feature:
                exp = feature["experiment"]
                assignment_key = {"scope": self._catalog["scope"], "assignment_boundary": assignment_boundary,
                                  "experiment_key": exp["key"], "allocation_epoch": exp["epoch"], "unit_key": context["assignment_key"]}
            assignment = self.assignments.read(deepcopy(assignment_key)) if assignment_key and self.assignments and self.assignments.durable else None
            result = self._provider(payload, key, attributes, feature, assignment)
            if result.experimentResult and result.experimentResult.inExperiment and not preview and assignment_key and self.assignments:
                assignment = self.assignments.create_if_absent(deepcopy(assignment_key), result.experimentResult.key)
                ascii_id(assignment["assignment_id"])
                ascii_id(assignment["variant"])
                result = self._provider(payload, key, attributes, feature, assignment)
                if not result.experimentResult or not result.experimentResult.stickyBucketUsed or result.experimentResult.key != assignment["variant"]:
                    return fail("assignment_conflict")
            if not matches(result.value, feature):
                return fail("invalid_value")
            decision: Decision = {**base, "value": result.value, "status": "resolved",
                                  "reason": "preview" if preview else "experiment" if result.experimentResult and result.experimentResult.inExperiment else "released"}
            if assignment and result.experimentResult and result.experimentResult.inExperiment and not preview:
                decision["assignment"] = assignment
                decision["allocation_epoch"] = feature["experiment"]["epoch"]
                decision["unit_key"] = context["assignment_key"]
            if decision.get("assignment") and self.trust_policy == "local-test":
                for decision_id, old in list(self._issued.items()):
                    if old["expires_at"] <= now:
                        del self._issued[decision_id]
                        self._exposed.discard(decision_id)
                if len(self._issued) >= 1000:
                    return fail("decision_capacity")
                self._issued[decision["decision_id"]] = deepcopy(decision)
            return decision
        except (ValueError, TypeError, KeyError, RuntimeError, OSError):
            return fail("provider_or_assignment_unavailable")

    def _excluded(self, key: str, excluded: set[str], visited: set[str] | None = None) -> bool:
        visited = set() if visited is None else visited
        if key in visited:
            return False
        visited.add(key)
        return key in excluded or any(
            ancestor not in visited and self._excluded(ancestor, excluded, visited)
            for ancestor in self._catalog["features"][key]["ancestors"]
        )

    @staticmethod
    def _provider(payload: dict[str, Any], key: str, attributes: dict[str, Any], feature: Feature, assignment: Assignment | None):
        doc = None
        if assignment and "experiment" in feature:
            exp = feature["experiment"]
            doc = {"attributeName": "id", "attributeValue": attributes["id"],
                   "assignments": {f"{exp['key']}__{exp['epoch']}": assignment["variant"]}}
        store = ReadOnlyStickyFixture(doc) if "experiment" in feature else None
        gb = GrowthBook(attributes=deepcopy(attributes), sticky_bucket_service=store)
        try:
            gb.set_payload(deepcopy(payload))
            return gb.eval_feature(key)
        finally:
            gb.destroy()

    def snapshot(self, keys: list[str], surface_id: str, context: Context, *, preview: bool = False, now: int | None = None) -> Snapshot:
        keys, context = list(keys), deepcopy(context)
        validate("context", context)
        ascii_id(surface_id)
        if len(keys) > 64:
            raise ValueError("snapshot too large")
        now = clock(now)
        expires = min(context["expires_at"], now + 60000)
        capture: dict[str, Any] = {"state": {"bundle": None, "kill": None, "time_highwater": 0}}
        try:
            capture["state"] = self._observed_state(now, preview)
        except (ValueError, TypeError, KeyError, RuntimeError, OSError):
            capture["unavailable"] = True
        if capture["state"]["bundle"] and not capture.get("unavailable"):
            try:
                capture["payload"] = payload_of(capture["state"]["bundle"], self._catalog)
                self._approved(capture["state"]["bundle"])
            except (ValueError, TypeError, KeyError, RuntimeError, OSError):
                capture["invalid_bundle"] = True
        decisions = {}
        for key in keys:
            decision = self._evaluate(key, surface_id, context, preview=preview, now=now, capture=capture)
            expires = min(expires, decision["expires_at"])
            decision["configuration_revision"] = capture["state"]["bundle"]["revision"] if capture["state"]["bundle"] else None
            decision["kill_generation"] = capture["state"]["kill"]["generation"] if capture["state"]["kill"] else 0
            decisions[key] = {name: decision[name] for name in ("value", "reason", "status", "expires_at", "configuration_revision", "kill_generation")}
        for decision in decisions.values():
            decision["expires_at"] = min(decision["expires_at"], expires)
        return {"schema_version": 1, **self._catalog["scope"], "application_id": context["application_id"], "context_scope": context["context_scope"],
                "configuration_revision": capture["state"]["bundle"]["revision"] if capture["state"]["bundle"] else 0,
                "kill_generation": capture["state"]["kill"]["generation"] if capture["state"]["kill"] else 0,
                "time_highwater": capture["state"]["time_highwater"],
                "surface_id": surface_id, "generated_at": now, "expires_at": expires, "decisions": decisions}

    def record_event(self, event: Event, now: int | None = None) -> bool:
        event = deepcopy(event)
        if self.trust_policy != "local-test" or not self.events or self.events.test_only is not True:
            raise ValueError("measurement is local-test only")
        validate("event", event)
        now = clock(now)
        if self.events is None or self.events.durable is not True:
            raise ValueError("durable event sink required")
        d = self._issued.get(event["decision_id"])
        if not d or d["preview"] or d["reason"] != "experiment" or not d.get("assignment") or d["expires_at"] <= now or \
                event["scope"] != d["scope"] or not now - 60000 <= event["timestamp"] <= now or \
                event["application_id"] != d["application_id"] or event["surface_id"] != d["surface_id"] or event["context_scope"] != d["context_scope"] or \
                event["feature_key"] != d["feature_key"] or event["assignment_id"] != d["assignment"]["assignment_id"] or \
                event["variant"] != d["assignment"]["variant"] or event["configuration_revision"] != d["configuration_revision"] or \
                event["evidence"]["unit_key"] != d["unit_key"] or \
                event["allocation_epoch"] != d["allocation_epoch"] or \
                ((event["kind"] == "outcome") != (event["evidence"]["source"] == "business-transition")) or \
                (event["kind"] == "outcome" and (event["decision_id"] not in self._exposed or event["event_id"] != event["evidence"]["transition_key"])):
            raise ValueError("event attribution mismatch")
        added = self.events.append_if_absent(deepcopy(event))
        if event["kind"] == "exposure":
            self._exposed.add(event["decision_id"])
        return added
