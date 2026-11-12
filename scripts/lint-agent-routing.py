#!/usr/bin/env python3
"""Lint agent YAML frontmatter for model-routing data-classification violations.

RULE 1 — Data classification (restricted / restricted_us_oss_ok):
  An agent with data_sensitivity 'restricted' is a governance no-AI label and
  MUST NOT declare a runnable model at all. An agent with 'restricted_us_oss_ok'
  must declare a model/alias whose data_sensitivity_max covers that tier
  (an owner-qualified governed route or explicitly cleared native host).

The LiteLLM proxy intercepts certain model IDs (including claude-haiku-4-5-20251001
and its 'haiku' short alias) and silently routes them to OSS providers. An agent
with protected data classification must use the intercepted route's actual
declared clearance; a vendor-looking name does not establish permission.

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
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
# When bundled into a Claude Code plugin, the routing config lives at
# `data/model-routing.json`, not `.claude/model-routing.json`. Search both
# so `/drift-check` (which runs `${CLAUDE_PLUGIN_ROOT}/scripts/lint-agent-routing.py`)
# works from the plugin bundle without exiting 2 on a "missing" config.
# Reference: Codex review, PR #28 (ai-starter-pack), 2026-07-17.
_ROUTING_CONFIG_CANDIDATES = (
    REPO_ROOT / ".claude" / "model-routing.json",
    REPO_ROOT / "data" / "model-routing.json",
)
ROUTING_CONFIG_PATH = next(
    (p for p in _ROUTING_CONFIG_CANDIDATES if p.exists()),
    _ROUTING_CONFIG_CANDIDATES[0],  # deterministic fallback for the "not found" error msg
)
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

# Sensitivity levels that require declared route clearance.
REQUIRES_NO_AI = {"restricted"}
REQUIRES_US_OR_ANTHROPIC = {"restricted_us_oss_ok"}


def _declared_sensitivity_max(meta):
    order = {"public": 0, "internal": 1, "restricted_us_oss_ok": 2}
    if "data_sensitivity_max" in meta:
        value = meta["data_sensitivity_max"]
        return value if isinstance(value, str) and value in order else "unknown"
    if "data_clearance" not in meta:
        return "internal"
    levels = meta["data_clearance"]
    if (not isinstance(levels, list) or not levels
            or any(not isinstance(level, str) or level not in order for level in levels)):
        return "unknown"
    return max(levels, key=order.get)


def _registered_platform_models(platform):
    aliases = platform.get("available_models", [])
    resolution = platform.get("alias_resolution", {})
    if (not isinstance(aliases, list) or not isinstance(resolution, dict)
            or any(not isinstance(alias, str) or not alias for alias in aliases)):
        return set()
    registered = set(aliases)
    for alias in aliases:
        target = resolution.get(alias)
        if target is not None:
            if not isinstance(target, str) or not target:
                return set()
            registered.add(target)
    return registered


def build_registered_routes(config):
    """Resolve owner-declared clearance with proxy interception taking precedence."""
    routes = {}
    order = {"public": 0, "internal": 1, "restricted_us_oss_ok": 2, "restricted": 3}
    def add(name, meta, source):
        if not isinstance(name, str) or not name or name.startswith("$"):
            return
        if not isinstance(meta, dict):
            meta = {"data_sensitivity_max": "unknown"}
        if source == "platforms" and name in routes:
            if order.get(routes[name]["data_sensitivity_max"], 0) <= order.get(_declared_sensitivity_max(meta), 0):
                return
        routes[name] = {"data_sensitivity_max": _declared_sensitivity_max(meta),
                        "resolves_to": meta.get("resolves_to", meta.get("model", name)),
                        "source": source, "warning": meta.get("warning", "")}
    for platform in config.get("platforms", {}).values():
        if isinstance(platform, dict):
            for name in _registered_platform_models(platform):
                add(name, platform, "platforms")
    tiers = config.get("tier_definitions", {})
    tier_routes = {}
    for name, meta in tiers.items():
        if isinstance(meta, dict):
            for alias in (meta.get("model"), meta.get("litellm_alias")):
                if not isinstance(alias, str):
                    continue
                previous = tier_routes.get(alias)
                if previous is None or order.get(_declared_sensitivity_max(meta), 0) < order.get(_declared_sensitivity_max(previous), 0):
                    tier_routes[alias] = meta
    # Raw backends remain ambiguous with platform model names.
    for name, meta in tier_routes.items():
        previous = routes.get(name)
        if (previous is None or order.get(_declared_sensitivity_max(meta), 0)
                < order.get(previous["data_sensitivity_max"], 0)):
            add(name, meta, "tier_definitions")
    # Explicit route names retain their own ceiling and alias precedence.
    for name, meta in tiers.items():
        add(name, meta, "tier_definitions")
    for section in ("tier_aliases", "proxy_intercepted_models"):
        for name, meta in config.get(section, {}).items():
            add(name, meta, section)
    return routes


def build_anthropic_native_set(config):
    """Derive Anthropic-native model identifiers from routing config.

    Legacy inventory API only: membership never grants data clearance.

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


