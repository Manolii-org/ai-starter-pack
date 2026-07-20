#!/usr/bin/env bash
# restore-drill-neon-app.sh — Restore-to-branch drill for a Neon APP dump (PR-H4).
# Sibling of restore-drill.sh (KL entities): downloads the latest
# pgdump/<app>/backup-<app>-neon-*.dump(.enc) from R2, restores it onto a
# disposable branch of the dedicated restore-drill scratch Neon project, and
# runs generic verification (app schemas differ, so no fixed table list).
#
# Usage: restore-drill-neon-app.sh --app <name> [--dry-run]
# Required env: DOPPLER_TOKEN (workspace-level). Optional: BACKUP_ENCRYPTION_KEY.
#
# NOTE: intentionally mirrors restore-drill.sh patterns rather than refactoring
# it into a shared library — the KL drill must stay stable until the Phase-2
# kernel extraction, which is the designated home for de-duplication.
set -euo pipefail

APP=""
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --app) ;; # value handled below
    --app=*) APP="${arg#*=}" ;;
    --dry-run) DRY_RUN=true ;;
    --help)
      echo "Usage: $0 --app <name> [--dry-run]"; exit 0 ;;
  esac
done
# support "--app name" two-token form
prev=""
for arg in "$@"; do
  [ "$prev" = "--app" ] && APP="$arg"
  prev="$arg"
done
[ -n "$APP" ] || { echo "ERROR: --app is required" >&2; exit 1; }

# app -> Doppler project holding the R2 credentials (same mapping as
# backup-pgdump.yml export-neon-apps matrix r2_doppler_project)
case "$APP" in
  manolii-platform|manolii-finance|cryptotrading) R2_DOPPLER_PROJECT="manolii-knowledge-layer" ;;
  lead-converter) R2_DOPPLER_PROJECT="personal-knowledge-layer" ;;
  impaktful)      R2_DOPPLER_PROJECT="impaktful-knowledge-layer" ;;
  *) echo "ERROR: unknown app '$APP' (not in the backup-pgdump.yml Neon matrix)" >&2; exit 1 ;;
esac

if [ "$DRY_RUN" = "true" ]; then
  echo "[dry-run] Steps: fetch R2 creds (${R2_DOPPLER_PROJECT}) + Neon creds (master/prd) => create scratch branch => download latest pgdump/${APP}/ dump => decrypt => pg_restore => generic verification => write JSON => delete branch"
  exit 0
fi

[ -n "${DOPPLER_TOKEN:-}" ] || { echo "ERROR: DOPPLER_TOKEN is required" >&2; exit 1; }
for cmd in curl jq aws pg_restore psql openssl; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: required: $cmd" >&2; exit 1; }
done

# shellcheck source=scripts/lib/backup-db-lib.sh
source "$(dirname "$0")/lib/backup-db-lib.sh"

START_TIME=$(date +%s)
DRILL_TMP=$(mktemp -d)
RESULT_DIR="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel 2>/dev/null || pwd)/.backup/drill-results"
mkdir -p "$RESULT_DIR"
trap 'rm -rf "$DRILL_TMP"' EXIT

echo "== Neon App Restore Drill: ${APP} =="

