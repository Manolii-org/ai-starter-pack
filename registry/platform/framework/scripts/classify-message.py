#!/usr/bin/env python3
"""UserPromptSubmit hook — classifies user messages for memory routing and model tier hints.

Receives hook JSON on stdin with 'prompt' field containing the user's message.
Outputs plain text classification hints (injected as additionalContext).
Always outputs a <model-routing> block so the orchestrator knows which model
tier to use when spawning sub-agents for this prompt.

Always exits 0 — never blocks Claude.
"""
import sys
import json
import re
import os
import hashlib
import traceback
from pathlib import Path

# --- Content type patterns (word-boundary regex to avoid false positives) ---
CONTENT_PATTERNS = {
    "decision": re.compile(
        r"\b(decide[ds]?|decision|let'?s\s+use|we'?ll\s+go\s+with|choosing|architectural\s+choice|let'?s\s+go\s+with)\b", re.I
    ),
    "incident": re.compile(
        r"\b(error|bug|broken|504|timeout|outage|failing|crashed|incident|P0|P1)\b", re.I
    ),
    "financial": re.compile(
        r"\b(invoice|payment|tax|bank\s+account|TFN|ABN|BAS|superannuation|insurance\s+claim)\b", re.I
    ),
    "architecture": re.compile(
        r"\b(migration|schema\s+change|database|deployment|infrastructure|refactor|redesign)\b", re.I
    ),
    "gotcha": re.compile(
        r"\b(gotcha|watch\s+out|quirk|workaround|caveat|pitfall|heads\s+up)\b", re.I
    ),
}


# --- Build the DISPATCH RULE string from routing config ---
def _build_dispatch_rule(cfg):
    """Construct the DISPATCH RULE from routing config, splitting OSS-routed agents into dispatch buckets."""
    tier_aliases = cfg.get("tier_aliases") or {}
    non_proxy_tiers = {"light-main", "medium-main", "heavy-main", "fast-escape"}
    proxy_backed_tiers = {
        k for k, v in tier_aliases.items()
        if not k.startswith("$") and k not in non_proxy_tiers
        and (not isinstance(v, dict) or v.get("requires_proxy") is not False)
    }
    oss_tiers = (
        proxy_backed_tiers
        | {k for k in (cfg.get("proxy_intercepted_models") or {}).keys() if not k.startswith("$")}
    )
    _SONNET_PROXY = frozenset({"sonnet"})
    _REASONING_OSS = frozenset({"tier-review"})
    _HAIKU_OSS = oss_tiers - _SONNET_PROXY - _REASONING_OSS
    agent_routing = {k: v for k, v in (cfg.get("agent_routing") or {}).items() if not k.startswith("$")}
    anthropic_locked = {k for k, v in agent_routing.items() if v not in oss_tiers}
    builtin_haiku = ["Explore"]
    haiku_named = sorted(k for k, v in agent_routing.items() if v in _HAIKU_OSS and k not in anthropic_locked and k not in builtin_haiku)
    haiku_all = builtin_haiku + haiku_named
    sonnet_named = sorted(k for k, v in agent_routing.items() if v in _SONNET_PROXY and k not in anthropic_locked)
    reasoning_named = sorted(k for k, v in agent_routing.items() if v in _REASONING_OSS and k not in anthropic_locked)
    restricted_listed = sorted(anthropic_locked)
    reasoning_clause = f"tier-review=reasoning/editorial ({', '.join(reasoning_named)}). " if reasoning_named else ""
    return (
        "DISPATCH RULE: The tier above is for the MAIN THREAD ONLY. "
        f"For Agent tool calls: haiku=search/grep/format/file-read AND ({', '.join(haiku_all)}). "
        f"sonnet=({', '.join(sonnet_named)}). "
        f"{reasoning_clause}"
        f"claude-sonnet-4-6=restricted/client ({', '.join(restricted_listed)}). "
        "opus=MAIN THREAD ONLY — NEVER pass model=\"opus\" to any named sub-agent. "
        "Passing opus to a named sub-agent overrides its configured tier and wastes 5-16x cost."
    )

