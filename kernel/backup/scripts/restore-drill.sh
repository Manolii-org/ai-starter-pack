#!/usr/bin/env bash
# restore-drill.sh — Safe restore-to-branch drill for backup verification.
# Usage: restore-drill.sh --entity {manolii|personal|impaktful} [--dry-run] [--help]
#
# Required env vars:
#   DOPPLER_TOKEN   — Workspace-level Doppler token (same as DOPPLER_PERSONAL in CI)
# Optional env vars:
#   BACKUP_ENCRYPTION_KEY        — AES key; if set, overrides Doppler value (use repo secret)
#   RESTORE_DRILL_NEON_ROLE      — Neon role name (default: neondb_owner)
#   RESTORE_DRILL_NEON_DB        — Neon database name (default: neondb)
set -euo pipefail

# Shared R2 helpers — backup_r2_s3_secret_access_key() derives the R2 S3 secret
# (= sha256 of the CF API token); the raw token returns HTTP 403 SignatureDoesNotMatch.
RESTORE_DRILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/backup-db-lib.sh
source "${RESTORE_DRILL_DIR}/lib/backup-db-lib.sh"

ENTITY=""
DRY_RUN=false
BRANCH_ID=""
NEON_PROJECT_ID=""
NEON_API_KEY=""

usage() {
  cat <<EOF
Usage: $(basename "$0") --entity {manolii|personal|impaktful} [--dry-run] [--help]

Options:
  --entity    Required. One of: manolii, personal, impaktful
  --dry-run   Show what would happen without creating or deleting resources
  --help      Show this message and exit 0

Required env: DOPPLER_TOKEN (workspace-level Doppler token)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --entity)   ENTITY="${2:?--entity requires a value}"; shift 2 ;;
    --dry-run)  DRY_RUN=true; shift ;;
    --help)     usage; exit 0 ;;
    *)          echo "Unknown flag: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [ -z "$ENTITY" ]; then
  echo "ERROR: --entity is required" >&2
  usage >&2
  exit 1
fi

case "$ENTITY" in
  manolii)   DOPPLER_PROJECT="manolii-knowledge-layer" ;;
  personal)  DOPPLER_PROJECT="personal-knowledge-layer" ;;
  impaktful) DOPPLER_PROJECT="impaktful-knowledge-layer" ;;
  *)         echo "ERROR: Invalid entity '${ENTITY}'. Must be one of: manolii, personal, impaktful" >&2; exit 1 ;;
esac

if [ "$DRY_RUN" = "true" ]; then
  echo "[dry-run] Would drill restore for entity: ${ENTITY} (Doppler project: ${DOPPLER_PROJECT})"
  echo "[dry-run] Steps: fetch R2 creds (${DOPPLER_PROJECT}) + Neon creds (master/prd) => create Neon branch => download latest R2 dump => decrypt => pg_restore => row counts => write JSON => delete branch"
  exit 0
fi

[ -n "${DOPPLER_TOKEN:-}" ] || { echo "ERROR: DOPPLER_TOKEN env var is required" >&2; exit 1; }

START_TIME=$(date +%s)
DRILL_TMP=$(mktemp -d)
RESULT_DIR="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel 2>/dev/null || pwd)/.backup/drill-results"

mkdir -p "$RESULT_DIR"
trap 'rm -rf "$DRILL_TMP"' EXIT

echo "== Restore Drill: ${ENTITY} =="

# Step 1 — Fetch R2 + encryption credentials from per-entity Doppler project
echo "[1/9] Fetching R2 credentials from Doppler project: ${DOPPLER_PROJECT}"
ENTITY_SECRETS=$(curl -fsS --max-time 30 --connect-timeout 10 \
  "https://api.doppler.com/v3/configs/config/secrets/download?format=json&project=${DOPPLER_PROJECT}&config=prd" \
  -H "Authorization: Bearer ${DOPPLER_TOKEN}") || {
  echo "ERROR: Failed to fetch secrets from Doppler project ${DOPPLER_PROJECT}" >&2; exit 1
}

