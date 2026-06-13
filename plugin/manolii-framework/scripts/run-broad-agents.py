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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="[broad-agents] %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
}

BROAD_AGENTS = [
    "systems-consistency",
    "architecture-impact",
    "security-deep-dive",
]

MAX_DIFF_CHARS = 8000
TIMEOUT_SECS = 120


@dataclass
class AgentConfig:
    """Agent frontmatter config."""
    name: str
    model: str
    data_sensitivity: str
    system_prompt: str
    instructions: str


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
    )


def get_api_key() -> Optional[str]:
    """Get API key from env."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("ANTHROPIC_API_KEY not set; skipping agent invocations")
        return None
    return key


def get_changed_files() -> list[str]:
    """Parse changed files from CHANGED_FILES env (newline-separated)."""
    changed = os.getenv("CHANGED_FILES", "").strip()
    if not changed:
        return []
    return [f.strip() for f in changed.split("\n") if f.strip()]


def build_user_message(diff: str, changed_files: list[str]) -> str:
    """Build user message with untrusted diff and changed files."""
    msg = f"<untrusted_diff>\n{diff}\n</untrusted_diff>"

    if changed_files:
        files_str = "\n".join(f"  - {f}" for f in changed_files)
        msg += f"\n\nChanged files:\n{files_str}"

    return msg


def invoke_agent(
    agent_config: AgentConfig,
    api_key: str,
    user_message: str,
) -> Optional[dict[str, Any]]:
    """Invoke agent via Anthropic API, return parsed findings."""
    model = MODEL_ALIASES.get(agent_config.model, agent_config.model)

    # For restricted agents, ensure Anthropic direct (never proxy)
    if agent_config.data_sensitivity == "restricted":
        if model not in ["claude-sonnet-4-6", "claude-opus-4-1"]:
            logger.warning(
                f"Agent {agent_config.name} has restricted sensitivity but model={model} "
                f"may route via proxy; forcing claude-sonnet-4-6"
            )
            model = "claude-sonnet-4-6"

    payload = {
        "model": model,
        "max_tokens": 4096,
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
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        req = Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(req, timeout=TIMEOUT_SECS) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
    except (URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.error(f"Agent {agent_config.name} API error: {e}")
        return None

    try:
        content = resp_data.get("content", [{}])[0].get("text", "")
        if not content:
            logger.error(f"Agent {agent_config.name} empty response")
            return None

        # Strip markdown fences
        content = re.sub(r"^```json\n?", "", content)
        content = re.sub(r"\n?```$", "", content)

        parsed = json.loads(content)

        # Normalise to {source, findings:[...]} contract expected by run-judge.py
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

        return {"source": agent_config.name, "findings": normalised}
    except json.JSONDecodeError as e:
        logger.error(f"Agent {agent_config.name} JSON parse error: {e}")
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

    # Get API key
    api_key = get_api_key()
    if not api_key:
        logger.info("[broad-agents] no API key, exiting")
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

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                invoke_agent, agent, api_key, user_message
            ): agent.name
            for agent in agents_to_run
        }

        for future in as_completed(futures):
            agent_name = futures[future]
            try:
                findings = future.result()
                if findings:
                    results[agent_name] = findings
                    out_file = output_dir / f"{agent_name}.json"
                    out_file.write_text(
                        json.dumps(findings, indent=2),
                        encoding="utf-8",
                    )
                    logger.info(f"Wrote {agent_name} findings to {out_file}")
            except Exception as e:
                logger.error(f"Agent {agent_name} execution error: {e}")

    if results:
        logger.info(f"[broad-agents] completed {len(results)}/{len(agents_to_run)} agents")
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
