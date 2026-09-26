"""Model-plane proxy detection — single source for cost/provider attribution.

After CLAUDE_CLIENT_USE_PROXY=0 (default), the Claude *client* is Anthropic-direct
while programmatic OSS still uses the named LiteLLM endpoint. Sniffing only
ANTHROPIC_BASE_URL therefore under-attributes OSS alias rates (haiku/sonnet) and
mis-labels providers — but the reverse error is worse: treating a named endpoint
alone as proof that Agent-tool aliases are OSS.

Two questions, deliberately separate:

- ``client_proxy_active`` — Claude Code main-thread / Agent-tool traffic is
  redirected through LiteLLM. Prefer a live non-Anthropic ANTHROPIC_BASE_URL
  (actual transport) over the CLAUDE_CLIENT_USE_PROXY flag alone, so a stale
  Dashboard inject that contradicts flag=0 is still attributed correctly.
  Use this for Langfuse-as-authoritative grand totals when *everything* went
  via the client redirect.

- ``oss_alias_rates_active`` — haiku/sonnet *Agent-tool / transcript* aliases
  should be priced/labeled as OSS-via-proxy. This is the client transport, NOT
  the named LITELLM_PROXY_URL. sdk_runner uses the named endpoint, but those
  calls never appear in Claude transcripts; Agent-tool sub-agents stay
  Anthropic-direct when CLAUDE_CLIENT_USE_PROXY=0 (docs/model-plane-boundary.md).
"""
from __future__ import annotations

import os


def _named_proxy_url() -> str:
    return (os.environ.get("LITELLM_PROXY_URL") or "").strip().rstrip("/")


def _anthropic_base_url() -> str:
    return (os.environ.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")


def _base_is_non_anthropic_proxy(base: str) -> bool:
    if not base:
        return False
    return "anthropic.com" not in base.lower()


def client_proxy_active() -> bool:
    """True when the Claude client plane is effectively on the LiteLLM proxy.

    A live non-Anthropic ANTHROPIC_BASE_URL wins over CLAUDE_CLIENT_USE_PROXY=0:
    the integrity check treats that contradiction as a failure, and cost
    accounting must follow the actual transport, not the intended flag.
    """
    base = _anthropic_base_url()
    if _base_is_non_anthropic_proxy(base):
        return True
    flag = (os.environ.get("CLAUDE_CLIENT_USE_PROXY") or "").strip()
    return flag == "1"


def oss_alias_rates_active() -> bool:
    """True when Agent-tool / transcript haiku|sonnet aliases are OSS-via-proxy.

    Named LITELLM_PROXY_URL alone does NOT imply this — programmatic sdk_runner
    traffic uses the named endpoint, but Agent-tool sub-agents remain
    Anthropic-direct on the default plane.
    """
    return client_proxy_active()
