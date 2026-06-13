"""
sonnet_advisor_guardrail.py — Fail-closed post-call advisor for 'sonnet' proxy intercept.

When Claude Code dispatches Agent(model="sonnet"), the proxy routes to DeepSeek V4 Pro
(Fireworks, PRC-origin weights on US infra; load-balanced with Together AI). This callback
reviews every primary response using Llama 3.3 70B Versatile (Groq, Meta US-origin) before
returning it to the caller. Reviewer is OSS (not Anthropic) — the sonnet alias is
restricted_us_oss_ok clearance, so content is never restricted/anthropic_only and does not
require Anthropic review for the steady-state path.

FAIL-CLOSED: any advisor call failure (API error, timeout, network) causes the
original request to fail. Silent passthrough is never permitted when this guard is active.

Empty-response handling: when the primary returns empty content, the guardrail performs an
inline fallback to Anthropic-native Sonnet and replaces the response in-place. The
advisor then reviews the fallback content. This avoids the LiteLLM v1.82.x limitation
where ContentPolicyViolationError raised from post-call hooks is wrapped as BadRequestError,
preventing content_policy_fallbacks from firing. The fallback uses the deployed
ANTHROPIC_API_KEY and fires only on empty primary content (rare), while steady-state
sonnet responses remain OSS-routed and advisor-reviewed.

Scope: only activates for model_name="sonnet" requests. All other models pass through.

Deployment:
  Injected to /tmp/sonnet_advisor_guardrail.py via Fly [[files]] (local_path or secret_name).
  Referenced in config.yaml litellm_settings.callbacks.
  PYTHONPATH=/tmp ensures LiteLLM can import the module at startup.

See .claude/model-routing.json proxy_intercepted_models.sonnet.advisor_guardrail
for the governance spec.
"""

import json
import logging
import os

import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

logger = logging.getLogger(__name__)

# Only intercept calls dispatched to this model_name alias.
_INTERCEPT_MODEL = "sonnet"

# Reviewer model — Llama 3.3 70B Versatile via Groq (Meta US-origin).
# OSS review is sufficient: sonnet alias is restricted_us_oss_ok clearance, so content
# is never restricted/anthropic_only. Eliminates Anthropic API cost for every sonnet call.
# Switched 2026-05-15 from together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo: Together
# ran out of credits 2026-05-14, second Together outage in three weeks (Gemma 4 31B
# serverless revocation 2026-04-25 caused the prior advisor migration). Groq is a third
# provider distinct from the sonnet primary's Fireworks+Together load-balanced pair —
# eliminates provider-concentration risk on the fail-closed review chain. Same model
# (Llama 3.3 70B, Meta US-origin, SII 35) so review quality is unchanged.
_REVIEWER_MODEL = "groq/llama-3.3-70b-versatile"

# Inline fallback model when the primary returns empty content.
# Keep this Anthropic-native: the fallback receives the original request messages
# (which may contain restricted/client content) and produces user-visible content.
# Cost reduction for eval/scoring tools should stay in proxy-routing changes, not this path.
_FALLBACK_MODEL = "anthropic/claude-sonnet-4-6"

# Truncation limits — keep advisor latency/cost bounded.
_MAX_MESSAGES = 3        # last N messages from original conversation
_MAX_MSG_CHARS = 2000    # chars per message
_MAX_RESPONSE_CHARS = 10000  # chars of model response to review

_SYSTEM_PROMPT = (
    "You are a quality reviewer for an AI routing system. "
    "A downstream model has responded to a user request. "
    "Verify the response is: (1) factually accurate given the request, "
    "(2) free from dangerous or harmful content, (3) appropriately structured. "
    "Reply with EXACTLY 'APPROVED' or 'REJECTED: <one sentence reason>'. No other text."
)


def _read_response_content(response) -> str:
    """Return the assistant text from a LiteLLM response across known shapes.

    Handles OpenAI-style ModelResponse (object or dict with ``choices[0].message.content``)
    and Anthropic-format responses returned by /v1/messages
    (dict/object with top-level ``content: [{"type": "text", "text": "..."}]``).
    Returns an empty string when no text is present so the caller can trigger the
    inline-fallback path instead of fail-closing.
    """
    if isinstance(response, dict):
        choices = response.get("choices") or []
        # Defend against choices[0] being None or a non-dict (LiteLLM has been
        # observed returning [None] or [str] from some fallback providers under
        # streaming-cancellation conditions). Treat as no-content and fall
        # through to top-level content extraction rather than AttributeError.
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return _flatten_anthropic_blocks(content)
        top_content = response.get("content")
        if isinstance(top_content, list):
            return _flatten_anthropic_blocks(top_content)
        if isinstance(top_content, str):
            return top_content
        return ""

    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return _flatten_anthropic_blocks(content)
    top_content = getattr(response, "content", None)
    if isinstance(top_content, list):
        return _flatten_anthropic_blocks(top_content)
    if isinstance(top_content, str):
        return top_content
    return ""


