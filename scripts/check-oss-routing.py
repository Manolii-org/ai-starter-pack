#!/usr/bin/env python3
"""
check-oss-routing.py — Verify the LiteLLM proxy is routing OSS tier aliases correctly.

Tests:
  1. /health/liveliness — proxy is up and responding
  2. Each tier alias  — successful completion from the OSS backend
  3. Fallback aliases — secondary/tertiary routes are reachable (--test-fallbacks)
  4. Per-tier latency — response time within thresholds (warn/fail)
  5. Langfuse traces  — traces received in the last 30 min (warns, does not fail)

Usage:
  python3 scripts/check-oss-routing.py                    # full check
  python3 scripts/check-oss-routing.py --skip-langfuse    # skip trace verification
  python3 scripts/check-oss-routing.py --tier tier-1-fast # single tier only
  python3 scripts/check-oss-routing.py --test-fallbacks   # include fallback aliases
  python3 scripts/check-oss-routing.py --strict-latency   # latency thresholds are hard failures
  python3 scripts/check-oss-routing.py --check-cache-affinity  # advisory Fireworks KV probe

Credential resolution (first found wins):
  1. --proxy-url / --api-key flags
  2. LITELLM_PROXY_URL / LITELLM_MASTER_KEY env vars
  3. your Doppler project/config via DOPPLER_TOKEN_PRD (fallback)

Exit codes:
  0  all checks passed
  1  one or more tier checks failed (routing is broken)
  2  configuration error — missing credentials or proxy unreachable
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _urlopen(
    req: urllib.request.Request | str, timeout: int = 30
) -> Any:
    """Scheme-validating wrapper around urllib.request.urlopen.

    Bandit B310 flags urlopen for permitting file:// and custom schemes.
    This helper explicitly restricts to https/http before opening, making
    the scheme constraint visible and auditable.
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(
            f"Blocked: unexpected URL scheme '{parsed.scheme}' — only https/http allowed. URL: {url!r}"
        )
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310

# ── Expected-live tier aliases ───────────────────────────────────────────────
# Deliberately a HARDCODED, curated subset of the tiers actually deployed in
# deploy/litellm-proxy/config.yaml — intentionally NOT derived from
# .claude/model-routing.json tier_definitions. tier_definitions also holds
# registry-only / not-yet-deployed entries (e.g. tier-vision, tier-3-tool-us);
# deriving this list from it would make check_model_drift() report those as
# MISSING on a correctly-deployed proxy. Keep in sync with config.yaml's
# model_list when a tier is genuinely added to / removed from the deployment.
# (Fallback aliases ARE supplemented dynamically — see load_fallback_aliases.)
TIERS: list[str] = [
    "claude-haiku-4-5-20251001",  # intercept → DeepSeek V4 Flash on OpenRouter (tier-1-fast)
    "tier-0-oss-heavy",            # Kimi K2.6 on Fireworks
    "tier-1-fast",                 # DeepSeek V4 Flash on OpenRouter (2026-05-23 capability primary)
    "tier-2-agentic",              # Kimi K2.6 on Fireworks
    "tier-3-tool",                 # Gemma 4 31B on Together (2026-05-23 capability primary)
    "tier-3-tool-us",              # Gemma 4 31B on Together (US-origin chain)
    "tier-4-extract",              # DeepSeek V4 Flash on OpenRouter
    "tier-5-latency",              # Llama 3.1 8B Instant on Groq
    "tier-review",                 # DeepSeek V4 Flash on Fireworks (reasoning; needs max_tokens >= ~1500 — see TIER_MAX_TOKENS)
]

# Minimal prompt — just enough to get a non-empty response without burning tokens
SMOKE_PROMPT = "Reply with a single digit: what is 1+1?"
# 20 tokens: enough for extended-thinking models (e.g. gpt-oss-20b on Fireworks) to
# surface at least the start of their thinking block. With 5 tokens these models
# exhaust the budget in the thinking phase and return an empty text block, causing
# a false-negative smoke-test failure.
MAX_TOKENS = 20
# Per-tier smoke-test max_tokens overrides. Reasoning models (e.g. tier-review →
# DeepSeek V4 Flash) emit chain-of-thought BEFORE any answer, so the default 20
# tokens is exhausted in the thinking phase and the response truncates before
# content — a false-negative. Give such tiers enough budget to surface an answer.
TIER_MAX_TOKENS: dict[str, int] = {
    "tier-review": 2000,
}
REQUEST_TIMEOUT = 45  # seconds — generous for cold Groq/Together starts
# Per-tier socket-timeout overrides (seconds). Reasoning tiers (tier-review) get a
# larger output budget via TIER_MAX_TOKENS and can legitimately run longer than the
# 45s default yet still within the proxy's own 60s request_timeout — so the client
# socket timeout must exceed the proxy timeout, or a healthy slow response would be
# cut off and false-reported as a failed tier.
TIER_REQUEST_TIMEOUT: dict[str, int] = {
    "tier-review": 90,
}

# Streaming check — prompt long enough to exercise inter-chunk stalls.
# The incident class was 60-90s stalls on long-form generation; a 20-number
# prompt finishes in a handful of fast chunks and cannot reproduce it.
# Override via env vars for local testing (CI always uses the defaults).
STREAM_SMOKE_PROMPT = os.environ.get(
    "STREAM_SMOKE_PROMPT",
    "Count from 1 to 200. Output each number on its own line, nothing else.",
)
STREAM_MAX_TOKENS = int(os.environ.get("STREAM_MAX_TOKENS", "500"))
# Inter-tier delay: 0.3s between sequential tier calls to avoid triggering the
# Fireworks per-model rate limit during health checks. Running all 7 tiers
# back-to-back (especially with --check-stream-timeout) can exhaust M2.7's
# RPM/TPM limit and cause RateLimitError on subsequent real workload requests.
# Observed pattern: check runs correlated with rate-limit bursts in Langfuse
# (root cause investigation 2026-04-18). 0.3s is negligible for a ~30s run.
def _parse_inter_tier_delay() -> float:
    raw = os.environ.get("CHECK_INTER_TIER_DELAY", "0.3")
    try:
        val = float(raw)
    except ValueError:
        print(f"ERROR: CHECK_INTER_TIER_DELAY must be a finite non-negative number, got {raw!r}", file=sys.stderr)
        sys.exit(2)
    if not math.isfinite(val) or val < 0:
        print(f"ERROR: CHECK_INTER_TIER_DELAY must be a finite non-negative number, got {raw!r}", file=sys.stderr)
        sys.exit(2)
    return val


INTER_TIER_DELAY_S: float = _parse_inter_tier_delay()

