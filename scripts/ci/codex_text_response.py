"""Validate a tool-free Codex Responses completion, never a CLI selection claim.

Shared by the trusted subscription broker and its credential-free guest client.
It has no network, credential, configuration, retry or provider-routing surface.
"""

from __future__ import annotations

import json

MAX_BODY = 32 * 1024 * 1024
MAX_TEXT = 131072


class TextResponseError(ValueError):
    """A fixed diagnostic category; never include provider text or prompt data."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TextResponseError("ambiguous_text_response")
        result[key] = value
    return result


def _constant(_value):
    raise TextResponseError("invalid_text_response")


def _item(item):
    if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning"}:
        raise TextResponseError("unexpected_text_tool")
    if item["type"] == "reasoning":
        return ""
    if (
        item.get("role") != "assistant"
        or item.get("status", "completed") != "completed"
    ):
        raise TextResponseError("incomplete_text_response")
    parts = item.get("content")
    if not isinstance(parts, list):
        raise TextResponseError("invalid_text_response")
    text = []
    for part in parts:
        if not isinstance(part, dict):
            raise TextResponseError("invalid_text_response")
        if part.get("type") == "refusal":
            raise TextResponseError("subscription_text_refused")
        if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
            raise TextResponseError("invalid_text_response")
        text.append(part["text"])
    if item.get("phase") not in {None, "final_answer", "commentary"}:
        raise TextResponseError("invalid_text_response")
    return "" if item.get("phase") == "commentary" else "".join(text)


def text_completion(body: bytes, content_type: str, expected_model: str) -> dict:
    """Require one actual completion, exact served model and valid usage.

    SSE deltas never establish completion. Completed output items support a
    provider that omits the final output array; final output otherwise wins so
    text is not counted twice. Every structured item is checked before use.
    """
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BODY:
        raise TextResponseError("text_response_budget_exhausted")
    if content_type == "application/json":
        documents = [body]
    elif content_type == "text/event-stream":
        documents = [
            line[5:].strip() for line in body.splitlines() if line.startswith(b"data:")
        ]
    else:
        raise TextResponseError("invalid_text_response")
    completed = None
    done_items = {}
    try:
        for raw in documents:
            if raw == b"[DONE]":
                continue
            event = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
            if not isinstance(event, dict):
                raise TextResponseError("invalid_text_response")
            kind = event.get("type", "")
            if kind in {
                "error",
                "response.error",
                "response.failed",
                "response.incomplete",
            }:
                raise TextResponseError("incomplete_text_response")
            if "function_call" in kind or "custom_tool_call" in kind:
                raise TextResponseError("unexpected_text_tool")
            if "item" in event:
                # Added messages can still be in_progress; enforce the terminal
                # item contract on done, and refuse tools even in added events.
                item = event["item"]
                if not isinstance(item, dict) or item.get("type") not in {
                    "message",
                    "reasoning",
                }:
                    raise TextResponseError("unexpected_text_tool")
                if kind == "response.output_item.done":
                    _item(item)
                    index = event.get("output_index")
                    if (
                        type(index) is not int
                        or not 0 <= index < 10000
                        or index in done_items
                    ):
                        raise TextResponseError("ambiguous_text_response")
                    done_items[index] = item
            response = (
                event.get("response")
                if kind == "response.completed"
                else (event if event.get("object") == "response" else None)
            )
            if response is None:
                continue
            if completed is not None or not isinstance(response, dict):
                raise TextResponseError("ambiguous_text_response")
            if response.get("status") != "completed" or response.get("error"):
                raise TextResponseError("incomplete_text_response")
            if response.get("model") != expected_model:
                raise TextResponseError("served_model_mismatch")
            completed = response
        if completed is None:
            raise TextResponseError("incomplete_text_response")
        output = completed.get("output", [])
        if not isinstance(output, list) or len(output) > 10000:
            raise TextResponseError("invalid_text_response")
        output = output or [done_items[k] for k in sorted(done_items)]
        text = "".join(_item(item) for item in output)
        if not text.strip() or len(text) > MAX_TEXT:
            raise TextResponseError("text_response_budget_exhausted")
        usage = completed.get("usage")
        if not isinstance(usage, dict) or any(
            type(usage.get(key)) is not int or usage[key] < 0
            for key in ("input_tokens", "output_tokens")
        ):
            raise TextResponseError("text_usage_unverified")
        details = usage.get("input_tokens_details", {})
        if not isinstance(details, dict):
            raise TextResponseError("text_usage_unverified")
        cached = details.get("cached_tokens", 0)
        if type(cached) is not int or not 0 <= cached <= usage["input_tokens"]:
            raise TextResponseError("text_usage_unverified")
        return {
            "text": text,
            "model": completed["model"],
            "model_verification": "provider_reported",
            "usage": {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cached_input_tokens": cached,
            },
        }
    except (ValueError, TypeError, UnicodeError, RecursionError, KeyError) as exc:
        if isinstance(exc, TextResponseError):
            raise
        raise TextResponseError("invalid_text_response") from None
