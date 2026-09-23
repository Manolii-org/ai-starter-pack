#!/usr/bin/env bash
# cross-repo-preflight.sh — Run at session start in Claude Code Web remote containers.
# Fetches GH_TOKEN from Doppler and writes ~/.netrc so git works across repos.
# Usage: bash scripts/cross-repo-preflight.sh
set -euo pipefail

SESSION_CONTEXT=".ai/session-context.md"
log() { echo "[preflight] $*" >&2; }

# --- Try to get GH_TOKEN ---
GH_TOKEN_VALUE="${GH_TOKEN:-}"

if [[ -z "$GH_TOKEN_VALUE" ]]; then
  # Try Doppler API with DOPPLER_TOKEN_PRD
  DOPPLER_TOKEN="${DOPPLER_TOKEN_PRD:-}"
  if [[ -n "$DOPPLER_TOKEN" ]]; then
    DOPPLER_PROJECT="${DOPPLER_PROJECT:-$(basename "$(pwd)")}"
    log "Fetching GH_TOKEN from Doppler ($DOPPLER_PROJECT/prd)..."
    GH_TOKEN_VALUE=$(curl -fsS --max-time 30 --connect-timeout 10 \
      "https://api.doppler.com/v3/configs/config/secrets/download?format=json&project=${DOPPLER_PROJECT}&config=prd" \
      -H "Authorization: Bearer ${DOPPLER_TOKEN}" 2>/dev/null \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('GH_TOKEN',''))" 2>/dev/null || echo "")
  fi
fi

if [[ -z "$GH_TOKEN_VALUE" ]]; then
  # Try .git/.credential-cache from session-start hook
  CACHE_FILE=".git/.credential-cache/secrets.json"
  if [[ -f "$CACHE_FILE" ]]; then
    GH_TOKEN_VALUE=$(python3 -c "import json; print(json.load(open('$CACHE_FILE')).get('GH_TOKEN',''))" 2>/dev/null || echo "")
  fi
fi

# --- Write ~/.netrc ---
STATUS="BLOCKED"
if [[ -n "$GH_TOKEN_VALUE" ]]; then
  if [[ -f ~/.netrc ]]; then
    cp -p ~/.netrc "$HOME/.netrc.preflight-bak" 2>/dev/null || true
    log "backed up existing netrc to \$HOME/.netrc.preflight-bak"
  fi
  (umask 077 && cat > ~/.netrc <<EOF
machine github.com
  login x-access-token
  password ${GH_TOKEN_VALUE}
machine api.github.com
  login x-access-token
  password ${GH_TOKEN_VALUE}
EOF
)
  log "netrc written with GitHub token (\$HOME/.netrc)"
  STATUS="READY"
else
  log "WARNING: Could not obtain GH_TOKEN — cross-repo git operations will fail"
  STATUS="DEGRADED"
fi

# --- Write session context stub ---
mkdir -p "$(dirname "$SESSION_CONTEXT")"

# Write/refresh preflight status (overwrite previous preflight block if exists)
PREFLIGHT_BLOCK="## Preflight Status ($(date -u +%Y-%m-%dT%H:%M:%SZ))
- Cross-repo auth: $STATUS
- GH_TOKEN: $([ -n "$GH_TOKEN_VALUE" ] && echo "loaded" || echo "not available")
- Run from: Claude Code Web remote container"

# Remove old preflight block if present, then append fresh one
if grep -q "^## Preflight Status" "$SESSION_CONTEXT" 2>/dev/null; then
  # Remove old block (from ## Preflight Status to next ## heading or EOF)
  python3 -c "
import re, sys
content = open('$SESSION_CONTEXT').read()
content = re.sub(r'\n## Preflight Status.*?(?=\n## |\Z)', '', content, flags=re.DOTALL)
open('$SESSION_CONTEXT', 'w').write(content)
" 2>/dev/null || true
fi
printf '\n%s\n' "$PREFLIGHT_BLOCK" >> "$SESSION_CONTEXT"

log "Preflight complete — status: $STATUS"
echo "$STATUS"
