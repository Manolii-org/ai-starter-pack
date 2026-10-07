from typing import Any, Literal, Protocol, TypedDict

PROVIDER_SEMANTICS = "growthbook:js-1.8.0:py-3.2.0:hash-2"

class Scope(TypedDict):
    namespace: str
    application: str
    environment: str

class Experiment(TypedDict):
    key: str
    epoch: int

class Feature(TypedDict, total=False):
    value_type: Literal["boolean", "number", "string", "json"]
    baseline: Any
    failure: Literal["baseline", "deny"]
    surfaces: list[str]
    ancestors: list[str]
    experiment: Experiment

class Catalog(TypedDict):
    schema_version: Literal[1]
    scope: Scope
    revision: int
    features: dict[str, Feature]

class ReleaseBundle(TypedDict):
    schema_version: Literal[1]
    scope: Scope
    revision: int
    catalog_revision: int
    created_at: int
    expires_at: int
    provider_semantics: str
    payload_sha256: str
    payload_bytes: str
    approval_ref: str

class KillState(TypedDict):
    schema_version: Literal[1]
    scope: Scope
    generation: int
    expires_at: int
    disabled: list[str]

class Context(TypedDict):
    assignment_key: str
    authorized: bool
    eligible: bool
    expires_at: int
    excluded: list[str]

class ControlState(TypedDict):
    bundle: ReleaseBundle | None
    kill: KillState | None

class DurableControlStore(Protocol):
    durable: Literal[True]
    test_only: bool
    def read(self, scope: Scope) -> ControlState: ...
    def compare_and_swap(self, scope: Scope, expected: ControlState, next_state: ControlState) -> bool: ...

class ApprovalVerifier(Protocol):
    def verify(self, message: bytes, approval_ref: str, purpose: Literal["release", "kill"]) -> bool: ...

class Assignment(TypedDict):
    assignment_id: str
    variant: str

class AssignmentKey(TypedDict):
    scope: Scope
    experiment_key: str
    allocation_epoch: int
    unit_key: str

class AssignmentStore(Protocol):
    durable: Literal[True]
    def read(self, key: AssignmentKey) -> Assignment | None: ...
    def create_if_absent(self, key: AssignmentKey, variant: str) -> Assignment: ...

class Decision(TypedDict, total=False):
    decision_id: str
    feature_key: str
    scope: Scope
    surface: str
    value: Any
    reason: str
    expires_at: int
    configuration_revision: int | None
    kill_generation: int
    status: Literal["resolved", "baseline", "denied"]
    preview: bool
    assignment: Assignment
    allocation_epoch: int

class Snapshot(TypedDict):
    schema_version: Literal[1]
    application: str
    environment: str
    surface: str
    generated_at: int
    expires_at: int
    decisions: dict[str, dict[str, Any]]

class Event(TypedDict):
    schema_version: Literal[1]
    scope: Scope
    event_id: str
    kind: Literal["exposure", "outcome"]
    decision_id: str
    feature_key: str
    assignment_id: str
    configuration_revision: int
    allocation_epoch: int
    variant: str
    timestamp: int
    evidence: dict[str, str]

class EventSink(Protocol):
    durable: Literal[True]
    def append_if_absent(self, event: Event) -> bool: ...