# --- Load all config from model-routing.json in a single read ---
def _load_routing_config():
    """Read model-routing.json once; return (heavy_kw, standard_kw, light_kw, tier_model_map,
    proxy_fallback, skill_tier_map, dispatch_rule, pricing_from_config, proxy_intercept_rates).
    All callers share this single parse."""
    _HEAVY_DEFAULT = ["architect", "design", "plan", "migration", "security", "audit",
                      "cross-repo", "orchestrate", "ambiguous", "trade-off"]
    _STANDARD_DEFAULT = ["implement", "refactor", "test", "review", "debug", "integrate",
                         "summarize", "analyze", "document", "optimize"]
    _LIGHT_DEFAULT = ["list", "count", "find", "search", "rename", "format", "lint",
                      "grep", "glob", "status", "boilerplate"]
    _TIER_MAP_DEFAULT = {"light": "haiku", "standard": "sonnet", "heavy": "sonnet"}
    _PROXY_DEFAULT = {
        "tier-1-fast": "haiku", "tier-2-agentic": "sonnet",
        "tier-3-tool": "sonnet", "tier-4-extract": "haiku",
        "tier-5-latency": "haiku", "tier-0-oss-heavy": "sonnet",
    }
    _DISPATCH_RULE_DEFAULT = (
        "DISPATCH RULE: The tier above is for the MAIN THREAD ONLY. "
        "For Agent tool calls, select by sub-task type — NOT by main-thread tier: "
        "haiku=search/grep/format/boilerplate/file-read AND haiku-tier named agents "
        "(Explore, default, generate, infra, qa, ecosystem-auditor, context-loader, "
        "insight-miner, review-internal). "
        "sonnet=sonnet-tier named agents: deep-analyse, ci-fixer, architecture-impact, "
        "systems-consistency, security-deep-dive, judge, pr-classifier, orchestrator, "
        "diff-reflex, test-hardener. "
        "claude-sonnet-4-6=restricted/client data (review). "
        "opus=MAIN THREAD ONLY — NEVER pass model=\"opus\" to any named sub-agent. "
        "Passing opus to a named sub-agent overrides its configured tier and wastes 5-16x cost."
    )
    # Default pricing: Anthropic-published rates only
    _PRICING_DEFAULT = {
        "claude-opus-4-7":           {"in": 15.00, "out": 75.00},
        "claude-opus-4-6":           {"in": 15.00, "out": 75.00},
        "claude-sonnet-4-6":         {"in":  3.00, "out": 15.00},
        "haiku":                     {"in":  0.30, "out":  1.20},
        "claude-haiku-4-5-20251001": {"in":  0.30, "out":  1.20},
    }
    _PROXY_INTERCEPT_RATES_DEFAULT = {}

    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = []
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        project_root = os.environ.get("CLAUDE_PROJECT_DIR")
        if project_root:
            candidates.append(os.path.join(project_root, ".claude", "model-routing.json"))
        if plugin_root:
            candidates.append(os.path.join(plugin_root, "data", "model-routing.json"))
        candidates.extend([
            os.path.join(root, ".claude", "model-routing.json"),
            os.path.join(root, "data", "model-routing.json"),
        ])
        config_path = next((c for c in candidates if os.path.exists(c)), candidates[-1])
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)

        overrides = cfg.get("overrides", {})
        tier_map = {"light": "haiku", "standard": "sonnet", "heavy": "sonnet"}
        tier_map.update(cfg.get("platforms", {}).get("claude_code", {}).get("tier_map", {}) or {})

        proxy_raw = cfg.get("proxy_fallback_aliases", {})
        proxy = {k: v for k, v in proxy_raw.items() if not k.startswith("$")}

        skill_cfg = cfg.get("skill_routing", {})
        skill_map = {}
        for tier in ("light", "standard", "heavy"):
            for skill_name in skill_cfg.get(tier, []):
                skill_map[skill_name] = tier

        # Build DISPATCH RULE from agent_routing + tier_aliases + proxy_intercepted_models.
        dispatch_rule = _build_dispatch_rule(cfg)

        # --- Build pricing from tier_definitions ---
        # Start with Anthropic defaults, then add/override from tier_definitions
        pricing_from_config = dict(_PRICING_DEFAULT)
        tier_defs = cfg.get("tier_definitions", {})
        for tier_name, tier_def in tier_defs.items():
            if isinstance(tier_def, dict) and not tier_name.startswith("$"):
                in_rate = tier_def.get("cost_per_m_input")
                out_rate = tier_def.get("cost_per_m_output")
                if in_rate is not None and out_rate is not None:
                    pricing_from_config[tier_name] = {"in": float(in_rate), "out": float(out_rate)}
                    # Also key by model ID so transcript lookups match actual model strings
                    tier_model_id = tier_def.get("model")
                    if tier_model_id:
                        pricing_from_config[tier_model_id] = {"in": float(in_rate), "out": float(out_rate)}

        # --- Build proxy_intercept_rates from proxy_intercepted_models ---
        # Map: model_id -> actual backend tier -> get rates from tier_definitions
        proxy_intercept_rates = {}
        proxy_models = cfg.get("proxy_intercepted_models", {})

        # Build reverse lookup: tier model name -> (in, out) rates
        tier_model_to_rates = {}
        for tier_name, tier_def in tier_defs.items():
            if isinstance(tier_def, dict) and not tier_name.startswith("$"):
                model = tier_def.get("model")
                in_rate = tier_def.get("cost_per_m_input")
                out_rate = tier_def.get("cost_per_m_output")
                if model and in_rate is not None and out_rate is not None:
                    tier_model_to_rates[model] = {"in": float(in_rate), "out": float(out_rate)}

        # For each proxy-intercepted model, resolve its backend tier and copy rates
        for model_id, model_cfg in proxy_models.items():
            if isinstance(model_cfg, dict) and not model_id.startswith("$"):
                # Explicit per-alias pricing wins — needed when the alias resolves
                # to a model that has no tier_definitions entry (e.g. sonnet/haiku
                # on DeepSeek V4, which no tier routes to).
                in_rate = model_cfg.get("cost_per_m_input")
                out_rate = model_cfg.get("cost_per_m_output")
                if in_rate is not None and out_rate is not None:
                    proxy_intercept_rates[model_id] = {"in": float(in_rate), "out": float(out_rate)}
                    continue
                resolves_to = model_cfg.get("resolves_to", "")
                # Match by finding which tier model path appears as a substring of resolves_to.
                # resolves_to format: "provider:model_path [optional annotations]"
                # Substring match handles both full paths ("accounts/fireworks/models/minimax-m2p7")
                # and short aliases without requiring exact format parsing.
                for tier_model_path, rates in tier_model_to_rates.items():
                    if tier_model_path in resolves_to:
                        proxy_intercept_rates[model_id] = rates
                        break

        # Rate parity check: warn if proxy rates differ from hardcoded _PRICING_DEFAULT by >10%.
        # Opt-in via CLASSIFY_PRICING_PARITY_CHECK=1 (mirrors manolii master, hook audit
        # 2026-06-12): OSS-routed aliases legitimately cost far less than the Anthropic
        # rates in _PRICING_DEFAULT, so an always-on warning is per-invocation noise.
        for model_id in ["haiku", "claude-haiku-4-5-20251001"] if os.environ.get(
            "CLASSIFY_PRICING_PARITY_CHECK"
        ) == "1" else []:
            if model_id in proxy_intercept_rates and model_id in _PRICING_DEFAULT:
                proxy_out = proxy_intercept_rates[model_id].get("out", 0.0)
                hardcoded_out = _PRICING_DEFAULT[model_id]["out"]
                if hardcoded_out > 0 and abs(proxy_out - hardcoded_out) / hardcoded_out > 0.10:
                    print(
                        f"classify-message: {model_id} proxy rate parity drift — "
                        f"proxy: ${proxy_out:.2f}/M, pricing dict: ${hardcoded_out:.2f}/M. "
                        f"Update _MODEL_PRICING.",
                        file=sys.stderr
                    )

        return (
            overrides.get("escalate_to_heavy", _HEAVY_DEFAULT),
            overrides.get("escalate_to_standard", _STANDARD_DEFAULT),
            overrides.get("deescalate_to_light", _LIGHT_DEFAULT),
            tier_map,
            proxy or _PROXY_DEFAULT,
            skill_map,
            dispatch_rule,
            pricing_from_config,
            proxy_intercept_rates,
        )
    except Exception as e:
        print(f"classify-message: failed to load model-routing.json, using defaults: {e}", file=sys.stderr)
        return (_HEAVY_DEFAULT, _STANDARD_DEFAULT, _LIGHT_DEFAULT,
                _TIER_MAP_DEFAULT, _PROXY_DEFAULT, {}, _DISPATCH_RULE_DEFAULT,
                _PRICING_DEFAULT, _PROXY_INTERCEPT_RATES_DEFAULT)


