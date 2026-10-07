from copy import deepcopy
import uuid
from typing import Any
from growthbook import GrowthBook, InMemoryStickyBucketService
from .lifecycle import Lifecycle, clock
from .types import Assignment, AssignmentKey, AssignmentStore, Catalog, Context, Decision, Event, EventSink, Feature, Snapshot
from .validation import ascii_id, matches, payload_of, validate

class FeatureRuntime(Lifecycle):
    def __init__(self, catalog: Catalog, *, assignments: AssignmentStore | None = None,
                 events: EventSink | None = None, **kwargs: Any):
        super().__init__(catalog, **kwargs)
        self.assignments, self.events = assignments, events
        self._issued: dict[str, Decision] = {}
        self._exposed: set[str] = set()

    def evaluate(self, key: str, surface: str, context: Context, *, preview: bool = False, now: int | None = None) -> Decision:
        now = clock(now)
        ascii_id(key)
        ascii_id(surface)
        validate("context", context)
        ascii_id(context["assignment_key"])
        feature = self.catalog["features"].get(key)
        base: Decision = {"decision_id": str(uuid.uuid4()), "feature_key": key, "scope": deepcopy(self.catalog["scope"]),
                          "surface": surface, "value": deepcopy(feature["baseline"] if feature else None), "reason": "unknown",
                          "status": "denied", "preview": preview, "expires_at": min(context["expires_at"], now + 60000),
                          "configuration_revision": None, "kill_generation": 0}
        if feature is None:
            return base
        def fail(reason: str) -> Decision:
            return {**base, "reason": reason, "status": "baseline" if feature["failure"] == "baseline" else "denied"}
        if not context["authorized"]:
            return {**base, "reason": "unauthorized"}
        if context["expires_at"] <= now:
            return fail("context_expired")
        if surface not in feature["surfaces"]:
            return fail("surface_ineligible")
        try:
            state = self._state()
        except Exception:
            return fail("controls_unavailable")
        kill = state["kill"]
        if kill:
            try:
                validate("kill", kill)
                if kill["scope"] != self.catalog["scope"]:
                    raise ValueError("kill scope mismatch")
            except ValueError:
                return fail("invalid_kills")
            base["kill_generation"] = kill["generation"]
        disabled = self.local_disabled | set(kill["disabled"] if kill else []) | set(context["excluded"])
        if self._excluded(key, disabled):
            return fail("disabled_or_excluded")
        if self.require_fresh_kills and (kill is None or kill["expires_at"] <= now):
            return fail("kills_expired")
        if not context["eligible"]:
            return fail("ineligible")
        bundle = state["bundle"]
        if bundle is None:
            return fail("bundle_unavailable")
        base["configuration_revision"] = bundle["revision"]
        base["expires_at"] = min(base["expires_at"], bundle["expires_at"], kill["expires_at"] if kill else base["expires_at"])
        if bundle["expires_at"] <= now or bundle["created_at"] > now:
            return fail("bundle_expired")
        try:
            payload = payload_of(bundle, self.catalog)
            self._approved(bundle)
        except (ValueError, TypeError, KeyError):
            return fail("invalid_bundle")
        if "experiment" in feature and (self.assignments is None or self.assignments.durable is not True) and not preview:
            return fail("assignment_store_required")
        try:
            assignment_key: AssignmentKey | None = None
            if "experiment" in feature:
                exp = feature["experiment"]
                assignment_key = {"scope": self.catalog["scope"], "experiment_key": exp["key"],
                                  "allocation_epoch": exp["epoch"], "unit_key": context["assignment_key"]}
            assignment = self.assignments.read(assignment_key) if assignment_key and self.assignments and self.assignments.durable else None
            result = self._provider(payload, key, context["assignment_key"], feature, assignment)
            if result.experimentResult and result.experimentResult.inExperiment and not preview and assignment_key and self.assignments:
                assignment = self.assignments.create_if_absent(assignment_key, result.experimentResult.key)
                ascii_id(assignment["assignment_id"])
                ascii_id(assignment["variant"])
                result = self._provider(payload, key, context["assignment_key"], feature, assignment)
                if not result.experimentResult or not result.experimentResult.stickyBucketUsed or result.experimentResult.key != assignment["variant"]:
                    return fail("assignment_conflict")
            if not matches(result.value, feature):
                return fail("invalid_value")
            decision: Decision = {**base, "value": result.value, "status": "resolved",
                                  "reason": "preview" if preview else "experiment" if result.experimentResult and result.experimentResult.inExperiment else "released"}
            if assignment and result.experimentResult and result.experimentResult.inExperiment and not preview:
                decision["assignment"] = assignment
                decision["allocation_epoch"] = feature["experiment"]["epoch"]
                for decision_id, old in list(self._issued.items()):
                    if old["expires_at"] <= now:
                        del self._issued[decision_id]
                        self._exposed.discard(decision_id)
                if len(self._issued) >= 1000:
                    return fail("decision_capacity")
                self._issued[decision["decision_id"]] = deepcopy(decision)
            return decision
        except Exception:
            return fail("provider_or_assignment_unavailable")

    def _excluded(self, key: str, excluded: set[str]) -> bool:
        return key in excluded or any(self._excluded(a, excluded) for a in self.catalog["features"][key]["ancestors"])

    @staticmethod
    def _provider(payload: dict[str, Any], key: str, unit: str, feature: Feature, assignment: Assignment | None):
        store = InMemoryStickyBucketService() if "experiment" in feature else None
        if store and assignment:
            exp = feature["experiment"]
            store.save_assignments({"attributeName": "id", "attributeValue": unit,
                                    "assignments": {f"{exp['key']}__{exp['epoch']}": assignment["variant"]}})
        gb = GrowthBook(attributes={"id": unit}, sticky_bucket_service=store)
        try:
            gb.set_payload(deepcopy(payload))
            return gb.eval_feature(key)
        finally:
            gb.destroy()

    def snapshot(self, keys: list[str], surface: str, context: Context, *, preview: bool = False, now: int | None = None) -> Snapshot:
        if len(keys) > 1000:
            raise ValueError("snapshot too large")
        now = clock(now)
        expires = min(context["expires_at"], now + 60000)
        decisions = {}
        for key in keys:
            decision = self.evaluate(key, surface, context, preview=preview, now=now)
            expires = min(expires, decision["expires_at"])
            decisions[key] = {name: decision[name] for name in ("value", "reason", "expires_at", "configuration_revision", "kill_generation")}
        for decision in decisions.values():
            decision["expires_at"] = min(decision["expires_at"], expires)
        return {"schema_version": 1, "application": self.catalog["scope"]["application"], "environment": self.catalog["scope"]["environment"],
                "surface": surface, "generated_at": now, "expires_at": expires, "decisions": decisions}

    def record_event(self, event: Event, now: int | None = None) -> bool:
        validate("event", event)
        now = clock(now)
        if self.events is None or self.events.durable is not True:
            raise ValueError("durable event sink required")
        d = self._issued.get(event["decision_id"])
        if not d or d["preview"] or d["reason"] != "experiment" or not d.get("assignment") or d["expires_at"] <= now or \
                event["scope"] != d["scope"] or not now - 60000 <= event["timestamp"] <= now or \
                event["feature_key"] != d["feature_key"] or event["assignment_id"] != d["assignment"]["assignment_id"] or \
                event["variant"] != d["assignment"]["variant"] or event["configuration_revision"] != d["configuration_revision"] or \
                event["allocation_epoch"] != d["allocation_epoch"] or \
                ((event["kind"] == "outcome") != (event["evidence"]["source"] == "business-transition")) or \
                (event["kind"] == "outcome" and event["decision_id"] not in self._exposed):
            raise ValueError("event attribution mismatch")
        added = self.events.append_if_absent(deepcopy(event))
        if event["kind"] == "exposure":
            self._exposed.add(event["decision_id"])
        return added
