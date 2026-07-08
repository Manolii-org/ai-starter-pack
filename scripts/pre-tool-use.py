#!/usr/bin/env python3
"""PreToolUse hook — model routing enforcement + PR repo targeting guard.

Three invariants:
  1. OSS-eligible agents MUST pass model= explicitly (otherwise tier overrides are bypassed).
  2. Anthropic-locked agents MUST NOT use OSS tier aliases (data sensitivity enforcement).
  3. mcp__github__create_pull_request MUST target the repo whose remote matches the session cwd.

Exit codes:
  0  always (non-zero exit causes Claude Code to treat hook as crashed; use decision:block instead)

Block output:
  {"decision": "block", "reason": "..."} → Claude Code aborts the tool call and shows reason
"""
import json
import re
import sys
from pathlib import Path

# ── Single stdin read ────────────────────────────────────────────────────────
try:
    data = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)

if not isinstance(data, dict):
    sys.exit(0)

tool_name = data.get("tool_name", "")

# ── PR repo targeting guard ───────────────────────────────────────────────────
if tool_name == "mcp__github__create_pull_request":
    def _pr_guard() -> None:
        import subprocess
        import re
        inp = data.get("tool_input") or {}
        pr_owner = (inp.get("owner") or "").lower()
        pr_repo = (inp.get("repo") or "").lower()
        if not pr_owner or not pr_repo:
            return
        cwd = data.get("cwd")
        if not cwd:
            return  # cwd absent from payload — fail open rather than risk false-positive block
        try:
            out = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5, cwd=cwd,
            ).stdout.strip()
            m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", out)
            if not m:
                return
            if m.group(1).lower() == pr_owner and m.group(2).lower() == pr_repo:
                return
            print(json.dumps({
                "decision": "block",
                "reason": (
                    f"[PR-GUARD] session cwd remote is '{m.group(1)}/{m.group(2)}' but PR targets "
                    f"'{pr_owner}/{pr_repo}'. cd into a local clone of the target repo first — "
                    f"all edits, commits, pushes, and PR creation must happen from there. "
                    f"Ensure you have cloned the target repo locally before creating PRs."
                ),
            }))
        except Exception:
            pass  # fail open
    _pr_guard()
    sys.exit(0)

def _token_leak_guard(cmd: str):
    """Block shell commands that would print a secret-named variable's value into
    logs or the transcript (the #1 token-leak incident class). Covers echo/printf
    expansion, printenv/declare introspection, and env|grep dumps. Returns a block
    reason, or None if clean. See docs/token-leak-hygiene.md."""
    import re
    if not cmd:
        return None
    # A "secret-named" variable contains one of these keyword components.
    tvar = r"[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|CREDENTIALS?|PRIVATE_KEY|_KEY|_PAT)[A-Za-z0-9_]*"
    note = (" Use a presence check that never prints the value, e.g. "
            "'[ -n \"${VAR:-}\" ] && echo set'. See docs/token-leak-hygiene.md.")
    # echo/printf expanding a secret-named var ($VAR, ${VAR}, ${VAR:-x}, ${VAR:+x}).
    if re.search(r"(?i)\b(?:echo|printf)\b[^\n|;&]*\$\{?\s*" + tvar, cmd):
        return ("[TOKEN-LEAK] Command echoes/printfs a secret-named variable — this can "
                "leak its value into logs and the transcript." + note)
    # printenv / declare -p of a secret-named var.
    if re.search(r"(?i)\b(?:printenv|declare\s+-p)\b\s+\$?\{?\s*" + tvar, cmd):
        return ("[TOKEN-LEAK] Command introspects a secret-named variable "
                "(printenv/declare -p) — this prints its value." + note)
    # env | grep / printenv | grep — dumps env values through grep.
    if re.search(r"(?i)\b(?:env|printenv)\b[^\n|]*\|\s*grep\b", cmd):
        return ("[TOKEN-LEAK] Piping env/printenv through grep prints matching variable "
                "values." + note)
    return None


