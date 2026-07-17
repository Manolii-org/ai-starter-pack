#!/usr/bin/env python3
"""Session cost logger — appends per-session token usage to .ai/metrics/session-costs.jsonl.

Invoked by the Stop hook (session-stop-checklist.sh) after every session.
Reads the active session JSONL from ~/.claude/projects/<project>/<session-id>.jsonl,
sums all usage blocks (deduped by uuid), calculates cost at current Anthropic pricing,
and appends one record to .ai/metrics/session-costs.jsonl.

Also fetches session-scoped Langfuse proxy spend if LANGFUSE_* env vars are available,
using the session's first_turn/last_turn timestamps to filter observations.

Usage:
  python3 scripts/session-cost-logger.py                  # auto-detect session
  python3 scripts/session-cost-logger.py --session <id>   # specific session ID
  python3 scripts/session-cost-logger.py --summary        # print last 7 days
"""
import json
import os
import sys
import argparse
import warnings
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

def _resolve_repo_root() -> Path:
    env = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    if env:
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / ".git").exists() or (cwd / ".ai").exists():
        return cwd
    return Path(__file__).resolve().parent.parent

REPO_ROOT = _resolve_repo_root()
LOG_PATH = REPO_ROOT / ".ai" / "metrics" / "session-costs.jsonl"

# Anthropic pricing (claude-sonnet-4-6, as of 2026-04-19).
# Retained for the cache-savings display in main(); per-message pricing now
# flows through _main_rates() so an Opus main thread is no longer mispriced.
PRICING = {
    "input":        3.00 / 1_000_000,
    "cache_write":  3.75 / 1_000_000,
    "cache_read":   0.30 / 1_000_000,
    "output":      15.00 / 1_000_000,
}

