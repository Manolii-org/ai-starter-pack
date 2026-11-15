"""Required text transport for a trusted pipeline INSIDE its isolated guest.

The controller owns admission, consent, custody and immutable context mounting.
These checks prevent accidental mixed-runtime/fallback use; a guest context is
not host authorization, independently verified execution or publication proof.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from . import codex_subscription_text as transport
from .codex_automation_context import SCHEMA as SCHEMA, read_context

CONTEXT = Path("/input/codex-automation.json")


class AutomationHold(RuntimeError):
    """Fixed category only; never replay through a legacy provider."""


def required() -> bool:
    value = os.environ.get("CODEX_AUTOMATION_MODE", "")
    if value not in {"", "required"}:
        raise AutomationHold("subscription_mode_invalid")
    return value == "required"


def context() -> dict:
    """Read inert controller coordinates; they are not host authorization."""
    try:
        value = read_context(CONTEXT)
    except ValueError as exc:
        raise AutomationHold(str(exc)) from None
    if value["role"] not in transport.ROLES:
        raise AutomationHold("subscription_context_invalid")
    return value


_ADMISSION = None


def _admit(system: str, user: str) -> None:
    """Apply the HTTP lanes' review-content admission to the subscription lane.

    The guest runs only the accepted closure (built from the trusted workflow
    SHA, never candidate code), so this is the same strict screen the HTTP
    lanes apply before export; a refusal holds instead of sending.
    """
    global _ADMISSION
    if _ADMISSION is None:
        path = Path(__file__).resolve().parents[1] / "lib/agent_capabilities.py"
        spec = importlib.util.spec_from_file_location("_guest_review_admission", path)
        if spec is None or spec.loader is None:
            raise AutomationHold("unsafe_review_input")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (OSError, ImportError, SyntaxError):
            raise AutomationHold("unsafe_review_input") from None
        _ADMISSION = module
    payload = json.dumps({"system": system, "messages": [{"role": "user", "content": user}]}).encode("utf-8")
    try:
        _ADMISSION.admit_review_wire(payload)
    except _ADMISSION.ReviewInputError:
        raise AutomationHold("unsafe_review_input") from None


def complete(system: str, user: str, *, role: str, data_sensitivity: str,
             max_tokens: int, first_party: bool = False, required_model: str | None = None) -> str:
    """One admitted full-input turn; no retries, truncation or HTTP fallback.

    Existing explicit client/direct-only and independent-family policies are
    not silently converted to subscription policy. Their owning adoption must
    establish an eligible contract before those lanes can run here.
    """
    if not required():
        raise AutomationHold("subscription_mode_required")
    value = context()
    if role != value["role"] or data_sensitivity != value["data_sensitivity"]:
        raise AutomationHold("subscription_context_scope_mismatch")
    if (type(first_party) is not bool or first_party or required_model is not None
            or os.environ.get("CLIENT_AI_POLICY")):
        raise AutomationHold("subscription_explicit_provider_policy_held")
    _admit(system, user)
    try:
        response = transport.invoke(system, user, role=role, data_sensitivity=data_sensitivity,
                                    max_tokens=max_tokens, reasoning_effort=value["reasoning_effort"])
    except transport.SubscriptionTextError as exc:
        raise AutomationHold(str(exc)) from None
    return response["text"]