if tool_name == "Bash":
    _cmd = str((data.get("tool_input") or {}).get("command", ""))
    _tl = _token_leak_guard(_cmd)
    if _tl:
        print(json.dumps({"decision": "block", "reason": _tl}))
    sys.exit(0)

if tool_name != "Agent":
    sys.exit(0)

tool_input = data.get("tool_input") or {}
model = tool_input.get("model")  # None when caller omitted model=
subagent_type = str(tool_input.get("subagent_type", "general-purpose"))

# ── Scope-budget + broad-dispatch guards ─────────────────────────────────────
# Two containment guards on sub-agent dispatch. Both emit {"decision":"block"}
# with actionable remediation. Tune the agent sets below for your project, or
# relax either to a stderr warning if it is too aggressive for your workflow.
prompt_text = str(tool_input.get("prompt", ""))
run_in_background = bool(tool_input.get("run_in_background", False))

# Write/mutation-capable agents. A dispatch to one of these MUST declare a
# SCOPE_BUDGET block (allowed_paths) so the sub-agent cannot drift off-scope.
WRITE_CAPABLE_AGENTS = {
    "generate", "orchestrator", "infra", "ci-fixer", "test-hardener",
    "prompt-hardener", "memory-keeper", "secrets-handler",
    "main-thread-executor", "general-purpose",
}
# Read-only search/analysis agents subject to the broad-dispatch breadth guard.
BROAD_DISPATCH_AGENTS = {"Explore", "explore-summarised"}

_BREADTH_SIGNALS = (
    "all files", "every file", "entire repo", "whole repo", "whole codebase",
    "across the codebase", "all subdirectories", "all directories",
    "everything in the repo", "scan the repo", "all of the files",
)


def _has_scope_budget(text: str) -> bool:
    import re
    return bool(re.search(r"(?im)^\s*SCOPE_BUDGET\s*:", text)) or "allowed_paths:" in text


def _agent_guard():
    # 1. Scope-budget: write-capable dispatch must declare allowed_paths.
    if subagent_type in WRITE_CAPABLE_AGENTS and not _has_scope_budget(prompt_text):
        return (
            f"[SCOPE-BUDGET] Dispatch to write-capable agent {subagent_type!r} is missing a "
            "SCOPE_BUDGET block. Add one so the sub-agent cannot drift off-scope, e.g.:\n"
            "  SCOPE_BUDGET:\n"
            "  allowed_paths: path/or/glob, second/path\n"
            "If new blocking work falls outside that list, the sub-agent must stop and ask."
        )
    # 2. Broad-dispatch: a single read-only Explore-class agent told to sweep the
    #    whole repo reliably times out. Decompose, scope it, or background it.
    low = prompt_text.lower()
    if (subagent_type in BROAD_DISPATCH_AGENTS
            and any(sig in low for sig in _BREADTH_SIGNALS)
            and not run_in_background):
        return (
            f"[BROAD-DISPATCH] Single {subagent_type!r} agent with whole-repo breadth signals "
            "reliably times out on large repos. Either (1) scope it to specific paths/globs, "
            "(2) decompose via subagent_type=\"orchestrator\", or (3) re-dispatch with "
            "run_in_background=True if you intend a single long-running sweep."
        )
    return None


_guard_reason = _agent_guard()
if _guard_reason:
    print(json.dumps({"decision": "block", "reason": _guard_reason}))
    sys.exit(0)


# ── Model sets ───────────────────────────────────────────────────────────────
# OSS tier aliases resolved by LiteLLM proxy.
# "haiku" is proxy-intercepted → GPT-OSS 120B on Fireworks ($0.80/$3.20/M).
OSS_TIER_ALIASES = {
    "haiku",
    "tier-0-oss-heavy",
    "tier-1-fast",
    "tier-2-agentic",
    "tier-3-tool",
    "tier-3-tool-us",
    "tier-4-extract",
    "tier-5-latency",
    # Reasoning review tier (DeepSeek V4 Flash). Dispatched under its own alias rather
    # than the haiku bucket — haiku proxy-remaps to GPT-OSS-120B (non-reasoning), which
    # would discard the reasoning budget. classify-message.py emits model="tier-review"
    # for agents routed here; the proxy (config.yaml model_name: tier-review) routes it.
    "tier-review",
}
ANTHROPIC_MODELS = {
    "sonnet",
    "opus",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
}
VALID_MODELS = OSS_TIER_ALIASES | ANTHROPIC_MODELS

