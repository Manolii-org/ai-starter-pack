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

MAX_DIFF_CHARS = int(os.environ.get("BROAD_AGENTS_MAX_DIFF_CHARS", "40000"))
# 240s covers a primary call plus its fail-closed advisor round trip on the
# proxy — 120s was too tight for advisor-backed security-deep-dive-sized
# payloads (buromaster#25) and aborts mid-advisor into api_error markers.
TIMEOUT_SECS = 240
MAX_RETRIES = 4
RETRIABLE_STATUS = {429, 500, 502, 503, 504}


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
        key = os.getenv("ANTHROPIC_API_KEY")
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
    # Route through the LiteLLM proxy when configured (Bearer auth); otherwise
    # direct Anthropic (x-api-key). The retired anthropic_only tier has no
    # callers left — its agents were remapped to restricted_us_oss_ok.
    proxy = _proxy_base()
    # True once this call is bound to the Anthropic-direct plane — a runtime
    # failure must then degrade to the skipped first-party marker so the judge
    # fails closed instead of adjudicating without the required direct leg.
    direct_required = False
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
    base_url = proxy or "https://api.anthropic.com"
    if proxy and model in {"claude-haiku-4-5-20251001", "claude-sonnet-4-6"}:
        model = {"claude-haiku-4-5-20251001": "haiku", "claude-sonnet-4-6": "sonnet"}[model]
    if not proxy:
        model = DIRECT_MODEL_MAP.get(model, model)
    api_url = f"{base_url}/v1/messages"

    payload = {
        "model": model,
        # 2400 tokens ≈ 9.6K chars keeps the visible text inside the advisor
        # guardrail's 10K-char review window (longer responses fail closed with
        # 503). Reasoning tokens (thinking blocks) share this budget but are
        # excluded from the reviewed text, so real findings output sits well
        # under the window.
        "max_tokens": 2400,
        "system": [
            {
                "type": "text",
                "text": agent_config.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": f"{agent_config.instructions}\n\n{user_message}",
            }
        ],
    }

    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if proxy:
        headers["Authorization"] = f"Bearer {api_key.removeprefix('Bearer ')}"
    else:
        headers["x-api-key"] = api_key

    # Transient upstream errors (rate limits, 5xx, network resets) retry with
    # exponential backoff; a terminal failure takes the skip-marker path so the
    # judge still sees the agent as assessed-but-unavailable.
    resp_data = None
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(
                api_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with _urlopen_https(req, timeout=TIMEOUT_SECS, host=urlparse(api_url).hostname or "") as response:
                resp_data = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:2000]
            # The fail-closed sonnet-advisor guardrail rejects with a 400 whose
            # body instructs a retry — that one rejection is transient (a
            # provider blip behind the advisor); every other 400 is terminal.
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
        # OSError covers URLError plus the response-phase failures urlopen does
        # not convert (RemoteDisconnected, ConnectionResetError, TimeoutError).
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

    try:
        # Reasoning models (DeepSeek V4 via the proxy) prepend a `thinking`
        # block to the content array — select text blocks, not content[0].
        blocks = resp_data.get("content") or []
        content = "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type", "text") == "text"
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

        # Strip markdown fences
        content = content.strip()
        content = re.sub(r"^```(?:json)?\n?", "", content)
        content = re.sub(r"\n?```$", "", content)

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # OSS models sometimes wrap the JSON in prose — extract the first
            # top-level array or object instead of failing the agent outright.
            m = re.search(r"\[[\s\S]*\]|\{[\s\S]*\}", content)
            if not m:
                raise
            parsed = json.loads(m.group(0))

        # Normalise to {source, findings:[...]} contract expected by run-judge.py
        if isinstance(parsed, dict) and "findings" in parsed and not isinstance(parsed["findings"], list):
            # A non-list findings value (e.g. an object) would silently
            # normalise to an empty result — a false clean for the check.
            # Raise so the executor writes an api_error marker that preserves
            # this agent's first-party status.
            raise ValueError(f"Agent {agent_config.name} returned non-list findings: {type(parsed['findings'])}")
        if isinstance(parsed, list):
            raw_findings = parsed
        elif isinstance(parsed, dict) and "findings" in parsed:
            raw_findings = parsed["findings"]
        elif isinstance(parsed, dict) and any(k in parsed for k in ("file", "message", "severity")):
            raw_findings = [parsed]
        else:
            raw_findings = parsed.get("findings", []) if isinstance(parsed, dict) else []

        # Ensure each finding has required keys
        _DEFAULTS = {"file": "", "line": None, "severity": "WARNING", "message": "", "fix": ""}
        normalised = [{**_DEFAULTS, **f} for f in raw_findings if isinstance(f, dict)]

        return {
            "source": agent_config.name,
            # An empty result must not mark the file first-party: the judge
            # would demand a direct key for a batch with nothing to adjudicate.
            # direct_required covers BOTH direct lanes — static first_party
            # frontmatter and CLIENT_AI_POLICY — so policy-driven findings keep
            # the judge off the OSS proxy even when the flag never reaches the
            # judge job's env.
            "first_party": direct_required and bool(normalised),
            "findings": normalised,
        }
    except json.JSONDecodeError as e:
        logger.error(f"Agent {agent_config.name} JSON parse error: {e}")
        if direct_required:
            return {
                "source": agent_config.name,
                "findings": [],
                "first_party": True,
                "skipped": "api_error",
            }
        return None


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
    if len(diff) > MAX_DIFF_CHARS:
        logger.warning(
            f"Diff truncated from {len(diff)} to {MAX_DIFF_CHARS} chars"
        )
        diff = diff[: MAX_DIFF_CHARS] + "\n... (truncated)"

    changed_files = get_changed_files()
    user_message = build_user_message(diff, changed_files)

    # Get API key. A direct-only install (ANTHROPIC_DIRECT_API_KEY without a
    # shared transport credential) must still run: every agent dispatches
    # Anthropic-direct through it (the proxy is absent by definition), so the
    # direct key is also the shared transport credential here. When a proxy IS
    # configured but has no credential, the direct key must NOT stand in — a
    # non-direct agent would send `Bearer <anthropic key>` to the proxy host,
    # leaking a first-party credential outside the Anthropic boundary. That
    # state proceeds with an empty shared key instead: first_party /
    # CLIENT_AI_POLICY agents re-resolve their own direct credential inside
    # invoke_agent, and the rest skip on their empty key.
    api_key = get_api_key() or (
        os.getenv("ANTHROPIC_DIRECT_API_KEY", "") if _proxy_base() is None else ""
    )
    if not api_key and not os.getenv("ANTHROPIC_DIRECT_API_KEY"):
        # No credential at all: write one marker per requested agent so the
        # judge records the missing coverage instead of a false-clean verdict.
        logger.warning(
            "[broad-agents] no API key — writing skip markers for all requested agents"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        for agent_name in invoke_list:
            marker = {
                "source": agent_name,
                "findings": [],
                "skipped": "no_credential",
            }
            (output_dir / f"{agent_name}.json").write_text(
                json.dumps(marker, indent=2), encoding="utf-8"
            )
        return 0

    # Load agent configs
    agents_to_run = []
    for agent_name in invoke_list:
        if agent_name not in BROAD_AGENTS:
            logger.warning(f"Skipping unknown agent: {agent_name}")
            continue

        agent_path = agents_dir / f"{agent_name}.md"
        if not agent_path.exists():
            logger.error(f"Agent file not found: {agent_path}")
            continue

        config = parse_agent_file(agent_path)
        if not config:
            continue

        agents_to_run.append(config)

    if not agents_to_run:
        logger.info("[broad-agents] no valid agents to run")
        return 0

    # Run agents in parallel
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    # Proxy configured but no proxy credential: non-direct agents would
    # dispatch with `Bearer ` and fail silently (no file → judge sees clean).
    # Skip them up front and write the same durable marker the judge's
    # coverage accounting understands.
    runnable = []
    for agent in agents_to_run:
        if (
            not api_key
            and not agent.first_party
            and not os.environ.get("CLIENT_AI_POLICY")
        ):
            marker = {
                "source": agent.name,
                "findings": [],
                "first_party": False,
                "skipped": "no_proxy_credential",
            }
            (output_dir / f"{agent.name}.json").write_text(
                json.dumps(marker, indent=2), encoding="utf-8"
            )
            results[agent.name] = marker
            logger.warning(
                f"{agent.name}: no shared credential for the configured proxy — skipping (marker written)"
            )
            continue
        runnable.append(agent)

    if not runnable:
        logger.warning("[broad-agents] every agent skipped — no runnable credential")
        return 0

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
                json.dumps(findings, indent=2),
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

    return run_broad_agents(
        tmp_manifest,
        args.diff,
        args.candidates_dir,
        Path(".claude/agents"),
    )


if __name__ == "__main__":
    sys.exit(main())
