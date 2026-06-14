#!/usr/bin/env python3
"""UserPromptSubmit hook — model-tier routing + session init + PR comment check.

Receives hook JSON on stdin with 'prompt' field containing the user's message.
Outputs:
  - A <model-routing> block on every prompt (tells the orchestrator which
    model tier to suggest when spawning sub-agents).
  - System messages on first prompt (Husky setup, active plans check).
  - PR comment warnings when unresolved comments are cached.

Always exits 0 — never blocks Claude.
"""
import sys
import json
import re
import os
import time
import subprocess
from pathlib import Path

# ── State file (persists prompt count / edit count across hook calls) ─────────
STATE_FILE = Path(".git/.session-state.json")
PR_CACHE = Path(".git/.pr-comments-cache/latest.json")
PR_CACHE_TTL = 300  # 5 minutes


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"prompt_count": 0, "edit_count": 0, "session_start": time.time()}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        with open(STATE_FILE, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(state))
    except ImportError:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE_FILE)


# ── Model routing ─────────────────────────────────────────────────────────────

def _load_routing_keywords():
    """Load keyword lists from .claude/model-routing.json. Falls back to defaults."""
    try:
        _proj = os.environ.get("CLAUDE_PROJECT_DIR")
        _plug = os.environ.get("CLAUDE_PLUGIN_ROOT")
        # consumer override -> bundled plugin default (data/) -> in-tree fallback.
        _candidates = []
        if _proj:
            _candidates.append(Path(_proj) / ".claude" / "model-routing.json")
        if _plug:
            _candidates.append(Path(_plug) / "data" / "model-routing.json")
        _candidates.append(Path(__file__).parent.parent / "model-routing.json")
        # plugin-layout fallback if CLAUDE_PLUGIN_ROOT is unset (data/ beside hooks/)
        _candidates.append(Path(__file__).parent.parent / "data" / "model-routing.json")
        config_path = next((c for c in _candidates if c.exists()), _candidates[-1])
        with open(config_path) as f:
            cfg = json.load(f)
        overrides = cfg.get("overrides", {})
        claude_code = cfg.get("platforms", {}).get("claude_code", {})
        tier_map = claude_code.get("tier_map", {})
        default_tier = claude_code.get("default_tier", "standard")
        full_tier_map = {"light": "haiku", "standard": "sonnet", "heavy": "opus"}
        if isinstance(tier_map, dict):
            full_tier_map.update(tier_map)
        proxy_enabled = cfg.get("litellm_proxy", {}).get("enabled", False)
        return (
            overrides.get("escalate_to_heavy", []),
            overrides.get("escalate_to_standard", []),
            overrides.get("deescalate_to_light", []),
            full_tier_map,
            default_tier,
            proxy_enabled,
        )
    except Exception:
        return (
            ["architect", "design", "plan", "migration", "security", "audit",
             "cross-repo", "orchestrate", "ambiguous", "trade-off"],
            ["implement", "refactor", "test", "review", "debug", "integrate",
             "summarize", "analyze", "document", "docs", "optimize"],
            ["list", "count", "find", "search", "rename", "format", "lint",
             "grep", "glob", "status", "boilerplate"],
            {"light": "haiku", "standard": "sonnet", "heavy": "opus"},
            "standard",
            False,
        )


HEAVY_KW, STANDARD_KW, LIGHT_KW, TIER_MODEL_MAP, DEFAULT_TIER, PROXY_ENABLED = _load_routing_keywords()


def _whole_word(text, keyword):
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None


def classify_tier(prompt_text: str) -> dict:
    """Classify prompt into light/standard/heavy tier using keyword heuristics."""
    lower = prompt_text.lower()
    heavy_matches = [k for k in HEAVY_KW if _whole_word(lower, k)]
    if heavy_matches:
        return {"tier": "heavy", "model": TIER_MODEL_MAP.get("heavy", "opus"), "matched_keywords": heavy_matches}
    standard_matches = [k for k in STANDARD_KW if _whole_word(lower, k)]
    if standard_matches:
        return {"tier": "standard", "model": TIER_MODEL_MAP.get("standard", "sonnet"), "matched_keywords": standard_matches}
    light_matches = [k for k in LIGHT_KW if _whole_word(lower, k)]
    if light_matches:
        return {"tier": "light", "model": TIER_MODEL_MAP.get("light", "haiku"), "matched_keywords": light_matches}
    # No keyword match — use configured default tier
    tier = DEFAULT_TIER if DEFAULT_TIER in TIER_MODEL_MAP else "standard"
    return {"tier": tier, "model": TIER_MODEL_MAP.get(tier, "sonnet"), "matched_keywords": []}


# ── Content patterns (for stderr observability, not injected into context) ────

