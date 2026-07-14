#!/usr/bin/env bash
# scripts/setup-litellm.sh
#
# Interactive setup: deploy a LiteLLM proxy to Fly.io so the multi-model
# tier framework (tier-1-fast, tier-2-agentic, tier-3-tool, tier-4-extract,
# tier-5-latency) routes to OSS providers (Fireworks, Together, Groq).
#
# Prerequisites:
#   - flyctl CLI installed and authenticated  (https://fly.io/docs/hands-on/install-flyctl/)
#   - API keys for the providers you want to enable
#   - openssl  (for generating LITELLM_MASTER_KEY)
#
# Usage:
#   bash scripts/setup-litellm.sh
#
# Multi-instance (different app per environment):
#   APP_NAME=myapp-litellm-staging bash scripts/setup-litellm.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROXY_DIR="$REPO_ROOT/deploy/litellm-proxy"
MODEL_ROUTING_JSON="$REPO_ROOT/.claude/model-routing.json"

PASS="\033[32m✓\033[0m"
FAIL="\033[31m✗\033[0m"
INFO="\033[36mℹ\033[0m"

log()  { echo -e "  ${INFO} $*"; }
ok()   { echo -e "  ${PASS} $*"; }
fail() { echo -e "  ${FAIL} $*" >&2; exit 1; }
ask()  { read -r -p "    $1 " "$2"; }

echo ""
echo "── LiteLLM Proxy Setup ─────────────────────────────────────────────────"
echo ""
echo "  This script deploys a LiteLLM proxy to Fly.io so tier names like"
echo "  'tier-1-fast' route to OSS providers, cutting cost 5–30× on"
echo "  internal/public data tasks vs Claude API pay-per-token."
echo ""

# ── 1. Check prerequisites ────────────────────────────────────────────────────
echo "Checking prerequisites..."

if ! command -v flyctl &>/dev/null; then
  fail "flyctl not found. Install from https://fly.io/docs/hands-on/install-flyctl/"
fi
ok "flyctl found: $(flyctl version | head -1)"

if ! command -v openssl &>/dev/null; then
  fail "openssl not found. Install OpenSSL to generate LITELLM_MASTER_KEY."
fi
ok "openssl found"

if [ ! -d "$PROXY_DIR" ]; then
  fail "deploy/litellm-proxy/ not found in repo root ($REPO_ROOT). Clone the full starter pack."
fi
ok "deploy/litellm-proxy/ found"

# ── 2. Configuration ──────────────────────────────────────────────────────────
echo ""
echo "Configuration:"
echo ""

APP_NAME="${APP_NAME:-}"
if [ -z "$APP_NAME" ]; then
  ask "Fly.io app name (e.g. myapp-litellm):" APP_NAME
fi
[ -n "$APP_NAME" ] || fail "App name cannot be empty."
ok "App name: $APP_NAME"

# ── 3. Collect API keys ───────────────────────────────────────────────────────
echo ""
echo "API keys (leave blank to skip a provider; at least one OSS provider required):"
echo ""

_prompt_key() {
  local var_name="$1" label="$2" env_var="${3:-}"
  local current=""
  if [ -n "$env_var" ]; then current="${!env_var:-}"; fi
  if [ -n "$current" ]; then
    echo -e "    ${PASS} $label — using \$$env_var from environment"
    eval "$var_name=\"\$current\""
    return
  fi
  ask "$label (press Enter to skip):" "$var_name"
}

_prompt_key ANTHROPIC_KEY   "ANTHROPIC_API_KEY  (Claude passthrough + fallback routes)"        ANTHROPIC_API_KEY
_prompt_key FIREWORKS_KEY   "FIREWORKS_API_KEY  (tier-1-fast, tier-0-oss-heavy)"               FIREWORKS_API_KEY
_prompt_key TOGETHER_KEY    "TOGETHER_API_KEY   (tier-2-agentic — Kimi K2.6, sonnet alias)"    TOGETHER_API_KEY
_prompt_key GROQ_KEY        "GROQ_API_KEY       (tier-4-extract, tier-5-latency)"              GROQ_API_KEY
_prompt_key OPENROUTER_KEY  "OPENROUTER_API_KEY (tier-3-tool — Gemma 4 31B)"                   OPENROUTER_API_KEY

if [ -z "$ANTHROPIC_KEY" ] && [ -z "$FIREWORKS_KEY" ] && [ -z "$TOGETHER_KEY" ] && [ -z "$GROQ_KEY" ] && [ -z "$OPENROUTER_KEY" ]; then
  fail "At least one provider key is required (Anthropic, Fireworks, Together, Groq, or OpenRouter)."
fi

# ── 4. Deploy to Fly.io ───────────────────────────────────────────────────────
echo ""
echo "Deploying to Fly.io..."
echo ""

# Create Fly app if it doesn't exist
if ! flyctl status --app "$APP_NAME" &>/dev/null; then
  log "Creating Fly app '$APP_NAME'..."
  flyctl apps create "$APP_NAME" || fail "Failed to create Fly app '$APP_NAME'."
  ok "Fly app '$APP_NAME' created"
else
  ok "Fly app '$APP_NAME' already exists"
fi

# Generate LITELLM_MASTER_KEY (local run). For CI/shared use, store this in
# Doppler litellm/prd so clients send the same key.
LITELLM_MASTER_KEY="$(openssl rand -hex 24)"
ok "Generated LITELLM_MASTER_KEY"