def lint_agent_file(path, oss_routed, anthropic_native, verbose=False, known_sensitivities=None):
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

    if known_sensitivities is not None and data_sensitivity not in known_sensitivities:
        # Unknown / retired sensitivity label (e.g. anthropic_only): the hook
        # would silently fall back to the lowest tier — fail closed here so a
        # stale agent cannot pass lint while effectively unguarded.
        violations.append({
            "file": str(path),
            "agent": fm.get("name", path.stem),
            "model": model,
            "data_sensitivity": data_sensitivity,
            "resolves_to": "n/a",
            "data_sensitivity_max": "unknown",
            "source": "unknown_sensitivity",
            "warning": (
                f"data_sensitivity '{data_sensitivity}' is not a declared "
                "classification in data_classification — failing closed."
            ),
        })
        return violations

    if data_sensitivity in REQUIRES_NO_AI:
        # Governance no-AI tier: declaring any runnable model is a violation.
        violations.append({
            "file": str(path),
            "agent": fm.get("name", path.stem),
            "model": model,
            "data_sensitivity": data_sensitivity,
            "resolves_to": "n/a",
            "data_sensitivity_max": "none",
            "source": "no_ai_tier",
            "warning": "data_sensitivity 'restricted' is governance no-AI; remove the model field.",
        })
        return violations

    if data_sensitivity not in REQUIRES_US_OR_ANTHROPIC:
        return []

    if model in oss_routed:
        meta = oss_routed[model]
        if meta.get("data_sensitivity_max") == "restricted_us_oss_ok":
            return []  # declared eligible US-OSS / guardrailed alias
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
        # Unknown model + restricted_us_oss_ok data sensitivity → fail closed.
        # We cannot prove declared clearance for an unregistered identifier.
        violations.append({
            "file": str(path),
            "agent": fm.get("name", path.stem),
            "model": model,
            "data_sensitivity": data_sensitivity,
            "resolves_to": "unregistered",
            "data_sensitivity_max": "unknown",
            "source": "unregistered_model",
            "warning": (
                "Model has no registered route metadata; cannot prove declared data clearance."
            ),
        })
        if verbose:
            print(
                f"  WARN  {path.name}: model '{model}' is not registered in "
                f"tier_aliases or proxy_intercepted_models — treating as violation "
                f"(fail-closed for restricted_us_oss_ok agents)"
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
        "  FIX:         Select an owner-configured route whose declared data_sensitivity_max "
        "or data_clearance covers this classification in .claude/model-routing.json. "
        "The restricted classification permits no AI model."
    )
    return "\n".join(lines)


# ── RULE 7: subagent dispatch must pin an explicit model= ─────────────────────
# Claude Code v2.1.198 changed the built-in Explore agent (and by the same
# release-channel behaviour, every built-in subagent that previously defaulted
# to haiku) to INHERIT the main-session model — capped at opus. Any
# `Agent(subagent_type="<name>", ...)` call site that omits `model=` therefore
# silently escalates from OSS/haiku routing to Anthropic Opus pricing.
#
# Reference: master/reports/explore-opus-regression-fix-2026-07-12.md
#
# This is a minimal port of master's Rule 7 for the ai-starter-pack
# distribution — same paren-balance span extractor and quote-aware masking,
# so `model=` embedded inside a prompt string does not false-pass.
_SUBAGENT_RE = re.compile(r"""["'`]?subagent_type["'`]?\s*[=:]\s*["'`]([A-Za-z0-9_.:\-/]+)["'`]""")
_MODEL_RE = re.compile(r"""["'`]?\bmodel\b["'`]?\s*[=:]""")
_AGENT_CALL_RE = re.compile(r"""\bAgent\s*\(""")
_ALLOW_MARKER = "lint-agent-routing:allow-missing-model"
_ALLOW_MARKER_FILE = "lint-agent-routing:allow-file"
_DISPATCH_SCAN_EXTS_SOURCE = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".sh"}
_DISPATCH_SCAN_EXTS_DOCS = {".md"}
_DISPATCH_SCAN_EXCLUDES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", "coverage",
}
_MAX_CALL_SPAN_CHARS = 20000