BACKUP_CF_TOKEN_ID=$(echo "$ENTITY_SECRETS"   | jq -r '.BACKUP_CF_TOKEN_ID // empty')
BACKUP_CF_API_TOKEN=$(echo "$ENTITY_SECRETS"  | jq -r '.BACKUP_CF_API_TOKEN // empty')
BACKUP_CF_ACCOUNT_ID=$(echo "$ENTITY_SECRETS" | jq -r '.BACKUP_CF_ACCOUNT_ID // empty')
BACKUP_R2_BUCKET=$(echo "$ENTITY_SECRETS"     | jq -r '.BACKUP_R2_BUCKET // empty')
# BACKUP_ENCRYPTION_KEY: env var (repo secret) takes precedence over Doppler value
BACKUP_ENCRYPTION_KEY="${BACKUP_ENCRYPTION_KEY:-$(echo "$ENTITY_SECRETS" | jq -r '.BACKUP_ENCRYPTION_KEY // empty')}"

for var in BACKUP_CF_TOKEN_ID BACKUP_CF_API_TOKEN BACKUP_CF_ACCOUNT_ID BACKUP_R2_BUCKET; do
  [ -n "${!var}" ] || { echo "ERROR: ${var} not found in Doppler ${DOPPLER_PROJECT}" >&2; exit 1; }
done

# Step 2 — Fetch Neon API key + scratch project ID from master/prd
echo "[2/9] Fetching Neon credentials from Doppler project: master"
MASTER_SECRETS=$(curl -fsS --max-time 30 --connect-timeout 10 \
  "https://api.doppler.com/v3/configs/config/secrets/download?format=json&project=master&config=prd" \
  -H "Authorization: Bearer ${DOPPLER_TOKEN}") || {
  echo "ERROR: Failed to fetch secrets from Doppler project master" >&2; exit 1
}

NEON_API_KEY=$(echo "$MASTER_SECRETS"    | jq -r '.NEON_API_KEY // empty')
NEON_PROJECT_ID=$(echo "$MASTER_SECRETS" | jq -r '.RESTORE_DRILL_NEON_PROJECT_ID // empty')

for var in NEON_API_KEY NEON_PROJECT_ID; do
  [ -n "${!var}" ] || { echo "ERROR: ${var} not found in Doppler master/prd — add RESTORE_DRILL_NEON_PROJECT_ID" >&2; exit 1; }
done

# Step 3 — Create disposable Neon branch
echo "[3/9] Creating Neon restore-drill branch"
BRANCH_NAME="restore-drill-${ENTITY}-$(date +%s)"
BRANCH_RESPONSE=$(curl -sS --max-time 30 --connect-timeout 10 \
  -X POST "https://console.neon.tech/api/v2/projects/${NEON_PROJECT_ID}/branches" \
  -H "Authorization: Bearer ${NEON_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"branch\":{\"name\":\"${BRANCH_NAME}\"},\"endpoints\":[{\"type\":\"read_write\"}]}") || {
  echo "ERROR: Neon branch create request failed" >&2; exit 1
}

BRANCH_ID=$(echo "$BRANCH_RESPONSE" | jq -r '.branch.id // empty')
[ -n "$BRANCH_ID" ] || { echo "ERROR: Neon branch creation failed - no branch ID returned" >&2; echo "$BRANCH_RESPONSE" >&2; exit 1; }
echo "  Branch: ${BRANCH_ID}"

# Poll until all branch operations complete — Neon ops can stay 'running'/'scheduling' for several seconds
OPERATIONS=$(echo "$BRANCH_RESPONSE" | jq -r '.operations[]?.id // empty' 2>/dev/null || true)
if [ -n "$OPERATIONS" ]; then
  echo "  Waiting for branch operations to complete..."
  for OP_ID in $OPERATIONS; do
    ATTEMPTS=0
    while [ $ATTEMPTS -lt 24 ]; do
      OP_STATUS=$(curl -sS --max-time 30 --connect-timeout 10 \
        "https://console.neon.tech/api/v2/projects/${NEON_PROJECT_ID}/operations/${OP_ID}" \
        -H "Authorization: Bearer ${NEON_API_KEY}" | jq -r '.operation.status // "unknown"')
      if [ "$OP_STATUS" = "finished" ]; then break; fi
      if [ "$OP_STATUS" = "failed" ] || [ "$OP_STATUS" = "error" ]; then
        echo "ERROR: Neon operation ${OP_ID} failed (status: ${OP_STATUS})" >&2; exit 1
      fi
      ATTEMPTS=$((ATTEMPTS + 1))
      sleep 5
    done
    [ $ATTEMPTS -lt 24 ] || { echo "ERROR: Timed out waiting for Neon operation ${OP_ID}" >&2; exit 1; }
  done
  echo "  Branch operations: COMPLETE"
