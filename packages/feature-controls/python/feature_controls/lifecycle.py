import json
import hashlib
import re
import time
from copy import deepcopy

from .types import (
    ApprovalVerifier,
    Catalog,
    ControlState,
    DurableControlStore,
    KillState,
    ReleaseBundle,
    ReleaseManifest,
)
from .validation import approval_message, compatible_release_manifest, payload_of, release_catalog_features, validate, validate_catalog


def clock(now: int | None) -> int:
    value = int(time.time() * 1000) if now is None else now
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise ValueError("invalid clock")
    return value

def kill_message(kill: KillState) -> bytes:
    validate("kill", kill)
    if set(kill.get("exceptions", {})) - set(kill["disabled"]):
        raise ValueError("exception without disable")
    s = kill["scope"]
    message = ["feature-controls/kill/v2" if "exceptions" in kill else "feature-controls/kill/v1",
               s["ecosystem_id"], s["deployment_id"], s["environment_id"], s["feature_namespace"],
               int(kill["generation"]), int(kill["expires_at"]), kill["disabled"]]
    if "exceptions" in kill:
        entries = []
        for key, entry in sorted(kill["exceptions"].items()):
            if not entry["reason"].strip():
                raise ValueError("empty disable reason")
            entries.append([key, entry["owner"], entry["reason"], int(entry["review_at"])])
        message.append(entries)
    return json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode()

