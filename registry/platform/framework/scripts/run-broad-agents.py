#!/usr/bin/env python3
"""
Stage 2: Broad agents for PR assessment.

Invokes systems-consistency, architecture-impact, and security-deep-dive agents
in parallel. Reads PR diff and manifest, writes findings JSON per agent.

Usage:
  python3 scripts/run-broad-agents.py \\
    --manifest .ai/candidates/manifest.json \\
    --diff /tmp/pr.diff \\
    --output-dir .ai/candidates/
"""

import argparse
import importlib.util
import hashlib
import math
import subprocess
import tempfile
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml

# Load the exact hydrated helper, never an ambient/PR-added lib package initializer.
# The launcher still owns complete runtime custody; this is not shell isolation.
try:
    _policy_spec = importlib.util.spec_from_file_location(
        "_assessment_route_policy", Path(__file__).resolve().parent / "lib/agent_capabilities.py")
    if _policy_spec is None or _policy_spec.loader is None:
        raise ImportError("missing policy loader")
    _policy_module = importlib.util.module_from_spec(_policy_spec)
    # Execute hydrated source bytes, not a PR-retained bytecode cache.
    _policy_path = _policy_spec.origin
    exec(compile(Path(_policy_path).read_bytes(), _policy_path, "exec"),
         _policy_module.__dict__)
    RouteAdmissionError = _policy_module.RouteAdmissionError
    require_direct_llm_admission = _policy_module.require_direct_llm_admission
except (ImportError, OSError, AttributeError):
    # Mixed/older consumer runtime: preserve ordinary proxy calls, but never
    # admit a direct call without the shared policy implementation.
    class RouteAdmissionError(ValueError):
        pass

    def require_direct_llm_admission():
        raise RouteAdmissionError("direct review denied: compatible policy runtime missing")