fi

# Register full cleanup trap (overwrites the tmp-only trap above).
# Note: Neon's API v2 does not expose a hard/permanent-delete endpoint; the DELETE call marks the
# branch deleted subject to Neon's platform grace period. RESTORE_DRILL_NEON_PROJECT_ID must point
# to a dedicated scratch project (not an application project) so any recoverable branch holds only
# backup data, not live production data.
trap 'if [ -n "${BRANCH_ID:-}" ] && [ -n "${NEON_PROJECT_ID:-}" ] && [ -n "${NEON_API_KEY:-}" ]; then
  echo "[cleanup] Deleting Neon branch ${BRANCH_ID}"
  curl -sS --max-time 30 --connect-timeout 10 \
    -X DELETE "https://console.neon.tech/api/v2/projects/${NEON_PROJECT_ID}/branches/${BRANCH_ID}" \
    -H "Authorization: Bearer ${NEON_API_KEY}" > /dev/null 2>&1 || true
fi
rm -rf "$DRILL_TMP"' EXIT

# Resolve connection URI — try inline first, fall back to explicit API call
NEON_BRANCH_DSN=$(echo "$BRANCH_RESPONSE" | jq -r '.connection_uris[0].connection_uri // empty')
if [ -z "$NEON_BRANCH_DSN" ]; then
  NEON_ROLE="${RESTORE_DRILL_NEON_ROLE:-neondb_owner}"
  NEON_DB="${RESTORE_DRILL_NEON_DB:-neondb}"
  NEON_BRANCH_DSN=$(curl -sS --max-time 30 --connect-timeout 10 \
    "https://console.neon.tech/api/v2/projects/${NEON_PROJECT_ID}/connection_uri?branch_id=${BRANCH_ID}&role_name=${NEON_ROLE}&database_name=${NEON_DB}" \
    -H "Authorization: Bearer ${NEON_API_KEY}" \
    | jq -r '.uri // empty') || true
fi
[ -n "$NEON_BRANCH_DSN" ] || { echo "ERROR: Could not resolve Neon branch DSN — set RESTORE_DRILL_NEON_ROLE and RESTORE_DRILL_NEON_DB in master/prd if non-default" >&2; exit 1; }

# Step 4 — Find and download only the latest dump from R2
R2_ENDPOINT="https://${BACKUP_CF_ACCOUNT_ID}.r2.cloudflarestorage.com"
# R2's S3 API requires AWS_SECRET_ACCESS_KEY = sha256(CF API token), NOT the raw token
# (raw token -> HTTP 403 SignatureDoesNotMatch). Same derivation as backup-pgdump.yml.
R2_SECRET="$(backup_r2_s3_secret_access_key "$BACKUP_CF_API_TOKEN")"
# Dumps are written under pgdump/<entity>/ (current); fall back to the legacy <entity>/ prefix.
LATEST_KEY=""
R2_PREFIX=""
for prefix in "pgdump/${ENTITY}/" "${ENTITY}/"; do
  echo "[4/9] Looking for latest dump in R2: ${BACKUP_R2_BUCKET}/${prefix}"
  # Filter for 'supabase' in the filename to avoid picking up Neon app dumps that share
  # the same prefix. backup-pgdump.yml writes both backup-<entity>-supabase-*.dump and
  # backup-<app>-neon-*.dump under pgdump/<entity>/ when the Neon app name matches the
  # entity name (e.g. pgdump/impaktful/ holds both). The Neon dump has no KL tables, so
  # restoring it and querying pending_actions/standing_orders/project_facts returns ERROR.
  candidate=$(AWS_ACCESS_KEY_ID="$BACKUP_CF_TOKEN_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET" \
    aws s3 ls --endpoint-url "$R2_ENDPOINT" "s3://${BACKUP_R2_BUCKET}/${prefix}" \
    | grep -E 'supabase.*\.(dump\.enc|dump)$' | sort | tail -1 | awk '{print $NF}' || true)
  if [ -n "$candidate" ]; then LATEST_KEY="$candidate"; R2_PREFIX="$prefix"; break; fi