HEAVY_KW, STANDARD_KW, LIGHT_KW, TIER_MODEL_MAP, PROXY_FALLBACK, SKILL_TIER_MAP, DISPATCH_RULE, _MODEL_PRICING, _PROXY_INTERCEPT_RATES = _load_routing_config()
PROXY_DOWN = os.environ.get("LITELLM_FALLBACK_MODE") == "1"

# --- Daily spend circuit breaker ---
_SOFT_LIMIT_USD = 20.0   # nudge → haiku/OSS
_HARD_LIMIT_USD = 35.0   # force → haiku/OSS + strong warning

# Default pricing fallback
_DEFAULT_PRICING: dict[str, float] = {"in": 3.00, "out": 15.00}


_SPEND_CACHE_DIR = Path(__file__).parent.parent / ".ai" / "spend-cache"


def _compute_session_spend(transcript_path: str | None) -> float:
    """Sum token costs incrementally using per-session sidecar cache files.

    Each transcript gets its own file in .ai/spend-cache/<sha256>.json so
    concurrent sessions never race on a shared file. Binary I/O ensures
    tell()/seek() are true byte offsets, safe to compare with st_size and
    persist across processes. Partial JSONL lines (mid-write appends) are
    detected via the missing newline sentinel and excluded from the offset
    so the bytes are re-read once the write completes.

    Sub-agent transcripts are read from {main_transcript_stem}/subagents/*.jsonl
    and their costs are accumulated with proxy-aware pricing applied.
    """
    if not transcript_path:
        return 0.0
    try:
        p = Path(transcript_path)
        if not p.exists():
            return 0.0

        # Per-session sidecar: one file per transcript, no shared state
        cache_key = hashlib.sha256(str(p.resolve()).encode()).hexdigest()
        cache_path = _SPEND_CACHE_DIR / f"{cache_key}.json"

        cache_entry: dict = {}
        try:
            with cache_path.open() as cf:
                loaded = json.load(cf)
                if isinstance(loaded, dict):
                    cache_entry = loaded
        except FileNotFoundError:
            pass  # expected on first run — no cache yet
        except Exception as e:
            print(f"classify-message: spend-cache read failed: {type(e).__name__}: {e}",
                  file=sys.stderr)

        try:
            offset = int(cache_entry.get("offset", 0))
            total = float(cache_entry.get("total", 0.0))
            if offset < 0 or total < 0:
                raise ValueError("negative spend-cache values")
        except (TypeError, ValueError) as e:
            print(f"classify-message: spend-cache invalid, resetting: {type(e).__name__}: {e}",
                  file=sys.stderr)
            offset = 0
            total = 0.0

        # Detect proxy_active from cache or environment
        proxy_active = cache_entry.get("proxy_active")
        if proxy_active is None:
            # First time: detect from ANTHROPIC_BASE_URL
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
            proxy_active = bool(base_url) and "anthropic.com" not in base_url
        proxy_active = bool(proxy_active)

        # Reset if transcript was truncated (e.g. compaction rewrote the file).
        # Binary-mode offsets equal byte counts, so this comparison is valid.
        if p.stat().st_size < offset:
            offset = 0
            total = 0.0

        # Helper function to process a transcript JSONL and accumulate costs
        def _process_transcript(fpath: Path, is_subagent: bool = False,
                                cache_entry_sub: dict | None = None,
                                initial_total: float = 0.0) -> tuple[int, float, str]:
            """Process a transcript and return (new_offset, accumulated_cost, last_model)."""
            nonlocal proxy_active

            if cache_entry_sub is None:
                cache_entry_sub = {}

            offset_sub = int(cache_entry_sub.get("offset", 0))
            # initial_total is passed in — don't derive from cache_entry_sub here,
            # because the main sidecar uses "total" while sub-agent entries use "offset_total".
            total_sub = initial_total
            model_sub = cache_entry_sub.get("model", "")

            if fpath.stat().st_size < offset_sub:
                offset_sub = 0
                total_sub = 0.0

            new_offset_sub = offset_sub
            with fpath.open("rb") as f:
                f.seek(offset_sub)
                while True:
                    raw_line = f.readline()
                    if not raw_line:
                        break
                    stripped = raw_line.strip()
                    if not stripped:
                        new_offset_sub = f.tell()
                        continue
                    try:
                        entry = json.loads(stripped.decode("utf-8"))
                        new_offset_sub = f.tell()
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        if raw_line.endswith(b"\n"):
                            new_offset_sub = f.tell()
                        else:
                            break
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    msg = entry.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict) or not usage:
                        continue

                    # Rate lookup with proxy awareness
                    model = msg.get("model", "")
                    model_sub = model
                    if proxy_active and model in _PROXY_INTERCEPT_RATES:
                        rates = _PROXY_INTERCEPT_RATES[model]
                    else:
                        rates = _MODEL_PRICING.get(model, _DEFAULT_PRICING)

                    in_tok       = usage.get("input_tokens", 0) or 0
                    out_tok      = usage.get("output_tokens", 0) or 0
                    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
                    cache_read   = usage.get("cache_read_input_tokens", 0) or 0
                    total_sub += (
                        in_tok       * rates["in"]
                        + out_tok    * rates["out"]
                        + cache_create * rates["in"] * 1.25
                        + cache_read   * rates["in"] * 0.10
                    ) / 1_000_000

            return new_offset_sub, total_sub, model_sub

        # Process main transcript
        new_offset, total, _ = _process_transcript(p, is_subagent=False, cache_entry_sub=cache_entry, initial_total=total)

        # Capture Anthropic-direct sub-agent costs (proxy-routed agents go to Langfuse instead)
        # Sub-agent transcripts: {main_transcript_stem}/subagents/*.jsonl
        subagents_dir = p.parent / p.stem / "subagents"
        subagents_cache: dict[str, dict] = cache_entry.get("subagents", {})

        subagents_changed = False
        if subagents_dir.is_dir():
            for sa_path in sorted(subagents_dir.glob("*.jsonl")):
                sa_key = hashlib.sha256(str(sa_path.resolve()).encode()).hexdigest()
                sa_cache = subagents_cache.get(sa_key, {})
                sa_prior_total = float(sa_cache.get("offset_total", 0.0))
                sa_new_offset, sa_total, sa_model = _process_transcript(
                    sa_path, is_subagent=True, cache_entry_sub=sa_cache, initial_total=sa_prior_total
                )

                # Update the cache entry for this sub-agent
                subagents_cache[sa_key] = {
                    "offset": sa_new_offset,
                    "offset_total": sa_total,
                    "model": sa_model,
                }
                # Only add the incremental cost since last cache read to avoid double-counting
                total += max(0.0, sa_total - sa_prior_total)
                if sa_new_offset > sa_cache.get("offset", 0):
                    subagents_changed = True

        # Persist cache atomically only when new data was parsed
        if new_offset > offset or subagents_changed:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.parent / f".{cache_key}.{os.getpid()}.tmp"
                sidecar_data = {
                    "schema_version": 2,
                    "proxy_active": proxy_active,
                    "offset": new_offset,
                    "total": total,
                }
                if subagents_cache:
                    sidecar_data["subagents"] = subagents_cache
                with tmp.open("w") as cf:
                    json.dump(sidecar_data, cf, sort_keys=True)
                tmp.replace(cache_path)
            except Exception as e:
                print(f"classify-message: spend-cache write failed: {type(e).__name__}: {e}",
                      file=sys.stderr)

        return total
    except Exception as e:
        print(f"classify-message: failed to compute session spend from {transcript_path!r}: {e}",
              file=sys.stderr)
        return 0.0