logging.basicConfig(
    level=logging.INFO,
    format="[broad-agents] %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Use proxy model aliases directly; do not remap to full Claude model IDs.
# The LiteLLM proxy maps these aliases to OSS models (see deploy/litellm-proxy/README.md).
MODEL_ALIASES = {
    "haiku": "haiku",
    "sonnet": "sonnet",
}

# Short frontmatter aliases are proxy-side names; on the direct Anthropic plane
# they are not valid model IDs — map them back to dated IDs.
DIRECT_MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

BROAD_AGENTS = [
    "systems-consistency",
    "architecture-impact",
    "security-deep-dive",
]

# The former implicit 40K excerpt was not complete review. An explicit owner
# limit remains a rejection boundary; it must never turn into a prefix review.
MAX_DIFF_CHARS = (int(os.environ["BROAD_AGENTS_MAX_DIFF_CHARS"])
                  if "BROAD_AGENTS_MAX_DIFF_CHARS" in os.environ else None)
if MAX_DIFF_CHARS is not None and MAX_DIFF_CHARS <= 0:
    raise ValueError("BROAD_AGENTS_MAX_DIFF_CHARS must be positive")
# Same conservative Messages transport ceiling as the specialist runner.
# https://platform.claude.com/docs/en/api/errors#request-size-limits
# This is not evidence of a model's context entitlement; a tighter upstream
# context limit still fails the review instead of authorizing an excerpt.
_MAX_REQUEST_BYTES = 32_000_000
# 240s covers a primary call plus its fail-closed advisor round trip on the
# proxy — 120s was too tight for advisor-backed security-deep-dive-sized
# payloads (buromaster#25) and aborts mid-advisor into api_error markers.
TIMEOUT_SECS = 240
MAX_RETRIES = 4
RETRIABLE_STATUS = {429, 500, 502, 503, 504}
# Extra max_tokens for a starved-answer retry on the proxy: OSS aliases
# reason in-band and `thinking` shares max_tokens (budget_tokens is ignored —
# see the budgets comment in _invoke_agent). Sized at ~2x observed reasoning.
_PROXY_THINKING_HEADROOM = 2048


@dataclass
class AgentConfig:
    """Agent frontmatter config."""
    name: str
    model: str
    data_sensitivity: str
    system_prompt: str
    instructions: str
    client_policy_model: Optional[str] = None
    first_party: bool = False


def parse_agent_file(agent_path: Path) -> AgentConfig:
    """Parse agent markdown file, extract frontmatter and content."""
    content = agent_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        logger.error(f"Agent {agent_path.name} missing frontmatter")
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        logger.error(f"Agent {agent_path.name} malformed frontmatter")
        return None

    try:
        frontmatter = yaml.safe_load(parts[1])
        instructions = parts[2].strip()
    except yaml.YAMLError as e:
        logger.error(f"Agent {agent_path.name} YAML parse error: {e}")
        return None
    if not isinstance(frontmatter, dict):
        logger.error("Agent %s frontmatter is not an object", agent_path.name)
        return None

    name = frontmatter.get("name", agent_path.stem)
    model = frontmatter.get("model", "sonnet")
    data_sensitivity = frontmatter.get("data_sensitivity", "internal")
    system_prompt = frontmatter.get("system_prompt", "You are a helpful code reviewer.")

    return AgentConfig(
        name=name,
        model=model,
        data_sensitivity=data_sensitivity,
        system_prompt=system_prompt,
        instructions=instructions,
        client_policy_model=frontmatter.get("client_policy_model"),
        first_party=bool(frontmatter.get("first_party")),
    )


_ANTHROPIC_HOST = "api.anthropic.com"


def _proxy_base() -> Optional[str]:
    """Non-Anthropic proxy base when configured, else None."""
    base = (
        os.environ.get("LITELLM_PROXY_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or ""
    ).rstrip("/")
    if not base:
        return None
    if (urlparse(base).hostname or "").lower().rstrip(".") == _ANTHROPIC_HOST:
        return None
    return base


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects: a 3xx would re-send Authorization/x-api-key to the target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen_https(req: Request, *, timeout: int, host: str):
    """Open one trusted HTTPS origin without following redirects."""
    parsed = urlparse(req.full_url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise ValueError("refusing non-HTTPS or unexpected request host")
    return build_opener(_NoRedirectHandler()).open(req, timeout=timeout)  # nosec B310


def get_api_key() -> Optional[str]:
    """Transport token: proxy accepts LLM_API_KEY/LITELLM_MASTER_KEY; direct uses ANTHROPIC_API_KEY."""
    key: Optional[str] = None
    if _proxy_base():
        # A genuine sk-ant-* key is a first-party credential and must never
        # leave for a non-Anthropic host — legacy configs keep the proxy key
        # under ANTHROPIC_API_KEY, which is not sk-ant-shaped. Skip candidates
        # that fail the check so a lower-priority proxy token still works.
        for candidate in (
            os.getenv("LLM_API_KEY"),
            os.getenv("LITELLM_MASTER_KEY"),
            os.getenv("ANTHROPIC_API_KEY"),
        ):
            if not candidate:
                continue
            normalized = candidate.strip()
            # The auth-scheme token is case-insensitive (RFC 7235).
            if normalized.lower() == "bearer" or normalized.lower().startswith("bearer "):
                normalized = normalized[7:].strip()
            if not normalized or normalized.lower().startswith("sk-ant-"):
                continue
            key = normalized
            break
    else:
        require_direct_llm_admission()
        key = os.getenv("ANTHROPIC_DIRECT_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("no API credential set; skipping agent invocations")
        return None
    return key


def get_changed_files() -> list[str]:
    """Parse changed files from CHANGED_FILES env (newline-separated)."""
    changed = os.getenv("CHANGED_FILES", "").strip()
    if not changed:
        return []
    return [f.strip() for f in changed.split("\n") if f.strip()]


_WRAP_TAGS = ("untrusted_diff", "untrusted_pr_meta", "changed_paths")


def _neutralize(text: str) -> str:
    """Defang wrapper tag names inside untrusted content (same as run-specialists)."""
    for tag in _WRAP_TAGS:
        text = text.replace(f"<{tag}>", f"<{tag} >").replace(f"</{tag}>", f"</{tag} >")
    return text


def build_user_message(diff: str, changed_files: list[str]) -> str:
    """Build user message with untrusted diff and changed files."""
    diff = _neutralize(diff)
    if changed_files:
        # File paths are PR-author-controlled — keep them inside the
        # untrusted boundary (changed_paths convention, same as
        # run-specialists.py) rather than as plain trailing text, and
        # neutralize them so a crafted path can't close the boundary.
        files_str = "\n".join(f"  - {_neutralize(f)}" for f in changed_files)
        diff = f"{diff}\n<changed_paths>\n{files_str}\n</changed_paths>"
    return f"<untrusted_diff>\n{diff}\n</untrusted_diff>"


# Provider-free preparation only. No caller in run_broad_agents/invoke_agent is
# changed to dispatch this contract or turn it into accepted protected coverage.
_DEEP_DIVE_VERSION = "security-deep-dive-input-v1"


def _deep_json(raw: str | bytes):
    """One strict JSON value; duplicate fields/NaN are never coverage evidence."""
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate deep-dive JSON field")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("nonfinite deep-dive JSON value")

    # JSON bytes already accept a UTF-8 BOM. Match that parsing for retained
    # text without changing the original bytes, packet text or identity hash.
    if isinstance(raw, str) and raw.startswith("\ufeff"):
        raw = raw[1:]
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _deep_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _deep_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid deep-dive source path")
    # Bandit's real producer uses ./ prefixes. Retain the original finding,
    # but use one canonical repository-relative path for immutable lookup.
    path = value[2:] if value.startswith("./") else value
    if (not path or path.startswith("/") or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(char in path for char in "\x85\u2028\u2029")):
        raise ValueError("invalid deep-dive source path")
    return path


def _deep_git(root: Path, *args: str, limit: int) -> bytes:
    """Local immutable objects only; no inherited Git redirects or credentials.

    Spool output under a bounded execution timeout, then refuse excess rather
    than return a prefix. No filters, remote fetching, hooks or candidate code.
    """
    with tempfile.TemporaryFile() as output:
        try:
            completed = subprocess.run(
                ["git", "--no-pager", "--no-replace-objects", "--no-lazy-fetch",
                 "--literal-pathspecs", "-C", str(root), "-c", "core.fsmonitor=false",
                 *args], stdout=output, stderr=subprocess.DEVNULL, timeout=15,
                env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1",
                     "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
            size = output.tell()
            if completed.returncode or size > limit:
                raise ValueError("deep-dive immutable evidence unavailable or over budget")
            output.seek(0)
            return output.read()
        except (OSError, subprocess.SubprocessError):
            raise ValueError("deep-dive immutable evidence unavailable") from None


def _deep_screen_helpers():
    # Reuse the existing canonical Judge source/privacy guard. Its modules are
    # already part of the hydrated broad/Judge runtime. Do not execute Git blobs.
    path = Path(__file__).resolve().with_name("run-judge.py")
    spec = importlib.util.spec_from_file_location("_deep_dive_source_guard", path)
    if spec is None or spec.loader is None:
        raise ValueError("deep-dive source guard unavailable")
    module = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def _deep_entries(sast_hash: str, findings: list) -> list[dict]:
    entries = []
    for position, finding in enumerate(findings):
        if (not isinstance(finding, dict)
                or any(not isinstance(finding.get(k), str) or not finding[k].strip()
                       for k in ("finding_id", "rule", "file", "message", "severity"))
                or type(finding.get("line")) is not int or finding["line"] < 1):
            raise ValueError("invalid deep-dive SAST entry")
        entries.append({"entry_id": _deep_digest({"sast_sha256": sast_hash,
                        "position": position, "finding": finding}),
                        "position": position, "finding": finding,
                        "path": _deep_path(finding["file"])})
    return entries


def build_security_deep_dive_input(
    root: Path, *, repository: str, head: str, base: str, sast: bytes,
    sast_binding: dict, evidence_paths: list[str], max_bytes: int = _MAX_REQUEST_BYTES,
) -> dict:
    """Prepare complete local evidence, NOT an authenticated/accepted review.

    Bind supplied SAST bytes to caller control metadata, every entry's position
    and content, full base-to-head diff and complete old/new requested blobs.
    Caller metadata/hashes do not prove SAST producer authenticity or data-flow
    completeness. A future owning launcher must establish those independently.
    Exceptions mean incomplete preparation; never silently omit unavailable data.
    """
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not all(isinstance(ref, str) and re.fullmatch(r"[0-9a-f]{40}", ref)
                       for ref in (head, base))
            or type(max_bytes) is not int or not 0 < max_bytes <= _MAX_REQUEST_BYTES
            or not isinstance(sast, bytes) or len(sast) > max_bytes):
        raise ValueError("invalid deep-dive input binding or budget")
    sast_hash = hashlib.sha256(sast).hexdigest()
    identity = {"repository": repository, "head": head, "base": base,
                "sast_sha256": sast_hash}
    if sast_binding != identity:
        raise ValueError("missing or stale deep-dive SAST binding")
    try:
        sast_text = sast.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("invalid deep-dive SAST encoding") from None
    findings = _deep_json(sast_text)
    if not isinstance(findings, list) or not findings:
        raise ValueError("missing deep-dive SAST entries")
    guard = _deep_screen_helpers()
    entries = _deep_entries(sast_hash, findings)
    if not isinstance(evidence_paths, list):
        raise ValueError("missing deep-dive source scope")
    paths = sorted({entry["path"] for entry in entries}
                   | {_deep_path(path) for path in evidence_paths})
    if any(guard._sensitive_evidence_path(path) for path in paths):
        raise ValueError("sensitive deep-dive evidence path")
    for ref in (head, base):
        if _deep_git(root, "rev-parse", "--verify", ref + "^{commit}", limit=64).decode().strip() != ref:
            raise ValueError("deep-dive commit identity mismatch")
    raw_diff = _deep_git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                         "--binary", "--full-index", base, head, "--", limit=max_bytes)
    changed = _deep_git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                        "--name-only", "-z", base, head, "--", limit=max_bytes)
    changed_paths = [_deep_path(path.decode("utf-8")) for path in changed.split(b"\0") if path]
    if any(guard._sensitive_evidence_path(path) for path in changed_paths):
        raise ValueError("sensitive deep-dive diff path")
    # Git's binary patch is encoded data, not plaintext admission. Inspect the
    # complete old/new blobs of EVERY changed path, not only SAST locations.
    paths = sorted(set(paths) | set(changed_paths))
    # Raw bytes and JSON strings both remain intact. No AST source exceptions
    # are invented for a diff or a caller-supplied evidence document.
    guard._require_safe_packet(raw_diff.decode("utf-8"))
    sources = []
    used = len(sast) + len(raw_diff)
    for path in paths:
        for side, ref in (("base", base), ("head", head)):
            row = _deep_git(root, "ls-tree", "-z", ref, "--", path, limit=max_bytes)
            if not row:
                if path not in changed_paths and side == "head":
                    raise ValueError("deep-dive candidate source missing")
                sources.append({"side": side, "path": path, "status": "absent", "commit": ref})
                continue
            metadata, actual = row.rstrip(b"\0").split(b"\t", 1)
            mode, kind, blob = metadata.decode("ascii").split()
            if (actual.decode("utf-8") != path or mode not in {"100644", "100755"}
                    or kind != "blob" or not re.fullmatch(r"[0-9a-f]{40}", blob)):
                raise ValueError("nonregular or ambiguous deep-dive source")
            body = _deep_git(root, "cat-file", "blob", blob, limit=max(0, max_bytes - used))
            if b"\0" in body:
                raise ValueError("binary deep-dive source")
            text = body.decode("utf-8")
            if any(char in text for char in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
                raise ValueError("ambiguous deep-dive physical source lines")
            guard._require_safe_packet(text)
            used += len(body)
            sources.append({"side": side, "path": path, "status": "available", "commit": ref,
                            "mode": mode, "blob": blob, "sha256": hashlib.sha256(body).hexdigest(),
                            "text": text})
    for entry in entries:
        source = next(s for s in sources if s["side"] == "head" and s["path"] == entry["path"])
        if source["status"] != "available" or entry["finding"]["line"] > len(source["text"].splitlines()):
            raise ValueError("deep-dive SAST location not in candidate source")
    packet = {"version": _DEEP_DIVE_VERSION, **identity,
              "provenance": "caller-bound; producer authenticity not established",
              "scope": "complete requested blobs; flow/validation completeness requires verification",
              "sast_utf8": sast_text, "entries": entries, "sources": sources,
              "diff": {"text": raw_diff.decode("utf-8"), "sha256": hashlib.sha256(raw_diff).hexdigest()}}
    guard._require_safe_packet(packet)
    packet["input_sha256"] = _deep_digest(packet)
    # Include every JSON wrapper/escape in this existing conservative ceiling.
    if len(json.dumps(packet, ensure_ascii=True).encode()) > max_bytes:
        raise ValueError("complete deep-dive input exceeds budget")
    return packet


def parse_security_deep_dive_output(raw: str, packet: dict, *, stop_reason: str) -> dict:
    """Account for ALL entries; return provisional data, never a Judge candidate.

    References demonstrate supplied evidence was addressed, not that a model's
    reasoning is sound. Even a valid result is not protected-route qualification,
    a provider receipt or accepted runtime coverage. Independent legs must each
    account for every entry; disagreement is retained, never voted away.
    """
    if not isinstance(packet, dict) or not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ValueError("invalid deep-dive output or input")
    binding = packet.get("input_sha256")
    if binding != _deep_digest({key: value for key, value in packet.items() if key != "input_sha256"}):
        raise ValueError("changed deep-dive input")
    if packet.get("version") != _DEEP_DIVE_VERSION or stop_reason != "end_turn":
        raise ValueError("incomplete deep-dive output")
    # A digest binds bytes, not authority. Reconcile retained producer bytes
    # with every position/content identity before trusting even local accounting.
    sast_text = packet.get("sast_utf8")
    if not isinstance(sast_text, str):
        raise ValueError("missing retained deep-dive SAST")
    sast_hash = hashlib.sha256(sast_text.encode("utf-8")).hexdigest()
    findings = _deep_json(sast_text)
    if (sast_hash != packet.get("sast_sha256") or not isinstance(findings, list)
            or not findings or packet.get("entries") != _deep_entries(sast_hash, findings)):
        raise ValueError("inconsistent deep-dive SAST accounting")
    # Recomputed outer hashes cannot bless internally contradictory immutable
    # evidence. Git OIDs here are byte identities, never approval authority.
    sources = packet.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("missing deep-dive source evidence")
    seen_sources = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("invalid deep-dive source evidence")
        side, path = source.get("side"), source.get("path")
        if (not isinstance(side, str) or side not in {"base", "head"} or _deep_path(path) != path
                or source.get("commit") != packet.get(side) or (side, path) in seen_sources):
            raise ValueError("inconsistent deep-dive source identity")
        seen_sources.add((side, path))
        if source.get("status") == "absent":
            if set(source) != {"side", "path", "status", "commit"}:
                raise ValueError("invalid absent deep-dive source")
            continue
        if (source.get("status") != "available" or source.get("mode") not in {"100644", "100755"}
                or not isinstance(source.get("text"), str)):
            raise ValueError("invalid available deep-dive source")
        body = source["text"].encode("utf-8")
        blob = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body,
                            usedforsecurity=False).hexdigest()
        if (source.get("sha256") != hashlib.sha256(body).hexdigest() or source.get("blob") != blob):
            raise ValueError("deep-dive immutable source bytes mismatch")
    for entry in packet["entries"]:
        matches = [s for s in sources if s["side"] == "head" and s["path"] == entry["path"]]
        if (len(matches) != 1 or matches[0]["status"] != "available"
                or entry["finding"]["line"] > len(matches[0]["text"].splitlines())):
            raise ValueError("missing deep-dive triggering source")
    diff = packet.get("diff")
    if (not isinstance(diff, dict) or not isinstance(diff.get("text"), str)
            or hashlib.sha256(diff["text"].encode("utf-8")).hexdigest() != diff.get("sha256")):
        raise ValueError("deep-dive retained diff mismatch")
    result = _deep_json(raw)
    if (not isinstance(result, dict) or set(result) != {"input_sha256", "entries"}
            or result["input_sha256"] != binding or not isinstance(result["entries"], list)):
        raise ValueError("invalid deep-dive output binding")
    _deep_screen_helpers()._require_safe_packet(result)
    expected = {entry["entry_id"]: entry for entry in packet["entries"]}
    seen, promoted, dismissed, incomplete = set(), [], [], []
    for row in result["entries"]:
        if not isinstance(row, dict) or set(row) != {
                "entry_id", "decision", "true_positive_likelihood", "data_flow",
                "validation", "exploitability", "missing_context", "message", "fix"}:
            raise ValueError("invalid deep-dive triage entry")
        identifier = row["entry_id"]
        if not isinstance(identifier, str) or identifier not in expected or identifier in seen:
            raise ValueError("unknown or repeated deep-dive entry")
        seen.add(identifier)
        if not isinstance(row["decision"], str):
            raise ValueError("invalid deep-dive decision")
        likelihood = row["true_positive_likelihood"]
        missing = row["missing_context"]
        if (not isinstance(missing, list) or any(not isinstance(x, str) or not x.strip() for x in missing)
                or any(not isinstance(row[k], str) or not row[k].strip() for k in ("message", "fix"))):
            raise ValueError("invalid deep-dive triage detail")
        if row["decision"] == "incomplete":
            if not missing or likelihood is not None:
                raise ValueError("incomplete deep-dive requires explicit unknown context")
            incomplete.append(row)
            continue
        if (row["decision"] not in {"promote", "dismiss"} or missing
                or type(likelihood) not in {float, int} or not 0 <= likelihood <= 1
                or not math.isfinite(likelihood)
                or (row["decision"] == "promote") != (likelihood >= 0.7)):
            raise ValueError("invalid deep-dive likelihood or disposition")
        triggered_source_cited = False
        for key in ("data_flow", "validation", "exploitability"):
            evidence = row[key]
            if (not isinstance(evidence, dict) or set(evidence) != {"summary", "citations"}
                    or not isinstance(evidence["summary"], str) or not evidence["summary"].strip()
                    or not isinstance(evidence["citations"], list) or not evidence["citations"]):
                raise ValueError("missing deep-dive reasoning evidence")
            for citation in evidence["citations"]:
                if not isinstance(citation, dict) or set(citation) != {"path", "side", "blob", "start", "end"}:
                    raise ValueError("invalid deep-dive citation")
                if not all(isinstance(citation[field], str) for field in ("path", "side", "blob")):
                    raise ValueError("invalid deep-dive citation identity")
                matches = [source for source in packet["sources"] if source["status"] == "available" and all(
                    source.get(field) == citation[field] for field in ("path", "side", "blob"))]
                if (len(matches) != 1 or type(citation["start"]) is not int
                        or type(citation["end"]) is not int
                        or not 1 <= citation["start"] <= citation["end"] <= len(matches[0]["text"].splitlines())):
                    raise ValueError("deep-dive evidence mismatch")
                entry = expected[identifier]
                if (key == "data_flow" and citation["side"] == "head"
                        and citation["path"] == entry["path"]
                        and citation["start"] <= entry["finding"]["line"] <= citation["end"]):
                    triggered_source_cited = True
        if not triggered_source_cited:
            raise ValueError("deep-dive triggering source not traced")
        (promoted if likelihood >= 0.7 else dismissed).append(row)
    if seen != set(expected):
        raise ValueError("unaccounted deep-dive SAST entries")
    # No source/findings/protected_review/first_party receipt fields: this object
    # cannot satisfy the existing Judge's candidate schema or protected adapter.
    return {"kind": "security-deep-dive-preparation", "input_sha256": binding,
            "status": "incomplete" if incomplete else "provisional_complete",
            "promoted": promoted, "dismissed": dismissed, "incomplete": incomplete,
            "escalation_required": [row["entry_id"] for row in promoted
                                    if row["true_positive_likelihood"] >= 0.9],
            "provider_execution": "unknown", "effectiveness": "unknown"}


def _incomplete(agent: AgentConfig, reason: str) -> dict[str, Any]:
    """Retain unavailable role coverage without inventing a clean result."""
    return {"source": agent.name, "findings": [], "skipped": reason,
            "first_party": bool(agent.first_party or os.environ.get("CLIENT_AI_POLICY"))}


def _parse_findings(raw: str) -> dict[str, Any]:
    """Validate one complete findings payload, retaining legacy list results.

    Prose/fences may surround one JSON container. Never salvage a nested empty
    list from malformed JSON or choose a clean result among multiple payloads.
    """
    text = raw.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = re.search(r"[\[{]", text)
        if start is None:
            raise
        parsed, end = json.JSONDecoder().raw_decode(text, start.start())
        if re.search(r"[\[{]", text[end:]):
            raise ValueError("ambiguous findings payload")
    skipped = None
    # Absent status is the legacy contract. When supplied, accept only explicit
    # completion, rather than guessing that an unfamiliar state means success.
    if isinstance(parsed, dict) and ("error" in parsed or
            ("status" in parsed and parsed["status"] not in
             ("complete", "completed", "success", "ok"))):
        raise ValueError("response reports incomplete assessment")
    if isinstance(parsed, list):
        findings = parsed
    elif isinstance(parsed, dict) and "findings" in parsed:
        findings = parsed["findings"]
        if "skipped" in parsed:
            skipped = parsed["skipped"]
            if not isinstance(skipped, str) or not skipped.strip():
                raise ValueError("invalid incomplete-coverage marker")
    elif isinstance(parsed, dict) and any(k in parsed for k in ("file", "message", "severity")):
        findings = [parsed]
    else:
        raise ValueError("missing findings contract")
    if not isinstance(findings, list) or not all(
        isinstance(f, dict) and any(k in f for k in ("file", "message", "severity"))
        for f in findings
    ):
        raise ValueError("findings must be a list of finding objects")
    for finding in findings:
        if any(key in finding and not isinstance(finding[key], str)
               for key in ("file", "message", "severity", "fix")):
            raise ValueError("finding text fields must be strings")
        line = finding.get("line")
        if line is not None and (type(line) is not int or line < 1):
            raise ValueError("finding line must be a positive integer or null")
    defaults = {"file": "", "line": None, "severity": "WARNING", "message": "", "fix": ""}
    result = {"findings": [{**defaults, **finding} for finding in findings]}
    if skipped is not None:
        result["skipped"] = skipped
    return result


def _api_instructions(instructions: str) -> str:
    """Omit explicitly marked interactive setup, preserving every check/output.

    Unmarked legacy roles remain unchanged. Mismatched markers fail before a
    request rather than accidentally discarding the rest of a review prompt.
    """
    start, end = "<!-- api-interactive:start -->", "<!-- api-interactive:end -->"
    if start not in instructions and end not in instructions:
        return instructions
    if instructions.count(start) != 1 or instructions.count(end) != 1:
        raise ValueError("invalid API instruction boundaries")
    before, body = instructions.split(start)
    interactive, after = body.split(end)
    return before + "API execution: no tools or file writes are available.\n" + after


def invoke_agent(
    agent_config: AgentConfig,
    api_key: str,
    user_message: str,
) -> Optional[dict[str, Any]]:
    """Invoke agent via the configured model API, return parsed findings."""
    # Use proxy alias from frontmatter directly (haiku / sonnet map to OSS models).
    model = MODEL_ALIASES.get(agent_config.model, agent_config.model)

    # data_sensitivity=restricted is governance no-AI — never dispatch to any model.
    if agent_config.data_sensitivity == "restricted":
        logger.warning(
            f"{agent_config.name}: data_sensitivity=restricted (no-AI) — skipping invocation"
        )
        return None
    if sum(len(part.encode("utf-8")) for part in
           (agent_config.system_prompt, agent_config.instructions, user_message)) > _MAX_REQUEST_BYTES:
        logger.error("%s: complete review input exceeds request limit", agent_config.name)
        return _incomplete(agent_config, "input_limit")
    # Route through the LiteLLM proxy when configured (Bearer auth); otherwise
    # direct Anthropic (x-api-key). The retired anthropic_only tier has no
    # callers left — its agents were remapped to restricted_us_oss_ok.
    proxy = _proxy_base()
    # True once this call is bound to the Anthropic-direct plane — a runtime
    # failure must then degrade to the skipped first-party marker so the judge
    # fails closed instead of adjudicating without the required direct leg.
    direct_required = False
    if (agent_config.first_party and not os.environ.get("CLIENT_AI_POLICY")
            and os.environ.get("SECURITY_REVIEW_MODE", "direct") != "direct"):
        # None of the broad security roles has an accepted estate producer.
        # Preserve the three-argument API and missing coverage; do not route an
        # unqualified role through either the proxy or a direct fallback.
        return {"source": agent_config.name, "findings": [],
                "first_party": True, "skipped": "protected_review_incomplete"}
    try:
        if agent_config.first_party or os.environ.get("CLIENT_AI_POLICY") or not proxy:
            require_direct_llm_admission()
    except RouteAdmissionError as exc:
        logger.warning("%s: %s", agent_config.name, exc)
        return {"source": agent_config.name, "findings": [],
                "first_party": bool(agent_config.first_party or os.environ.get("CLIENT_AI_POLICY")),
                "skipped": "route_policy_denied"}
    # first_party agents (security review per the eligibility matrix) always go
    # Anthropic-direct — never the OSS proxy, regardless of declared model.
    if agent_config.first_party:
        direct_required = True
        api_key = os.getenv("ANTHROPIC_DIRECT_API_KEY") or (
            os.getenv("ANTHROPIC_API_KEY") if not proxy else None
        )
        if not api_key:
            logger.warning(
                f"{agent_config.name}: first_party agent needs "
                "ANTHROPIC_DIRECT_API_KEY (the proxy credential cannot "
                "authenticate Anthropic-direct); skipping"
            )
            # Marker contract shared with run-specialists.py: a skipped
            # direct-only agent must leave a durable candidate file so the
            # judge cannot post a clean verdict on incomplete coverage.
            return {
                "source": agent_config.name,
                "findings": [],
                "first_party": True,
                "skipped": "no_direct_key",
            }
        proxy = None
        model = agent_config.client_policy_model or DIRECT_MODEL_MAP.get(model, model)
        logger.info(
            f"{agent_config.name}: first_party — dispatching {model} (Anthropic-direct)"
        )
    # Engagement carrying client_ai_policy is Anthropic-direct for EVERY
    # agent — the proxy/OSS route is bypassed entirely. Agents declaring
    # client_policy_model pin that model; the rest dispatch on their declared
    # model's Anthropic equivalent (DIRECT_MODEL_MAP below).
    elif os.environ.get("CLIENT_AI_POLICY"):
        direct_required = True
        # The shared credential resolved before this point is the proxy token
        # whenever a proxy is configured — api.anthropic.com would reject it
        # (and it must never leave the boundary as x-api-key to that host).
        # The direct plane requires a real Anthropic key.
        api_key = os.getenv("ANTHROPIC_DIRECT_API_KEY") or (
            os.getenv("ANTHROPIC_API_KEY") if not proxy else None
        )
        if not api_key:
            logger.warning(
                f"{agent_config.name}: CLIENT_AI_POLICY engagement needs "
                "ANTHROPIC_DIRECT_API_KEY (the proxy credential cannot "
                "authenticate Anthropic-direct); skipping"
            )
            # Same marker contract — a client-policy agent that could not run
            # must not let the batch look cleanly reviewed.
            return {
                "source": agent_config.name,
                "findings": [],
                "first_party": True,
                "skipped": "no_direct_key",
            }
        proxy = None
        model = agent_config.client_policy_model or model
        logger.info(
            f"{agent_config.name}: CLIENT_AI_POLICY active — dispatching {model} (Anthropic-direct)"
        )
    else:
        api_key = api_key or get_api_key()
    if not api_key:
        return {"source": agent_config.name, "findings": [],
                "first_party": direct_required, "skipped": "no_credential"}
    base_url = proxy or "https://api.anthropic.com"
    if proxy and model in {"claude-haiku-4-5-20251001", "claude-sonnet-4-6"}:
        model = {"claude-haiku-4-5-20251001": "haiku", "claude-sonnet-4-6": "sonnet"}[model]
    if not proxy:
        model = DIRECT_MODEL_MAP.get(model, model)
    api_url = f"{base_url}/v1/messages"

    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if proxy:
        headers["Authorization"] = f"Bearer {api_key.removeprefix('Bearer ')}"
    else:
        headers["x-api-key"] = api_key

    # Reasoning models behind the LiteLLM proxy share max_tokens with their
    # `thinking` blocks and ignore thinking.budget_tokens/disabled (verified
    # 2026-10-08): a long chain-of-thought can exhaust the answer budget and
    # return stop_reason=max_tokens with an empty text block. The 2400-token
    # ceiling is advisor-window sizing and stays — a starved answer gets one
    # retry with headroom on top, where thinking is already proven long so
    # the visible text still fits the review window.
    budgets = (2400, 2400 + _PROXY_THINKING_HEADROOM) if proxy else (2400,)

    # Transient upstream errors (rate limits, 5xx, network resets) retry with
    # exponential backoff; a terminal failure takes the skip-marker path so the
    # judge still sees the agent as assessed-but-unavailable.
    content = ""
    parsed_findings = None
    resp_data = None
    for budget in budgets:
        payload = {
            "model": model,
            # 2400 tokens ≈ 9.6K chars keeps the visible text inside the
            # advisor guardrail's 10K-char review window (longer responses
            # fail closed with 503). Reasoning tokens (thinking blocks) share
            # this budget but are excluded from the reviewed text, so real
            # findings output sits well under the window.
            "max_tokens": budget,
            "system": [
                {
                    "type": "text",
                    "text": agent_config.system_prompt + (
                        "\n\nReturn the findings payload in the API response; the caller writes the file. "
                        "Return one JSON object with source and findings (a list of finding objects). "
                        "Do not return a file-write acknowledgement or explanatory prose. "
                        "If evidence needed for the assigned checks is unavailable, include "
                        '\"skipped\": \"missing_context\" rather than claiming a clean review.'
                    ),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": f"{_api_instructions(agent_config.instructions)}\n\n{user_message}",
                }
            ],
        }
        request_bytes = json.dumps(payload).encode("utf-8")
        if len(request_bytes) > _MAX_REQUEST_BYTES:
            logger.error("%s: complete serialized review request exceeds limit", agent_config.name)
            return _incomplete(agent_config, "input_limit")
        resp_data = None
        for attempt in range(MAX_RETRIES):
            try:
                req = Request(
                    api_url,
                    data=request_bytes,
                    headers=headers,
                    method="POST",
                )
                with _urlopen_https(req, timeout=TIMEOUT_SECS, host=urlparse(api_url).hostname or "") as response:
                    resp_data = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")[:2000]
                # The fail-closed sonnet-advisor guardrail rejects with a 400
                # whose body instructs a retry — that one rejection is
                # transient (a provider blip behind the advisor); every other
                # 400 is terminal.
                retriable = e.code in RETRIABLE_STATUS or (
                    e.code == 400 and "advisor rejected" in err_body.lower()
                )
                if retriable and attempt < MAX_RETRIES - 1:
                    sleep_secs = 2 ** attempt
                    logger.warning(
                        f"Agent {agent_config.name} HTTP {e.code} on attempt "
                        f"{attempt + 1}/{MAX_RETRIES}; retrying in {sleep_secs}s"
                    )
                    time.sleep(sleep_secs)
                    continue
                logger.error(f"Agent {agent_config.name} API error: {e}")
                break
            # OSError covers URLError plus the response-phase failures urlopen
            # does not convert (RemoteDisconnected, ConnectionResetError,
            # TimeoutError).
            except OSError as e:
                if attempt < MAX_RETRIES - 1:
                    sleep_secs = 2 ** attempt
                    logger.warning(
                        f"Agent {agent_config.name} network error on attempt "
                        f"{attempt + 1}/{MAX_RETRIES}; retrying in {sleep_secs}s "
                        f"(error: {e})"
                    )
                    time.sleep(sleep_secs)
                    continue
                logger.error(f"Agent {agent_config.name} API error: {e}")
                break
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"Agent {agent_config.name} response JSON error: {e}")
                break
        if resp_data is None:
            if direct_required:
                return {
                    "source": agent_config.name,
                    "findings": [],
                    "first_party": True,
                    "skipped": "api_error",
                }
            return None

        # Reasoning models (DeepSeek V4 via the proxy) prepend a `thinking`
        # block to the content array — select text blocks, not content[0].
        if not isinstance(resp_data, dict):
            return _incomplete(agent_config, "invalid_response")
        blocks = resp_data.get("content") or []
        if not isinstance(blocks, list) or any(
            not isinstance(b, dict) or (b.get("type", "text") == "text"
                                       and not isinstance(b.get("text", ""), str))
            for b in blocks
        ):
            return _incomplete(agent_config, "invalid_response")
        content = "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type", "text") == "text"
        )
        if resp_data.get("stop_reason") == "max_tokens":
            # Reuse the already-bounded proxy headroom attempt. A syntactically
            # valid prefix can still omit findings, so it is never a full result.
            logger.warning("%s: output limit reached at max_tokens=%s", agent_config.name, budget)
            if budget == budgets[-1]:
                return _incomplete(agent_config, "output_limit")
            continue
        if content:
            try:
                parsed_findings = _parse_findings(content)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.error("%s: invalid findings response (%s)", agent_config.name, type(exc).__name__)
                return _incomplete(agent_config, "invalid_response")
            break
        logger.warning(
            f"Agent {agent_config.name} empty text at max_tokens={budget} "
            f"(stop_reason={resp_data.get('stop_reason')})"
        )

    if not content:
        logger.error(f"Agent {agent_config.name} empty response")
        if direct_required:
            return {
                "source": agent_config.name,
                "findings": [],
                "first_party": True,
                "skipped": "api_error",
            }
        return None

    if parsed_findings is None:
        return _incomplete(agent_config, "invalid_response")
    return {"source": agent_config.name,
            "first_party": direct_required and bool(parsed_findings.get("skipped") or parsed_findings["findings"]),
            **parsed_findings}


