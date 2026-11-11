"""Required text transport for a trusted pipeline INSIDE its isolated guest.

The controller owns admission, consent, custody and immutable context mounting.
These checks prevent accidental mixed-runtime/fallback use; a guest context is
not host authorization, independently verified execution or publication proof.
"""

from __future__ import annotations

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
    try:
        response = transport.invoke(system, user, role=role, data_sensitivity=data_sensitivity,
                                    max_tokens=max_tokens, reasoning_effort=value["reasoning_effort"])
    except transport.SubscriptionTextError as exc:
        raise AutomationHold(str(exc)) from None
    return response["text"]