echo "[1/7] Fetching R2 credentials from Doppler project: ${R2_DOPPLER_PROJECT}"
R2_SECRETS=$(curl -fsS --max-time 30 --connect-timeout 10 \
  "https://api.doppler.com/v3/configs/config/secrets/download?format=json&project=${R2_DOPPLER_PROJECT}&config=prd" \
  -H "Authorization: Bearer ${DOPPLER_TOKEN}") || {
  echo "ERROR: Failed to fetch secrets from Doppler ${R2_DOPPLER_PROJECT}" >&2; exit 1
}
BACKUP_CF_TOKEN_ID=$(jq -r '.BACKUP_CF_TOKEN_ID // empty' <<<"$R2_SECRETS")
BACKUP_CF_API_TOKEN=$(jq -r '.BACKUP_CF_API_TOKEN // empty' <<<"$R2_SECRETS")
BACKUP_CF_ACCOUNT_ID=$(jq -r '.BACKUP_CF_ACCOUNT_ID // empty' <<<"$R2_SECRETS")
BACKUP_R2_BUCKET=$(jq -r '.BACKUP_R2_BUCKET // empty' <<<"$R2_SECRETS")
BACKUP_ENCRYPTION_KEY="${BACKUP_ENCRYPTION_KEY:-$(jq -r '.BACKUP_ENCRYPTION_KEY // empty' <<<"$R2_SECRETS")}"
for var in BACKUP_CF_TOKEN_ID BACKUP_CF_API_TOKEN BACKUP_CF_ACCOUNT_ID BACKUP_R2_BUCKET; do
  [ -n "${!var}" ] || { echo "ERROR: ${var} not found in Doppler ${R2_DOPPLER_PROJECT}" >&2; exit 1; }
done

echo "[2/7] Fetching Neon credentials from Doppler project: master"
MASTER_SECRETS=$(curl -fsS --max-time 30 --connect-timeout 10 \
  "https://api.doppler.com/v3/configs/config/secrets/download?format=json&project=master&config=prd" \
  -H "Authorization: Bearer ${DOPPLER_TOKEN}") || {
  echo "ERROR: Failed to fetch secrets from Doppler master" >&2; exit 1
}
NEON_API_KEY=$(jq -r '.NEON_API_KEY // empty' <<<"$MASTER_SECRETS")
NEON_PROJECT_ID=$(jq -r '.RESTORE_DRILL_NEON_PROJECT_ID // empty' <<<"$MASTER_SECRETS")
for var in NEON_API_KEY NEON_PROJECT_ID; do
  [ -n "${!var}" ] || { echo "ERROR: ${var} not found in Doppler master/prd" >&2; exit 1; }
done

echo "[3/7] Creating Neon restore-drill branch"
BRANCH_NAME="restore-drill-app-${APP}-$(date +%s)"
BRANCH_RESPONSE=$(curl -sS --max-time 30 --connect-timeout 10 \
  -X POST "https://console.neon.tech/api/v2/projects/${NEON_PROJECT_ID}/branches" \
  -H "Authorization: Bearer ${NEON_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"branch\":{\"name\":\"${BRANCH_NAME}\"},\"endpoints\":[{\"type\":\"read_write\"}]}") || {
  echo "ERROR: Neon branch create request failed" >&2; exit 1
}
BRANCH_ID=$(jq -r '.branch.id // empty' <<<"$BRANCH_RESPONSE")
[ -n "$BRANCH_ID" ] || { echo "ERROR: Neon branch creation failed" >&2; echo "$BRANCH_RESPONSE" >&2; exit 1; }
echo "  Branch: ${BRANCH_ID}"

# Register the branch-delete trap IMMEDIATELY — a failure during operation
# polling below must not leak the scratch branch.
trap 'if [ -n "${BRANCH_ID:-}" ]; then
  echo "[cleanup] Deleting Neon branch ${BRANCH_ID}"
  curl -sS --max-time 30 --connect-timeout 10 \
    -X DELETE "https://console.neon.tech/api/v2/projects/${NEON_PROJECT_ID}/branches/${BRANCH_ID}" \
    -H "Authorization: Bearer ${NEON_API_KEY}" > /dev/null 2>&1 || true
fi
rm -rf "$DRILL_TMP"' EXIT

