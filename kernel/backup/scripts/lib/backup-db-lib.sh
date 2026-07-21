#!/usr/bin/env bash
# Shared backup helpers — source from other scripts; do not execute directly.
set -euo pipefail

BACKUP_DB_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPTS_DIR="$(cd "${BACKUP_DB_LIB_DIR}/.." && pwd)"

backup_doppler_token() {
  if [ -n "${DOPPLER_TOKEN:-}" ]; then
    printf '%s' "$DOPPLER_TOKEN"
  elif [ -n "${DOPPLER_PERSONAL:-}" ]; then
    printf '%s' "$DOPPLER_PERSONAL"
  else
    return 1
  fi
}

backup_require_doppler_token() {
  if ! backup_doppler_token >/dev/null 2>&1; then
    echo "ERROR: DOPPLER_TOKEN or DOPPLER_PERSONAL must be set" >&2
    exit 1
  fi
}

backup_fetch_doppler_secrets_json() {
  local project="$1"
  local token
  token="$(backup_doppler_token)" || {
    echo "ERROR: DOPPLER_TOKEN or DOPPLER_PERSONAL must be set" >&2
    return 1
  }
  curl -fsS --max-time 30 --connect-timeout 10 \
    "https://api.doppler.com/v3/configs/config/secrets/download?format=json&project=${project}&config=prd" \
    -H "Authorization: Bearer ${token}"
}

backup_r2_s3_secret_access_key() {
  # Cloudflare R2's S3 API expects AWS_SECRET_ACCESS_KEY to be the SHA-256 hex digest
  # of the Cloudflare API token value — NOT the raw token. The Access Key ID is the
  # token id (BACKUP_CF_TOKEN_ID), used as-is. Passing the raw token returns HTTP 403
  # SignatureDoesNotMatch. See scripts/cloudflare-r2-setup.sh.
  local token="${1:?backup_r2_s3_secret_access_key: token required}"
  # sha256sum (GNU) on Linux runners; shasum -a 256 fallback for macOS/BSD operators.
  printf '%s' "$token" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -d' ' -f1
}

backup_resolve_db_url() {
  local project="$1"
  local secrets_json
  secrets_json="$(backup_fetch_doppler_secrets_json "$project")" || {
    echo "ERROR: failed to download secrets from Doppler project ${project}" >&2
    return 1
  }
  python3 "${BACKUP_SCRIPTS_DIR}/backup-resolve-db-url.py" <<<"$secrets_json"
}

backup_pg_dump_url() {
  local db_url="$1"
  shift
  # libpq reads the password from the URI; avoid printing the URL.
  pg_dump --format=custom --no-password "$@" "$db_url"
}

backup_pg_dumpall_globals() {
  local db_url="$1"
  local outfile="$2"
  pg_dumpall --globals-only --no-password -d "$db_url" -f "$outfile" 2>/dev/null || return 1
}

backup_verify_dump() {
  local outfile="$1"
  if ! pg_restore --list "$outfile" >/dev/null 2>&1; then
    echo "ERROR: pg_restore --list failed on ${outfile} — dump is corrupt" >&2
    return 1
  fi
  echo "PASS: integrity check passed for ${outfile}"
}

# Run a long I/O or restore command under GNU timeout so hung aws/openssl/pg_*
# cannot strand a self-hosted runner until the workflow timeout-minutes fires.
# Usage: backup_run_timed SECONDS LABEL -- command [args...]
# Exit 124 → timed out (message printed). Requires `timeout` on PATH (Linux CI).
backup_run_timed() {
  local secs="${1:?backup_run_timed: seconds required}"
  local label="${2:?backup_run_timed: label required}"
  shift 2
  if [ "${1:-}" = "--" ]; then
    shift
  fi
  if [ "$#" -lt 1 ]; then
    echo "ERROR: backup_run_timed ${label}: command required" >&2
    return 1
  fi
  if ! command -v timeout >/dev/null 2>&1; then
    echo "ERROR: GNU timeout required for ${label} (install coreutils)" >&2
    return 127
  fi
  # --foreground: deliver SIGTERM/SIGKILL to the child process group in CI.
  if timeout --foreground "${secs}" "$@"; then
    return 0
  fi
  local rc=$?
  if [ "$rc" -eq 124 ]; then
    echo "ERROR: ${label} timed out after ${secs}s" >&2
  fi
  return "$rc"
}