def _flatten_anthropic_blocks(blocks: list) -> str:
    """Join Anthropic content blocks into reviewable text.

    The guardrail returns the original response object after advisor approval. Silent
    drops of non-text content would let unreviewed material pass through to the caller,
    so any block we can't represent as text must fail closed.

    Handled block types:
      ``text``                      — appended verbatim.
      ``thinking``/``redacted_thinking`` — skipped. Anthropic designates these as
                                     not-for-display reasoning; the user-visible answer
                                     is in sibling ``text`` blocks. Skipping preserves
                                     fail-closed posture without leaking reasoning into
                                     the advisor prompt.
      ``tool_use``                  — serialised as ``[tool_use: <name>(<json args>)]``
                                     so the advisor can evaluate the structured call.
                                     Tool calls are not arbitrary executable code — they
                                     are function-name + JSON-argument tuples the advisor
                                     can policy-check. ReAct broad agents return mixed
                                     [text, tool_use] arrays; prior fail-close on these
                                     made the guardrail unusable for tool-using agents
                                     (dominant failure mode observed 2026-05-17).

    Any other block type still fails closed — no safe text representation.
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            block_type = block.get("type")
            text = block.get("text")
        else:
            block_type = getattr(block, "type", None)
            text = getattr(block, "text", None)

        if block_type in ("thinking", "redacted_thinking"):
            continue
        if block_type == "text":
            if isinstance(text, str):
                parts.append(text)
            continue
        if block_type == "tool_use":
            if isinstance(block, dict):
                tname = block.get("name", "?")
                tinput = block.get("input", {})
            else:
                tname = getattr(block, "name", "?")
                tinput = getattr(block, "input", {})
            try:
                tinput_str = json.dumps(tinput) if isinstance(tinput, (dict, list)) else str(tinput)
            except (TypeError, ValueError):
                tinput_str = "<unserialisable>"
            parts.append(f"[tool_use: {tname}({tinput_str})]")
            continue
        raise ValueError(f"unsupported Anthropic content block type: {block_type!r}")
    result = "".join(parts)
    # Fail closed if the flattened content would be truncated by the reviewer window.
    # A truncated review leaves policy-relevant tool arguments unexamined.
    if len(result) > _MAX_RESPONSE_CHARS:
        raise ValueError(
            f"Flattened content ({len(result)} chars) exceeds reviewer window "
            f"({_MAX_RESPONSE_CHARS} chars); failing closed to avoid partial review."
        )
    return result


def _safe_setattr(obj, name: str, value) -> None:
    """Set an attribute on an object that may be a Pydantic v2 BaseModel with a
    frozen field. Falls back to ``object.__setattr__`` when direct assignment
    raises (Pydantic ValidationError, AttributeError on read-only properties,
    or any TypeError from descriptors). The fallback bypasses Pydantic
    validation but keeps the object instance identity, which is required by
    the inline-fallback path that mutates the response in place rather than
    returning a new copy.

    Added 2026-05-15 (PR #979 follow-up): the prior code did
    ``response.content = ...`` directly and lost responses to fail-closed
    503s when LiteLLM returned a Pydantic frozen model from certain
    fallback providers. The patch path is the only safe place to bypass
    validation — the original primary response is already past the
    validator, and the fallback content has already been advisor-reviewed.
    """
    try:
        setattr(obj, name, value)
        return
    except (AttributeError, TypeError, ValueError):
        # Pydantic v2 ValidationError inherits from ValueError; frozen-field
        # rejection lands here. object.__setattr__ writes directly to __dict__
        # (or __pydantic_fields_set__ for Pydantic v2) without invoking the
        # validator. If THAT also raises, surface — caller fail-closes.
        object.__setattr__(obj, name, value)


def _write_response_content(response, new_content: str) -> None:
    """Replace the assistant text in a LiteLLM response in place across known shapes.

    Mirrors :func:`_read_response_content` so the inline-fallback path can patch
    OpenAI-style ModelResponse objects, dict responses, and Anthropic-format
    responses without raising. Raises only when the response is neither — that
    truly is an unrecognised shape and fail-closed is correct.

    Pydantic v2 frozen-field assignments are handled via :func:`_safe_setattr`,
    which falls back to ``object.__setattr__`` to bypass model validation.
    """
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].setdefault("message", {})
            if not isinstance(message, dict):
                raise TypeError("choices[0].message is not a dict")
            message["content"] = new_content
            return
        top_content = response.get("content")
        if isinstance(top_content, list):
            response["content"] = [{"type": "text", "text": new_content}]
            return
        if "content" in response or top_content is not None:
            response["content"] = new_content
            return
        # No recognised shape — materialise OpenAI-style so downstream callers
        # that key off choices[0].message.content still see the fallback.
        response["choices"] = [{"message": {"role": "assistant", "content": new_content}}]
        return

    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is None:
            raise AttributeError("choices[0].message is missing")
        _safe_setattr(message, "content", new_content)
        return
    top_content = getattr(response, "content", None)
    if isinstance(top_content, list):
        # Rebuild a single-block text array. Frozen Pydantic content fields
        # are handled by _safe_setattr (object.__setattr__ fallback).
        _safe_setattr(response, "content", [{"type": "text", "text": new_content}])
        return
    if isinstance(top_content, str):
        # Symmetric with _read_response_content's str branch and with the dict
        # branch above — keep the simple-string shape rather than wrapping it.
        _safe_setattr(response, "content", new_content)
        return
    if isinstance(top_content, dict):
        # Single Anthropic-style block as a dict (e.g. {"type":"text","text":...}
        # or {"text":...}) — promote to the canonical list-of-blocks form.
        _safe_setattr(response, "content", [{"type": "text", "text": new_content}])
        return
    raise AttributeError("response has neither choices nor content attribute")


class SonnetAdvisorGuardrail(CustomLogger):
    """Fail-closed post-call advisor for model_name='sonnet' proxy-intercepted requests."""

    def __init__(self):
        self._reviewer_api_key = os.environ.get("GROQ_API_KEY", "")
        self._fallback_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    async def async_post_call_success_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        data: dict,
        response,
    ) -> None:
        """Review primary response via Llama 3.3 70B (Groq). Raise on rejection or advisor failure."""
        if data.get("model", "") != _INTERCEPT_MODEL:
            return  # Not a sonnet intercept call — skip

        # Fail closed immediately if no API key
        if not self._reviewer_api_key:
            raise litellm.ServiceUnavailableError(
                message=(
                    "SonnetAdvisorGuardrail: GROQ_API_KEY not configured — "
                    "fail-closed; cannot verify response quality without advisor"
                ),
                model=_REVIEWER_MODEL,
                llm_provider="groq",
            )

        # Extract primary response content — guard against dict responses from
        # fallback paths where LiteLLM returns a raw dict instead of a response object,
        # and against Anthropic-format responses (top-level `content` array) when the
        # proxy is invoked via /v1/messages.
        try:
            response_content = _read_response_content(response)
        except (AttributeError, IndexError, TypeError, KeyError, ValueError) as e:
            logger.exception(
                "SonnetAdvisorGuardrail: cannot parse model response — "
                "type=%s, error=%s (fail-closed)",
                type(response).__name__,
                e,
            )
            raise litellm.ServiceUnavailableError(
                message="SonnetAdvisorGuardrail: cannot parse model response — fail-closed",
                model=_REVIEWER_MODEL,
                llm_provider="groq",
            )

        # Empty response — try inline fallback to Anthropic-native Sonnet.
        # LiteLLM v1.82.x wraps ContentPolicyViolationError raised from post-call hooks
        # as BadRequestError, so content_policy_fallbacks never fires for hook exceptions.
        # Inline fallback modifies the response object in-place, bypassing this limitation.
        if not response_content.strip():
            response_content = await self._inline_fallback(data, response)

        # Build review context from original messages (truncated)
        original_messages = data.get("messages", [])
        messages_text = "\n".join(
            f"{m.get('role', 'user')}: {str(m.get('content', ''))[:_MAX_MSG_CHARS]}"
            for m in original_messages[-_MAX_MESSAGES:]
        )
        review_user_content = (
            f"Original request:\n{messages_text}\n\n"
            f"Model response:\n{response_content[:_MAX_RESPONSE_CHARS]}\n\n"
            "Is this response acceptable? Reply APPROVED or REJECTED: <reason>."
        )

        # Call Llama 3.3 70B reviewer via Groq
        try:
            advisor_response = await litellm.acompletion(
                model=_REVIEWER_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": review_user_content},
                ],
                api_key=self._reviewer_api_key,
                max_tokens=100,
                temperature=0,
                timeout=120,
            )
            verdict = (advisor_response.choices[0].message.content or "").strip()
        except Exception:
            logger.exception("SonnetAdvisorGuardrail: advisor call failed")
            raise litellm.ServiceUnavailableError(
                message="SonnetAdvisorGuardrail: advisor unavailable (fail-closed)",
                model=_REVIEWER_MODEL,
                llm_provider="groq",
            )

        # Split on whitespace and strip trailing punctuation so "APPROVED." and
        # "APPROVED — ..." pass, while "APPROVEDLY" and "APPROVED_REJECTED" do not.
        first_token = (
            verdict.strip().split(None, 1)[0].rstrip(":.,;!?-").upper()
            if verdict.strip() else ""
        )
        if first_token != "APPROVED":
            logger.warning(
                "SonnetAdvisorGuardrail: advisor REJECTED response — verdict=%r, "
                "response_len=%d",
                verdict,
                len(response_content),
            )
            raise litellm.ContentPolicyViolationError(
                message=(
                    "SonnetAdvisorGuardrail: advisor rejected response. "
                    "Retry or use model='claude-sonnet-4-6' for Anthropic-direct routing."  # model hint unchanged
                ),
                model=_INTERCEPT_MODEL,
                llm_provider="litellm_proxy",
            )

        logger.info("SonnetAdvisorGuardrail: advisor APPROVED response for model=%s", _INTERCEPT_MODEL)

    async def _inline_fallback(self, data: dict, response) -> str:
        """Call Anthropic-native Sonnet and replace response content in-place.

        Returns the fallback content string so the caller can proceed to advisor review.
        Raises ServiceUnavailableError (fail-closed) if fallback is unavailable or also empty.
        """
        if not self._fallback_api_key:
            raise litellm.ServiceUnavailableError(
                message=(
                    "SonnetAdvisorGuardrail: primary returned empty + ANTHROPIC_API_KEY "
                    "not configured — fail-closed (no inline fallback available)"
                ),
                model=_FALLBACK_MODEL,
                llm_provider="anthropic",
            )

        logger.warning(
            "SonnetAdvisorGuardrail: primary returned empty — inline fallback to %s",
            _FALLBACK_MODEL,
        )

        try:
            fb = await litellm.acompletion(
                model=_FALLBACK_MODEL,
                messages=data.get("messages", []),
                api_key=self._fallback_api_key,
                max_tokens=data.get("max_tokens") or 1024,
                temperature=data.get("temperature", 0),
                timeout=120,
            )
            fb_content = (fb.choices[0].message.content or "").strip()
        except Exception:
            logger.exception("SonnetAdvisorGuardrail: inline fallback call failed")
            raise litellm.ServiceUnavailableError(
                message=(
                    "SonnetAdvisorGuardrail: primary empty + Anthropic fallback "
                    "call failed — fail-closed"
                ),
                model=_FALLBACK_MODEL,
                llm_provider="anthropic",
            )

        if not fb_content:
            raise litellm.ServiceUnavailableError(
                message=(
                    "SonnetAdvisorGuardrail: both primary and Anthropic fallback returned "
                    "empty — fail-closed"
                ),
                model=_FALLBACK_MODEL,
                llm_provider="anthropic",
            )

        # Replace response content in-place so the caller receives the fallback result.
        # _write_response_content handles OpenAI-style ModelResponse, dict responses, and
        # Anthropic-format responses (top-level `content` array from /v1/messages).
        try:
            _write_response_content(response, fb_content)
        except (AttributeError, IndexError, TypeError, KeyError, ValueError) as e:
            # Fail closed: if the patch fails the caller receives the original empty
            # response while the advisor has reviewed different (fallback) content.
            # That mismatch violates fail-closed semantics — raise rather than swallow.
            logger.error(
                "SonnetAdvisorGuardrail: could not patch response in-place — %s "
                "(fail-closed: user would receive stale response)",
                e,
            )
            raise litellm.ServiceUnavailableError(
                message=(
                    "SonnetAdvisorGuardrail: inline fallback succeeded but response "
                    "patch failed — fail-closed"
                ),
                model=_FALLBACK_MODEL,
                llm_provider="anthropic",
            )
        logger.info("SonnetAdvisorGuardrail: inline fallback succeeded (%s)", _FALLBACK_MODEL)
        return fb_content

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        pass

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        pass


# Module-level instance — LiteLLM discovers this via the callbacks config.
sonnet_advisor_guardrail = SonnetAdvisorGuardrail()