CONTENT_PATTERNS = {
    "decision":  re.compile(r"\b(decide[ds]?|decision|let'?s\s+use|we'?ll\s+go\s+with|choosing|architectural\s+choice)\b", re.I),
    "incident":  re.compile(r"\b(error|bug|broken|timeout|outage|failing|crashed|incident|P0|P1)\b", re.I),
    "gotcha":    re.compile(r"\b(gotcha|watch\s+out|quirk|workaround|caveat|pitfall|heads\s+up)\b", re.I),
    "security":  re.compile(r"\b(auth|authentication|authorization|permission|secret|token|credential|xss|injection|csrf)\b", re.I),
}


def classify_content(prompt_text: str) -> list:
    return [cat for cat, pat in CONTENT_PATTERNS.items() if pat.search(prompt_text)]


# ── Session init (first prompt only) ─────────────────────────────────────────

def deferred_init() -> list:
    """Run once on first prompt — Husky setup, active plans check."""
    messages = []

    # Wire Husky if hooks dir exists but git config not set (--local so global
    # core.hooksPath overrides don't mask a missing local setting)
    try:
        result = subprocess.run(
            ["git", "config", "--local", "--get", "core.hooksPath"],
            capture_output=True, text=True, timeout=3
        )
        if (result.returncode != 0 or not result.stdout.strip()) and Path(".husky").exists():
            subprocess.run(["git", "config", "--local", "core.hooksPath", ".husky"],
                           capture_output=True, timeout=3)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Alert if active plans exist
    plans_dir = Path(".claude/plans")
    if plans_dir.exists():
        active = list(plans_dir.glob("*.md"))
        if active:
            names = ", ".join(p.stem for p in active[:3])
            messages.append({
                "type": "systemMessage",
                "message": f"Active plans found: {names}. Run /plan to review before starting."
            })

    return messages


# ── PR comment check ──────────────────────────────────────────────────────────

def check_pr_comments():
    if not PR_CACHE.exists():
        return None
    try:
        age = time.time() - PR_CACHE.stat().st_mtime
        if age > PR_CACHE_TTL:
            return None
        data = json.loads(PR_CACHE.read_text())
        count = data.get("unresolved_count", 0)
        if count > 0:
            return {
                "type": "systemMessage",
                "message": f"BLOCKING: {count} unresolved PR comment(s). Run /pr-watch to address before continuing."
            }
    except (json.JSONDecodeError, OSError):
        pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if not isinstance(data, dict):
        sys.exit(0)

    prompt_text = data.get("prompt", "")

    if not isinstance(prompt_text, str) or not prompt_text.strip():
        sys.exit(0)

    state = load_state()
    state["prompt_count"] = state.get("prompt_count", 0) + 1

    system_messages = []

    # Best-effort: first-prompt deferred init (failure must not suppress routing)
    try:
        if state["prompt_count"] == 1:
            system_messages.extend(deferred_init())
    except Exception:
        pass

    # Best-effort: PR comment warning
    try:
        pr_msg = check_pr_comments()
        if pr_msg:
            system_messages.append(pr_msg)
    except Exception:
        pass

    # Best-effort: persist session state
    try:
        save_state(state)
    except Exception:
        pass

    # Model routing block — always emitted regardless of above failures
    tier_result = classify_tier(prompt_text)
    tier  = tier_result["tier"]
    model = tier_result["model"]
    kw    = tier_result["matched_keywords"]
    kw_str = f" (keywords: {', '.join(kw)})" if kw else " (default)"

    if PROXY_ENABLED:
        proxy_line = (
            "OSS proxy: enabled — for internal/public data prefer tier-1-fast (boilerplate/format), "
            "tier-2-agentic (long chains), tier-4-extract (grep/lint) to reduce cost 5–30×. "
            "Keep anthropic_only data on Claude tiers only."
        )
    else:
        proxy_line = (
            "OSS proxy: disabled — run scripts/setup-litellm.sh and set "
            "litellm_proxy.enabled=true in .claude/model-routing.json to activate cheaper OSS tiers."
        )

    routing_block = "\n".join([
        "<model-routing>",
        f"Suggested tier: {tier} → model: {model}{kw_str}",
        f"When spawning sub-agents (Agent tool), pass model=\"{model}\" unless the specific sub-task complexity requires a different tier.",
        proxy_line,
        "</model-routing>",
    ])

    # Content classification → stderr only (observability, not injected into context)
    try:
        categories = classify_content(prompt_text)
        if categories:
            hints = []
            if "decision" in categories:
                hints.append("Consider /remember with type:decision")
            if "gotcha" in categories:
                hints.append("Consider /remember with type:gotcha or /learn")
            if "incident" in categories:
                hints.append("Consider /diagnose if this is a recurring issue")
            cc = ["<content-classification>", f"Categories: {', '.join(categories)}"]
            cc += [f"Suggestion: {h}" for h in hints]
            cc.append("</content-classification>")
            print("\n".join(cc), file=sys.stderr)
    except Exception:
        pass

    # Always emit a consistent JSON array — routing block is the final systemMessage
    system_messages.append({"type": "systemMessage", "message": routing_block})
    print(json.dumps(system_messages))

    sys.exit(0)


if __name__ == "__main__":
    main()
