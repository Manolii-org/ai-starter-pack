#!/usr/bin/env bash
# Session Start Hook — 30s budget
# Loads credentials from Doppler (with 1hr cache) and sets up session health.
set -euo pipefail

CACHE_DIR=".git/.credential-cache"
CACHE_FILE="$CACHE_DIR/secrets.json"
CACHE_TTL=3600  # 1 hour in seconds
HEALTH_FILE=".git/.session-health"

log() { echo "[session-start] $*" >&2; }

# --- Credential Loading ---

load_from_cache() {
  if [[ -f "$CACHE_FILE" ]]; then
    local age
    age=$(( $(date +%s) - $(stat -c %Y "$CACHE_FILE" 2>/dev/null || stat -f %m "$CACHE_FILE" 2>/dev/null || echo 0) ))
    if (( age < CACHE_TTL )); then
      log "Using cached credentials (${age}s old)"
      return 0
    fi
    log "Cache expired (${age}s old)"
  fi
  return 1
}

fetch_from_doppler() {
  # Prefer the ecosystem loader if available (e.g., in master repo)
  local ecosystem_loader="${CLAUDE_PLUGIN_ROOT:-.}/scripts/load-ecosystem.sh"
  if [[ -f "$ecosystem_loader" ]]; then
    log "Using ecosystem loader: $ecosystem_loader"
    # shellcheck source=/dev/null
    source "$ecosystem_loader" 2>/dev/null && return 0
  fi

  local token="${DOPPLER_TOKEN_PRD:-${DOPPLER_TOKEN:-${DOPPLER_PERSONAL:-}}}"
  if [[ -z "$token" ]]; then
    log "No DOPPLER_TOKEN_PRD or DOPPLER_TOKEN found — skipping secret loading"
    return 1
  fi

  local project="${DOPPLER_PROJECT:-$(basename "$(pwd)")}"
  local config="${DOPPLER_CONFIG:-prd}"

  log "Fetching secrets from Doppler ($project/$config)..."
  local secrets
  secrets=$(curl -fsS --max-time 15 \
    "https://api.doppler.com/v3/configs/config/secrets/download?format=json&project=${project}&config=${config}" \
    -H "Authorization: Bearer ${token}" 2>/dev/null) || {
    log "Doppler fetch failed — continuing without secrets"
    return 1
  }

  mkdir -p "$CACHE_DIR"
  # Cache only the keys we actually need — not the full Doppler project dump
  local filtered
  if ! filtered=$(echo "$secrets" | jq '{GH_TOKEN, SUPABASE_ACCESS_TOKEN, VERCEL_TOKEN, VOYAGE_API_KEY, LLM_API_KEY, MCP_API_KEY} | with_entries(select(.value != null))' 2>/dev/null); then
    log "Warning: jq filtering failed — caching only empty object to avoid full secret dump"
    filtered='{}'
  fi
  (umask 077 && echo "$filtered" > "$CACHE_FILE")
  log "Secrets cached to $CACHE_FILE"
  return 0
}

write_env_file() {
  local env_file="${CLAUDE_ENV_FILE:-}"
  if [[ -z "$env_file" ]]; then
    return 0
  fi

  if [[ ! -f "$CACHE_FILE" ]]; then
    return 0
  fi

  # Extract commonly needed keys and write to env file (with dedup)
  # Ensure env file has restricted permissions
  if [[ ! -f "$env_file" ]]; then
    (umask 077 && touch "$env_file")
  else
    chmod 600 "$env_file" 2>/dev/null || true
  fi

  # Only extract specific keys needed for the session — NOT the full Doppler dump
  local keys=("GH_TOKEN" "SUPABASE_ACCESS_TOKEN" "VERCEL_TOKEN" "VOYAGE_API_KEY" "LLM_API_KEY" "MCP_API_KEY")
  for key in "${keys[@]}"; do
    local value
    value=$(jq -r --arg k "$key" '.[$k] // empty' "$CACHE_FILE" 2>/dev/null || true)
    if [[ -n "$value" ]]; then
      # Avoid duplicates if hook runs multiple times
      if ! grep -q "^${key}=" "$env_file" 2>/dev/null; then
        printf '%s=%q\n' "$key" "$value" >> "$env_file"
      fi
    fi
  done
}

export_gh_token() {
  # GH_TOKEN is already written by write_env_file if CLAUDE_ENV_FILE is set.
  # This function handles the case where CLAUDE_ENV_FILE is not available
  # by logging whether GH_TOKEN was found in the cache.
  if [[ -f "$CACHE_FILE" ]]; then
    local gh_token
    gh_token=$(jq -r '.GH_TOKEN // empty' "$CACHE_FILE" 2>/dev/null || true)
    if [[ -n "$gh_token" ]]; then
      log "GH_TOKEN available in cache"
    fi
  fi
}

# --- Main ---

# Fast path: use cache if fresh
if ! load_from_cache; then
  # Cold path: fetch from Doppler
  fetch_from_doppler || true
fi

write_env_file
export_gh_token

# Clear sensitive vars from hook process before health checks
unset DOPPLER_TOKEN_PRD DOPPLER_TOKEN DOPPLER_PERSONAL 2>/dev/null || true

# --- Session Health ---

health_status="ok"
health_notes=()

if [[ -f "$CACHE_FILE" ]]; then
  health_notes+=("credentials: loaded")
else
  health_notes+=("credentials: not available")
  health_status="degraded"
fi

if command -v node &>/dev/null; then
  health_notes+=("node: $(node --version 2>/dev/null || echo 'error')")
else
  health_notes+=("node: not found")
fi

if command -v python3 &>/dev/null; then
  health_notes+=("python3: $(python3 --version 2>/dev/null || echo 'error')")
else
  health_notes+=("python3: not found")
fi

if [[ -f "package.json" ]]; then
  health_notes+=("package.json: present")
else
  health_notes+=("package.json: not found")
fi

# Write health marker
cat > "$HEALTH_FILE" <<EOF
status: $health_status
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
$(printf '%s\n' "${health_notes[@]}")
EOF


# Inject local retrospective navigation warnings (best-effort, <1s, never blocks).
_retro_inject() {
  local root="${CLAUDE_PLUGIN_ROOT:-}"
  local candidate=""
  if [ -n "$root" ] && [ -f "$root/scripts/session-retrospective.py" ]; then
    candidate="$root/scripts/session-retrospective.py"
  elif [ -f "scripts/session-retrospective.py" ]; then
    candidate="scripts/session-retrospective.py"
  fi
  [ -n "$candidate" ] || return 0
  python3 "$candidate" --mode inject >/dev/null 2>&1 || true
}
_retro_inject

log "Session health: $health_status"
for note in "${health_notes[@]}"; do
  log "  $note"
done

# WS3: refresh + surface the recent-navigation-warning file so high-
# dysfunction prior sessions produce an agent-visible warning at
# SessionStart. Fail-open. Codex P2 2026-07-19.
_REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
_INJECT="${CLAUDE_PLUGIN_ROOT:-$_REPO_ROOT}/scripts/session-start-inject-warning.sh"
[ -x "$_INJECT" ] || _INJECT="$_REPO_ROOT/scripts/session-start-inject-warning.sh"
if [ -x "$_INJECT" ]; then
    bash "$_INJECT" >/dev/null 2>&1 || true
fi
if [ -s "$_REPO_ROOT/.ai/recent-navigation-warning.md" ]; then
    echo ""
    echo "=== Recent navigation warning ==="
    cat "$_REPO_ROOT/.ai/recent-navigation-warning.md" 2>/dev/null || true
    echo ""
fi