# ── Latency thresholds (milliseconds) ────────────────────────────────────────
# Generous bounds to account for CI network variability and cold starts.
# "warn" logs a warning; "fail" marks the tier as degraded.
# Degradations only cause exit 1 with --strict-latency.
LATENCY_THRESHOLDS: dict[str, dict[str, int]] = {
    "tier-5-latency":              {"warn": 3000,  "fail": 10000},
    "tier-4-extract":              {"warn": 3000,  "fail": 10000},
    "tier-1-fast":                 {"warn": 8000,  "fail": 20000},
    "tier-3-tool":                 {"warn": 8000,  "fail": 20000},
    "tier-3-tool-us":              {"warn": 8000,  "fail": 20000},
    "tier-2-agentic":              {"warn": 10000, "fail": 25000},
    "tier-0-oss-heavy":            {"warn": 15000, "fail": 30000},
    "claude-haiku-4-5-20251001":   {"warn": 8000,  "fail": 20000},
}
# Fallback aliases inherit parent-tier thresholds with 50% headroom for cold starts
_FALLBACK_LATENCY_MULTIPLIER = 1.5


# ── Fallback alias loader ────────────────────────────────────────────────────

def _find_repo_root() -> Path | None:
    """Walk up from this script to find the repo root (has .git/)."""
    p = Path(__file__).resolve().parent
    for _ in range(10):
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


# Hardcoded fallback list — used when model-routing.json can't be read (e.g. CI
# without full checkout, or standalone script invocation). Keep in sync manually.
_HARDCODED_FALLBACKS: list[str] = [
    # Haiku alias — 3-hop chain (v2.5: Together first for cross-provider Fireworks escape)
    "claude-haiku-4-5-20251001-together-fallback",
    "claude-haiku-4-5-20251001-fireworks-fallback",
    "claude-haiku-4-5-20251001-groq-fallback",
    # tier-0-oss-heavy
    "tier-0-oss-heavy-fallback-together",
    "tier-0-oss-heavy-fallback-glm",
    # tier-1-fast
    "tier-1-fast-fallback-together",
    "tier-1-fast-fallback-groq",
    # tier-2-agentic
    "tier-2-agentic-fallback-kimi",
    "tier-2-agentic-fallback-fireworks",
    # tier-3-tool (2026-05-23: Gemma 4 31B primary; Kimi K2.6 PRC fallbacks)
    "tier-3-tool-fallback-kimi-fireworks",
    "tier-3-tool-fallback-kimi-together",
    # tier-3-tool-us fallbacks (v5.5.6 — all-US-origin chain; corrected gptoss20b→gptoss120b)
    "tier-3-tool-us-fallback-gptoss120b",
    "tier-3-tool-us-fallback-llama8b",
    # sonnet fallback chain (v2.11: was sonnet-fallback-openrouter-gemma4; renamed 2026-05-17 to Kimi K2.6)
    "sonnet-fallback-kimi-together",
    # tier-4-extract-fallback-gemma: alias retained; now routes Llama 3.3 70B
    # on Together (OpenRouter dropped Gemma 4 31B serverless 2026-04-25)
    "tier-4-extract-fallback-gemma",
    "tier-4-extract-fallback-llama",
    "tier-4-extract-fallback-gptoss",
    # tier-5-latency
    "tier-5-latency-fallback-gptoss",
    "tier-5-latency-fallback-fireworks",
]


def load_fallback_aliases() -> list[str]:
    """Load fallback aliases for smoke-testing.

    Always starts with the hardcoded list (covers haiku fallback aliases and any
    aliases outside tier_definitions, e.g. proxy_intercepted_models). Then
    supplements by extracting secondary/tertiary/quaternary entries from each
    tier's fallback_chain in .claude/model-routing.json — picks up any new
    tier_definitions entries that were added after the last hardcoded update.

    This union strategy ensures the hardcoded list is never silently bypassed
    when model-routing.json is readable (which it always is in CI).
    """
    # Always start with the full hardcoded list — it covers aliases that live
    # outside tier_definitions (haiku fallback chain, proxy_intercepted_models).
    aliases: list[str] = list(_HARDCODED_FALLBACKS)

    root = _find_repo_root()
    if not root:
        return aliases

    config_path = root / ".claude" / "model-routing.json"
    if not config_path.exists():
        return aliases

    try:
        config = json.loads(config_path.read_text())
        tier_defs = config.get("tier_definitions", {})
        for _tier_name, tier_def in tier_defs.items():
            if _tier_name.startswith("$") or not isinstance(tier_def, dict):
                continue
            chain = tier_def.get("fallback_chain", {})
            # Read all named hop positions — quaternary added for tier-4-extract
            # which gained a Together cross-provider entry in v2.5.
            for position in ("secondary", "tertiary", "quaternary"):
                entry = chain.get(position, {})
                alias = entry.get("litellm_alias", "")
                if alias and alias not in aliases:
                    aliases.append(alias)

        return list(dict.fromkeys(aliases))  # deduplicate, preserve order
    except Exception as exc:
        print(f"  WARN: Failed to load supplemental fallback aliases from config: {exc}", file=sys.stderr)
        return aliases


def _read_stream_timeout() -> int | None:
    """Read router_settings.stream_timeout from deploy/litellm-proxy/config.yaml.

    Only considers stream_timeout: lines that appear inside the router_settings:
    block, so a model-level or litellm_settings-level stream_timeout is not
    mistakenly used as the ceiling.

    Returns None when stream_timeout is absent from router_settings — this is
    the expected state since 2026-05-10 (M2.7 removed, no active model needs
    inter-chunk stall handling). Returns 45 when the config file is absent
    (conservative legacy default). Raises on parse/IO errors.
    """
    root = _find_repo_root()
    if not root:
        return 45
    config_path = root / "deploy" / "litellm-proxy" / "config.yaml"
    try:
        text = config_path.read_text()
    except FileNotFoundError:
        return 45
    in_router_settings = False
    router_indent: int | None = None
    try:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Detect entry into router_settings block (top-level key)
            if stripped.startswith("router_settings:"):
                in_router_settings = True
                router_indent = len(line) - len(line.lstrip())
                continue
            # Detect exit: a non-empty, non-comment line at the same or lower indent
            if in_router_settings and router_indent is not None:
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= router_indent and stripped and not stripped.startswith("-"):
                    in_router_settings = False
            if in_router_settings and stripped.startswith("stream_timeout:"):
                val = stripped.split(":", 1)[1].strip().split("#")[0].strip()
                return int(val)
    except (OSError, ValueError) as exc:
        print(
            f"ERROR: _read_stream_timeout: failed to parse {config_path}: {exc}",
            file=sys.stderr,
        )
        raise
    # stream_timeout not found inside router_settings → None (no enforcement active)
    return None