def _extract_agent_call_spans(text):
    """Yield (start_line_1indexed, span_text, span_cleaned) for each Agent(...) call.

    Uses paren-balance with quote awareness (basic backslash escape handling
    and triple-quoted-string support). span_cleaned has PROSE string bodies
    (whitespace or >24 chars) replaced with spaces so structural `model=`
    checks skip over prompt contents. Short bare-identifier strings like
    "model" / "haiku" stay unmasked so JSON-shape {"model": "haiku"} keys
    still match _MODEL_RE.
    """
    for m in _AGENT_CALL_RE.finditer(text):
        open_paren = m.end() - 1
        depth = 1
        i = open_paren + 1
        end = len(text)
        limit = min(end, open_paren + _MAX_CALL_SPAN_CHARS)
        in_str = None
        str_start = -1
        str_regions = []
        while i < limit and depth > 0:
            c = text[i]
            if in_str:
                if len(in_str) == 3:
                    if text[i: i + 3] == in_str:
                        str_regions.append((str_start, i))
                        i += 3
                        in_str = None
                        continue
                    if c == "\\" and i + 1 < end:
                        i += 2
                        continue
                    i += 1
                    continue
                if c == "\\" and i + 1 < end:
                    i += 2
                    continue
                if c == in_str:
                    str_regions.append((str_start, i))
                    in_str = None
                    i += 1
                    continue
                i += 1
                continue
            if c in ("'", '"', "`"):
                if text[i: i + 3] in ('"""', "'''", "```"):
                    in_str = text[i: i + 3]
                    i += 3
                    str_start = i
                    continue
                in_str = c
                i += 1
                str_start = i
                continue
            if c == "(":
                depth += 1
                i += 1
                continue
            if c == ")":
                depth -= 1
                if depth == 0:
                    line_end = text.find("\n", i + 1)
                    if line_end == -1:
                        line_end = end
                    span_text = text[m.start(): line_end]
                    span_start_abs = m.start()
                    span_len = len(span_text)
                    buf = list(span_text)
                    for s, e in str_regions:
                        body = text[s: e]
                        if len(body) <= 24 and not any(ch.isspace() for ch in body):
                            continue
                        rel_s = max(0, s - span_start_abs)
                        rel_e = min(span_len, e - span_start_abs)
                        for k in range(rel_s, rel_e):
                            if buf[k] != "\n":
                                buf[k] = " "
                    span_cleaned = "".join(buf)
                    start_line = text.count("\n", 0, m.start()) + 1
                    yield start_line, span_text, span_cleaned
                    break
                i += 1
                continue
            i += 1


def lint_subagent_dispatches_in_tree(root, include_docs=False):
    """Return list of violations: source-file dispatches missing explicit model=.

    Args:
        root: repo root to scan (Path or str).
        include_docs: also scan .md files (prose examples). Off by default.
    """
    violations = []
    root = Path(root).resolve()
    exts = set(_DISPATCH_SCAN_EXTS_SOURCE)
    if include_docs:
        exts |= _DISPATCH_SCAN_EXTS_DOCS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _DISPATCH_SCAN_EXCLUDES]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix not in exts:
                continue
            rel = path.relative_to(root)
            if rel.name == "lint-agent-routing.py":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            if "Agent(" not in text or "subagent_type" not in text:
                continue
            if _ALLOW_MARKER_FILE in text:
                continue
            for start_line, span, span_cleaned in _extract_agent_call_spans(text):
                if _ALLOW_MARKER in span:
                    continue
                sub_match = _SUBAGENT_RE.search(span)
                if not sub_match:
                    continue
                if _MODEL_RE.search(span_cleaned):
                    continue
                snippet_line = span.splitlines()[0].strip()[:160]
                violations.append({
                    "file": str(rel),
                    "line": start_line,
                    "subagent": sub_match.group(1),
                    "snippet": snippet_line,
                })
    return violations