done

[ -n "$LATEST_KEY" ] || { echo "ERROR: No supabase dump files found in R2 (checked pgdump/${ENTITY}/ and legacy ${ENTITY}/)" >&2; exit 1; }
echo "  Downloading: ${R2_PREFIX}${LATEST_KEY}"

AWS_ACCESS_KEY_ID="$BACKUP_CF_TOKEN_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET" \
  aws s3 cp --endpoint-url "$R2_ENDPOINT" \
  "s3://${BACKUP_R2_BUCKET}/${R2_PREFIX}${LATEST_KEY}" \
  "$DRILL_TMP/$(basename "$LATEST_KEY")" || { echo "ERROR: Failed to download dump from R2" >&2; exit 1; }

# Step 5 — Decrypt or use plaintext dump
echo "[5/9] Preparing dump file"
DOWNLOADED="$DRILL_TMP/$(basename "$LATEST_KEY")"
if [[ "$DOWNLOADED" == *.enc ]]; then
  echo "  Decrypting: $(basename "$DOWNLOADED")"
  [ -n "$BACKUP_ENCRYPTION_KEY" ] || { echo "ERROR: BACKUP_ENCRYPTION_KEY required but not set" >&2; exit 1; }
  BACKUP_ENCRYPTION_KEY="$BACKUP_ENCRYPTION_KEY" \
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
    -pass env:BACKUP_ENCRYPTION_KEY \
    -in "$DOWNLOADED" \
    -out "$DRILL_TMP/restore.dump" || { echo "ERROR: Decryption failed" >&2; exit 1; }
else
  echo "  Using plaintext dump: $(basename "$DOWNLOADED")"
  cp "$DOWNLOADED" "$DRILL_TMP/restore.dump" || { echo "ERROR: Failed to copy dump file" >&2; exit 1; }
fi

# Step 6 — Validate dump integrity
echo "[6/9] Validating dump integrity"
pg_restore --list "$DRILL_TMP/restore.dump" > /dev/null 2>&1 || {
  echo "ERROR: pg_restore --list failed - dump file is corrupt" >&2; exit 1
}
echo "  Integrity check: PASS"

# Step 7 — Restore to Neon branch.
# Supabase dumps reference Supabase-only extensions/roles/schemas (pg_cron, supabase_vault,
# cron, vault, authenticated) absent on a vanilla Neon branch, so pg_restore exits non-zero on
# those benign objects even when every public.* table restores cleanly. Tolerate the non-zero
# exit here; step 8's row-count verification is the authoritative restorability signal.
echo "[7/9] Restoring to Neon branch (Supabase-only extension/role errors are expected, non-fatal)"
pg_restore --no-owner --no-acl -d "$NEON_BRANCH_DSN" "$DRILL_TMP/restore.dump" \
  > "$DRILL_TMP/pg_restore.log" 2>&1 || true
PG_RESTORE_ERRORS=$(grep -c '^pg_restore: error:' "$DRILL_TMP/pg_restore.log" 2>/dev/null || true)
echo "  Restore: DONE (non-fatal pg_restore errors: ${PG_RESTORE_ERRORS:-0})"

# Step 7b — Also restore the latest activity_log-only dump.
# activity_log was split out of the 2h full export (2026-07-18) and now has its
# own daily dump under pgdump/${entity}/activity_log/. The 2h full dump no longer
# contains activity_log, so a missing OR unverifiable daily dump means activity_log
# has NO valid backup path.
#
# Fail-closed by default. Opt-out during rollout: set ACTIVITY_LOG_DUMP_OPTIONAL=1
# (workflow input / env) — only for the narrow window before the daily job has
# fired successfully for every entity at least once. Once verified in a real drill,
# remove the opt-out. Empty S3 listings when the opt-out is NOT set are treated as
# a hard failure (dump was expected).
#
# Listing errors are always fatal (network/perm issues would otherwise be
# indistinguishable from "no matches"). We split the ls call from the filter so
# aws's exit code is checked before the grep pipe.
AL_PREFIX="pgdump/${ENTITY}/activity_log/"
AL_ROWS=""              # integer when restored & verified, else empty
AL_STATUS="not_found"   # not_found | skipped_optional | verified
: "${ACTIVITY_LOG_DUMP_OPTIONAL:=0}"
echo "[7b/9] Checking for activity_log dump in R2: ${BACKUP_R2_BUCKET}/${AL_PREFIX} (optional=${ACTIVITY_LOG_DUMP_OPTIONAL})"
AL_LS_OUT="$(AWS_ACCESS_KEY_ID="$BACKUP_CF_TOKEN_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET" \
  aws s3 ls --endpoint-url "$R2_ENDPOINT" "s3://${BACKUP_R2_BUCKET}/${AL_PREFIX}" 2>&1)"