def _latency_threshold(alias: str) -> dict[str, int]:
    """Return latency thresholds for a given alias, with fallback headroom."""
    if alias in LATENCY_THRESHOLDS:
        return LATENCY_THRESHOLDS[alias]
    # Derive from parent tier (e.g., "tier-1-fast-fallback-together" → "tier-1-fast")
    for tier_name, thresholds in LATENCY_THRESHOLDS.items():
        if alias.startswith(tier_name):
            return {
                "warn": int(thresholds["warn"] * _FALLBACK_LATENCY_MULTIPLIER),
                "fail": int(thresholds["fail"] * _FALLBACK_LATENCY_MULTIPLIER),
            }
    # Haiku Fireworks fallback (gpt-oss-120b, renamed from -anthropic in PR #168)
    if "haiku" in alias:
        base = LATENCY_THRESHOLDS["claude-haiku-4-5-20251001"]
        return {
            "warn": int(base["warn"] * _FALLBACK_LATENCY_MULTIPLIER),
            "fail": int(base["fail"] * _FALLBACK_LATENCY_MULTIPLIER),
        }
    # Unknown alias — generous default
    return {"warn": 15000, "fail": 30000}


# ── Credential helpers ─────────────────────────────────────────────────────────

def _fetch_from_doppler(
    token: str,
    keys: list[str],
    project: str | None = None,
    config: str | None = None,
) -> dict[str, str]:
    """Read secrets from Doppler using a project service token or workspace token.

    ``project`` / ``config`` default to the ``DOPPLER_PROJECT`` /
    ``DOPPLER_CONFIG`` env vars so consumers can point the routing check at
    their own Doppler project without editing this script.
    """
    project = project or os.environ.get("DOPPLER_PROJECT") or ""
    config = config or os.environ.get("DOPPLER_CONFIG") or "prd"
    if not project:
        raise RuntimeError(
            "Doppler project unset. Set the DOPPLER_PROJECT env var (or pass "
            "project=... to _fetch_from_doppler) to the Doppler project that "
            "holds LITELLM_PROXY_URL / LITELLM_MASTER_KEY."
        )
    key_csv = ",".join(keys)
    url = (
        f"https://api.doppler.com/v3/configs/config/secrets/download"
        f"?format=json&project={project}&config={config}&keys={key_csv}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        resp = _urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(
            f"Failed to fetch secrets from Doppler ({project}/{config}): {exc}"
        ) from exc


def resolve_credentials(
    proxy_url_arg: str | None,
    api_key_arg: str | None,
    langfuse_pk_arg: str | None,
    langfuse_sk_arg: str | None,
    langfuse_host_arg: str | None,
    skip_langfuse: bool = False,
) -> tuple[str, str, str | None, str | None, str]:
    """Return (proxy_url, litellm_key, langfuse_pk, langfuse_sk, langfuse_host).

    Langfuse credential resolution priority:
      1. CLI flags / env vars
      2. Optional: Doppler project for Langfuse (if LANGFUSE_DOPPLER_PROJECT env var is set)
         — requires workspace token (DOPPLER_TOKEN / DOPPLER_PERSONAL) from proxy project
      3. Doppler fallback project — try DOPPLER_FALLBACK_PROJECT env var if Langfuse not found

    Proxy creds (LITELLM_PROXY_URL, LITELLM_MASTER_KEY) always come from the primary Doppler project.
    """
    proxy_url = proxy_url_arg or os.environ.get("LITELLM_PROXY_URL", "")
    api_key = api_key_arg or os.environ.get("LITELLM_MASTER_KEY", "")
    lf_pk = langfuse_pk_arg or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    lf_sk = langfuse_sk_arg or os.environ.get("LANGFUSE_SECRET_KEY", "")
    lf_host = langfuse_host_arg or os.environ.get("LANGFUSE_HOST", "")

    doppler_token = os.environ.get("DOPPLER_TOKEN_PRD", "")
    needs_proxy = not proxy_url or not api_key
    needs_langfuse = (not lf_pk or not lf_sk or not lf_host) and not skip_langfuse

    if needs_proxy and not doppler_token:
        raise RuntimeError(
            "Missing credentials. Set LITELLM_PROXY_URL + LITELLM_MASTER_KEY "
            "env vars, or DOPPLER_TOKEN_PRD for auto-fetch."
        )

    if doppler_token and (needs_proxy or needs_langfuse):
        # Step 1: Fetch proxy creds + workspace token from primary Doppler project.
        proxy_keys: list[str] = ["DOPPLER_TOKEN", "DOPPLER_PERSONAL"]
        if needs_proxy:
            proxy_keys += ["LITELLM_PROXY_URL", "LITELLM_MASTER_KEY"]
        master_secrets = _fetch_from_doppler(doppler_token, proxy_keys)
        proxy_url = proxy_url or master_secrets.get("LITELLM_PROXY_URL", "")
        api_key = api_key or master_secrets.get("LITELLM_MASTER_KEY", "")
        ws_token = master_secrets.get("DOPPLER_TOKEN") or master_secrets.get("DOPPLER_PERSONAL", "")

        # Step 2: Optionally fetch Langfuse from a configured Doppler project.
        langfuse_doppler_project = os.environ.get("LANGFUSE_DOPPLER_PROJECT", "")
        if needs_langfuse and ws_token and langfuse_doppler_project:
            try:
                kl = _fetch_from_doppler(
                    ws_token,
                    ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"],
                    project=langfuse_doppler_project,
                )
                lf_pk = lf_pk or kl.get("LANGFUSE_PUBLIC_KEY", "")
                lf_sk = lf_sk or kl.get("LANGFUSE_SECRET_KEY", "")
                lf_host = lf_host or kl.get("LANGFUSE_HOST", "")
            except Exception as exc:
                print(
                    f"  WARN: Failed to fetch Langfuse from {langfuse_doppler_project}/prd: {exc}",
                    file=sys.stderr,
                )

        # Step 3: Fallback — try a configured fallback project if Langfuse is incomplete.
        doppler_fallback_project = os.environ.get("DOPPLER_FALLBACK_PROJECT", "")
        if needs_langfuse and (not lf_pk or not lf_sk or not lf_host) and doppler_fallback_project:
            try:
                lf_secrets = _fetch_from_doppler(
                    doppler_token,
                    ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"],
                    project=doppler_fallback_project,
                )
                lf_pk = lf_pk or lf_secrets.get("LANGFUSE_PUBLIC_KEY", "")
                lf_sk = lf_sk or lf_secrets.get("LANGFUSE_SECRET_KEY", "")
                lf_host = lf_host or lf_secrets.get("LANGFUSE_HOST", "")
            except Exception as exc:
                print(
                    f"  WARN: Failed to fetch Langfuse from {doppler_fallback_project}/prd: {exc}",
                    file=sys.stderr,
                )

    lf_host = lf_host or "https://us.cloud.langfuse.com"

    if not proxy_url or not api_key:
        raise RuntimeError(
            "LITELLM_PROXY_URL or LITELLM_MASTER_KEY could not be resolved "
            "(checked env + Doppler)."
        )

    return proxy_url.rstrip("/"), api_key, lf_pk or None, lf_sk or None, lf_host