# ── Load routing policy to derive sensitivity clearance and recommendations ───
SENSITIVITY_ORDER = {
    "public": 0,
    "internal": 1,
    "restricted_us_oss_ok": 2,
    "restricted": 3,
    "anthropic_only": 4,
}


def _load_routing_config() -> dict | None:
    try:
        config_path = Path(__file__).parent.parent / ".claude" / "model-routing.json"
        with open(config_path) as f:
            return json.load(f)
    except Exception:
        return None


def _normalise_sensitivity(value: str) -> str:
    return value.strip().strip('"\'')


def _load_agent_sensitivity(agent_name: str) -> str:
    if not agent_name:
        return "internal"
    try:
        agent_path = Path(__file__).parent.parent / ".claude" / "agents" / f"{agent_name}.md"
        text = agent_path.read_text()
    except Exception:
        return "internal"
    match = re.search(r"^data_sensitivity:\s*([^#\n]+)", text, re.MULTILINE)
    return _normalise_sensitivity(match.group(1)) if match else "internal"


def _model_sensitivity_max(model_name: str | None, cfg: dict) -> str | None:
    if not model_name or model_name in ("", "default"):
        return None
    for section in ("proxy_intercepted_models", "tier_aliases"):
        meta = cfg.get(section, {}).get(model_name)
        if isinstance(meta, dict):
            return meta.get("data_sensitivity_max", "internal")
    tier = cfg.get("tier_definitions", {}).get(model_name)
    if isinstance(tier, dict):
        if tier.get("provider") == "anthropic":
            return "anthropic_only"
        return tier.get("data_sensitivity_max", "internal")
    if model_name in ANTHROPIC_MODELS:
        return "anthropic_only"
    return None


def _has_clearance(model_name: str | None, required: str, cfg: dict) -> bool:
    required_rank = SENSITIVITY_ORDER.get(required, SENSITIVITY_ORDER["internal"])
    max_sensitivity = _model_sensitivity_max(model_name, cfg)
    if max_sensitivity is None:
        return required_rank <= SENSITIVITY_ORDER["internal"]
    return SENSITIVITY_ORDER.get(max_sensitivity, 0) >= required_rank


routing_config = _load_routing_config()
routing_config_loaded = routing_config is not None
routing_config = routing_config or {}
agent_routing = routing_config.get("agent_routing", {})
recommended = agent_routing.get(subagent_type, agent_routing.get("default", "tier-1-fast"))
agent_sensitivity = _load_agent_sensitivity(subagent_type)
agent_sensitivity_rank = SENSITIVITY_ORDER.get(agent_sensitivity, SENSITIVITY_ORDER["internal"])
model_omitted = model is None or model == "" or model == "default"

if agent_sensitivity_rank > SENSITIVITY_ORDER["internal"] and not routing_config_loaded:
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"[OSS-GUARD] Agent dispatch blocked: {subagent_type!r} declares "
            f"data_sensitivity={agent_sensitivity!r}, but .claude/model-routing.json "
            "could not be loaded. Protected-agent routing requires data_sensitivity_max metadata."
        ),
    }))
    sys.exit(0)

if model and model not in VALID_MODELS and agent_sensitivity_rank > SENSITIVITY_ORDER["internal"]:
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"[OSS-GUARD] Agent dispatch blocked: {subagent_type!r} declares "
            f"data_sensitivity={agent_sensitivity!r}, but model={model!r} is not registered "
            "in the hook's valid model set. Add routing metadata before using it for protected data."
        ),
    }))
    sys.exit(0)

