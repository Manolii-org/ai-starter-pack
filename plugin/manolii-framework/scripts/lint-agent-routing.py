#!/usr/bin/env python3
"""Lint agent YAML frontmatter for model-routing data-classification violations.

RULE: An agent with data_sensitivity 'restricted' or 'anthropic_only' MUST NOT
declare a model that routes through a non-Anthropic provider (OSS tier alias or
proxy-intercepted model ID).

The LiteLLM proxy intercepts certain model IDs (including claude-haiku-4-5-20251001
and its 'haiku' short alias) and silently routes them to OSS providers. An agent
with restricted or anthropic_only data classification that declares such a model
would leak client/PII data to a third-party provider without any runtime warning.

SOURCE OF TRUTH: .claude/model-routing.json
  - tier_aliases.<alias>.data_sensitivity_max   -- OSS tier names
  - proxy_intercepted_models.<id>.data_sensitivity_max -- Anthropic IDs the proxy remaps
  - data_classification -- defines allowed_tiers per sensitivity level

Usage:
    python3 scripts/lint-agent-routing.py              # lint all .claude/agents/*.md
    python3 scripts/lint-agent-routing.py file1 file2  # lint specific files
    python3 scripts/lint-agent-routing.py --list-oss   # show all OSS-routed identifiers
    python3 scripts/lint-agent-routing.py -v           # include unknown-model warnings

Exit codes:
    0 -- all agents pass
    1 -- one or more violations found
    2 -- configuration error (routing config missing or unreadable)
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ROUTING_CONFIG_PATH = REPO_ROOT / ".claude" / "model-routing.json"
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

# Sensitivity levels that require Anthropic-only routing.
REQUIRES_ANTHROPIC_ONLY = {"restricted", "anthropic_only"}


def build_anthropic_native_set(config):
    """Derive Anthropic-native model identifiers from routing config.

    Collects tier names and litellm_aliases where provider == 'anthropic',
    then adds Claude Code short aliases from platforms.claude_code.available_models,
    excluding any that are intercepted by the proxy.
    """
    native: set[str] = set()
    intercepted = set(config.get("proxy_intercepted_models", {}).keys())
    for tier_name, tier_def in config.get("tier_definitions", {}).items():
        if tier_name.startswith("$"):
            continue  # skip $comment and other metadata keys
        if not isinstance(tier_def, dict):
            continue
        if tier_def.get("provider") == "anthropic":
            native.add(tier_name)
            litellm_alias = tier_def.get("litellm_alias")
            if litellm_alias:
                native.add(litellm_alias)
    # Claude Code Agent tool short aliases (e.g. "opus", "sonnet", "haiku")
    claude_code = config.get("platforms", {}).get("claude_code", {})
    for alias in claude_code.get("available_models", []):
        if alias not in intercepted:
            native.add(alias)
    return native


def load_routing_config():
    if not ROUTING_CONFIG_PATH.exists():
        print(f"ERROR: {ROUTING_CONFIG_PATH} not found", file=sys.stderr)
        sys.exit(2)
    try:
        with open(ROUTING_CONFIG_PATH) as f:
            return json.load(f)
    except OSError as e:
        print(f"ERROR: failed to read {ROUTING_CONFIG_PATH}: {e}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse {ROUTING_CONFIG_PATH}: {e}", file=sys.stderr)
        sys.exit(2)


def build_oss_routed_set(config):
    """Return {model_id: metadata} for every non-Anthropic-routed model identifier.

    Sources:
      1. tier_aliases -- OSS tier names (tier-1-fast, tier-2-agentic, …)
      2. proxy_intercepted_models -- Anthropic model IDs the LiteLLM proxy remaps
    """
    oss = {}

    for alias, meta in config.get("tier_aliases", {}).items():
        if alias.startswith("$"):
            continue
        resolves_to = meta.get("resolves_to", "")
        # Exclude entries that resolve to Anthropic (tier-0-opus / tier-0-sonnet)
        if resolves_to.startswith("anthropic:"):
            continue
        oss[alias] = {
            "data_sensitivity_max": meta.get("data_sensitivity_max", "internal"),
            "resolves_to": resolves_to,
            "label": meta.get("label", alias),
            "source": "tier_aliases",
            "warning": meta.get("warning", ""),
        }

    for model_id, meta in config.get("proxy_intercepted_models", {}).items():
        if model_id.startswith("$"):
            continue
        oss[model_id] = {
            "data_sensitivity_max": meta.get("data_sensitivity_max", "internal"),
            "resolves_to": meta.get("resolves_to", "unknown"),
            "label": meta.get("label", model_id),
            "source": "proxy_intercepted_models",
            "warning": meta.get("warning", ""),
        }

    return oss


def parse_frontmatter(content):
    """Extract simple key: value pairs from YAML frontmatter."""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    fm_text = content[3:end].strip()

    fields = {}
    for line in fm_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        m = re.match(r"^([\w][\w-]*)\s*:\s*(.+)$", line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            val = re.sub(r"\s+#.*$", "", val).strip()
            val = val.strip("\"'")
            fields[key] = val
    return fields


def lint_agent_file(path, oss_routed, anthropic_native, verbose=False):
    """Check one agent file. Returns a list of violation dicts (empty = pass)."""
    path = Path(path)  # Normalise to Path for consistent .name / .stem access
    violations = []

    try:
        content = path.read_text()
    except OSError as e:
        return [{"file": str(path), "agent": str(path), "error": str(e)}]

    fm = parse_frontmatter(content)
    if not fm:
        return []

    model = fm.get("model", "").strip()
    data_sensitivity = fm.get("data_sensitivity", "").strip()

    if not model or not data_sensitivity:
        return []

    if data_sensitivity not in REQUIRES_ANTHROPIC_ONLY:
        return []

    if model in anthropic_native:
        return []

    if model in oss_routed:
        meta = oss_routed[model]
        violations.append({
            "file": str(path),
            "agent": fm.get("name", path.stem),
            "model": model,
            "data_sensitivity": data_sensitivity,
            "resolves_to": meta["resolves_to"],
            "data_sensitivity_max": meta["data_sensitivity_max"],
            "source": meta["source"],
            "warning": meta.get("warning", ""),
        })
    else:
        # Unknown model + restricted data sensitivity → fail closed.
        # We cannot prove Anthropic routing for an unregistered identifier.
        violations.append({
            "file": str(path),
            "agent": fm.get("name", path.stem),
            "model": model,
            "data_sensitivity": data_sensitivity,
            "resolves_to": "unregistered",
            "data_sensitivity_max": "unknown",
            "source": "unregistered_model",
            "warning": (
                "Model is not registered in tier_aliases or proxy_intercepted_models; "
                "cannot prove Anthropic routing."
            ),
        })
        if verbose:
            print(
                f"  WARN  {path.name}: model '{model}' is not registered in "
                f"tier_aliases or proxy_intercepted_models — treating as violation "
                f"(fail-closed for restricted/anthropic_only agents)"
            )

    return violations


def format_violation(v):
    # I/O error payloads (from file read failures) have a different shape.
    if "error" in v:
        return "\n".join([
            f"  FILE:        {v['file']}",
            f"  AGENT:       {v['agent']}",
            f"  ERROR:       {v['error']}",
            "  FIX:         Ensure the file exists and is readable.",
        ])

    lines = [
        f"  FILE:        {v['file']}",
        f"  AGENT:       {v['agent']}",
        f"  MODEL:       {v['model']}  →  {v['resolves_to']}",
        f"  SENSITIVITY: declared={v['data_sensitivity']}  tier_max={v['data_sensitivity_max']}",
    ]
    if v.get("warning"):
        lines.append(f"  NOTE:        {v['warning']}")
    lines.append(
        "  FIX:         Set model to an Anthropic-native identifier defined in "
        ".claude/model-routing.json (tier_definitions where provider == 'anthropic', "
        "or a claude_code platform alias not listed in proxy_intercepted_models)."
    )
    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Agent .md files to lint (default: all .claude/agents/*.md)",
    )
    parser.add_argument(
        "--list-oss",
        action="store_true",
        help="Print all OSS-routed model identifiers and exit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show warnings for unknown model identifiers",
    )
    args = parser.parse_args()

    config = load_routing_config()
    oss_routed = build_oss_routed_set(config)
    anthropic_native = build_anthropic_native_set(config)

    if args.list_oss:
        print("OSS-routed model identifiers (NOT safe for restricted/anthropic_only data):\n")
        for model_id, meta in sorted(oss_routed.items()):
            print(f"  {model_id:<38}  →  {meta['resolves_to']}")
            print(f"  {'':38}     source: {meta['source']},  max: {meta['data_sensitivity_max']}")
        return 0

    if args.files:
        agent_files = [Path(f) for f in args.files]
    else:
        agent_files = sorted(AGENTS_DIR.glob("*.md"))

    all_violations = []
    for path in agent_files:
        vios = lint_agent_file(path, oss_routed, anthropic_native, verbose=args.verbose)
        all_violations.extend(vios)

    if all_violations:
        has_io_errors = any("error" in v for v in all_violations)
        print(f"\n[lint-agent-routing] FAILED — {len(all_violations)} issue(s)\n")
        for v in all_violations:
            print(format_violation(v))
            print()
        print("WHY THIS MATTERS:")
        print(
            "  Agents with data_sensitivity 'restricted' or 'anthropic_only' handle\n"
            "  client code, PII, or other Anthropic-only data.\n"
            "  OSS tier aliases and proxy-intercepted model IDs route through third-party\n"
            "  providers (Fireworks, Together AI, Groq) — data would leave Anthropic\n"
            "  infrastructure silently, violating the data classification policy.\n"
        )
        print(
            "  Source of truth: .claude/model-routing.json\n"
            "  Sections:  tier_aliases  /  proxy_intercepted_models  /  data_classification"
        )
        return 2 if has_io_errors else 1

    checked = len(agent_files)
    print(f"[lint-agent-routing] OK — {checked} agent(s) checked, 0 violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