# ── Check functions ────────────────────────────────────────────────────────────

def check_model_drift(proxy_url: str, api_key: str) -> dict[str, Any]:
    """Compare the live /v1/models list against the expected tier aliases.

    Only compares OSS tier aliases (tier-* and claude-haiku-*); ignores Anthropic
    passthrough models (claude-opus-4-6, claude-sonnet-4-6, etc.).

    Returns:
        ok        — True when live matches expected exactly
        missing   — expected aliases absent from the live proxy
        extra     — live aliases not found in the expected set
        live      — full list of live OSS tier aliases
        expected  — full list of expected OSS tier aliases
    """
    expected_tiers: set[str] = set(TIERS)
    try:
        expected_fallbacks: set[str] = set(load_fallback_aliases())
    except Exception as exc:
        print(f"  WARN: Failed to load fallback aliases for drift check: {exc}", file=sys.stderr)
        expected_fallbacks = set()
    all_expected = expected_tiers | expected_fallbacks

    req = urllib.request.Request(
        f"{proxy_url}/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        resp = _urlopen(req, timeout=20)
        data = json.loads(resp.read())
        live_all = {m.get("id", "") for m in data.get("data", [])}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    # Filter to OSS tier aliases only
    def _is_tier_alias(name: str) -> bool:
        return name.startswith("tier-") or name.startswith("claude-haiku-")

    live = {m for m in live_all if _is_tier_alias(m)}
    expected = {m for m in all_expected if _is_tier_alias(m)}

    missing = sorted(expected - live)
    extra = sorted(live - expected)

    return {
        "ok": not missing and not extra,
        "live_count": len(live),
        "expected_count": len(expected),
        "missing": missing,
        "extra": extra,
        "live": sorted(live),
        "expected": sorted(expected),
    }


def _block_nonempty(block: dict) -> str:
    """Return the non-empty text or thinking content from a response content block.

    Extended-thinking models (e.g. Kimi K2.5) return type='thinking' blocks
    alongside type='text' blocks. Both are valid indicators of a successful call.
    """
    return (block.get("text") or block.get("thinking") or "").strip()


def check_health(proxy_url: str) -> dict[str, Any]:
    url = f"{proxy_url}/health/liveliness"
    try:
        resp = _urlopen(urllib.request.Request(url), timeout=10)
        ok = resp.status == 200
        return {"ok": ok, "status": resp.status, "url": url}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "url": url, "error": str(e)}
    except Exception as exc:
        return {"ok": False, "status": 0, "url": url, "error": str(exc)}


def check_tier(proxy_url: str, api_key: str, alias: str) -> dict[str, Any]:
    """Smoke-test one tier alias via a minimal chat completion.

    Applies per-tier overrides so reasoning tiers (e.g. tier-review) get enough
    output budget (TIER_MAX_TOKENS) and a socket timeout above the proxy's own
    request_timeout (TIER_REQUEST_TIMEOUT), instead of false-failing on
    truncation or a healthy-but-slow response.
    """
    return _chat_messages(
        proxy_url,
        api_key,
        alias,
        SMOKE_PROMPT,
        max_tokens=TIER_MAX_TOKENS.get(alias, MAX_TOKENS),
        timeout=TIER_REQUEST_TIMEOUT.get(alias, REQUEST_TIMEOUT),
    )


