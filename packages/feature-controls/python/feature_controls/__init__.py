from .lifecycle import Lifecycle, kill_message
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import FeatureRuntime
from .types import (
    PROVIDER_SEMANTICS,
    ApprovalVerifier,
    Assignment,
    AssignmentKey,
    AssignmentStore,
    Catalog,
    DeploymentPolicy,
    DisableException,
    Context,
    ControlState,
    Decision,
    DurableControlStore,
    Event,
    EventSink,
    Feature,
    KillState,
    ReleaseBundle,
    ReleaseFeature,
    ReleaseManifest,
    Scope,
    Snapshot,
)
from .validation import approval_message, payload_of, validate_catalog
from .validation import release_catalog_features, release_manifest_message, validate_release_manifest
from .release_policy import assert_release_catalog, assess_release, compile_release_payload, prepare_deployment_payload, release_client_states, unfinished_release_features

__all__ = [
    "PROVIDER_SEMANTICS",
    "ApprovalVerifier",
    "Assignment",
    "AssignmentKey",
    "AssignmentStore",
    "Catalog",
    "Context",
    "ControlState",
    "Decision",
    "DurableControlStore",
    "Event",
    "EventSink",
    "Feature",
    "FeatureRuntime",
    "KillState",
    "Lifecycle",
    "ReleaseBundle",
    "Scope",
    "Snapshot",
    "approval_message",
    "kill_message",
    "payload_of",
    "validate_catalog",
    "DeploymentPolicy",
    "DisableException",
    "ReleaseFeature",
    "ReleaseManifest",
    "release_catalog_features",
    "release_manifest_message",
    "validate_release_manifest",
    "assert_release_catalog",
    "assess_release",
    "compile_release_payload",
    "prepare_deployment_payload",
    "unfinished_release_features",
    "release_client_states",
]


def __getattr__(name):
    # Source/deployment checks need no provider SDK or network client imports.
    if name == "FeatureRuntime":
        from .runtime import FeatureRuntime
        globals()[name] = FeatureRuntime
        return FeatureRuntime
    raise AttributeError(name)