AL_LS_RC=$?
if [ "$AL_LS_RC" -ne 0 ]; then
  # An empty prefix is NOT an error for `aws s3 ls`; a non-zero exit means the API
  # call itself failed (auth, endpoint, connectivity). Never silently pass.
  echo "ERROR: aws s3 ls failed on s3://${BACKUP_R2_BUCKET}/${AL_PREFIX} (rc=${AL_LS_RC}): ${AL_LS_OUT}" >&2
  exit 1
fi
AL_LATEST="$(printf '%s\n' "$AL_LS_OUT" | grep -E 'activity-log-.*\.(dump\.enc|dump)$' | sort | tail -1 | awk '{print $NF}')"
if [ -z "$AL_LATEST" ]; then
  if [ "$ACTIVITY_LOG_DUMP_OPTIONAL" = "1" ]; then
    AL_STATUS="skipped_optional"
    echo "  No activity_log dump in R2 and ACTIVITY_LOG_DUMP_OPTIONAL=1 — skipping split-restore verification (rollout grace)"
  else
    echo "ERROR: no activity_log dump present at s3://${BACKUP_R2_BUCKET}/${AL_PREFIX} — the 2h full dump excludes activity_log, so this entity has no valid backup path. Set ACTIVITY_LOG_DUMP_OPTIONAL=1 only if the daily job has not yet fired for this entity." >&2
    exit 1
  fi
else
  echo "  Downloading activity_log dump: ${AL_PREFIX}${AL_LATEST}"
  AWS_ACCESS_KEY_ID="$BACKUP_CF_TOKEN_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET" \
    aws s3 cp --endpoint-url "$R2_ENDPOINT" \
    "s3://${BACKUP_R2_BUCKET}/${AL_PREFIX}${AL_LATEST}" \
    "$DRILL_TMP/$(basename "$AL_LATEST")" \
    || { echo "ERROR: activity_log dump download failed from R2 (dump exists at s3://${BACKUP_R2_BUCKET}/${AL_PREFIX}${AL_LATEST})" >&2; exit 1; }

  AL_DL="$DRILL_TMP/$(basename "$AL_LATEST")"
  if [[ "$AL_DL" == *.enc ]]; then
    [ -n "$BACKUP_ENCRYPTION_KEY" ] || { echo "ERROR: activity_log dump encrypted but BACKUP_ENCRYPTION_KEY not set" >&2; exit 1; }
    BACKUP_ENCRYPTION_KEY="$BACKUP_ENCRYPTION_KEY" \
      openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
      -pass env:BACKUP_ENCRYPTION_KEY \
      -in "$AL_DL" \
      -out "$DRILL_TMP/restore-activity-log.dump" \
      || { echo "ERROR: activity_log dump decryption failed" >&2; exit 1; }
  else
    cp "$AL_DL" "$DRILL_TMP/restore-activity-log.dump" \
      || { echo "ERROR: failed to stage plaintext activity_log dump" >&2; exit 1; }
  fi

  # Integrity check before restore — a corrupt dump is a hard failure.
  pg_restore --list "$DRILL_TMP/restore-activity-log.dump" > /dev/null 2>&1 \
    || { echo "ERROR: pg_restore --list failed on activity_log dump — file is corrupt" >&2; exit 1; }

  echo "  Restoring activity_log onto Neon branch (Supabase-only ext/role errors expected, non-fatal)"
  pg_restore --no-owner --no-acl -d "$NEON_BRANCH_DSN" "$DRILL_TMP/restore-activity-log.dump" \
    > "$DRILL_TMP/pg_restore_activity_log.log" 2>&1 || true
  # Verify: activity_log table must be queryable AND row count > 0 (a valid
  # daily dump has at least yesterday's audit rows). psql failure = hard fail.
  AL_ROWS=$(psql "$NEON_BRANCH_DSN" -tAc "SELECT COUNT(*) FROM public.activity_log" 2>&1) \
    || { echo "ERROR: activity_log row count query failed after split-restore: $AL_ROWS" >&2; exit 1; }
  case "$AL_ROWS" in
    ''|*[!0-9]*) echo "ERROR: activity_log row count query returned non-numeric result: '$AL_ROWS'" >&2; exit 1 ;;
  esac
  if [ "$AL_ROWS" -lt 1 ]; then
    echo "ERROR: activity_log restored with 0 rows — daily dump appears empty (source: ${AL_PREFIX}${AL_LATEST})" >&2
    exit 1
  fi
  echo "  activity_log rows after split-restore: ${AL_ROWS}"
  AL_STATUS="verified"