if (
    model_omitted
    and agent_sensitivity_rank > SENSITIVITY_ORDER["internal"]
    and not _has_clearance(recommended, agent_sensitivity, routing_config)
):
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"[OSS-GUARD] Agent dispatch blocked: {subagent_type!r} declares "
            f"data_sensitivity={agent_sensitivity!r}, but its configured/default route "
            f"{recommended!r} lacks sufficient clearance. Add an agent_routing entry "
            "with data_sensitivity_max coverage before dispatching this protected agent."
        ),
    }))
    sys.exit(0)

# Protected agents may use approved OSS routes, but only when the alias advertises
# enough data_sensitivity_max clearance. This preserves the 2026-05 policy update
# (OSS approved for most work) without letting secret-bearing flows fall to haiku.
if model and model in VALID_MODELS and not _has_clearance(model, agent_sensitivity, routing_config):
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"[OSS-GUARD] Agent dispatch blocked: {subagent_type!r} declares "
            f"data_sensitivity={agent_sensitivity!r}, but model={model!r} only allows "
            f"data_sensitivity_max={_model_sensitivity_max(model, routing_config)!r}. "
            f"Use model=\"{recommended}\" or another route with sufficient clearance."
        ),
    }))
    sys.exit(0)

# Anthropic-only routes may omit model= because default behaviour is safe.
if agent_sensitivity_rank >= SENSITIVITY_ORDER["restricted"]:
    if model in OSS_TIER_ALIASES:
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"[OSS-GUARD] Agent dispatch blocked: {subagent_type!r} is Anthropic-only "
                f"(data_sensitivity={agent_sensitivity!r}) and cannot use OSS alias model={model!r}. "
                f"Use model=\"{recommended}\" or omit model=."
            ),
        }))
    elif model is not None and model not in ANTHROPIC_MODELS and model not in ("", "default"):
        print(
            f"[OSS-GUARD WARN] Anthropic-only agent {subagent_type!r} uses unrecognised "
            f"model={model!r}. Expected one of: {sorted(ANTHROPIC_MODELS)}",
            file=sys.stderr,
        )
    sys.exit(0)

# ── OSS-eligible agents: model= must be explicit ─────────────────────────────
if model and model in VALID_MODELS:
    sys.exit(0)

OSS_DISPATCH_NOTE = (
    "OSS routing only activates with explicit model=. "
    "The CLAUDE_CODE_SUBAGENT_MODEL default bypasses per-agent tier config."
)

if model_omitted:
    # Claude Code's Agent tool accepts short aliases; LiteLLM-internal tier names
    # need a Claude Code alias that the proxy intercepts.
    # tier-review is dispatched under its own alias (the proxy routes it to the
    # reasoning model); folding it into the haiku remap would drop the reasoning tier
    # this is meant to preserve. Other OSS tiers map to the haiku alias the proxy
    # intercepts.
    if recommended == "tier-review":
        cc_model = "tier-review"
    elif recommended in OSS_TIER_ALIASES:
        cc_model = "haiku"
    else:
        cc_model = recommended
    proxy_note = (
        f"(Proxy routes haiku → {recommended} tier internally.) "
        if cc_model == "haiku" else ""
    )
    reason = (
        f"[OSS-GUARD] Agent dispatch blocked: model= is required. "
        f"Add model=\"{cc_model}\" for {subagent_type!r}. "
        f"Example: Agent(subagent_type=\"{subagent_type}\", model=\"{cc_model}\", ...). "
        f"{proxy_note}{OSS_DISPATCH_NOTE}"
    )
elif model not in VALID_MODELS:
    # Unknown model — warn but allow for internal/public agents only.
    print(
        f"[OSS-GUARD WARN] Agent {subagent_type!r} uses unrecognised model={model!r}. "
        f"Expected one of: {sorted(VALID_MODELS)}",
        file=sys.stderr,
    )
    sys.exit(0)
else:
    sys.exit(0)

# ── Emit block decision ───────────────────────────────────────────────────────
print(json.dumps({"decision": "block", "reason": reason}))
sys.exit(0)