def format_subagent_dispatch_violation(v):
    return (
        f"  [RULE7_MISSING_MODEL] {v['file']}:{v['line']}\n"
        f"    subagent_type=\"{v['subagent']}\" has no explicit model= within its Agent() call\n"
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
        default=".",
        help="Rule 7: scan DIR (default: repo root) for Agent(subagent_type=...) "
             "call sites missing an explicit model= argument. On by default; use "
             "--no-check-subagent-dispatches to skip. See "
             "master/reports/explore-opus-regression-fix-2026-07-12.md",
    )
    parser.add_argument(
        "--no-check-subagent-dispatches",
        action="store_true",
        help="Skip the Rule 7 subagent-dispatch scan.",
    )
    parser.add_argument(
        "--include-docs",
        action="store_true",
        help="Extend the Rule 7 scan to .md files (prose examples). Off by default.",
    )
    parser.add_argument(
        "--dispatch-only",
        action="store_true",
        help="Run ONLY the Rule 7 subagent-dispatch scan; skip agent-frontmatter "
             "linting. Use in pre-commit source-file hooks.",
    )
    args = parser.parse_args()

    if args.dispatch_only:
        scan_root = Path(args.check_subagent_dispatches or ".")
        vios = lint_subagent_dispatches_in_tree(scan_root, include_docs=args.include_docs)
        if vios:
            print(f"\n[lint-agent-routing] SUBAGENT DISPATCH VIOLATIONS — {len(vios)} issue(s)\n")
            for v in vios:
                print(format_subagent_dispatch_violation(v))
                print()
            print(
                "RULE 7 — Subagent dispatches must pin an explicit model=. Add "
                "explicit model=\"haiku\" (or the intended tier) to the Agent(...) "
                "call. Reference: master/reports/explore-opus-regression-fix-2026-07-12.md"
            )
            return 1
        print(f"[lint-agent-routing] Rule 7 OK — subagent dispatches under {scan_root} all pin model=")
        return 0

    config = load_routing_config()
    oss_routed = build_oss_routed_set(config)
    registered_routes = build_registered_routes(config)
    anthropic_native = set()  # Legacy lint API argument; identity grants no clearance.

    if args.list_oss:
        print("OSS-routed model identifiers (NOT safe for restricted data; restricted_us_oss_ok requires a guardrailed/declared alias):\n")
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
        vios = lint_agent_file(
            path, registered_routes, anthropic_native, verbose=args.verbose,
            known_sensitivities=set(config.get("data_classification", {}).keys()),
        )
        all_violations.extend(vios)

    if all_violations:
        has_io_errors = any("error" in v for v in all_violations)
        print(f"\n[lint-agent-routing] FAILED — {len(all_violations)} issue(s)\n")
        for v in all_violations:
            print(format_violation(v))
            print()
        print("WHY THIS MATTERS:")
        print(
            "  Agents with data_sensitivity 'restricted' declare no model; 'restricted_us_oss_ok' agents handle\n"
            "  protected data and require a route with explicit owner-declared clearance.\n"
            "  A provider name or native model identifier grants no data permission.\n"
            "  Proxy interception takes precedence over native host metadata.\n"
        )
        print(
            "  Source of truth: .claude/model-routing.json\n"
            "  Sections:  tier_aliases  /  proxy_intercepted_models  /  data_classification"
        )
        return 2 if has_io_errors else 1

    checked = len(agent_files)
    print(f"[lint-agent-routing] OK — {checked} agent(s) checked, 0 violations")

    if not args.no_check_subagent_dispatches and args.check_subagent_dispatches is not None:
        scan_root = Path(args.check_subagent_dispatches)
        vios = lint_subagent_dispatches_in_tree(scan_root, include_docs=args.include_docs)
        if vios:
            print(f"\n[lint-agent-routing] SUBAGENT DISPATCH VIOLATIONS — {len(vios)} issue(s)\n")
            for v in vios:
                print(format_subagent_dispatch_violation(v))
                print()
            print(
                "RULE 7 — Subagent dispatches must pin an explicit model=. "
                "Reference: master/reports/explore-opus-regression-fix-2026-07-12.md"
            )
            return 1
        print(f"[lint-agent-routing] Rule 7 OK — subagent dispatches under {scan_root} all pin model=")

    return 0


if __name__ == "__main__":
    sys.exit(main())