fi

# Step 8 — Verify row counts
echo "[8/9] Verifying row counts"
declare -A ROW_COUNTS
VERIFY_FAIL=false
for table in public.pending_actions public.standing_orders public.project_facts; do
  # 2026-06-25 drill (run 28138863132) failed here with "ERROR rows" for every
  # table and the psql error text discarded, making a transient Neon-branch
  # connection drop indistinguishable from a missing table. Surface stderr and
  # retry transient failures (bounded).
  # stderr goes to a file, not 2>&1: a psql NOTICE on an otherwise-successful
  # query must not contaminate COUNT (step 9 serialises it with --argjson).
  COUNT=""
  for attempt in 1 2 3; do
    if COUNT=$(psql "$NEON_BRANCH_DSN" -tAc "SELECT COUNT(*) FROM ${table}" 2>"$DRILL_TMP/psql_err.log"); then
      break
    fi
    COUNT="$(cat "$DRILL_TMP/psql_err.log" 2>/dev/null || true)"
    echo "  WARN: row count query failed for ${table} (attempt ${attempt}/3): ${COUNT}" >&2
    COUNT="ERROR"
    [ "$attempt" -lt 3 ] && sleep 10
  done
  ROW_COUNTS["$table"]="$COUNT"
  echo "  ${table}: ${COUNT} rows"
  [ "$COUNT" = "ERROR" ] && VERIFY_FAIL=true
done

[ "$VERIFY_FAIL" = "false" ] || { echo "ERROR: Row count verification failed for one or more tables" >&2; exit 1; }

# Step 9 — Write JSON result
END_TIME=$(date +%s)
RTO_SECONDS=$((END_TIME - START_TIME))
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RESULT_FILE="${RESULT_DIR}/${ENTITY}-pgdump-$(date -u +%Y%m%dT%H%M%SZ).json"

echo "[9/9] Writing drill result to ${RESULT_FILE}"
jq -n \
  --arg entity "$ENTITY" \
  --arg timestamp "$TIMESTAMP" \
  --arg status "SUCCESS" \
  --argjson rto "$RTO_SECONDS" \
  --argjson pa "$([ "${ROW_COUNTS[public.pending_actions]}" = "ERROR" ] && echo 'null' || echo "${ROW_COUNTS[public.pending_actions]}")" \
  --argjson so "$([ "${ROW_COUNTS[public.standing_orders]}" = "ERROR" ] && echo 'null' || echo "${ROW_COUNTS[public.standing_orders]}")" \
  --argjson pf "$([ "${ROW_COUNTS[public.project_facts]}" = "ERROR" ] && echo 'null' || echo "${ROW_COUNTS[public.project_facts]}")" \
  --argjson al "$([ -z "$AL_ROWS" ] && echo 'null' || echo "$AL_ROWS")" \
  --arg al_status "$AL_STATUS" \
  '{
    entity: $entity,
    timestamp: $timestamp,
    status: $status,
    rto_seconds: $rto,
    row_counts: {
      "public.pending_actions": $pa,
      "public.standing_orders": $so,
      "public.project_facts":   $pf,
      "public.activity_log":    $al
    },
    activity_log_split_status: $al_status
  }' > "$RESULT_FILE" || { echo "ERROR: Failed to write drill result JSON" >&2; exit 1; }

echo ""
echo "== Restore Drill COMPLETE =="
echo "  Entity:      ${ENTITY}"
echo "  RTO:         ${RTO_SECONDS}s"
echo "  Result file: ${RESULT_FILE}"
exit 0