# Detect /skill invocations at the start of a prompt.
# Matches: "/remember ...", "/plan-review ...", "remember ...", "/qa"
_SKILL_PREFIX_RE = re.compile(r"^/([a-z][a-z0-9_-]*)\b", re.I)


def _detect_skill_tier(prompt_text: str) -> tuple[str, str] | None:
    """If the prompt starts with a known skill name, return (tier, skill_name).

    Returns None if no skill match — caller should fall through to keyword heuristics.
    """
    m = _SKILL_PREFIX_RE.match(prompt_text.strip())
    if not m:
        return None
    candidate = m.group(1).lower()
    tier = SKILL_TIER_MAP.get(candidate)
    if tier:
        return tier, candidate
    return None


def _whole_word(text, keyword):
    """Whole-word match to avoid partial hits (e.g. 'plan' in 'explain')."""
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None


# --- Compound phrase detection for long-form generation tasks ---
# Single-word matching ("generate", "write", "create") produces too many false positives
# (e.g. "generate boilerplate", "generate types"). These patterns require a generation
# verb paired with a substantial output noun, or an explicit complexity qualifier.
_GENERATION_PHRASE_RE = re.compile(
    r"\b(generate|write|create|produce|draft|compose)\b.{0,50}"
    r"\b(report|profile|analysis|assessment|document|narrative|essay|article|brief|overview|biography|summary)\b"
    r"|"
    r"\b(comprehensive|detailed|multi-system|in-depth|full|complete|thorough)\b.{0,40}"
    r"\b(report|profile|analysis|assessment|document|review|overview|breakdown)\b"
    r"|"
    r"\b(personality\s+report|personality\s+profile|multi-system\s+personality)\b",
    re.I | re.DOTALL,
)