def run_broad_agents(
    manifest_path: Path,
    diff_path: Path,
    output_dir: Path,
    agents_dir: Path = Path(".claude/agents"),
) -> int:
    """Main entry point."""
    # Load manifest
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Manifest error: {e}")
        return 1

    depth = manifest.get("depth", "quick")
    if depth != "broad":
        logger.info(f"[broad-agents] depth={depth}, skipping broad agents")
        return 0

    invoke_list = manifest.get("invoke_agents", [])
    if not invoke_list:
        logger.info("[broad-agents] nothing to run")
        return 0

    # Load diff
    if not diff_path.exists():
        logger.error(f"Diff not found: {diff_path}")
        return 1

    diff = diff_path.read_text(encoding="utf-8")
    exceeds_owner_limit = MAX_DIFF_CHARS is not None and len(diff) > MAX_DIFF_CHARS

    changed_files = get_changed_files()
    user_message = build_user_message(diff, changed_files)

    # Each role admits its route before selecting any transport credential.
    api_key = ""

    # Load agent configs
    output_dir.mkdir(parents=True, exist_ok=True)
    agents_to_run = []
    for agent_name in invoke_list:
        if agent_name not in BROAD_AGENTS:
            logger.error("Unknown requested broad agent; assessment unavailable")
            return 1

        agent_path = agents_dir / f"{agent_name}.md"
        try:
            config = parse_agent_file(agent_path)
        except (OSError, UnicodeError) as exc:
            logger.error("Cannot load agent %s (%s)", agent_name, type(exc).__name__)
            config = None
        if not config:
            # Unknown capability on failed load is conservative missing coverage,
            # never permission to run an ordinary/proxy replacement.
            (output_dir / f"{agent_name}.json").write_text(json.dumps(_policy_module.bind_review_candidate({
                "source": agent_name, "findings": [], "skipped": "load_error",
                "first_party": True}), indent=2), encoding="utf-8")
            continue

        agents_to_run.append(config)

    if not agents_to_run:
        logger.info("[broad-agents] no valid agents to run")
        return 0

    # Run agents in parallel
    results = {}

    if exceeds_owner_limit:
        logger.error("Complete diff exceeds explicit owner limit; no agent dispatched")
        for agent in agents_to_run:
            (output_dir / f"{agent.name}.json").write_text(
                json.dumps(_policy_module.bind_review_candidate(_incomplete(agent, "input_limit")), indent=2), encoding="utf-8")
        return 0  # Durable incomplete markers, never a completed review.

    runnable = agents_to_run

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                invoke_agent, agent, api_key, user_message
            ): agent
            for agent in runnable
        }

        for future in as_completed(futures):
            agent = futures[future]
            agent_name = agent.name
            try:
                findings = future.result()
            except Exception as e:
                logger.error(f"Agent {agent_name} execution error: {e}")
                findings = None
            if not findings:
                # No output at all — write the marker so the judge counts the
                # missing coverage instead of posting a false-clean verdict.
                # first_party is preserved: a failed security check still owes
                # the batch its fail-closed direct adjudication.
                findings = {
                    "source": agent_name,
                    "findings": [],
                    "skipped": "api_error",
                    "first_party": bool(agent.first_party or os.environ.get("CLIENT_AI_POLICY")),
                }
            results[agent_name] = findings
            out_file = output_dir / f"{agent_name}.json"
            out_file.write_text(
                json.dumps(_policy_module.bind_review_candidate(findings), indent=2),
                encoding="utf-8",
            )
            logger.info(f"Wrote {agent_name} findings to {out_file}")

    if results:
        logger.info(f"[broad-agents] completed {len(results)}/{len(agents_to_run)} agents (incl. skipped markers)")
        return 0
    else:
        logger.warning("[broad-agents] no findings generated")
        return 1


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run broad agents for PR assessment."
    )
    parser.add_argument(
        "--agents",
        default="",
        help="Comma-separated agent names (e.g. systems-consistency,security-deep-dive)",
    )
    parser.add_argument(
        "--diff",
        type=Path,
        default=Path(os.getenv("DIFF_FILE", "/tmp/pr.diff")),
        help="Path to PR diff file",
    )
    parser.add_argument(
        "--candidates-dir",
        type=Path,
        default=Path(".ai/candidates"),
        help="Output directory for findings JSON",
    )

    args = parser.parse_args()

    invoke_agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if not invoke_agents:
        logger.info("[broad-agents] nothing to run")
        return 0

    # Build a minimal manifest so run_broad_agents() can determine depth/agents
    manifest = {"invoke_agents": invoke_agents, "depth": "broad"}
    args.candidates_dir.mkdir(parents=True, exist_ok=True)
    tmp_manifest = args.candidates_dir / "_agents-manifest.json"
    tmp_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    try:
        return run_broad_agents(
            tmp_manifest, args.diff, args.candidates_dir, Path(".claude/agents"),
        )
    finally:
        tmp_manifest.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