# Anthropic native list price per million tokens, keyed by model. Cache write =
# 1.25x input, cache read = 0.10x input (Anthropic pricing). Unknown/empty model
# falls back to Sonnet rates (the historical default).
_MAIN_PER_M = {
    "claude-opus-4-8":           {"in": 5.00, "out": 25.00},
    "claude-opus-4-7":           {"in": 5.00, "out": 25.00},
    "claude-opus-4-6":           {"in": 5.00, "out": 25.00},
    "claude-sonnet-4-6":         {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5-20251001": {"in": 0.30, "out":  1.20},
    "claude-haiku-4-5":          {"in": 0.30, "out":  1.20},
}
_DEFAULT_MAIN_MODEL = "claude-sonnet-4-6"


def _main_rates(model: str) -> dict:
    """Per-token Anthropic rates for a main-thread model (with cache tiers)."""
    base = _MAIN_PER_M.get(model) or _MAIN_PER_M[_DEFAULT_MAIN_MODEL]
    i = base["in"] / 1_000_000
    return {
        "input":       i,
        "cache_write": i * 1.25,
        "cache_read":  i * 0.10,
        "output":      base["out"] / 1_000_000,
    }


def find_latest_session(project_path: Path) -> Path | None:
    """Return the most recently modified .jsonl in the project directory."""
    jsonl_files = list(project_path.glob("*.jsonl"))
    if not jsonl_files:
        return None
    return max(jsonl_files, key=lambda p: p.stat().st_mtime)


def parse_session(session_file: Path) -> dict:
    """Parse session JSONL and return aggregated token counts + per-model cost.

    Cost is computed per message at that message's model rate (via _main_rates),
    so an Opus main thread is no longer mispriced as Sonnet — the prior flat-rate
    bug that undercounted Opus turns by ~1.6x.
    """
    totals = defaultdict(int)
    seen = set()
    first_ts = last_ts = None
    cost = 0.0
    by_model: dict[str, int] = defaultdict(int)

    for line in session_file.read_text(errors="replace").splitlines():
        if '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = entry.get("message", {})
        usage = msg.get("usage")
        if not usage:
            continue

        # Deduplicate: same usage object appears in multiple event types per turn
        _uuid = entry.get("uuid")
        if _uuid:
            dedup_key = f"uuid:{_uuid}"
        else:
            _ts = entry.get("timestamp") or msg.get("timestamp") or ""
            dedup_key = f"fallback:{_ts}:{json.dumps(usage, sort_keys=True)}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        inp = usage.get("input_tokens", 0)
        cw  = usage.get("cache_creation_input_tokens", 0)
        cr  = usage.get("cache_read_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        totals["input_tokens"]                += inp
        totals["cache_creation_input_tokens"] += cw
        totals["cache_read_input_tokens"]     += cr
        totals["output_tokens"]               += out
        totals["turns"]                       += 1

        rates = _main_rates(msg.get("model", ""))
        cost += (
            inp * rates["input"] + cw * rates["cache_write"] +
            cr * rates["cache_read"] + out * rates["output"]
        )
        by_model[msg.get("model", "") or "unknown"] += 1

        ts_str = entry.get("timestamp") or msg.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            except ValueError as e:
                print(f"[session-cost-logger] Invalid timestamp '{ts_str}': {e}", file=sys.stderr)

    return {
        "turns":               totals["turns"],
        "input_tokens":        totals["input_tokens"],
        "cache_write_tokens":  totals["cache_creation_input_tokens"],
        "cache_read_tokens":   totals["cache_read_input_tokens"],
        "output_tokens":       totals["output_tokens"],
        "main_thread_cost":    round(cost, 4),
        "main_models":         dict(by_model),
        "first_turn":          first_ts.isoformat() if first_ts else None,
        "last_turn":           last_ts.isoformat() if last_ts else None,
    }


_SUBAGENT_PRICING: dict[str, dict] = {
    # Anthropic-direct sub-agents (full model ID bypasses proxy)
    "claude-sonnet-4-6":       {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},
    "claude-opus-4-7":         {"input": 5.00 / 1_000_000, "output": 25.00 / 1_000_000},
    "claude-opus-4-6":         {"input": 5.00 / 1_000_000, "output": 25.00 / 1_000_000},
    # Haiku aliases — Anthropic-direct rates unless LiteLLM proxy is active
    "haiku": {"input": 0.30 / 1_000_000, "output": 1.20 / 1_000_000},
    "claude-haiku-4-5-20251001": {"input": 0.30 / 1_000_000, "output": 1.20 / 1_000_000},
    "claude-haiku-4-5":          {"input": 0.30 / 1_000_000, "output": 1.20 / 1_000_000},
    "sonnet": {"input": 0.30 / 1_000_000, "output": 0.87 / 1_000_000},  # DeepSeek V4 Pro via proxy
    # Explicit tier-vision (Sonnet 4.6 when used as sub-agent, not via proxy)
    "tier-vision": {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},  # Claude Sonnet 4.6
}
_ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "").strip().lower()
_PROXY_ACTIVE = bool(_ANTHROPIC_BASE_URL) and "anthropic.com" not in _ANTHROPIC_BASE_URL
if _PROXY_ACTIVE:
    for _alias in ("haiku", "claude-haiku-4-5-20251001", "claude-haiku-4-5"):
        _SUBAGENT_PRICING[_alias] = {
            "input": 0.112 / 1_000_000,
            "output": 0.224 / 1_000_000,
        }
_ANTHROPIC_DIRECT_MODELS = {"claude-sonnet-4-6", "claude-opus-4-7", "claude-opus-4-6"}


def parse_subagents(subagent_dir: Path) -> dict:
    """Parse all sub-agent transcripts in <session>/subagents/ and return aggregated token counts.

    Sub-agent files follow the same JSONL format as the main session transcript.
    We separate Anthropic-direct (full model ID) from proxy-intercepted (haiku alias)
    sub-agents to give visibility into untracked Anthropic spend.

    Pricing is derived from the static _SUBAGENT_PRICING table. Unknown model IDs
    fall back to haiku proxy rates and emit a warnings.warn so the operator knows
    cost attribution may be inaccurate.
    """
    if not subagent_dir.is_dir():
        return {}

    totals: dict[str, int] = defaultdict(int)
    seen: set[str] = set()
    ant_cost = proxy_cost = 0.0

    for jsonl_file in subagent_dir.glob("*.jsonl"):
        for line in jsonl_file.read_text(errors="replace").splitlines():
            if '"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", {})
            usage = msg.get("usage")
            if not usage:
                continue

            _uuid = entry.get("uuid")
            dedup_key = f"uuid:{_uuid}" if _uuid else (
                f"fallback:{jsonl_file.name}:{entry.get('timestamp', '')}:{json.dumps(usage, sort_keys=True)}"
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            totals["input_tokens"]  += inp
            totals["output_tokens"] += out
            totals["count"]         += 1

            model = msg.get("model", "")
            if model and model not in _SUBAGENT_PRICING:
                warnings.warn(
                    f"[session-cost-logger] Unknown sub-agent model '{model}' — "
                    "falling back to haiku proxy rates; cost attribution may be inaccurate.",
                    stacklevel=2,
                )
            rates = _SUBAGENT_PRICING.get(model, _SUBAGENT_PRICING["claude-haiku-4-5-20251001"])
            call_cost = inp * rates["input"] + out * rates["output"]
            if model in _ANTHROPIC_DIRECT_MODELS:
                ant_cost += call_cost
            else:
                proxy_cost += call_cost

    if not totals["count"]:
        return {}

    return {
        "subagent_count":         totals["count"],
        "subagent_input_tokens":  totals["input_tokens"],
        "subagent_output_tokens": totals["output_tokens"],
        "subagent_ant_cost":      round(ant_cost, 6),
        "subagent_proxy_cost":    round(proxy_cost, 6),
        "subagent_total_cost":    round(ant_cost + proxy_cost, 4),
    }


def _get_env_float(key: str, default: float) -> float:
    """Parse a float env var, falling back to default on empty/invalid values.

    Empty-string overrides are a common container/CI pattern for 'unset'; a bare
    float("") would raise ValueError and abort the entire Langfuse fetch.
    """
    raw = os.environ.get(key, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _langfuse_from_doppler() -> dict:
    """Fetch LANGFUSE_* from Doppler master/prd when absent from the env.

    Remote/web containers don't reliably run the SessionStart hook, so
    LANGFUSE_* may never be exported (see CLAUDE.md § Claude Code Web). When
    DOPPLER_TOKEN_PRD is present — the one token guaranteed in those containers —
    fetch the keys directly. Bounded (3s) and fail-open: any error returns {}.
    Never prints the values.
    """
    token = os.environ.get("DOPPLER_TOKEN_PRD", "")
    if not token:
        return {}
    try:
        import urllib.request
        url = (
            "https://api.doppler.com/v3/configs/config/secrets/download"
            "?format=json&project=master&config=prd"
            "&keys=LANGFUSE_PUBLIC_KEY,LANGFUSE_SECRET_KEY,LANGFUSE_HOST"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310 — fixed Doppler API URL, not user input
            data = json.loads(resp.read().decode())
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception as e:
        # Sanitized — class name only, never the token/response (AGENTS.md:
        # broad excepts must log or re-raise; this is the sole path preventing
        # OSS spend from silently logging as 0, so a diagnostic matters).
        print(f"[session-cost-logger] Doppler LANGFUSE_* fallback failed: {type(e).__name__}", file=sys.stderr)
        return {}


def fetch_proxy_spend_session(from_time: str | None = None, to_time: str | None = None) -> dict:
    """Fetch OSS/Anthropic proxy spend for a session time window from Langfuse.

    Uses fromStartTime/toStartTime to scope observations to this session's duration.
    Falls back to a 1h look-back when session timestamps are unavailable.

    Caveat: concurrent overlapping sessions may share proxy observations in the time
    window, causing slight double-counting. This is acceptable for cost tracking
    purposes given the LiteLLM proxy does not propagate Claude Code session IDs.
    """
    try:
        import urllib.request
        import urllib.error
        import urllib.parse
        import base64
        base = os.environ.get("LANGFUSE_HOST", "")
        pub  = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sec  = os.environ.get("LANGFUSE_SECRET_KEY", "")
        # Remote/web container fallback: keys may be absent from the env even
        # though the Doppler token is present. Fetch them once (bounded, fail-open)
        # so OSS spend isn't silently logged as 0.
        if not (pub and sec) and os.environ.get("DOPPLER_TOKEN_PRD"):
            _dop = _langfuse_from_doppler()
            base = base or _dop.get("LANGFUSE_HOST", "")
            pub  = pub  or _dop.get("LANGFUSE_PUBLIC_KEY", "")
            sec  = sec  or _dop.get("LANGFUSE_SECRET_KEY", "")
        base = base or "https://us.cloud.langfuse.com"
        if not (pub and sec):
            return {}

        # Scope to session time window; fall back to last 1h if timestamps unavailable.
        # Normalise to Z suffix for consistency with existing patterns (isoformat uses +00:00).
        since = (from_time or (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")).replace("+00:00", "Z")

        oss_cost = 0.0
        ant_cost = 0.0
        oss_calls = ant_calls = 0
        auth_header = "Basic " + base64.b64encode(f"{pub}:{sec}".encode()).decode()

        import time as _time
        # Wall-clock budget across pagination — a slow/unreachable Langfuse must
        # never hang the caller. Returns partial (rare) rather than blocking.
        _deadline = _time.monotonic() + _get_env_float("LANGFUSE_FETCH_BUDGET_S", 8.0)
        _req_timeout = _get_env_float("LANGFUSE_TIMEOUT_S", 4.0)
        page = 1
        incomplete = False  # True if the budget expired after ≥1 page (partial total)
        while page <= 20:  # cap at 1000 observations
            if _time.monotonic() > _deadline:
                incomplete = page > 1
                break
            params: dict = {"limit": 50, "page": page, "fromStartTime": since}
            if to_time:
                # Add 60s buffer so proxy observations recorded slightly after the
                # last session turn are still captured.
                params["toStartTime"] = (
                    datetime.fromisoformat(to_time.replace("Z", "+00:00")) + timedelta(seconds=60)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"{base}/api/public/observations?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"Authorization": auth_header})
            with urllib.request.urlopen(req, timeout=_req_timeout) as resp:  # nosec B310 — URL from LANGFUSE_HOST operator env var, not user input
                data = json.loads(resp.read().decode())
            items = data.get("data", [])
            if not items:
                break
            total = data.get("meta", {}).get("totalItems", 0)

            for obs in items:
                if obs.get("type") != "GENERATION":
                    continue
                name = obs.get("name", "")
                if "/v1/models" in name:
                    continue
                meta   = obs.get("metadata", {})
                hidden = meta.get("hidden_params", {})
                api_base = hidden.get("api_base") or meta.get("api_base") or ""
                cost   = float(obs.get("calculatedTotalCost") or 0)

                host = (urlparse(api_base).hostname or "").lower() if api_base else ""
                if any(p in host for p in ("groq", "fireworks", "together", "openrouter")):
                    oss_cost += cost
                    oss_calls += 1
                elif host == "anthropic.com" or host.endswith(".anthropic.com"):
                    ant_cost += cost
                    ant_calls += 1

            if len(items) * page >= total:
                break
            page += 1

        return {
            "proxy_oss_calls":  oss_calls,
            "proxy_oss_cost":   round(oss_cost, 4),
            "proxy_ant_calls":  ant_calls,
            "proxy_ant_cost":   round(ant_cost, 6),
            "proxy_incomplete": incomplete,
        }
    except Exception as e:
        print(f"[session-cost-logger] Langfuse fetch failed: {e}", file=sys.stderr)
        return {}


def print_summary(days: int = 7) -> None:
    """Print a cost summary table for the last N days.

    Merges the container-local log with the git-tracked rollup mirror so
    reports include sessions from ephemeral remote containers (whose local
    LOG_PATH died with the container). Exact-duplicate lines are deduped.
    """
    rollup_path = REPO_ROOT / ".ai" / "memory" / "session-costs-rollup.jsonl"
    raw_lines: list[str] = []
    for src in (LOG_PATH, rollup_path):
        if src.exists():
            raw_lines.extend(src.read_text(encoding="utf-8").splitlines())
    if not raw_lines:
        print("No session-costs.jsonl found.")
        return
    seen_lines: set[str] = set()
    merged_lines = []
    for raw in raw_lines:
        if raw.strip() and raw not in seen_lines:
            seen_lines.add(raw)
            merged_lines.append(raw)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records = []
    for line in merged_lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            ts_str = rec.get("logged_at", "")
            if ts_str:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts >= cutoff:
                    records.append(rec)
        except (json.JSONDecodeError, ValueError):
            continue

    if not records:
        print(f"No sessions in last {days} days.")
        return

    print(f"\n{'='*70}")
    print(f"Session cost summary — last {days} days ({len(records)} sessions)")
    print(f"{'='*70}")
    print(f"{'Date':<12} {'Turns':>5} {'Cache W':>10} {'Cache R':>12} {'Output':>10} {'Main $':>8} {'OSS $':>7}")
    print(f"{'-'*70}")

    total_main = total_oss = total_ant = 0.0
    grand = 0.0
    for r in sorted(records, key=lambda x: x.get("logged_at", "")):
        date = r.get("logged_at", "?")[:10]
        turns = r.get("turns", 0)
        cw = r.get("cache_write_tokens", 0)
        cr = r.get("cache_read_tokens", 0)
        out = r.get("output_tokens", 0)
        main_cost = r.get("main_thread_cost", 0)
        oss_cost  = r.get("proxy_oss_cost", 0)
        ant_cost  = r.get("proxy_ant_cost", 0)
        total_main += main_cost
        total_oss  += oss_cost
        total_ant  += ant_cost
        # Grand total uses the per-record canonical total_cost, which is already
        # de-duplicated (proxy-active sessions store Langfuse-only, not main+oss).
        # Re-summing the breakdown columns would double-count those sessions.
        grand += r.get("total_cost", main_cost + oss_cost + ant_cost)
        print(f"{date:<12} {turns:>5} {cw:>10,} {cr:>12,} {out:>10,} ${main_cost:>7.2f} ${oss_cost:>6.4f}")

    oss_pct = 100 * total_oss / grand if grand > 0 else 0
    print(f"{'-'*70}")
    print(f"{'TOTAL':<12} {'':>5} {'':>10} {'':>12} {'':>10} ${total_main:>7.2f} ${total_oss:>6.4f}")
    if total_ant > 0:
        print(f"{'':>12} {'':>5} {'':>10} {'':>12} {'':>10} {'':>8} Anthropic proxy=${total_ant:>6.4f}")
    print(f"\nGrand total (de-duplicated): ${grand:.2f}   |   OSS share: {oss_pct:.1f}%  (target: maximise sub-agent delegation)")
    print("Cache savings note: cache reads at $0.30/M vs $3.00/M input = 10x cheaper.")
    print(f"{'='*70}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", help="Specific session ID (UUID without .jsonl)")
    parser.add_argument("--summary", action="store_true", help="Print 7-day cost summary")
    parser.add_argument("--days", type=int, default=7, help="Days for --summary (default 7)")
    parser.add_argument("--project", help="Override project path (default: auto-detect from cwd)")
    args = parser.parse_args()

    if args.summary:
        print_summary(args.days)
        return

    # Locate the projects directory
    if args.project:
        project_path = Path(args.project)
    else:
        # Derive from cwd: ~/.claude/projects/<cwd-as-path-slug>/
        cwd = Path.cwd()
        slug = cwd.as_posix().replace("/", "-").replace(":", "").lstrip("-")
        project_path = Path.home() / ".claude" / "projects" / slug

    if not project_path.exists():
        print(f"[session-cost-logger] Project path not found: {project_path}", file=sys.stderr)
        sys.exit(0)  # non-fatal — Stop hook must not crash

    if args.session:
        session_file = project_path / f"{args.session}.jsonl"
    else:
        session_file = find_latest_session(project_path)

    if not session_file or not session_file.exists():
        print(f"[session-cost-logger] No session file found in {project_path}", file=sys.stderr)
        sys.exit(0)

    session_id = session_file.stem
    parsed = parse_session(session_file)

    if parsed["turns"] == 0:
        print("[session-cost-logger] No usage data found in session — skipping log.", file=sys.stderr)
        sys.exit(0)

    # Parse sub-agent transcripts (previously invisible to cost tracking).
    # Subagent transcripts live under the session-scoped subdirectory:
    # ~/.claude/projects/<slug>/<session_id>/subagents/
    subagents = parse_subagents(project_path / session_id / "subagents")

    # Fetch proxy spend scoped to this session's time window.
    # first_turn/last_turn provide fromStartTime/toStartTime bounds; falls back
    # to a 1h window when timestamps are unavailable.
    proxy = fetch_proxy_spend_session(
        from_time=parsed.get("first_turn"),
        to_time=parsed.get("last_turn"),
    )

    proxy_oss = proxy.get("proxy_oss_cost", 0)
    proxy_ant = proxy.get("proxy_ant_cost", 0)
    main_cost = parsed["main_thread_cost"]
    sa_total  = subagents.get("subagent_total_cost", 0)

    # Avoid double-counting. When the LiteLLM proxy is active, ALL traffic
    # (main thread + sub-agents) flows through it and is captured in Langfuse,
    # so Langfuse (proxy_oss + proxy_ant) is authoritative for the grand total
    # and the transcript estimates (main_cost, sa_total) are kept only as a
    # breakdown. When the proxy is inactive — or Langfuse returned nothing —
    # the transcript estimate is the only source of truth. The previous
    # main + proxy_oss + proxy_ant + sa_total summed the same proxy calls twice.
    # Only trust Langfuse as the authoritative total when the fetch completed
    # (a budget-truncated partial would understate spend). A partial or empty
    # result falls back to the transcript estimate.
    _langfuse_has_data = (proxy_oss + proxy_ant) > 0
    _langfuse_complete = not proxy.get("proxy_incomplete", False)
    if _PROXY_ACTIVE and _langfuse_has_data and _langfuse_complete:
        grand_total = proxy_oss + proxy_ant
    else:
        grand_total = main_cost + sa_total
    oss_pct = round(100 * proxy_oss / grand_total, 1) if grand_total > 0 else 0

    # Always emit proxy fields so the JSONL schema is stable regardless of
    # whether Langfuse credentials are configured.
    proxy_fields = {
        "proxy_oss_calls": proxy.get("proxy_oss_calls", 0),
        "proxy_oss_cost":  round(proxy_oss, 4),
        "proxy_ant_calls": proxy.get("proxy_ant_calls", 0),
        "proxy_ant_cost":  round(proxy_ant, 6),
        "proxy_incomplete": bool(proxy.get("proxy_incomplete", False)),
    }

    _subagent_defaults = {
        "subagent_count":      0,
        "subagent_ant_cost":   0.0,
        "subagent_proxy_cost": 0.0,
        "subagent_total_cost": 0.0,
    }
    record = {
        "logged_at":         datetime.now(timezone.utc).isoformat(),
        "session_id":        session_id,
        "session_id_short":  session_id[:8],
        **parsed,
        **proxy_fields,
        **{**_subagent_defaults, **subagents},
        "total_cost":        round(grand_total, 4),
        "oss_pct":           oss_pct,
    }

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")

    # Durable rollup (cost-review F1): LOG_PATH is gitignored and remote
    # containers are ephemeral, so the weekly aggregator never sees their
    # sessions. Mirror each record to a git-tracked rollup file — committed via
    # the existing stop-hook git-check nag, same mechanism as hook-latency.jsonl.
    rollup_path = REPO_ROOT / ".ai" / "memory" / "session-costs-rollup.jsonl"
    try:
        rollup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(rollup_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        # Best-effort mirror, but never silent (AGENTS.md except/pass rule):
        # this is the only signal that weekly summaries will miss this session.
        print(f"[session-cost-logger] WARN: durable rollup mirror not written "
              f"({type(e).__name__}) — weekly summary will miss this session",
              file=sys.stderr)

    # Human-readable stdout (becomes Stop hook additionalContext)
    cache_savings = parsed["cache_read_tokens"] * (PRICING["input"] - PRICING["cache_read"])
    sa_line = (
        f" | sub-agents={subagents['subagent_count']} "
        f"(ant=${subagents['subagent_ant_cost']:.6f} "
        f"proxy=${subagents['subagent_proxy_cost']:.6f})"
        if subagents else ""
    )
    print(
        f"[COST] Session {session_id[:8]}: "
        f"{parsed['turns']} turns | "
        f"main=${main_cost:.2f} (cache saved ~${cache_savings:.2f}) | "
        f"proxy-OSS=${proxy_oss:.4f} | "
        f"proxy-Ant=${proxy_ant:.6f}"
        f"{sa_line} | "
        f"total=${grand_total:.2f} | "
        f"oss={oss_pct:.1f}%"
    )


if __name__ == "__main__":
    main()