# --- Orchestrator dispatch signal ---
# Detects true-parallel multi-repo tasks that warrant orchestrator routing.
# Fires ONLY when 3+ distinct action verbs appear alongside parallel scope indicators
# (different repos, concurrent execution signals). Sequential tasks must NOT fire this —
# they should use executor+advisor on the main thread instead.
_ORCHESTRATOR_VERBS = re.compile(
    r"\b(investigate|analyse|analyze|check|fix|implement|read|write|update|review|"
    r"search|find|verify|validate|compare|generate|create|refactor|test|audit|"
    r"deploy|migrate|diagnose|report|scan|extract|run)\b",
    re.I,
)
_MULTI_SCOPE_INDICATORS = re.compile(
    r"\b(across\s+repos?|multi.repo|cross.repo|in\s+parallel|"
    r"each\s+(?:repo|service|agent|file)|all\s+(?:repos?|services?|agents?)|"
    r"simultaneously|at\s+the\s+same\s+time|concurrently|"
    r"agent\s+\d|agents?\s+\d|\d+\s+agents?)\b",
    re.I,
)


def _needs_orchestrator(prompt_text: str) -> bool:
    """Return True when prompt has 3+ action verbs AND true-parallel multi-scope indicators.

    Sequential tasks (read-then-modify, step-by-step) must NOT match this function.
    Orchestrator is reserved for tasks where 2+ steps can run concurrently.
    """
    verbs = set(m.group(1).lower() for m in _ORCHESTRATOR_VERBS.finditer(prompt_text))
    if len(verbs) < 3:
        return False
    return bool(_MULTI_SCOPE_INDICATORS.search(prompt_text))


# --- Stream-checkpoint signal ---
# Detects prompts that are likely to trigger >5 sequential Bash/MCP tool calls on the
# main thread without Agent isolation. These are the patterns that cause stream idle
# timeouts (no configurable Claude Code threshold — fixed ~30s client-side).
# Emits [STREAM-CHECKPOINT] as an advisory in the model-routing block.
_MULTI_REPO_OPS = re.compile(
    r"\b(create\s+PRs?|push\s+(?:all|to\s+all)|fix\s+all|apply\s+to\s+all|"
    r"update\s+all|sync\s+(?:all|across)|across\s+all\s+repos?|for\s+all\s+repos?|"
    r"all\s+repos?|advisor.0[0-9]|advisor\s+PR|each\s+repo|"
    r"routing[- ]sync|sync[- ]routing|deploy[- ]all|migrate[- ]all|"
    r"run\s+autonomously|run\s+all|apply.*every|every.*repo)\b",
    re.I,
)
_SEQUENTIAL_TOOL_SIGNALS = re.compile(
    r"\b(then\s+(?:push|commit|check|verify|run|fetch|pull)|"
    r"(?:check|verify|fix)\s+(?:each|every|all)|"
    r"step\s+by\s+step|one\s+by\s+one|sequentially|in\s+order|"
    r"for\s+each\s+(?:repo|file|service)|iterate\s+(?:over|through))\b",
    re.I,
)


def _needs_stream_checkpoint(prompt_text: str) -> bool:
    """Return True when the prompt is likely to trigger >5 sequential Bash/MCP calls."""
    if _MULTI_REPO_OPS.search(prompt_text):
        return True
    # Sequential-tool signal + verb count ≥ 4 (multi-step plan)
    if _SEQUENTIAL_TOOL_SIGNALS.search(prompt_text):
        verbs = set(m.group(1).lower() for m in _ORCHESTRATOR_VERBS.finditer(prompt_text))
        return len(verbs) >= 4
    return False


