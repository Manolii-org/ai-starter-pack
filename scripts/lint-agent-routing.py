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


# ── RULE 7: subagent dispatch must pin an explicit model= ─────────────────────
# Claude Code v2.1.198 changed the built-in Explore agent (and every built-in
# subagent that previously defaulted to haiku) to INHERIT the main-session model
# — capped at opus. Any `Agent(subagent_type="<name>", ...)` call site that
# omits `model=` therefore silently escalates from OSS routing to Opus pricing.
# This scanner is intentionally shallow: for each `subagent_type="<name>"` line,
# it checks a ±6-line window for both `Agent(` and `model=` (or `model:`).
# Escape a specific line with the trailing comment
# `# lint-agent-routing:allow-missing-model`.
_SUBAGENT_RE = re.compile(r"""subagent_type\s*[=:]\s*["'`]([A-Za-z0-9_.\-/]+)["'`]""")
_MODEL_RE = re.compile(r"""\bmodel\s*[=:]""")
_AGENT_CALL_RE = re.compile(r"""\bAgent\s*\(""")
_ALLOW_MARKER = "lint-agent-routing:allow-missing-model"
_DISPATCH_SCAN_EXTS_SOURCE = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".sh"}
_DISPATCH_SCAN_EXTS_DOCS = {".md"}
_DISPATCH_SCAN_EXCLUDES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", "coverage",
}


def lint_subagent_dispatches_in_tree(root, include_docs=False):
    """Return list of violations: source dispatches missing explicit model=."""
    violations = []
    root = Path(root).resolve()
    exts = set(_DISPATCH_SCAN_EXTS_SOURCE)
    if include_docs:
        exts |= _DISPATCH_SCAN_EXTS_DOCS
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in exts:
            continue
        rel = path.relative_to(root)
        if any(part in _DISPATCH_SCAN_EXCLUDES for part in rel.parts):
            continue
        if rel.name == "lint-agent-routing.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        if "subagent_type" not in text:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = _SUBAGENT_RE.search(line)
            if not m:
                continue
            if _ALLOW_MARKER in line:
                continue
            window = "\n".join(lines[max(0, i - 6): i + 7])
            if not _AGENT_CALL_RE.search(window):
                continue
            if _MODEL_RE.search(window):
                continue
            violations.append({
                "file": str(rel),
                "line": i + 1,
                "subagent": m.group(1),
                "snippet": line.strip()[:160],
            })
    return violations


def format_subagent_dispatch_violation(v):
    return (
        f"  [RULE7_MISSING_MODEL] {v['file']}:{v['line']}\n"
        f"    subagent_type=\"{v['subagent']}\" has no explicit model= within ±6 lines\n"
        f"    snippet: {v['snippet']}"
    )


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
    parser.add_argument(
        "--check-subagent-dispatches",
        metavar="DIR",
        nargs="?",
        const=".",
        default=None,
        help="Scan DIR (default: repo root) for Agent(subagent_type=...) call sites "
             "missing an explicit model= argument. Rule 7 — see v2.1.198 Explore regression.",
    )
    parser.add_argument(
        "--no-check-subagent-dispatches",
        action="store_true",
        help="Skip the Rule 7 subagent-dispatch scan even when it would run by default.",
    )
    parser.add_argument(
        "--include-docs",
        action="store_true",
        help="Extend the Rule 7 scan to .md files (prose examples).",
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

    # ── RULE 7: subagent-dispatch scan (default-on, opt-out via flag) ─────────
    dispatch_violations = []
    scan_root = None
    if not args.no_check_subagent_dispatches and not args.files:
        scan_root = Path(args.check_subagent_dispatches or ".")
    elif args.check_subagent_dispatches and not args.no_check_subagent_dispatches:
        scan_root = Path(args.check_subagent_dispatches)
    if scan_root is not None:
        dispatch_violations = lint_subagent_dispatches_in_tree(
            scan_root, include_docs=args.include_docs
        )
        if dispatch_violations:
            print(
                f"\n[lint-agent-routing] SUBAGENT DISPATCH VIOLATIONS — "
                f"{len(dispatch_violations)} issue(s)\n"
            )
            for v in dispatch_violations:
                print(format_subagent_dispatch_violation(v))
                print()
            print(
                "RULE 7 — Subagent dispatches must pin an explicit model=:\n"
                "  Claude Code v2.1.198 changed built-in subagents (Explore, generate,\n"
                "  review-internal, work-critic, quick-critic, ...) to inherit the\n"
                "  main-session model instead of defaulting to haiku. Any dispatch that\n"
                "  omits model= now silently escalates to Opus pricing.\n"
                "  Fix: add explicit model=\"haiku\" (or the intended tier per\n"
                "  .claude/model-routing.json) to the Agent(...) call. To exempt a\n"
                "  specific line, append the trailing comment\n"
                "  # lint-agent-routing:allow-missing-model.\n"
            )
            return 1

    checked = len(agent_files)
    print(f"[lint-agent-routing] OK — {checked} agent(s) checked, 0 violations")
    if scan_root is not None:
        print(f"[lint-agent-routing] Rule 7 OK — subagent dispatches under {scan_root} all pin model=")
    return 0


if __name__ == "__main__":
    sys.exit(main())
