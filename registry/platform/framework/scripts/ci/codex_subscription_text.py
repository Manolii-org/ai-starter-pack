"""No-fallback text client INSIDE the credential-free automation container.

The only transport is the trusted host's fixed subscription broker socket.
Importing this module does not authorize a host, account, role or workflow.
Controller admission, protected consent, account locking and bounded container
execution remain required. No real credential belongs in this guest.
"""

from __future__ import annotations

import http.client
import json
import socket

from codex_review_models import MODEL
from codex_text_response import MAX_BODY, TextResponseError, text_completion

SOCKET = "/broker/model.sock"
MAX_INPUT_CHARS = 180000
ROLES = frozenset({"assessment", "triage", "critic", "judge"})


class SubscriptionTextError(RuntimeError):
    """Terminal fixed category. The caller must not replay on another provider."""


class _BrokerConnection(http.client.HTTPConnection):
    def __init__(self, seconds):
        super().__init__("subscription-broker", timeout=seconds)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def invoke(
    system: str,
    user_message: str,
    *,
    role: str,
    data_sensitivity: str,
    max_tokens: int = 2000,
    seconds: int = 180,
    reasoning_effort: str = "medium",
) -> dict:
    """One bounded stateless inference; quota/auth/unknown outcomes all hold.

    max_tokens is an output instruction, not a fabricated API-enforced limit.
    Actual byte, time, request count and quota bounds belong to the broker and
    trusted controller. Returned model and usage come from provider completion.
    """
    if (
        not isinstance(role, str)
        or role not in ROLES
        or not isinstance(data_sensitivity, str)
        or data_sensitivity not in {"public", "internal"}
    ):
        raise SubscriptionTextError("subscription_text_scope_refused")
    if not isinstance(system, str) or not isinstance(user_message, str):
        raise SubscriptionTextError("subscription_text_input_invalid")
    if len(system) + len(user_message) > MAX_INPUT_CHARS:
        raise SubscriptionTextError("subscription_text_input_exhausted")
    if (
        type(max_tokens) is not int
        or not 1 <= max_tokens <= 32000
        or type(seconds) is not int
        or not 1 <= seconds <= 300
        or not isinstance(reasoning_effort, str)
        or reasoning_effort not in {"medium", "xhigh"}
    ):
        raise SubscriptionTextError("subscription_text_budget_invalid")
    payload = {
        "model": MODEL,
        "store": False,
        "stream": True,
        "instructions": system
        + f"\nReturn only the requested final text/JSON within {max_tokens} tokens. Do not use tools.",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": user_message}]}
        ],
        "reasoning": {"effort": reasoning_effort},
        "tools": [],
    }
    connection = _BrokerConnection(seconds)
    try:
        connection.request(
            "POST",
            "/v1/responses",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise SubscriptionTextError("subscription_text_unavailable")
        raw = response.read(MAX_BODY + 1)
        result = text_completion(
            raw, response.getheader("Content-Type", "").split(";", 1)[0], MODEL
        )
        return {
            **result,
            "billing_mode": "subscription",
            "runtime_provider": "codex",
            "transport": "subscription_broker",
            "role": role,
            "cost_usd": None,
        }
    except TextResponseError as exc:
        raise SubscriptionTextError(str(exc)) from None
    except (OSError, http.client.HTTPException):
        raise SubscriptionTextError("subscription_text_transport_unknown") from None
    finally:
        connection.close()