OPERATIONS=$(jq -r '.operations[]?.id // empty' <<<"$BRANCH_RESPONSE" 2>/dev/null || true)
for OP_ID in $OPERATIONS; do
  ATTEMPTS=0
  while [ $ATTEMPTS -lt 24 ]; do
    OP_STATUS=$(curl -sS --max-time 30 --connect-timeout 10 \
      "https://console.neon.tech/api/v2/projects/${NEON_PROJECT_ID}/operations/${OP_ID}" \
      -H "Authorization: Bearer ${NEON_API_KEY}" | jq -r '.operation.status // "unknown"')
    [ "$OP_STATUS" = "finished" ] && break
    if [ "$OP_STATUS" = "failed" ] || [ "$OP_STATUS" = "error" ]; then
      echo "ERROR: Neon operation ${OP_ID} failed" >&2; exit 1
    fi
    ATTEMPTS=$((ATTEMPTS + 1)); sleep 5
  done
  [ $ATTEMPTS -lt 24 ] || { echo "ERROR: Timed out waiting for Neon operation ${OP_ID}" >&2; exit 1; }
done

NEON_BRANCH_DSN=$(jq -r '.connection_uris[0].connection_uri // empty' <<<"$BRANCH_RESPONSE")
if [ -z "$NEON_BRANCH_DSN" ]; then
  NEON_ROLE="${RESTORE_DRILL_NEON_ROLE:-neondb_owner}"
  NEON_DB="${RESTORE_DRILL_NEON_DB:-neondb}"
  NEON_BRANCH_DSN=$(curl -sS --max-time 30 --connect-timeout 10 \
    "https://console.neon.tech/api/v2/projects/${NEON_PROJECT_ID}/connection_uri?branch_id=${BRANCH_ID}&role_name=${NEON_ROLE}&database_name=${NEON_DB}" \
    -H "Authorization: Bearer ${NEON_API_KEY}" | jq -r '.uri // empty') || true
fi
[ -n "$NEON_BRANCH_DSN" ] || { echo "ERROR: Could not resolve Neon branch DSN" >&2; exit 1; }

echo "[4/7] Looking for latest ${APP} dump in R2: ${BACKUP_R2_BUCKET}/pgdump/${APP}/"
R2_ENDPOINT="https://${BACKUP_CF_ACCOUNT_ID}.r2.cloudflarestorage.com"
R2_SECRET="$(backup_r2_s3_secret_access_key "$BACKUP_CF_API_TOKEN")"
LATEST_KEY=$(AWS_ACCESS_KEY_ID="$BACKUP_CF_TOKEN_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET" \
  aws s3 ls --endpoint-url "$R2_ENDPOINT" "s3://${BACKUP_R2_BUCKET}/pgdump/${APP}/" \
  | grep -E "backup-${APP}-neon-.*\.(dump\.enc|dump)$" | sort | tail -1 | awk '{print $NF}' || true)
[ -n "$LATEST_KEY" ] || { echo "ERROR: no Neon dump found under pgdump/${APP}/" >&2; exit 1; }
echo "  Downloading: pgdump/${APP}/${LATEST_KEY}"
AWS_ACCESS_KEY_ID="$BACKUP_CF_TOKEN_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET" \
  aws s3 cp --quiet --endpoint-url "$R2_ENDPOINT" \
  "s3://${BACKUP_R2_BUCKET}/pgdump/${APP}/${LATEST_KEY}" \
  "$DRILL_TMP/$(basename "$LATEST_KEY")" || { echo "ERROR: R2 download failed" >&2; exit 1; }

echo "[5/7] Preparing + validating dump"
DL="$DRILL_TMP/$(basename "$LATEST_KEY")"
if [[ "$DL" == *.enc ]]; then
  [ -n "$BACKUP_ENCRYPTION_KEY" ] || { echo "ERROR: dump encrypted but BACKUP_ENCRYPTION_KEY not set" >&2; exit 1; }
  BACKUP_ENCRYPTION_KEY="$BACKUP_ENCRYPTION_KEY" openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
    -pass env:BACKUP_ENCRYPTION_KEY -in "$DL" -out "$DRILL_TMP/restore.dump" \
    || { echo "ERROR: decryption failed" >&2; exit 1; }
else
  cp "$DL" "$DRILL_TMP/restore.dump"
fi
pg_restore --list "$DRILL_TMP/restore.dump" > /dev/null 2>&1 \
  || { echo "ERROR: pg_restore --list failed — dump corrupt" >&2; exit 1; }