# --- Continuation/retry detection ---
# Short retry messages in an ongoing session should not downgrade a heavy task to light.
# Heuristic: if the entire prompt is ≤12 words AND contains no explicit task keywords
# (i.e. nothing that would independently justify a tier assignment), treat as continuation
# and floor the tier at standard. This catches "Try again", "ok continue", "carry on",
# "Rebase and try again", "try this in smaller chunks", etc. without requiring an exact
# phrase match that breaks on minor variations.
_CONTINUATION_KEYWORDS = re.compile(
    r"\b(try\s+again|try\s+this|continue|keep\s+going|carry\s+on|go\s+ahead|proceed|"
    r"next\s+section|from\s+where\s+(you|we)\s+left|finish\s+it|complete\s+it|rebase|"
    r"same\s+again|do\s+it\s+again|move\s+on|next\s+part|keep\s+writing|go\s+on|"
    r"in\s+smaller\s+chunks|in\s+chunks|chunk\s+it|break\s+it\s+(up|down))\b",
    re.I,
)

def _is_continuation(prompt_text: str) -> bool:
    """Return True if the prompt looks like a short retry/continuation with no new task context."""
    words = prompt_text.strip().split()
    if len(words) > 15:
        return False  # Long enough to contain real task context
    return bool(_CONTINUATION_KEYWORDS.search(prompt_text))


def classify_tier(prompt_text: str) -> dict:
    """Classify prompt into light/standard/heavy tier.

    Precedence:
      1. Skill routing (deterministic — /skill-name prefix)
      2. Continuation detection (short retry → floor at standard)
      3. Long-form generation phrase detection (→ heavy)
      4. Single-keyword heuristics (heavy > standard > light)
    Returns dict with tier, model, matched_keywords, and optional skill field.
    """
    # 1. Skill routing — deterministic override, bypasses all heuristics
    skill_match = _detect_skill_tier(prompt_text)
    if skill_match:
        tier, skill_name = skill_match
        model = TIER_MODEL_MAP.get(tier, tier)
        return {"tier": tier, "model": model, "matched_keywords": [], "skill": skill_name}

    # 2. Continuation detection — short retry/carry-on messages should not downgrade
    #    an ongoing heavy task. Floor at standard so we don't send haiku for "try again".
    if _is_continuation(prompt_text):
        model = TIER_MODEL_MAP.get("standard", "sonnet")
        return {"tier": "standard", "model": model, "matched_keywords": ["[continuation]"]}

    # 3. Long-form generation phrase detection — must precede single-keyword heuristics
    #    because "write", "generate", "create" are too broad as standalone words.
    if _GENERATION_PHRASE_RE.search(prompt_text):
        model = TIER_MODEL_MAP.get("heavy", "sonnet")
        return {"tier": "heavy", "model": model, "matched_keywords": ["[generation-phrase]"]}

    # 4. Keyword heuristics — highest precedence wins, skip lower tiers once matched.
    # Short-circuit: as soon as a heavy keyword is found, stop scanning — there is no
    # value in also checking standard/light keywords once the tier is already heavy.
    lower = prompt_text.lower()
    heavy_matches = []
    for k in HEAVY_KW:
        if _whole_word(lower, k):
            heavy_matches.append(k)
            break  # First heavy match is enough — skip remaining heavy + all standard/light
    if heavy_matches:
        tier = "heavy"
        matched = heavy_matches
    else:
        standard_matches = []
        for k in STANDARD_KW:
            if _whole_word(lower, k):
                standard_matches.append(k)
                break  # First standard match is enough
        if standard_matches:
            tier = "standard"
            matched = standard_matches
        else:
            matched = [k for k in LIGHT_KW if _whole_word(lower, k)]
            tier = "light"  # default (matches agent_routing.default in model-routing.json)

    model = TIER_MODEL_MAP.get(tier, tier)
    return {"tier": tier, "model": model, "matched_keywords": matched}


# --- Pre-flight context injection helpers ---
# Repo root — same derivation used above for config_path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Match script paths including kebab-case names like "scripts/post-tool-use.py"
_PY_SCRIPT_RE = re.compile(r'\bscripts/([a-z0-9_/-]+)\.py\b', re.I)
_MIGRATION_SIGNAL_RE = re.compile(r'migrations/|\b(?:migration|migrate|\.sql)\b', re.I)
_AGENT_FILE_RE = re.compile(r'\.claude/(agents|skills)/[a-z_-]', re.I)


def _preflight_hints(prompt_text: str) -> list[str]:
    """Return coding-constraint hints based on what the prompt is about to do.

    Scans for file references and keyword signals then emits targeted reminders.
    Kept fast (no subprocess, only os.path.exists) — always completes within 1ms.
    """
    hints: list[str] = []

    # SQL migration reminder — catches "migration", ".sql", "migrations/"
    if _MIGRATION_SIGNAL_RE.search(prompt_text):
        hints.append(
            "Migration checklist: wrap in BEGIN/COMMIT, use IF NOT EXISTS/IF EXISTS "
            "guards, include a down-migration file for any DROP operation."
        )

    # Test file hints for Python scripts explicitly mentioned in the prompt
    py_refs = _PY_SCRIPT_RE.findall(prompt_text)
    seen: set[str] = set()
    for ref in py_refs[:3]:          # cap at 3 to avoid noise on large prompts
        name = os.path.basename(ref).replace("-", "_")
        if name in seen:
            continue
        seen.add(name)
        for candidate in (
            f"scripts/tests/test_{name}.py",
            f"scripts/tests/{name}_test.py",
        ):
            if os.path.exists(os.path.join(_REPO_ROOT, candidate)):
                hints.append(f"Existing test file: {candidate} — update it if logic changes.")
                break

    # Agent/skill file hint — routing lint must run after any agent edit
    if _AGENT_FILE_RE.search(prompt_text):
        hints.append(
            "Agent/skill file being modified: run "
            "`python3 scripts/lint-agent-routing.py` after editing."
        )

    return hints


