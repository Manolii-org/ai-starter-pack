#!/usr/bin/env bash
# Run pg_dump for a Doppler project with password resolution and integrity check.
# Usage:
#   backup-pg-dump.sh --doppler-project <name> --outfile <path> \
#     [--tables "public.t1 public.t2"] \
#     [--exclude-tables "public.activity_log public.activity_log_*"]
#
# --tables and --exclude-tables are space-separated lists; each entry is
# passed to pg_dump as `-t <pat>` or `-T <pat>` respectively. Patterns follow
# pg_dump's glob syntax (see pg_dump(1) --exclude-table).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/backup-db-lib.sh
source "${ROOT_DIR}/scripts/lib/backup-db-lib.sh"

DOPPLER_PROJECT=""
OUTFILE=""
TABLES=""
EXCLUDE_TABLES=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --doppler-project) DOPPLER_PROJECT="${2:?}"; shift 2 ;;
    --outfile) OUTFILE="${2:?}"; shift 2 ;;
    --tables) TABLES="${2:?}"; shift 2 ;;
    --exclude-tables) EXCLUDE_TABLES="${2:?}"; shift 2 ;;
    -h|--help)
      echo "Usage: backup-pg-dump.sh --doppler-project <name> --outfile <path> [--tables \"public.t1 ...\"] [--exclude-tables \"public.t2 ...\"]"
      exit 0
      ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
  esac
done

[ -n "$DOPPLER_PROJECT" ] || { echo "ERROR: --doppler-project required" >&2; exit 1; }
[ -n "$OUTFILE" ] || { echo "ERROR: --outfile required" >&2; exit 1; }

backup_require_doppler_token

DB_URL="$(backup_resolve_db_url "$DOPPLER_PROJECT")" || exit 1

PG_DUMP_ARGS=()
if [ -n "$TABLES" ]; then
  for t in $TABLES; do
    PG_DUMP_ARGS+=(-t "$t")
  done
fi
if [ -n "$EXCLUDE_TABLES" ]; then
  for t in $EXCLUDE_TABLES; do
    PG_DUMP_ARGS+=(-T "$t")
  done
fi

if [ ${#PG_DUMP_ARGS[@]} -gt 0 ]; then
  backup_pg_dump_url "$DB_URL" -f "$OUTFILE" "${PG_DUMP_ARGS[@]}"
else
  backup_pg_dump_url "$DB_URL" -f "$OUTFILE"
fi

backup_verify_dump "$OUTFILE"