class Lifecycle:
    def __init__(self, catalog: Catalog, *, controls: DurableControlStore | None = None,
                 verifier: ApprovalVerifier | None = None, trust_policy: str = "verified",
                 local_disabled: list[str] | None = None, require_fresh_kills: bool = True,
                 release_manifest: ReleaseManifest | None = None, release_source_sha: str | None = None):
        self._catalog = deepcopy(catalog)
        validate_catalog(self._catalog)
        for feature in self._catalog["features"].values():
            if "experiment" in feature:
                feature["experiment"]["epoch"] = int(feature["experiment"]["epoch"])
        self.controls, self.verifier, self.trust_policy = controls, verifier, trust_policy
        if trust_policy == "local-test" and controls is not None and getattr(controls, "test_only", False) is not True:
            raise ValueError("local trust requires a test-only store")
        self.local_disabled = set(local_disabled or [])
        self.release_manifest = deepcopy(release_manifest)
        self.release_source_sha = release_source_sha
        if self.release_manifest is not None:
            definitions = release_catalog_features(self.release_manifest)
            if definitions and re.fullmatch(r"[0-9a-f]{40}", release_source_sha or "") is None:
                raise ValueError("release source binding required")
            for key, expected in definitions.items():
                if self._catalog["features"].get(key) != expected:
                    raise ValueError("release catalogue mismatch")
                if self.release_manifest["features"][key]["state"] == "development":
                    self.local_disabled.add(key)
        if self.local_disabled - set(catalog["features"]):
            raise ValueError("unknown local disable")
        self.require_fresh_kills = require_fresh_kills
        self._local: ControlState = {"bundle": None, "kill": None, "time_highwater": 0}

    @property
    def catalog(self) -> Catalog:
        return deepcopy(self._catalog)

    def _observed_state(self, now: int, preview: bool = False) -> ControlState:
        for _ in range(4):
            state = self._state()
            clock(state["time_highwater"])
            if now < state["time_highwater"]:
                raise ValueError("clock rollback")
            if preview or now == state["time_highwater"]:
                return state
            next_state = {**state, "time_highwater": now}
            if self.controls is not None and self.controls.durable is True:
                if not self.controls.compare_and_swap(self._catalog["scope"], state, next_state):
                    continue
            else:
                self._swap(state, next_state)
            return next_state
        raise ValueError("clock fence conflict")

    def _state(self) -> ControlState:
        if self.controls is not None and self.controls.durable is True:
            state = deepcopy(self.controls.read(self._catalog["scope"]))
            validate("control_state", state)
            state["time_highwater"] = int(state["time_highwater"])
            if state["bundle"] and state["bundle"]["scope"] != self._catalog["scope"] or state["kill"] and state["kill"]["scope"] != self._catalog["scope"]:
                raise ValueError("control scope mismatch")
            return state
        if self.trust_policy != "local-test":
            raise ValueError("durable control store required")
        return deepcopy(self._local)

    def _swap(self, current: ControlState, next_state: ControlState) -> None:
        if self.controls is not None and self.controls.durable is True:
            if not self.controls.compare_and_swap(self._catalog["scope"], current, next_state):
                raise ValueError("activation conflict")
        else:
            if self._local != current:
                raise ValueError("activation conflict")
            self._local = deepcopy(next_state)

    def _approved(self, bundle: ReleaseBundle) -> None:
        if self.release_manifest is not None and self.release_manifest["features"] and \
                ("deployment" not in bundle or not compatible_release_manifest(self.release_manifest, bundle["deployment"]["manifest"])):
            raise ValueError("release does not match deployed source policy")
        if self.trust_policy == "local-test":
            return
        if self.verifier is None or not self.verifier.verify(approval_message(bundle), bundle["approval_ref"], "release"):
            raise ValueError("release approval required")

    def _stored_kill_approved(self, kill: KillState) -> None:
        if self.trust_policy == "local-test":
            return
        verify_kill = getattr(self.verifier, "verify_kill", None)
        if not callable(verify_kill) or verify_kill(deepcopy(kill)) is not True:
            raise ValueError("stored kill approval required")

    def activate(self, bundle: ReleaseBundle, expected_revision: int, now: int | None = None) -> None:
        bundle = deepcopy(bundle)
        payload_of(bundle, self._catalog)
        if now is not None:
            clock(now)
        if type(expected_revision) is not int or now is not None and (bundle["created_at"] > now + 30000 or bundle["expires_at"] <= now):
            raise ValueError("stale bundle")
        self._approved(bundle)
        current = self._state()
        # Sample implicit time after I/O that can overlap durable clock observations.
        now = clock(now)
        if bundle["created_at"] > now + 30000 or bundle["expires_at"] <= now:
            raise ValueError("stale bundle")
        if now < current["time_highwater"]:
            raise ValueError("clock rollback")
        revision = current["bundle"]["revision"] if current["bundle"] else 0
        if revision != expected_revision or bundle["revision"] <= revision:
            raise ValueError("revision conflict or replay")
        self._swap(current, {"bundle": bundle, "kill": current["kill"], "time_highwater": now})

    def inspect_for_release(self, now: int | None = None) -> ControlState:
        """Private owning-process evidence; never expose this state to browsers."""
        if now is not None:
            clock(now)
        state = self._state()
        bundle, kill = state["bundle"], state["kill"]
        if not bundle or not kill:
            raise ValueError("release controls unavailable or stale")
        payload_of(bundle, self._catalog)
        kill_message(kill)
        self._approved(bundle)
        self._stored_kill_approved(kill)
        now = clock(now)
        if now < state["time_highwater"] or bundle["created_at"] > now or bundle["expires_at"] <= now or kill["expires_at"] <= now:
            raise ValueError("release controls unavailable or stale")
        return deepcopy(state)

    def update_kills(self, kill: KillState, approval_ref: str, now: int | None = None) -> None:
        kill = deepcopy(kill)
        message = kill_message(kill)
        if now is not None:
            clock(now)
        if kill["scope"] != self._catalog["scope"] or now is not None and kill["expires_at"] <= now or set(kill["disabled"]) - set(self._catalog["features"]):
            raise ValueError("invalid kill binding")
        if self.trust_policy != "local-test" and (self.verifier is None or not self.verifier.verify(message, approval_ref, "kill")):
            raise ValueError("kill approval required")
        current = self._state()
        now = clock(now)
        if kill["expires_at"] <= now:
            raise ValueError("invalid kill binding")
        generation = current["kill"]["generation"] if current["kill"] else 0
        if now < current["time_highwater"]:
            raise ValueError("clock rollback")
        if kill["generation"] <= generation:
            raise ValueError("kill replay")
        self._swap(current, {"bundle": current["bundle"], "kill": kill, "time_highwater": now})

    def previous_for_deployment(self, now: int | None = None) -> ControlState:
        """Publisher-only prior approval check before adding a new catalogue."""
        if now is not None:
            clock(now)
        state = self._state()
        bundle, kill = state["bundle"], state["kill"]
        if not bundle or not kill:
            raise ValueError("previous controls unavailable")
        validate("bundle", bundle)
        kill_message(kill)
        if len(bundle["payload_bytes"].encode()) > 1048576 or hashlib.sha256(bundle["payload_bytes"].encode()).hexdigest() != bundle["payload_sha256"]:
            raise ValueError("invalid previous release")
        if self.trust_policy != "local-test" and (self.verifier is None or not self.verifier.verify(approval_message(bundle), bundle["approval_ref"], "release")):
            raise ValueError("previous approval required")
        self._stored_kill_approved(kill)
        now = clock(now)
        if now < state["time_highwater"] or bundle["created_at"] > now or bundle["expires_at"] <= now or kill["expires_at"] <= now:
            raise ValueError("previous controls stale")
        return deepcopy(state)
