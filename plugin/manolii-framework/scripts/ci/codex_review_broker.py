"""Bounded, subscription-only Responses broker outside the review container.

This is a fixed operation, never a general HTTP proxy. Provider response filtering
is defensive; isolation, rather than exhaustive encoding detection, keeps real
credentials out of model input and reviewer processes. Import/run only trusted
controller code, with a socket directory created by that controller.
"""

from __future__ import annotations

import base64
import hashlib
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

UPSTREAM = "https://chatgpt.com/backend-api/codex/responses"
REVIEW_MODEL = "gpt-5.6-sol"
MAX_INPUT = 2 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
MAX_METADATA = 4096
EXEC_GRAMMAR_SHA256 = "8eead048e20069c17fcd0b6fe5ee9ed29364868723f8e08889de146a2b4b7a8e"
LOCAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("non-finite JSON number")


def shape(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ValueError("unsupported request shape")


def local_text(value):
    if not isinstance(value, str):
        raise ValueError("inline text required")


def text_content(value):
    if not isinstance(value, list):
        raise ValueError("inline text content required")
    for part in value:
        shape(part, {"type", "text", "annotations"}, {"type", "text"})
        if part["type"] not in {"input_text", "output_text"} or part.get("annotations", []) != []:
            raise ValueError("nontext content refused")
        local_text(part["text"])


def local_schema(value):
    """JSON Schema is data; resource resolution and unknown dialects are not."""
    if isinstance(value, bool):
        return
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "anyOf", "oneOf", "allOf", "enum", "const", "description", "title",
               "default", "minimum", "maximum", "minLength", "maxLength",
               "minItems", "maxItems", "pattern", "format", "encrypted",
               "$defs", "definitions", "$ref", "not"}
    shape(value, allowed)
    if "type" in value:
        types = value["type"] if isinstance(value["type"], list) else [value["type"]]
        if not types or any(not isinstance(t, str) or t not in {"object", "array", "string", "number", "integer", "boolean", "null"} for t in types):
            raise ValueError("invalid schema type")
    for key in ("description", "title", "pattern", "format"):
        if key in value:
            local_text(value[key])
    if "encrypted" in value and type(value["encrypted"]) is not bool:
        raise ValueError("invalid schema annotation")
    if "required" in value and (not isinstance(value["required"], list) or not all(isinstance(k, str) for k in value["required"])):
        raise ValueError("invalid required properties")
    if "enum" in value and (not isinstance(value["enum"], list) or not value["enum"]):
        raise ValueError("invalid schema enum")
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if key in value and type(value[key]) not in {int, float}:
            raise ValueError("invalid schema bound")
    if "$ref" in value and (not isinstance(value["$ref"], str) or not value["$ref"].startswith("#/")):
        raise ValueError("external schema reference refused")
    for key in ("properties", "$defs", "definitions"):
        if key in value:
            if not isinstance(value[key], dict):
                raise ValueError("invalid schema map")
            for child in value[key].values():
                local_schema(child)
    for key in ("items", "additionalProperties", "not"):
        if key in value:
            local_schema(value[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in value:
            if not isinstance(value[key], list):
                raise ValueError("invalid schema union")
            for child in value[key]:
                local_schema(child)


def local_tools(value, declarations, namespace=""):
    if not isinstance(value, list):
        raise ValueError("local tools required")
    for tool in value:
        if not isinstance(tool, dict) or not LOCAL_NAME.fullmatch(tool.get("name", "")):
            raise ValueError("invalid local tool")
        kind = tool.get("type")
        if kind == "namespace" and not namespace:
            shape(tool, {"type", "name", "description", "tools"}, {"type", "name", "tools"})
            if "description" in tool:
                local_text(tool["description"])
            local_tools(tool["tools"], declarations, tool["name"])
            continue
        if kind == "function":
            shape(tool, {"type", "name", "description", "parameters", "strict"}, {"type", "name", "parameters"})
            if "strict" in tool and type(tool["strict"]) is not bool:
                raise ValueError("invalid local tool strictness")
            if not isinstance(tool["parameters"], dict) or tool["parameters"].get("type") != "object":
                raise ValueError("object tool parameters required")
            local_schema(tool["parameters"])
        elif kind == "custom":
            shape(tool, {"type", "name", "description", "format"}, {"type", "name", "format"})
            fmt = tool["format"]
            shape(fmt, {"type", "syntax", "definition"}, {"type", "syntax", "definition"})
            if (tool["name"] != "exec" or fmt["type"] != "grammar" or fmt["syntax"] != "lark"
                    or not isinstance(fmt["definition"], str)
                    or hashlib.sha256(fmt["definition"].encode()).hexdigest() != EXEC_GRAMMAR_SHA256):
                raise ValueError("unqualified custom grammar")
        else:
            raise ValueError("hosted tool refused")
        if "description" in tool:
            local_text(tool["description"])
        identity = (namespace, tool["name"])
        if identity in declarations:
            raise ValueError("ambiguous local tool")
        declarations[identity] = kind


def review_request(raw, server):
    """Admit only stateless inline review traffic before reading host auth."""
    payload = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    shape(payload, {"model", "instructions", "input", "tools", "tool_choice",
                    "parallel_tool_calls", "reasoning", "store", "stream", "include",
                    "prompt_cache_key", "text", "client_metadata"},
          {"model", "input", "store", "stream"})
    if payload["model"] != REVIEW_MODEL or payload["store"] is not False or payload["stream"] is not True:
        raise ValueError("stateless subscription review required")
    # Bound recursive validation independently of Python's JSON recursion limit.
    pending = [(payload, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 100000:
            raise ValueError("request shape budget exhausted")
        if isinstance(value, (dict, list)) and nodes + len(pending) + len(value) > 100000:
            raise ValueError("request shape budget exhausted")
        if isinstance(value, dict):
            pending.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            pending.extend((v, depth + 1) for v in value)
    if "instructions" in payload:
        local_text(payload["instructions"])
    if payload.get("tool_choice", "auto") != "auto" or payload.get("parallel_tool_calls", False) is not False:
        raise ValueError("unsupported tool control")
    if payload.get("include", []) not in ([], ["reasoning.encrypted_content"]):
        raise ValueError("unsupported response expansion")
    if "reasoning" in payload:
        value = payload["reasoning"]
        shape(value, {"effort", "summary", "context"})
        if (value.get("effort", "low") not in {"low", "medium", "high", "xhigh"}
                or value.get("summary", "auto") not in {"auto", "concise", "detailed"}
                or value.get("context", "all_turns") != "all_turns"):
            raise ValueError("unsupported reasoning control")
    if "text" in payload:
        shape(payload["text"], {"verbosity"})
        if payload["text"].get("verbosity", "low") not in {"low", "medium", "high"}:
            raise ValueError("unsupported text control")
    # Untrusted account/session metadata never reaches the authenticated route.
    if "client_metadata" in payload:
        metadata = payload.pop("client_metadata")
        if not isinstance(metadata, dict) or len(json.dumps(metadata)) > MAX_METADATA or not all(isinstance(v, str) for v in metadata.values()):
            raise ValueError("invalid client metadata")
    if "prompt_cache_key" in payload and (not isinstance(payload["prompt_cache_key"], str) or len(payload["prompt_cache_key"]) > 256):
        raise ValueError("invalid cache key")
    payload["prompt_cache_key"] = server.cache_key
    declarations = {}
    local_tools(payload.get("tools", []), declarations)
    if not isinstance(payload["input"], list) or not payload["input"]:
        raise ValueError("inline review history required")
    calls = {}
    for item in payload["input"]:
        if not isinstance(item, dict):
            raise ValueError("inline history object required")
        kind = item.get("type", "message")
        if kind == "message":
            shape(item, {"type", "id", "role", "content"}, {"role", "content"})
            if item["role"] not in {"system", "developer", "user", "assistant"}:
                raise ValueError("unsupported message role")
            text_content(item["content"])
        elif kind == "additional_tools":
            shape(item, {"type", "id", "role", "tools"}, {"type", "role", "tools"})
            if item["role"] != "developer":
                raise ValueError("unsupported tool declaration")
            local_tools(item["tools"], declarations)
        elif kind in {"function_call", "custom_tool_call"}:
            argument = "arguments" if kind == "function_call" else "input"
            shape(item, {"type", "id", "call_id", "name", "namespace", argument}, {"type", "call_id", "name", argument})
            if declarations.get((item.get("namespace", ""), item["name"])) != ("function" if kind == "function_call" else "custom"):
                raise ValueError("undeclared local tool call")
            local_text(item[argument])
            local_text(item["call_id"])
            if not item["call_id"] or len(item["call_id"]) > 256 or item["call_id"] in calls:
                raise ValueError("invalid local call identity")
            calls[item["call_id"]] = kind
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            shape(item, {"type", "id", "call_id", "output"}, {"type", "call_id", "output"})
            if calls.get(item["call_id"]) != kind.removesuffix("_output"):
                raise ValueError("orphan local tool output")
            if isinstance(item["output"], str):
                local_text(item["output"])
            else:
                text_content(item["output"])
        elif kind == "reasoning":
            shape(item, {"type", "id", "summary", "encrypted_content"}, {"type", "summary", "encrypted_content"})
            if not isinstance(item["summary"], list):
                raise ValueError("invalid reasoning summary")
            for summary in item["summary"]:
                shape(summary, {"type", "text"}, {"type", "text"})
                if summary["type"] != "summary_text":
                    raise ValueError("unsupported reasoning summary")
                local_text(summary["text"])
            local_text(item["encrypted_content"])
            with server.lock:
                if hashlib.sha256(item["encrypted_content"].encode()).digest() not in server.reasoning_context:
                    raise ValueError("foreign opaque context refused")
        else:
            raise ValueError("resource or hosted input refused")
        item.pop("id", None)
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_INPUT:
        raise ValueError("canonical request budget exhausted")
    return encoded


class BrokerError(Exception):
    """A bounded diagnostic category; never retain provider response text."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Subscription:
    """Private host memory, never serialized into a container or receipt."""

    def __init__(self, auth: dict):
        tokens = auth.get("tokens", {})
        self.access = tokens.get("access_token")
        self.account = tokens.get("account_id")
        if auth.get("auth_mode", "chatgpt") != "chatgpt" or not self.access:
            raise BrokerError("subscription_auth_required")
        if any(
            not isinstance(x, str) or not x or any(c in x for c in "\r\n\0")
            for x in (self.access, self.account)
        ):
            raise BrokerError("invalid_subscription_auth")

    def response_safe(self, body: bytes) -> bool:
        # Bound allocations, fragment count and depth. Decode JSON/SSE strings
        # before checking common reflection encodings, including split deltas.
        strings = []
        content = []
        size = 0
        nodes = 0
        documents = (
            [body]
            if body.lstrip().startswith((b"{", b"["))
            else (
                line[5:].strip()
                for line in io.BytesIO(body)
                if line.startswith(b"data:")
            )
        )
        try:
            for document in documents:
                if len(document) > MAX_INPUT:
                    return False
                if document == b"[DONE]":
                    continue
                try:
                    pending = [(json.loads(document), 0, "")]
                except (ValueError, UnicodeError):
                    continue
                while pending:
                    value, depth, field = pending.pop()
                    nodes += 1
                    if depth > 64 or nodes > 100000:
                        return False
                    if isinstance(value, str):
                        encoded = value.encode()
                        size += len(encoded)
                        if size > MAX_OUTPUT:
                            return False
                        strings.append(encoded)
                        if field in {"delta", "text"}:
                            content.append(encoded)
                    elif isinstance(value, dict):
                        if len(value) * 2 + nodes > 100000:
                            return False
                        for key, item in reversed(list(value.items())):
                            pending.extend(
                                [(item, depth + 1, key), (key, depth + 1, "")]
                            )
                    elif isinstance(value, list):
                        if len(value) + nodes > 100000:
                            return False
                        pending.extend(
                            (item, depth + 1, field) for item in reversed(value)
                        )
        except (RecursionError, UnicodeError):
            return False
        decoded = b"".join(strings) + b"".join(content)
        compact = (body + decoded).translate(None, b" \t\r\n\v\f")
        variants = []
        for secret in (self.access.encode(), self.account.encode()):
            variants.extend(
                [
                    secret,
                    secret[::-1],
                    secret.hex().encode(),
                    secret.hex().upper().encode(),
                ]
            )
            for encoder in (base64.b64encode, base64.urlsafe_b64encode):
                encoded = encoder(secret)
                variants.extend([encoded, encoded.rstrip(b"=")])
        return not any(
            value in body or value in decoded or value in compact for value in variants
        )

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access,
            "ChatGPT-Account-Id": self.account,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
        }


def provider_child():
    """One killable host process bounds DNS/connect/header/body wall time.

    Secret material arrives only on a private pipe, never argv, env or disk.
    This child is trusted code, and emits at most MAX_OUTPUT + bounded metadata.
    """
    metadata = {"status": 502, "category": "subscription_upstream_refused"}
    body = b""
    try:
        header_line = sys.stdin.buffer.readline(MAX_METADATA + 1)
        if len(header_line) > MAX_METADATA:
            raise BrokerError("invalid_private_input")
        headers = json.loads(header_line)
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise BrokerError("invalid_private_input")
        request = urllib.request.Request(
            UPSTREAM, data=raw, headers=headers, method="POST"
        )
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=60
        ) as response:
            content_type = response.headers.get_content_type()
            if (
                response.status != 200
                or content_type
                not in {"application/json", "text/event-stream", "text/plain"}
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise BrokerError("invalid_upstream_response")
            body = response.read(MAX_OUTPUT + 1)
            if len(body) > MAX_OUTPUT:
                raise BrokerError("upstream_response_budget_exhausted")
            content_type = response_content_type(body, content_type)
            metadata = {"status": 200, "content_type": content_type}
    except urllib.error.HTTPError as exc:
        metadata = {
            "status": 429 if exc.code == 429 else 502,
            "category": "subscription_quota_exhausted"
            if exc.code == 429
            else "subscription_upstream_refused",
        }
        exc.close()
    except (OSError, ValueError, urllib.error.URLError, BrokerError):
        metadata = {"status": 502, "category": "subscription_upstream_refused"}
    if metadata["status"] != 200:
        body = b""
    encoded = json.dumps(metadata).encode()
    sys.stdout.buffer.write(struct.pack("!I", len(encoded)) + encoded + body)


def completed_turn(body, content_type):
    """Host-observed Responses completion and final assistant text, not CLI logs."""
    completed = False
    text = []
    documents = (
        [body]
        if content_type == "application/json"
        else (
            line[5:].strip() for line in io.BytesIO(body) if line.startswith(b"data:")
        )
    )
    try:
        for document in documents:
            if len(document) > MAX_INPUT:
                return False, ""
            if document == b"[DONE]":
                continue
            value = json.loads(document)
            if not isinstance(value, dict):
                continue
            if value.get("type") in {
                "error",
                "response.error",
                "response.failed",
                "response.incomplete",
            }:
                return False, ""
            if value.get("type") == "response.completed":
                response = value.get("response") or {}
                if not isinstance(response, dict):
                    return False, ""
                if response.get("error") or response.get("status") != "completed":
                    return False, ""
                completed = response.get("status") == "completed"
                items = response.get("output") or []
                # output_item.done is authoritative when the final response
                # omits its output array. Avoid duplicating both representations.
                if items:
                    text = []
            elif value.get("type") == "response.output_item.done":
                items = [value.get("item") or {}]
            elif value.get("object") == "response":
                completed = value.get("status") == "completed"
                items = value.get("output") or []
            else:
                continue
            if not isinstance(items, list) or len(items) > 10000:
                return False, ""
            for item in items:
                if not isinstance(item, dict):
                    return False, ""
                if (
                    item.get("type") != "message"
                    or item.get("role") != "assistant"
                    or item.get("phase") not in {None, "final_answer"}
                ):
                    continue
                contents = item.get("content") or []
                if not isinstance(contents, list):
                    return False, ""
                for content in contents:
                    if not isinstance(content, dict):
                        return False, ""
                    if (
                        isinstance(content, dict)
                        and content.get("type") == "output_text"
                        and isinstance(content.get("text"), str)
                    ):
                        text.append(content["text"])
            if sum(len(part) for part in text) > 1024 * 1024:
                return False, ""
    except (ValueError, TypeError, RecursionError):
        return False, ""
    return completed, "".join(text)


def response_content_type(body, declared):
    """Some subscription gateways label genuine completed SSE as text/plain.

    Only a completed Responses event stream can qualify that compatibility path;
    arbitrary text, HTML, partial/error streams and JSON remain refused.
    """
    if declared == "text/plain":
        if (
            not body.lstrip().startswith(b"event: response.")
            or not completed_turn(body, "text/event-stream")[0]
        ):
            raise BrokerError("invalid_upstream_response")
        return "text/event-stream"
    if declared not in {"application/json", "text/event-stream"}:
        raise BrokerError("invalid_upstream_response")
    return declared


class BufferedResponse(io.BytesIO):
    status = 200

    def __init__(self, body, content_type):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


def upstream_request(body, headers, *, deadline, server, connection):
    # -I ignores PYTHONPATH/user-site and puts no source checkout on sys.path.
    allowed = {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--provider-child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
    )
    server.register_process(connection, process)
    try:
        private_input = json.dumps(headers).encode() + b"\n" + body
        output, _ = process.communicate(
            private_input, timeout=max(0.01, deadline - time.monotonic())
        )
        if (
            process.returncode != 0
            or len(output) < 4
            or len(output) > MAX_OUTPUT + MAX_METADATA + 4
        ):
            raise BrokerError("subscription_upstream_refused")
        length = struct.unpack("!I", output[:4])[0]
        if not 0 < length <= MAX_METADATA or len(output) < 4 + length:
            raise BrokerError("subscription_upstream_refused")
        metadata = json.loads(output[4 : 4 + length])
        if metadata["status"] != 200:
            raise BrokerError(
                "subscription_quota_exhausted"
                if metadata["status"] == 429
                else "subscription_upstream_refused"
            )
        return BufferedResponse(output[4 + length :], metadata["content_type"])
    except subprocess.TimeoutExpired as exc:
        raise BrokerError("review_time_budget_exhausted") from exc
    finally:
        # kill()+wait cancels connect/header/slow body work, including DNS waits.
        if process.poll() is None:
            process.kill()
        process.communicate()
        server.unregister_process(connection)


class ModelBroker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 4

    def __init__(
        self,
        path: Path,
        subscription: Subscription,
        *,
        transport=upstream_request,
        max_requests=32,
        max_seconds=1200,
        max_connections=4,
        parse_seconds=5,
        turn_seconds=300,
    ):
        self.subscription = subscription
        self.transport = transport
        self.max_requests = max_requests
        self.deadline = time.monotonic() + max_seconds
        self.parse_seconds = parse_seconds
        self.turn_seconds = turn_seconds
        self.lock = threading.Lock()
        self.cache_key = "review-" + uuid.uuid4().hex
        self.reasoning_context = set()
        self.slots = threading.BoundedSemaphore(max_connections)
        self.connections = {}
        self.processes = {}
        self.stopping = threading.Event()
        self.calls = self.active = self.output_bytes = self.successful_turns = 0
        self.last_verdict = ""
        self.terminal_error = None
        super().__init__(str(path), BrokerHandler)
        os.chmod(path, 0o600)

    def process_request(self, request, client_address):
        # Admission happens before creating a handler thread or parsing a byte.
        if self.stopping.is_set() or not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self.lock:
            self.connections[request] = min(
                self.deadline, time.monotonic() + self.parse_seconds
            )
        try:
            super().process_request(request, client_address)
        except Exception:
            self.release_connection(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.release_connection(request)

    def release_connection(self, request):
        with self.lock:
            self.connections.pop(request, None)
        self.slots.release()

    def register_process(self, connection, process):
        with self.lock:
            if self.stopping.is_set() or time.monotonic() >= self.connections.get(
                connection, 0
            ):
                process.kill()
            self.processes[connection] = process

    def unregister_process(self, connection):
        with self.lock:
            self.processes.pop(connection, None)

    def watchdog(self):
        while not self.stopping.wait(0.025):
            self.cancel_expired()

    def cancel_expired(self, *, all_connections=False):
        with self.lock:
            expired = [
                connection
                for connection, deadline in self.connections.items()
                if all_connections or time.monotonic() >= deadline
            ]
            for connection in expired:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    self.last_error = "client_connection_closed"
                process = self.processes.get(connection)
                if process is not None and process.poll() is None:
                    process.kill()

    def begin(self, connection):
        with self.lock:
            if self.terminal_error:
                raise BrokerError(self.terminal_error)
            if self.calls >= self.max_requests:
                self.terminal_error = "review_request_budget_exhausted"
                raise BrokerError(self.terminal_error)
            if (
                self.stopping.is_set()
                or self.active >= 2
                or time.monotonic() >= self.connections.get(connection, 0)
            ):
                return None
            self.calls += 1
            self.active += 1
            deadline = min(self.deadline, time.monotonic() + self.turn_seconds)
            self.connections[connection] = deadline
            return deadline

    def fail(self, category):
        with self.lock:
            if self.terminal_error is None:
                self.terminal_error = category

    def finish(self, count):
        with self.lock:
            self.active -= 1
            self.output_bytes += count

    def audit(self):
        return {
            "requests": self.calls,
            "successful_provider_turns": self.successful_turns,
            "output_bytes": self.output_bytes,
            "caller_headers_forwarded": False,
            "provider_route": "fixed_responses_only",
        }

    def remember_reasoning(self, body, content_type):
        documents = [body] if content_type == "application/json" else [
            line[5:].strip() for line in io.BytesIO(body) if line.startswith(b"data:")
        ]
        found = set()
        try:
            for document in documents:
                if document == b"[DONE]":
                    continue
                value = json.loads(document)
                if not isinstance(value, dict):
                    continue
                if value.get("type") == "response.output_item.done":
                    items = [value.get("item")]
                elif value.get("type") == "response.completed":
                    items = value["response"].get("output", [])
                elif value.get("object") == "response":
                    items = value.get("output", [])
                else:
                    continue
                if not isinstance(items, list):
                    raise ValueError("invalid provider output")
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError("invalid provider item")
                    if item.get("type") == "reasoning" and "encrypted_content" in item:
                        local_text(item["encrypted_content"])
                        found.add(hashlib.sha256(item["encrypted_content"].encode()).digest())
        except (KeyError, ValueError, TypeError, RecursionError) as exc:
            raise BrokerError("subscription_upstream_refused") from exc
        with self.lock:
            if len(self.reasoning_context | found) > 4096:
                raise BrokerError("subscription_upstream_refused")
            self.reasoning_context.update(found)

    def handle_error(self, request, client_address):
        self.last_error = "client_request_failed"


class BrokerHandler(BaseHTTPRequestHandler):
    server: ModelBroker
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        return

    def reject(self, status, category):
        body = json.dumps({"error": category}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        self.reject(405, "unsupported_operation")

    do_GET = do_CONNECT
    do_PUT = do_CONNECT
    do_DELETE = do_CONNECT
    do_PATCH = do_CONNECT

    def do_POST(self):
        if self.path != "/v1/responses":
            self.reject(404, "unsupported_operation")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            self.reject(400, "invalid_framing")
            return
        try:
            length = int(lengths[0])
            if (
                not 0 < length <= MAX_INPUT
                or self.headers.get_content_type() != "application/json"
            ):
                raise ValueError("invalid input")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("short body")
            raw = review_request(raw, self.server)
        except (OSError, ValueError, TypeError, RecursionError):
            self.reject(400, "invalid_request")
            return
        try:
            deadline = self.server.begin(self.connection)
        except BrokerError as exc:
            self.reject(400, str(exc))
            return
        if deadline is None:
            self.reject(503, "review_admission_unavailable")
            return
        count = 0
        try:
            with self.server.transport(
                raw,
                self.server.subscription.headers(),
                deadline=deadline,
                server=self.server,
                connection=self.connection,
            ) as response:
                content_type = response.headers.get_content_type()
                if (
                    response.status != 200
                    or response.headers.get("Content-Encoding", "identity")
                    != "identity"
                    or content_type
                    not in {"application/json", "text/event-stream", "text/plain"}
                ):
                    self.server.fail("subscription_upstream_refused")
                    self.reject(502, "invalid_upstream_response")
                    return
                body = response.read(MAX_OUTPUT + 1)
                count = len(body)
                if count > MAX_OUTPUT:
                    self.server.fail("subscription_upstream_refused")
                    self.reject(502, "upstream_response_budget_exhausted")
                    return
                content_type = response_content_type(body, content_type)
                if not self.server.subscription.response_safe(body):
                    self.server.fail("subscription_upstream_refused")
                    self.reject(502, "upstream_response_withheld")
                    return
                completed, verdict = completed_turn(body, content_type)
                if not completed:
                    self.server.fail("subscription_upstream_refused")
                    self.reject(502, "incomplete_upstream_response")
                    return
                self.server.remember_reasoning(body, content_type)
                # Record validated provider completion before the client can
                # observe HTTP success; otherwise an immediate audit races it.
                with self.server.lock:
                    self.server.successful_turns += 1
                    self.server.last_verdict = verdict
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            self.server.fail(
                "subscription_quota_exhausted"
                if status == 429
                else "subscription_upstream_refused"
            )
            self.reject(
                429 if status == 429 else 502,
                "subscription_quota_exhausted"
                if status == 429
                else "subscription_upstream_refused",
            )
        except BrokerError as exc:
            category = str(exc)
            if category not in {
                "subscription_quota_exhausted",
                "subscription_upstream_refused",
                "review_time_budget_exhausted",
            }:
                category = "subscription_upstream_refused"
            self.server.fail(category)
            self.reject(
                429 if category == "subscription_quota_exhausted" else 502, category
            )
        except (OSError, urllib.error.URLError):
            self.server.fail("subscription_upstream_refused")
            self.close_connection = True
        finally:
            self.server.finish(count)


@contextmanager
def serve(path: Path, subscription: Subscription, **kwargs):
    server = ModelBroker(path, subscription, **kwargs)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    watchdog = threading.Thread(target=server.watchdog, daemon=True)
    thread.start()
    watchdog.start()
    try:
        yield server
    finally:
        server.stopping.set()
        server.shutdown()
        server.cancel_expired(all_connections=True)
        server.server_close()
        thread.join(timeout=3)
        watchdog.join(timeout=3)
        # Production transports are killable; wait for their handlers to reap.
        until = time.monotonic() + 3
        while server.connections and time.monotonic() < until:
            time.sleep(0.01)
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:] != ["--provider-child"]:
        raise SystemExit("private provider child only")
    # Linux runner parent-death cancellation also covers abrupt supervisor loss.
    import ctypes
    import signal

    parent = os.getppid()
    if (
        parent == 1
        or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) != 0
        or os.getppid() != parent
    ):
        raise SystemExit("provider_parent_unavailable")
    provider_child()
