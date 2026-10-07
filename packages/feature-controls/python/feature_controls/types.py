from typing import Any, Literal, Protocol, TypedDict

PROVIDER_SEMANTICS = "growthbook:js-1.8.0:py-3.2.0:hash-2"

class Scope(TypedDict):
    ecosystem_id: str
    deployment_id: str
    feature_namespace: str
    environment_id: str

class AssignmentBoundary(TypedDict):
    mode: Literal["application", "shared"]
    key: str

class SharedBoundary(TypedDict):
    key: str
    applications: list[str]

class Experiment(TypedDict, total=False):
    key: str
    epoch: int
    assignment_boundary: SharedBoundary

class Feature(TypedDict, total=False):
    value_type: Literal["boolean", "number", "string", "json"]
    baseline: Any
    disabled_value: Any
    allowed_values: list[Any]
    value_schema: dict[str, Any]
    failure: Literal["baseline", "deny"]
    surfaces: list[str]
    applications: list[str]
    ancestors: list[str]
    experiment: Experiment

class Catalog(TypedDict):
    applications: list[str]
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
    application_id: str
    projection_source: Literal["trusted-server"]
    context_scope: str
    schema_version: Literal[1]
    scope: Scope
    surface_id: str
    groups: list[str]
    group_ancestors: dict[str, list[str]]
    excluded_groups: list[str]
    roles: list[str]
    tenant_key: str
    assignment_key: str
    authorized: bool
    eligible: bool
    expires_at: int
    excluded: list[str]

class ControlState(TypedDict):
    time_highwater: int
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
    assignment_boundary: AssignmentBoundary
    scope: Scope
    experiment_key: str
    allocation_epoch: int
    unit_key: str

class AssignmentStore(Protocol):
    durable: Literal[True]
    def read(self, key: AssignmentKey) -> Assignment | None: ...
    def create_if_absent(self, key: AssignmentKey, variant: str) -> Assignment: ...

class Decision(TypedDict, total=False):
    context_scope: str
    application_id: str
    unit_key: str
    decision_id: str
    feature_key: str
    scope: Scope
    surface_id: str
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
    context_scope: str
    configuration_revision: int
    kill_generation: int
    time_highwater: int
    ecosystem_id: str
    deployment_id: str
    feature_namespace: str
    schema_version: Literal[1]
    application_id: str
    environment_id: str
    surface_id: str
    generated_at: int
    expires_at: int
    decisions: dict[str, dict[str, Any]]

class Event(TypedDict):
    context_scope: str
    application_id: str
    surface_id: str
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
    test_only: Literal[True]
    def append_if_absent(self, event: Event) -> bool: ...