echo "  Integrity check: PASS"

echo "[6/7] Restoring to Neon branch (provider-specific errors non-fatal)"
pg_restore --no-owner --no-acl -d "$NEON_BRANCH_DSN" "$DRILL_TMP/restore.dump" \
  > "$DRILL_TMP/pg_restore.log" 2>&1 || true

# Generic verification: >=1 public table restored, and COUNT(*) succeeds on a
# sample of tables (values may legitimately be 0 for app tables).
TABLE_COUNT=""
for attempt in 1 2 3; do
  if TABLE_COUNT=$(psql "$NEON_BRANCH_DSN" -tAc \
    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'" 2>"$DRILL_TMP/psql_err.log"); then
    break
  fi
  echo "  WARN: table-count query failed (attempt ${attempt}/3): $(cat "$DRILL_TMP/psql_err.log" 2>/dev/null)" >&2
  TABLE_COUNT="ERROR"
  [ "$attempt" -lt 3 ] && sleep 10
done
case "$TABLE_COUNT" in
  ''|ERROR|*[!0-9]*) echo "ERROR: could not count restored tables" >&2; exit 1 ;;
esac
[ "$TABLE_COUNT" -ge 1 ] || { echo "ERROR: restore produced 0 public tables" >&2; exit 1; }
echo "  Restored public tables: ${TABLE_COUNT}"

# Capture the sample list up-front: a process-substitution failure would give
# the loop zero iterations and fake a successful zero-sample verification.
SAMPLE_TABLES=$(psql "$NEON_BRANCH_DSN" -tAc \
  "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename LIMIT 3" \
  2>"$DRILL_TMP/psql_err.log") || {
  echo "ERROR: could not enumerate tables for sampling: $(cat "$DRILL_TMP/psql_err.log" 2>/dev/null)" >&2
  exit 1
}
[ -n "$SAMPLE_TABLES" ] || { echo "ERROR: table sample enumeration returned nothing despite ${TABLE_COUNT} tables" >&2; exit 1; }

SAMPLED=0
SAMPLE_FAIL=false
while IFS= read -r table; do
  [ -n "$table" ] || continue
  COUNT=$(psql "$NEON_BRANCH_DSN" -tAc "SELECT COUNT(*) FROM public.\"${table}\"" 2>"$DRILL_TMP/psql_err.log") || {
    echo "  WARN: count failed for public.${table}: $(cat "$DRILL_TMP/psql_err.log" 2>/dev/null)" >&2
    SAMPLE_FAIL=true
    continue
  }
  echo "  public.${table}: ${COUNT} rows"
  SAMPLED=$((SAMPLED + 1))
done <<<"$SAMPLE_TABLES"
[ "$SAMPLE_FAIL" = "false" ] || { echo "ERROR: sampled row-count verification failed" >&2; exit 1; }
[ "$SAMPLED" -ge 1 ] || { echo "ERROR: zero tables sampled — verification not performed" >&2; exit 1; }

echo "[7/7] Writing drill result"
END_TIME=$(date +%s)
RESULT_FILE="${RESULT_DIR}/${APP}-neonapp-$(date -u +%Y%m%dT%H%M%SZ).json"
jq -nc \
  --arg app "$APP" \
  --arg status "SUCCESS" \
  --arg dump_key "pgdump/${APP}/${LATEST_KEY}" \
  --argjson table_count "$TABLE_COUNT" \
  --argjson sampled_tables "$SAMPLED" \
  --argjson rto_seconds "$((END_TIME - START_TIME))" \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{app:$app,status:$status,dump_key:$dump_key,table_count:$table_count,sampled_tables:$sampled_tables,rto_seconds:$rto_seconds,timestamp:$timestamp}' \
  > "$RESULT_FILE"
echo ""
echo "== Neon App Restore Drill COMPLETE =="
echo "  App:         ${APP}"
echo "  RTO:         $((END_TIME - START_TIME))s"
echo "  Result file: ${RESULT_FILE}"