def _chat_messages(
    proxy_url: str,
    api_key: str,
    alias: str,
    prompt: str,
    *,
    max_tokens: int = MAX_TOKENS,
    timeout: int = REQUEST_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = json.dumps({
        "model": alias,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(
        f"{proxy_url}/v1/messages",
        data=payload,
        headers=headers,
    )
    t0 = time.monotonic()
    try:
        resp = _urlopen(req, timeout=timeout)
        resp_data = resp.read()
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        body = json.loads(resp_data)
        content = body.get("content", [])
        nonempty = next((b for b in content if _block_nonempty(b)), None)
        reply = _block_nonempty(nonempty)[:40] if nonempty else ""
        has_content = bool(nonempty)  # require at least one non-empty text/thinking block
        return {
            "ok": has_content,
            "alias": alias,
            "model_returned": body.get("model", ""),
            "reply": reply,
            "elapsed_ms": elapsed_ms,
            "usage": body.get("usage", {}),
        }
    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            err_body = e.read().decode()[:200]
        except Exception:
            err_body = ""
        return {
            "ok": False,
            "alias": alias,
            "elapsed_ms": elapsed_ms,
            "http_status": e.code,
            "error": f"HTTP {e.code}: {err_body}" if err_body else f"HTTP {e.code}",
        }
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "alias": alias, "elapsed_ms": elapsed_ms, "error": str(exc)}


CACHE_PROBE_PREFIX = (
    "Reference context for Fireworks KV cache probe. The secret code is ALPHA-7749. "
) * 40


def check_cache_affinity(
    proxy_url: str,
    api_key: str,
    alias: str = "tier-2-agentic",
) -> dict[str, Any]:
    """Two-turn latency probe with fixed x-session-affinity (advisory only).

    Turn 2 reuses the long prefix from turn 1. When Fireworks KV cache hits, turn 2
    is typically faster. This is a heuristic — never fails the overall routing check.
    """
    headers = {"x-session-affinity": "cache-probe"}
    turn1 = _chat_messages(
        proxy_url,
        api_key,
        alias,
        CACHE_PROBE_PREFIX + "Reply with only the word OK.",
        extra_headers=headers,
    )
    if not turn1.get("ok"):
        return {
            "ok": True,
            "alias": alias,
            "skipped": True,
            "warning": f"turn1 failed: {turn1.get('error', 'unknown')}",
        }

    time.sleep(INTER_TIER_DELAY_S)

    turn2 = _chat_messages(
        proxy_url,
        api_key,
        alias,
        CACHE_PROBE_PREFIX + "What was the secret code? Reply with the code only.",
        extra_headers=headers,
    )
    if not turn2.get("ok"):
        return {
            "ok": True,
            "alias": alias,
            "skipped": True,
            "warning": f"turn2 failed: {turn2.get('error', 'unknown')}",
        }

    t1 = turn1.get("elapsed_ms", 0) or 1
    t2 = turn2.get("elapsed_ms", 0) or 0
    speedup = (t1 - t2) / t1
    warning = None
    if speedup < 0.05:
        warning = (
            f"turn2 not materially faster than turn1 ({t1}ms vs {t2}ms) — "
            "Fireworks KV cache may be cold or x-session-affinity not applied"
        )

    return {
        "ok": True,
        "alias": alias,
        "turn1_ms": t1,
        "turn2_ms": t2,
        "speedup_ratio": round(speedup, 3),
        "warning": warning,
    }


def check_stream_timeout(
    proxy_url: str, api_key: str, alias: str, stream_timeout_s: int
) -> dict[str, Any]:
    """Send a streaming request and measure inter-chunk gaps.

    Detects when a model's streaming behaviour risks tripping the proxy's
    stream_timeout setting — the root cause of the 2026-04-14 incident where
    MiniMax M2.7 paused 60-90s between chunks but stream_timeout was only 45s.

    Thresholds (relative to stream_timeout_s):
      >= 40%  → warning (approaching the 2× headroom rule; consider increasing stream_timeout)
      >= 50%  → failure (breaches the documented rule: stream_timeout must be ≥ 2× max gap)

    Returns keys: ok, alias, ttfb_ms, max_gap_ms, chunk_count,
                  stream_timeout_s, warning (str|None), error (str|None)
    """
    if stream_timeout_s is None:
        return {"ok": True, "alias": alias, "skipped": True, "stream_timeout_s": None,
                "warning": None, "note": "stream_timeout not set in config — check skipped"}

    payload = json.dumps({
        "model": alias,
        "stream": True,
        "max_tokens": STREAM_MAX_TOKENS,
        "messages": [{"role": "user", "content": STREAM_SMOKE_PROMPT}],
    }).encode()

    req = urllib.request.Request(
        f"{proxy_url}/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    t0 = time.monotonic()
    # Allow the stream up to stream_timeout + 60s to complete. This matches the
    # config's required contract (request_timeout >= stream_timeout + 60s) so a
    # valid long-running stream isn't cut short locally and falsely reported as failed.
    sock_timeout = max(REQUEST_TIMEOUT, stream_timeout_s + 60)
    try:
        resp = _urlopen(req, timeout=sock_timeout)
    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            err_body = e.read().decode()[:200]
        except Exception:
            err_body = ""
        return {
            "ok": False, "alias": alias, "elapsed_ms": elapsed_ms,
            "error": f"HTTP {e.code}: {err_body}" if err_body else f"HTTP {e.code}",
        }
    except Exception as exc:
        return {"ok": False, "alias": alias, "elapsed_ms": 0, "error": str(exc)}

    last_t = t0
    ttfb_ms: int | None = None
    max_gap_ms = 0
    chunk_count = 0
    read_error: str | None = None

    try:
        while True:
            line = resp.readline()
            if not line:
                break
            text_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not text_line.startswith("data:"):
                continue
            payload_str = text_line[5:].strip()
            if payload_str == "[DONE]":
                break
            now = time.monotonic()
            chunk_count += 1
            if ttfb_ms is None:
                ttfb_ms = int((now - t0) * 1000)
            else:
                gap_ms = int((now - last_t) * 1000)
                if gap_ms > max_gap_ms:
                    max_gap_ms = gap_ms
            last_t = now
    except Exception as exc:
        read_error = f"Stream read error: {exc}"

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    # Thresholds enforce the 2× headroom rule: stream_timeout must be ≥ 2× max gap,
    # so a gap > 50% of stream_timeout is a policy violation (fail); > 40% is a warning.
    warn_ms = int(stream_timeout_s * 0.40 * 1000)
    fail_ms = int(stream_timeout_s * 0.50 * 1000)

    warning: str | None = None
    # Require at least one data chunk — a 200 with no chunks hides a broken stream path.
    if read_error is None and chunk_count == 0:
        read_error = "Stream ended without any data events"
    ok = read_error is None
    if ok and max_gap_ms >= fail_ms:
        ok = False
        warning = (
            f"{alias}: max inter-chunk gap {max_gap_ms}ms is ≥50% of "
            f"stream_timeout ({stream_timeout_s}s) — violates the 2× headroom rule. "
            f"Increase stream_timeout in config.yaml and redeploy."
        )
    elif ok and max_gap_ms >= warn_ms:
        pct = int(max_gap_ms / (stream_timeout_s * 10))
        warning = (
            f"{alias}: max inter-chunk gap {max_gap_ms}ms is {pct}% of "
            f"stream_timeout ({stream_timeout_s}s). Consider increasing stream_timeout "
            f"(must be ≥ 2× max gap to satisfy the headroom rule)."
        )

    return {
        "ok": ok,
        "alias": alias,
        "elapsed_ms": elapsed_ms,
        "ttfb_ms": ttfb_ms or 0,
        "max_gap_ms": max_gap_ms,
        "chunk_count": chunk_count,
        "stream_timeout_s": stream_timeout_s,
        "warning": warning,
        "error": read_error,
    }


def check_langfuse_traces(
    pk: str, sk: str, host: str, lookback_minutes: int = 30
) -> dict[str, Any]:
    creds = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    since = (
        datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{host}/api/public/traces?limit=10&fromTimestamp={since}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        resp = _urlopen(req, timeout=15)
        data = json.loads(resp.read())
        traces = data.get("data", [])
        litellm_traces = [t for t in traces if "litellm" in t.get("name", "")]
        return {
            "ok": True,
            "total": len(traces),
            "litellm": len(litellm_traces),
            "lookback_minutes": lookback_minutes,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "lookback_minutes": lookback_minutes}


# ── Output helpers ─────────────────────────────────────────────────────────────

def _icon(ok: bool) -> str:
    return "✓" if ok else "✗"


def _latency_tag(alias: str, elapsed_ms: int) -> str:
    """Return a latency annotation: empty, ' SLOW(...)' or ' DEGRADED(...)'."""
    thresholds = _latency_threshold(alias)
    if elapsed_ms >= thresholds["fail"]:
        return f" DEGRADED({elapsed_ms}ms>{thresholds['fail']}ms)"
    if elapsed_ms >= thresholds["warn"]:
        return f" SLOW({elapsed_ms}ms>{thresholds['warn']}ms)"
    return ""


def print_human_report(
    health: dict,
    tier_results: list[dict],
    langfuse: dict | None,
    elapsed: float,
    latency_failures: list[str],
    drift: dict | None = None,
    stream_results: list[dict] | None = None,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n── OSS Routing Health Check  {now} ──")

    print(f"\n  Proxy health:  {_icon(health['ok'])} {health.get('status','')}  {health['url']}")
    if not health["ok"]:
        print(f"  Error: {health.get('error','')}")

    print("\n  Tier smoke tests:")
    for r in tier_results:
        alias = r["alias"]
        ms = r.get("elapsed_ms", 0)
        tag = _latency_tag(alias, ms)
        if r["ok"]:
            reply = r.get("reply", "")
            model_ret = r.get("model_returned", "")
            print(f"    {_icon(True)} {alias:<45}  {ms:>5}ms  reply={repr(reply)}  returned={model_ret}{tag}")
        else:
            err = r.get("error") or f"HTTP {r.get('http_status','?')}"
            print(f"    {_icon(False)} {alias:<45}  {ms:>5}ms  FAILED: {err}")

    if latency_failures:
        print(f"\n  Latency degradations ({len(latency_failures)}):")
        for msg in latency_failures:
            print(f"    ! {msg}")

    if drift is not None:
        if "error" in drift:
            print(f"\n  Model drift:  {_icon(False)} FAIL — {drift['error']}")
        elif drift["ok"]:
            print(f"\n  Model drift:  {_icon(True)} live={drift['live_count']} expected={drift['expected_count']} — no drift")
        else:
            print(f"\n  Model drift:  {_icon(False)} DRIFT DETECTED")
            if drift["missing"]:
                print(f"    Missing from proxy ({len(drift['missing'])}): {', '.join(drift['missing'])}")
            if drift["extra"]:
                print(f"    Extra in proxy ({len(drift['extra'])}): {', '.join(drift['extra'])}")
            print("    Hint: config.yaml and TIERS list may be out of sync with live proxy config.")

    if langfuse:
        if langfuse["ok"]:
            trace_warn = " — WARN: callbacks may not be firing" if langfuse["litellm"] == 0 else ""
            print(
                f"\n  Langfuse ({langfuse['lookback_minutes']}m):  {_icon(langfuse['litellm'] > 0)} "
                f"{langfuse['litellm']} litellm traces  "
                f"({langfuse['total']} total){trace_warn}"
            )
        else:
            print(f"\n  Langfuse:  WARN — {langfuse.get('error','unknown error')}")

    if stream_results:
        sto = stream_results[0].get("stream_timeout_s", "?") if stream_results else "?"
        print(f"\n  Stream timeout check (stream_timeout={sto}s):")
        for r in stream_results:
            alias = r["alias"]
            if not r.get("ok") and r.get("error"):
                print(f"    {_icon(False)} {alias:<45}  FAILED: {r['error']}")
            else:
                ttfb = r.get("ttfb_ms", 0)
                gap = r.get("max_gap_ms", 0)
                chunks = r.get("chunk_count", 0)
                pct = int(gap / (sto * 10)) if isinstance(sto, int) and sto > 0 else "?"
                warn = f"  ⚠ {r['warning']}" if r.get("warning") else ""
                ok_icon = _icon(r["ok"])
                print(
                    f"    {ok_icon} {alias:<45}  "
                    f"ttfb={ttfb}ms  max_gap={gap}ms({pct}% of limit)  "
                    f"chunks={chunks}{warn}"
                )

    failed = [r for r in tier_results if not r["ok"]]
    print(f"\n  Result: {len(tier_results) - len(failed)}/{len(tier_results)} tiers OK  ({elapsed:.1f}s)\n")


def print_json_report(
    health: dict,
    tier_results: list[dict],
    langfuse: dict | None,
    elapsed: float,
    overall_ok: bool,
    latency_failures: list[str],
    drift: dict | None = None,
    stream_results: list[dict] | None = None,
) -> None:
    print(json.dumps({
        "ok": overall_ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "health": health,
        "tiers": tier_results,
        "langfuse": langfuse,
        "latency_failures": latency_failures,
        "drift": drift,
        "stream_checks": stream_results,
    }, indent=2))


# ── Telemetry health check ─────────────────────────────────────────────────────

def check_telemetry_health(
    window_hours: int = 24,
    unknown_rate_threshold: float = 0.30,
    misroute_rate_threshold: float = 0.20,
) -> dict:
    """Read routing-telemetry.jsonl and return misrouting rate stats for the window.

    Returns dict with:
      ok           — False if any threshold is breached
      total        — Agent dispatches in the window
      unknown_rate — fraction with tier="unknown" (model= omitted or unrecognised)
      misroute_rate— fraction flagged misrouted by keyword heuristic
      anthropic_oss_eligible — count of Anthropic-model dispatches to OSS-eligible agents
      errors       — list of human-readable threshold violation messages
      window_hours — the window used
      telemetry_path — path checked
    """
    repo_root = Path(__file__).parent.parent
    tel_path = repo_root / ".ai" / "memory" / "routing-telemetry.jsonl"

    OSS_TIER_ALIASES = {
        "tier-0-oss-heavy", "tier-1-fast", "tier-2-agentic",
        "tier-3-tool", "tier-3-tool-us", "tier-4-extract", "tier-5-latency",
    }

    # Load agent_routing to identify OSS-eligible agents.
    # Config load failure is a hard error — without it we can't detect
    # anthropic-on-oss-eligible misroutes, producing false negatives.
    oss_eligible_agents: set = set()
    config_errors: list[str] = []
    try:
        cfg_path = repo_root / ".claude" / "model-routing.json"
        with open(cfg_path) as _f:
            _cfg = json.load(_f)
        for agent, tier in _cfg.get("agent_routing", {}).items():
            if tier in OSS_TIER_ALIASES or tier == "haiku":
                oss_eligible_agents.add(agent)
    except Exception as exc:
        config_errors.append(
            f"Failed to load model-routing.json — anthropic-on-oss-eligible check disabled: {exc}"
        )

    if not tel_path.exists():
        return {
            "ok": False,
            "total": 0,
            "unknown_rate": 0.0,
            "misroute_rate": 0.0,
            "anthropic_oss_eligible": 0,
            "errors": [
                f"Telemetry file missing: {tel_path}. "
                "No Agent dispatches have been recorded. "
                "Ensure PostToolUse hook is wired in settings.json."
            ],
            "window_hours": window_hours,
            "telemetry_path": str(tel_path),
        }

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total = unknown = misrouted = anthropic_oss = 0

    try:
        with open(tel_path) as _f:
            for line in _f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    ts = datetime.fromisoformat(e.get("timestamp", ""))
                    if ts < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue

                total += 1
                if e.get("tier") == "unknown":
                    unknown += 1
                if e.get("misrouted"):
                    misrouted += 1
                # Count dispatches to OSS-eligible agents that went to Anthropic
                subagent = e.get("subagent_type", "")
                provider = e.get("provider", "")
                if (
                    subagent in oss_eligible_agents
                    and "anthropic" in provider.lower()
                    and e.get("tier") not in OSS_TIER_ALIASES
                ):
                    anthropic_oss += 1
    except Exception as exc:
        return {
            "ok": False,
            "total": 0,
            "unknown_rate": 0.0,
            "misroute_rate": 0.0,
            "anthropic_oss_eligible": 0,
            "errors": [f"Failed to read telemetry file: {exc}"],
            "window_hours": window_hours,
            "telemetry_path": str(tel_path),
        }

    errors: list[str] = list(config_errors)
    unknown_rate = unknown / total if total else 0.0
    misroute_rate = misrouted / total if total else 0.0

    if total == 0:
        errors.append(
            f"No Agent dispatches recorded in the last {window_hours}h. "
            "OSS delegation cannot be verified without telemetry data."
        )
    if unknown_rate > unknown_rate_threshold:
        errors.append(
            f"Unknown-tier rate {unknown_rate:.0%} exceeds threshold {unknown_rate_threshold:.0%}. "
            f"{unknown}/{total} dispatches omitted model= or used an unrecognised alias."
        )
    if misroute_rate > misroute_rate_threshold:
        errors.append(
            f"Misroute rate {misroute_rate:.0%} exceeds threshold {misroute_rate_threshold:.0%}. "
            f"{misrouted}/{total} dispatches were over- or under-provisioned."
        )
    if anthropic_oss > 0:
        errors.append(
            f"{anthropic_oss} dispatch(es) to OSS-eligible agents went to Anthropic. "
            "Add model= tier alias to these calls."
        )

    return {
        "ok": len(errors) == 0,
        "total": total,
        "unknown_rate": unknown_rate,
        "misroute_rate": misroute_rate,
        "anthropic_oss_eligible": anthropic_oss,
        "errors": errors,
        "window_hours": window_hours,
        "telemetry_path": str(tel_path),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LiteLLM OSS tier routing")
    parser.add_argument("--proxy-url", help="LiteLLM proxy base URL")
    parser.add_argument("--api-key", help="LiteLLM master key")
    parser.add_argument("--langfuse-pk", help="Langfuse public key")
    parser.add_argument("--langfuse-sk", help="Langfuse secret key")
    parser.add_argument("--langfuse-host", help="Langfuse host URL")
    parser.add_argument(
        "--tier",
        help="Test a single tier alias only (e.g. tier-1-fast)",
        default=None,
    )
    parser.add_argument(
        "--test-fallbacks",
        action="store_true",
        help="Also test secondary/tertiary fallback aliases (~13 extra calls)",
    )
    parser.add_argument(
        "--strict-latency",
        action="store_true",
        help="Treat latency threshold breaches as hard failures (exit 1)",
    )
    parser.add_argument(
        "--skip-langfuse",
        action="store_true",
        help="Skip Langfuse trace verification",
    )
    parser.add_argument(
        "--langfuse-strict",
        action="store_true",
        help="Treat Langfuse errors or 0 traces as hard failures (exit 1)",
    )
    parser.add_argument(
        "--check-drift",
        action="store_true",
        help="Compare live proxy model list against expected TIERS + fallbacks; fail on mismatch",
    )
    parser.add_argument(
        "--check-cache-affinity",
        action="store_true",
        help=(
            "Run a two-turn tier-2-agentic probe with x-session-affinity and compare "
            "latencies (advisory — warns when turn2 is not faster, never fails exit code)"
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output machine-readable JSON",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=30,
        help="Langfuse trace lookback window in minutes (default: 30)",
    )
    parser.add_argument(
        "--check-stream-timeout",
        action="store_true",
        help=(
            "Send streaming requests to each primary tier and measure inter-chunk gaps. "
            "Warns when any gap exceeds 40%% of stream_timeout (from config.yaml); "
            "fails when it exceeds 50%%."
        ),
    )
    parser.add_argument(
        "--strict-stream-timeout",
        action="store_true",
        help="Treat stream timeout warnings (>=40%% threshold) as hard failures (exit 1)",
    )
    parser.add_argument(
        "--verify-telemetry",
        action="store_true",
        help="Read routing-telemetry.jsonl and fail if misrouting rate exceeds thresholds",
    )
    parser.add_argument(
        "--telemetry-window",
        type=int,
        default=24,
        help="Telemetry lookback window in hours (default: 24)",
    )
    parser.add_argument(
        "--strict-telemetry",
        action="store_true",
        help="Treat telemetry threshold violations as hard failures (exit 1). Implied by --verify-telemetry.",
    )
    args = parser.parse_args()

    if args.skip_langfuse and args.langfuse_strict:
        parser.error("--skip-langfuse cannot be combined with --langfuse-strict")

    start = time.monotonic()

    # Resolve credentials
    try:
        proxy_url, api_key, lf_pk, lf_sk, lf_host = resolve_credentials(
            args.proxy_url,
            args.api_key,
            args.langfuse_pk,
            args.langfuse_sk,
            args.langfuse_host,
            skip_langfuse=args.skip_langfuse,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not args.json_output:
        print(f"Proxy: {proxy_url}", file=sys.stderr)

    # Health check
    health = check_health(proxy_url)
    if not health["ok"]:
        if args.json_output:
            print(json.dumps({"ok": False, "health": health}))
        else:
            print(f"FATAL: Proxy unreachable — {health}", file=sys.stderr)
        return 3

    # Build tier list
    tiers_to_test = [args.tier] if args.tier else list(TIERS)
    if args.test_fallbacks and args.tier:
        print("  WARN: --test-fallbacks is ignored when --tier is specified", file=sys.stderr)
    if args.test_fallbacks and not args.tier:
        fallbacks = load_fallback_aliases()
        tiers_to_test.extend(fallbacks)
        if not args.json_output:
            print(f"  Including {len(fallbacks)} fallback aliases", file=sys.stderr)

    # Tier tests
    tier_results = []
    for i, alias in enumerate(tiers_to_test):
        if not args.json_output:
            print(f"  Testing {alias}...", end="", flush=True, file=sys.stderr)
        result = check_tier(proxy_url, api_key, alias)
        tier_results.append(result)
        if not args.json_output:
            ms = result.get("elapsed_ms", 0)
            print(f" {'OK' if result['ok'] else 'FAIL'} ({ms}ms)", file=sys.stderr)
        if i < len(tiers_to_test) - 1:
            time.sleep(INTER_TIER_DELAY_S)

    # Evaluate latency thresholds
    latency_failures: list[str] = []
    for r in tier_results:
        if not r["ok"]:
            continue
        alias = r["alias"]
        ms = r.get("elapsed_ms", 0)
        thresholds = _latency_threshold(alias)
        if ms >= thresholds["fail"]:
            latency_failures.append(
                f"{alias}: {ms}ms exceeds fail threshold ({thresholds['fail']}ms)"
            )

    # Stream timeout check — measure inter-chunk gaps for primary tiers
    stream_results: list[dict] | None = None
    if args.check_stream_timeout:
        # Pause between the smoke-test phase and the streaming phase so the last
        # smoke-test request and the first streaming request don't burst the same
        # provider back-to-back (Gemini review comment on PR #240).
        if tier_results:
            time.sleep(INTER_TIER_DELAY_S)
        stream_timeout_s = _read_stream_timeout()
        if stream_timeout_s is None:
            if not args.json_output:
                print(
                    "  Skipping stream timeout gap check — stream_timeout not set in"
                    " router_settings (no enforcement active).",
                    file=sys.stderr,
                )
            stream_results = []
        else:
            if not args.json_output:
                print(
                    f"  Checking stream timeout gaps (stream_timeout={stream_timeout_s}s)...",
                    file=sys.stderr,
                )
            stream_results = []
            # Use TIERS (canonical primary list) unless the user explicitly named a tier,
            # in which case respect their choice regardless of whether it's a fallback alias.
            # The old suffix-filter was brittle (missed -glm5, -kimi, etc.) and silently
            # skipped explicitly-requested fallback tiers. Fix per Gemini review on PR #164.
            primary_tiers = tiers_to_test if args.tier else list(TIERS)
            for i, alias in enumerate(primary_tiers):
                if not args.json_output:
                    print(f"    Streaming {alias}...", end="", flush=True, file=sys.stderr)
                result = check_stream_timeout(proxy_url, api_key, alias, stream_timeout_s)
                stream_results.append(result)
                if not args.json_output:
                    gap = result.get("max_gap_ms", 0)
                    status = "OK" if result["ok"] else "FAIL"
                    warn = " WARN" if result.get("warning") and result["ok"] else ""
                    print(f" {status}{warn} (max_gap={gap}ms)", file=sys.stderr)
                if i < len(primary_tiers) - 1:
                    time.sleep(INTER_TIER_DELAY_S)

    # Model drift check
    drift_result: dict | None = None
    if args.check_drift:
        if not args.json_output:
            print("  Checking model drift...", end="", flush=True, file=sys.stderr)
        drift_result = check_model_drift(proxy_url, api_key)
        if not args.json_output:
            status = "OK" if drift_result.get("ok") else "DRIFT DETECTED"
            print(f" {status}", file=sys.stderr)

    # Langfuse trace verification
    langfuse_result: dict | None = None
    if not args.skip_langfuse and lf_pk and lf_sk:
        langfuse_result = check_langfuse_traces(lf_pk, lf_sk, lf_host, args.lookback)
    elif not args.skip_langfuse and not (lf_pk and lf_sk):
        if not args.json_output:
            print(
                "  Langfuse: SKIP — no credentials (set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY)",
                file=sys.stderr,
            )

    # Telemetry health check
    telemetry_result: dict | None = None
    if args.verify_telemetry:
        if not args.json_output:
            print("  Checking telemetry health...", end="", flush=True, file=sys.stderr)
        telemetry_result = check_telemetry_health(window_hours=args.telemetry_window)
        if not args.json_output:
            status = "OK" if telemetry_result["ok"] else "FAIL"
            total = telemetry_result["total"]
            unknown_pct = f"{telemetry_result['unknown_rate']:.0%}"
            print(f" {status} ({total} dispatches, {unknown_pct} unknown-tier)", file=sys.stderr)
            if not telemetry_result["ok"]:
                for err in telemetry_result["errors"]:
                    print(f"    FAIL: {err}", file=sys.stderr)

    # Cache affinity probe (advisory)
    cache_affinity_result: dict | None = None
    if args.check_cache_affinity:
        if not args.json_output:
            print(
                "  Checking Fireworks cache affinity (two-turn tier-2-agentic probe)...",
                end="",
                flush=True,
                file=sys.stderr,
            )
        cache_affinity_result = check_cache_affinity(proxy_url, api_key)
        if not args.json_output:
            if cache_affinity_result.get("skipped"):
                print(f" SKIP ({cache_affinity_result.get('warning')})", file=sys.stderr)
            elif cache_affinity_result.get("warning"):
                print(
                    f" WARN ({cache_affinity_result.get('turn1_ms')}ms → "
                    f"{cache_affinity_result.get('turn2_ms')}ms)",
                    file=sys.stderr,
                )
            else:
                print(
                    f" OK ({cache_affinity_result.get('turn1_ms')}ms → "
                    f"{cache_affinity_result.get('turn2_ms')}ms, "
                    f"speedup={cache_affinity_result.get('speedup_ratio')})",
                    file=sys.stderr,
                )

    elapsed = time.monotonic() - start
    routing_ok = health["ok"] and all(r["ok"] for r in tier_results)
    latency_ok = not latency_failures or not args.strict_latency
    drift_ok = (drift_result is None) or drift_result.get("ok", True)
    stream_ok = True
    if stream_results:
        hard_failures = [r for r in stream_results if not r["ok"]]
        warnings = [r for r in stream_results if r.get("warning") and r["ok"] and not r.get("skipped")]
        if hard_failures:
            stream_ok = False
        elif warnings and args.strict_stream_timeout:
            stream_ok = False
    langfuse_ok = True
    if args.langfuse_strict:
        if not (lf_pk and lf_sk):
            langfuse_ok = False  # Credentials missing — strict mode cannot be satisfied
            print(
                "  FAIL (strict): Langfuse credentials unavailable — set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY",
                file=sys.stderr,
            )
        elif langfuse_result is None or not langfuse_result.get("ok"):
            langfuse_ok = False  # API error or unexpectedly skipped
        elif langfuse_result.get("litellm", 0) == 0:
            langfuse_ok = False  # Callbacks not firing
    # --verify-telemetry implies --strict-telemetry (failures count toward exit code)
    telemetry_ok = (
        telemetry_result is None
        or telemetry_result.get("ok", True)
        or not (args.strict_telemetry or args.verify_telemetry)
    )
    overall_ok = routing_ok and latency_ok and drift_ok and langfuse_ok and stream_ok and telemetry_ok

    if args.json_output:
        print(json.dumps({
            "ok": overall_ok,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed, 2),
            "health": health,
            "tiers": tier_results,
            "langfuse": langfuse_result,
            "telemetry": telemetry_result,
            "latency_failures": latency_failures,
            "drift": drift_result,
            "stream_checks": stream_results,
            "cache_affinity": cache_affinity_result,
        }, indent=2))
    else:
        print_human_report(
            health, tier_results, langfuse_result, elapsed,
            latency_failures, drift_result, stream_results,
        )
        if telemetry_result is not None:
            tel = telemetry_result
            print(
                f"\nTelemetry ({tel['window_hours']}h): "
                f"{tel['total']} dispatches | "
                f"unknown-tier {tel['unknown_rate']:.0%} | "
                f"misroute {tel['misroute_rate']:.0%} | "
                f"anthropic-on-oss-eligible {tel['anthropic_oss_eligible']}",
                file=sys.stderr,
            )

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