# --- Sensitive content signals — route to Anthropic-only (no OSS proxy) ---
SENSITIVE_SIGNALS = re.compile(
    r"\b(TFN|SSN|passport|date.of.birth|DOB|bank.account|credit.card|"
    r"api.?key|secret.?key|private.?key|password|credentials)\b", re.I
)


def classify(prompt_text: str) -> dict:
    """Classify a user message and return structured hints."""
    categories = []
    routing = "local"  # default
    suggestions = []

    # Content type detection
    for category, pattern in CONTENT_PATTERNS.items():
        if pattern.search(prompt_text):
            categories.append(category)

    # Routing: sensitive content → restricted (Anthropic-only, no OSS proxy)
    if SENSITIVE_SIGNALS.search(prompt_text):
        routing = "restricted"

    # Suggestions based on detected content
    if "decision" in categories:
        suggestions.append("Consider /remember with type:decision")
    if "gotcha" in categories:
        suggestions.append("Consider /remember with type:gotcha or /learn")
    if "incident" in categories:
        suggestions.append("Consider /diagnose if this is a recurring issue")

    return {
        "categories": categories,
        "routing": routing,
        "suggestions": suggestions,
    }


def main():
    try:
        data = json.loads(sys.stdin.read())
        prompt_text = data.get("prompt", "")

        if not prompt_text:
            sys.exit(0)
        # Size guard: task-type keywords always appear in the opening lines.
        # Long data-heavy prompts (planetary tables, large task briefs) contain no
        # classification signal past the first ~3000 chars and trigger expensive
        # re.DOTALL regex scanning over tens of KB of irrelevant text.
        # Truncating here keeps the hook well within its 10-second budget even on
        # very large prompts (100KB+), while preserving 100% classification accuracy.
        _CLASSIFY_MAX_CHARS = 3000
        classify_text = prompt_text[:_CLASSIFY_MAX_CHARS]

        tier_result = classify_tier(classify_text)
        _has_short_signal = (
            bool(tier_result["matched_keywords"])
            or tier_result.get("skill") is not None
            or SENSITIVE_SIGNALS.search(classify_text) is not None
            or any(p.search(classify_text) for p in CONTENT_PATTERNS.values())
        )
        if len(prompt_text) < 10 and not _has_short_signal:
            sys.exit(0)

        result = classify(classify_text)
        # tier_result already computed above (same bounded input window)

        lines = []

        # Sensitive content lock: if kl-only routing is active, the prompt contains
        # PII or client data that must NOT reach OSS providers via the proxy.
        # Override haiku (which the proxy remaps to DeepSeek) with sonnet (Anthropic-only).
        tier = tier_result["tier"]
        model = tier_result["model"]
        is_sensitive = result["routing"] == "restricted"
        if is_sensitive and model == "haiku":
            model = "sonnet"
            tier = "standard"

        # Spend visibility — surface session cost as context only. No model override.
        # The routing system already selects the cheapest model appropriate for each task;
        # overriding to haiku based on cumulative spend would degrade quality on tasks that
        # genuinely need opus/sonnet, potentially causing re-runs that cost more overall.
        # Trust the routing to stay efficient — these notes inform, they do not redirect.
        transcript_path = data.get("transcript_path")
        session_spend = _compute_session_spend(transcript_path)
        _circuit_note: str | None = None
        if session_spend >= _HARD_LIMIT_USD:
            _circuit_note = (
                f"SPEND ALERT: session spend ${session_spend:.2f} >= ${_HARD_LIMIT_USD:.0f}. "
                "Routing continues as normal — verify task complexity warrants the selected tier."
            )
        elif session_spend >= _SOFT_LIMIT_USD:
            _circuit_note = (
                f"SPEND NOTE: session spend ${session_spend:.2f} >= ${_SOFT_LIMIT_USD:.0f}. "
                "Routing continues as normal."
            )

        # Always emit model-routing block — orchestrator needs this to select
        # the correct model parameter when spawning sub-agents.
        kw = tier_result["matched_keywords"]
        skill = tier_result.get("skill")
        if skill:
            reason_str = f" (skill: /{skill})"
        elif kw:
            reason_str = f" (keywords: {', '.join(kw)})"
        else:
            reason_str = " (default)"
        if is_sensitive:
            reason_str += " [sensitive: Anthropic-only]"

        lines.append("<model-routing>")
        lines.append(f"Suggested tier: {tier} → model: {model}{reason_str} [MAIN THREAD ONLY]")
        if _circuit_note:
            lines.append(_circuit_note)
        # Orchestrator routing signal: emitted when the prompt has 3+ action verbs + multi-scope
        # indicators. This is the automatic gate missing from the original orchestration design —
        # it converts ad-hoc dispatch decisions into a structured signal the main thread can act on.
        if _needs_orchestrator(prompt_text):
            lines.append(
                "[ORCHESTRATE] 3+ action verbs + multi-scope → "
                "Agent(subagent_type=\"orchestrator\", model=\"sonnet\"). "
                "Single broad-agent dispatch will likely timeout."
            )
        # Also fires for large heavy-tier prompts. These involve orientation reads +
        # advisor() + large Write calls, with extended reasoning between each step.
        # The ~30s stream idle timer can expire during the think-before-write phase
        # even when Bash/MCP call count is below the sequential-tool threshold.
        _is_large_heavy = tier == "heavy" and len(prompt_text) >= 1500
        _checkpoint_emitted = False
        if _needs_stream_checkpoint(prompt_text) or _is_large_heavy:
            _checkpoint_emitted = True
            lines.append(
                "[STREAM-CHECKPOINT] This prompt may trigger >5 sequential Bash/MCP tool calls "
                "on the main thread. No configurable streamIdleTimeout exists — the Claude Code "
                "client kills streams idle for ~30s. Required: (1) batch independent tool calls "
                "in parallel; (2) isolate heavy work in Agent sub-agents (each has its own stream); "
                "(3) write a checkpoint to .ai/sessions/active-task.json after each phase so the "
                "turn can resume after timeout without replaying completed steps. "
                "For large generation tasks specifically: write a skeleton/stub to disk BEFORE "
                "calling advisor() — this keeps the stream alive and makes partial work durable. "
                "See CLAUDE.md § Stream Idle Timeout Mitigation."
            )
        # Active-task checkpoint: fires on short continuation prompts when an interrupted
        # large-write task is in progress. Covers the timeout pattern: user says "continue"
        # (light-tier, <1500 chars), Claude goes into extended think-before-Write, stream dies.
        if not _checkpoint_emitted and _is_continuation(prompt_text):
            _at_path = os.path.join(_REPO_ROOT, ".ai", "sessions", "active-task.json")
            try:
                with open(_at_path, encoding="utf-8") as _atf:
                    _at = json.load(_atf)
                _active_id = _at.get("active_step_id")
                _at_steps = _at.get("steps", [])
                # Keywords narrow to large document generation — "commit" excluded because
                # it matches virtually every dev task and produces false positives.
                _WRITE_TASK_KW = ("write", "document", "report")
                _active_step = next(
                    (
                        step for step in _at_steps
                        if isinstance(step, dict)
                        and step.get("id") == _active_id
                        and step.get("status") == "in_progress"
                    ),
                    None,
                )
                _active_desc_full = str((_active_step or {}).get("description") or "")
                _active_label = _active_desc_full.splitlines()[0] if _active_desc_full else "unknown"
                _at_has_write = any(
                    kw in _active_desc_full.lower() for kw in _WRITE_TASK_KW
                )
                if _at_has_write:
                    _pending_count = sum(
                        1
                        for s in _at_steps
                        if isinstance(s, dict) and s.get("status") in ("pending", "in_progress")
                    )
                    lines.append(
                        f"[ACTIVE-TASK] Interrupted write task '{_active_label}' "
                        f"detected ({_pending_count} pending steps). "
                        "Do NOT compose >1000 words inline — stream times out during extended "
                        "thinking before Write calls. Required: delegate to "
                        "Agent(subagent_type='generate', model='haiku') with OUTPUT_FILE spec, OR write a "
                        "skeleton stub first, then fill sections with sequential Edit calls."
                    )
            except FileNotFoundError:
                pass
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                AttributeError,
            ) as _at_err:
                print(
                    f"classify-message: active-task checkpoint unreadable "
                    f"({_at_path}): {type(_at_err).__name__}: {_at_err}",
                    file=sys.stderr,
                )
        # Sub-agent dispatch rule — always emitted so the orchestrator selects
        # sub-agent models by sub-task type, not by the main-thread tier.
        # The line above is for the main thread; sub-agents have independent rules below.
        # Critical: opus is MAIN THREAD ONLY. haiku-tier agents use haiku; sonnet-tier use sonnet.
        lines.append(DISPATCH_RULE)
        if PROXY_DOWN:
            subs = ", ".join(f"{k} → {v}" for k, v in PROXY_FALLBACK.items())
            lines.append(
                "PROXY DOWN: LiteLLM proxy is unreachable. Do NOT use tier aliases "
                "as Agent model parameters — they will fail. Substitute: " + subs
            )
        lines.append("</model-routing>")

        # Pre-flight context hints — emitted only when relevant signals are found.
        # Injected before model-routing so they appear at the top of additionalContext.
        _hints = _preflight_hints(prompt_text)
        if _hints:
            _pf = ["<pre-flight-hints>"]
            _pf.extend(f"• {h}" for h in _hints)
            _pf.append("</pre-flight-hints>")
            # Prepend so hints appear before routing block in additionalContext
            lines = _pf + lines

        # Content classification: log to stderr for observability only — not injected into context.
        if result["categories"] or result["routing"] != "local":
            cc_lines = ["<content-classification>"]
            if result["categories"]:
                cc_lines.append(f"Categories: {', '.join(result['categories'])}")
            cc_lines.append(f"Routing: {result['routing']}")
            for s in result["suggestions"]:
                cc_lines.append(f"Suggestion: {s}")
            cc_lines.append("</content-classification>")
            print("\n".join(cc_lines), file=sys.stderr)

        print("\n".join(lines))

    except Exception as e:
        print(f"classify-message: unhandled exception: {type(e).__name__}: {e}\n{traceback.format_exc()}",
              file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