# Push secrets (only non-empty keys)
log "Pushing secrets to Fly..."
SECRETS_ARGS=("LITELLM_MASTER_KEY=$LITELLM_MASTER_KEY")
[ -n "$ANTHROPIC_KEY"   ] && SECRETS_ARGS+=("ANTHROPIC_API_KEY=$ANTHROPIC_KEY")
[ -n "$FIREWORKS_KEY"   ] && SECRETS_ARGS+=("FIREWORKS_API_KEY=$FIREWORKS_KEY")
[ -n "$TOGETHER_KEY"    ] && SECRETS_ARGS+=("TOGETHER_API_KEY=$TOGETHER_KEY")
[ -n "$GROQ_KEY"        ] && SECRETS_ARGS+=("GROQ_API_KEY=$GROQ_KEY")
[ -n "$OPENROUTER_KEY"  ] && SECRETS_ARGS+=("OPENROUTER_API_KEY=$OPENROUTER_KEY")

# Pass via stdin to avoid secrets appearing in process list
printf '%s\n' "${SECRETS_ARGS[@]}" | flyctl secrets import --app "$APP_NAME"
ok "Secrets pushed"

# Deploy
log "Deploying proxy image (this may take ~2 minutes)..."
( cd "$PROXY_DIR" && set -o pipefail && timeout 300 flyctl deploy --app "$APP_NAME" --remote-only 2>&1 | tail -5 )
ok "Deployed"

# ── 5. Health check ───────────────────────────────────────────────────────────
echo ""
echo "Verifying deployment..."

PROXY_URL="https://$APP_NAME.fly.dev"
sleep 3

if curl --max-time 30 --connect-timeout 10 -sf "$PROXY_URL/health/liveliness" &>/dev/null; then
  ok "Health check passed: $PROXY_URL"
else
  log "Health check pending — proxy may still be starting. Try manually:"
  echo "    curl $PROXY_URL/health/liveliness"
fi

# ── 6. Smoke test enabled tiers ───────────────────────────────────────────────
echo ""
echo "Smoke testing active tiers..."

_smoke_test() {
  local tier="$1"
  # Reasoning models (DeepSeek V4 on sonnet/haiku) spend thinking tokens from
  # the max_tokens budget before emitting text — 5 tokens produces an empty
  # completion and a false FAIL. 128 covers thinking + a short reply while
  # keeping the smoke cheap. Success also requires non-empty message content.
  local max_tokens="${2:-128}"
  local result
  result=$(curl --max-time 30 --connect-timeout 10 -sf -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$tier\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":$max_tokens}" \
    2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = (d.get('choices') or [{}])[0].get('message', {}).get('content') or ''
print('OK' if c.strip() else 'FAIL')" 2>/dev/null || echo "UNREACHABLE")
  if [ "$result" = "OK" ]; then
    ok "$tier"
  else
    echo -e "  ! $tier: $result (may need a different model for this provider — check deploy/litellm-proxy/config.yaml)"
  fi
}

[ -n "$FIREWORKS_KEY"   ] && _smoke_test "tier-1-fast"
[ -n "$TOGETHER_KEY"   ] && _smoke_test "tier-2-agentic"
[ -n "$OPENROUTER_KEY" ] && _smoke_test "tier-3-tool"
[ -n "$GROQ_KEY"       ] && _smoke_test "tier-4-extract"
[ -n "$GROQ_KEY"       ] && _smoke_test "tier-5-latency"
# sonnet is Fireworks-primary (DeepSeek V4 Pro) with a mandatory Groq advisor;
# Together is only an optional fallback hop — gate on the keys the path needs.
[ -n "$FIREWORKS_KEY" ] && [ -n "$GROQ_KEY" ] && _smoke_test "sonnet"
[ -n "$FIREWORKS_KEY" ] && _smoke_test "haiku"

# ── 7. Update model-routing.json ─────────────────────────────────────────────
echo ""
echo "Updating .claude/model-routing.json..."

if command -v python3 &>/dev/null && [ -f "$MODEL_ROUTING_JSON" ]; then
  python3 - <<PYEOF
import json, sys
path = "$MODEL_ROUTING_JSON"
try:
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("litellm_proxy", {})["enabled"] = True
    cfg["litellm_proxy"]["url"] = "$PROXY_URL"
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    print("  \033[32m✓\033[0m Updated litellm_proxy.enabled=true and url=$PROXY_URL")
except Exception as e:
    print(f"  ! Could not auto-update model-routing.json: {e}", file=sys.stderr)
    print("  ! Update manually: set litellm_proxy.enabled=true and url=$PROXY_URL")
PYEOF
else
  log "Update model-routing.json manually:"
  echo "    Set litellm_proxy.enabled=true and url=\"$PROXY_URL\""
fi

# ── 8. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────────────────────────────"
echo ""
echo "  Proxy live at: $PROXY_URL"
echo "  Master key:    $LITELLM_MASTER_KEY"
echo ""
echo "  Store these in your secrets manager (Doppler / .env / CI):"
echo "    LITELLM_PROXY_URL=$PROXY_URL"
echo "    LITELLM_MASTER_KEY=$LITELLM_MASTER_KEY"
echo ""
echo "  USE_LITELLM_PROXY=true in your app environment activates OSS routing."
echo "  See .claude/model-routing.md for the full routing guide."
echo ""
