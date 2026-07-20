#!/usr/bin/env bash
# Sentry Crons check-in via Relay ingest (legacy org check-in API returns HTTP 405).
#
# Required env:
#   SENTRY_AUTH_TOKEN   — Bearer token (keys API + monitor preflight)
#   SENTRY_ORG          — organisation slug (e.g. impaktful — D6 pending)
#   SENTRY_PROJECT      — project slug that owns the monitors (e.g. manolii)
#   SENTRY_MONITOR_SLUG — monitor slug (e.g. backup-pgdump)
#   SENTRY_STATUS       — ok | error | in_progress
#
# Optional:
#   SENTRY_CRONS_DSN    — if set, skip keys-API resolution and use this DSN
#                         (must be the DSN for SENTRY_PROJECT — NOT a random
#                         org DSN; master/prd:SENTRY_DSN currently points at
#                         atroengine and must NOT be used for these monitors)
#   SENTRY_CHECKIN_SKIP_PREFLIGHT=1 — skip monitor existence/project check
#                         (default: fail closed if monitor missing or on wrong project)
#
# Names/metadata only on stdout/stderr — never print token or DSN values.
# API-Verified: sentry-crons@2026-07-20
# Doc: https://docs.sentry.io/product/monitors-and-alerts/monitors/crons/getting-started/http/
# Migration: https://docs.sentry.io/product/crons/legacy-endpoint-migration/
set -euo pipefail

: "${SENTRY_AUTH_TOKEN:?SENTRY_AUTH_TOKEN is required}"
: "${SENTRY_ORG:?SENTRY_ORG is required}"
: "${SENTRY_PROJECT:?SENTRY_PROJECT is required}"
: "${SENTRY_MONITOR_SLUG:?SENTRY_MONITOR_SLUG is required}"
: "${SENTRY_STATUS:?SENTRY_STATUS is required}"

case "${SENTRY_STATUS}" in
  ok|error|in_progress) ;;
  *)
    echo "ERROR: SENTRY_STATUS must be ok|error|in_progress (got '${SENTRY_STATUS}')" >&2
    exit 2
    ;;
esac

_auth_header_file() {
  # Token via 0600 file — never on curl argv (ps /proc cmdline exposure).
  local hf
  hf="$(mktemp)"
  chmod 600 "${hf}"
  printf 'Authorization: Bearer %s\n' "${SENTRY_AUTH_TOKEN}" >"${hf}"
  printf '%s' "${hf}"
}

_preflight_monitor() {
  if [ "${SENTRY_CHECKIN_SKIP_PREFLIGHT:-0}" = "1" ]; then
    echo "WARN: skipping monitor preflight (SENTRY_CHECKIN_SKIP_PREFLIGHT=1)"
    return 0
  fi
  local http_code tmp project_slug hf
  tmp="$(mktemp)"
  hf="$(_auth_header_file)"
  http_code="$(curl -sS --retry 3 --max-time 15 --connect-timeout 5 \
    -o "${tmp}" -w '%{http_code}' \
    -H @"${hf}" \
    -H "Accept: application/json" \
    "https://sentry.io/api/0/organizations/${SENTRY_ORG}/monitors/${SENTRY_MONITOR_SLUG}/")" || {
    echo "ERROR: curl failed preflight for monitor ${SENTRY_MONITOR_SLUG}" >&2
    rm -f "${tmp}" "${hf}"
    return 1
  }
  rm -f "${hf}"
  if [ "${http_code}" = "404" ]; then
    echo "ERROR: Sentry monitor '${SENTRY_MONITOR_SLUG}' missing in org '${SENTRY_ORG}' — run scripts/sentry-crons-setup.sh" >&2
    rm -f "${tmp}"
    return 1
  fi
  if [ "${http_code}" != "200" ]; then
    echo "ERROR: monitor preflight HTTP ${http_code} for ${SENTRY_MONITOR_SLUG}" >&2
    rm -f "${tmp}"
    return 1
  fi
  project_slug="$(jq -r '.project.slug // empty' <"${tmp}")" || project_slug=""
  rm -f "${tmp}"
  if [ -z "${project_slug}" ]; then
    echo "ERROR: monitor '${SENTRY_MONITOR_SLUG}' response missing project.slug" >&2
    return 1
  fi
  if [ "${project_slug}" != "${SENTRY_PROJECT}" ]; then
    echo "ERROR: monitor '${SENTRY_MONITOR_SLUG}' is on project '${project_slug}', expected '${SENTRY_PROJECT}' (duplicate/mis-routed monitor?) — fix in Sentry or run scripts/sentry-crons-setup.sh" >&2
    return 1
  fi
  echo "Preflight OK: monitor ${SENTRY_MONITOR_SLUG} on ${SENTRY_ORG}/${SENTRY_PROJECT}"
}

_resolve_dsn() {
  if [ -n "${SENTRY_CRONS_DSN:-}" ]; then
    printf '%s' "${SENTRY_CRONS_DSN}"
    return 0
  fi

  local keys_json http_code hf
  keys_json="$(mktemp)"
  hf="$(_auth_header_file)"
  http_code="$(curl -sS --retry 3 --max-time 15 --connect-timeout 5 \
    -o "${keys_json}" -w '%{http_code}' \
    -H @"${hf}" \
    -H "Accept: application/json" \
    "https://sentry.io/api/0/projects/${SENTRY_ORG}/${SENTRY_PROJECT}/keys/")" || {
    echo "ERROR: curl failed resolving DSN keys for ${SENTRY_ORG}/${SENTRY_PROJECT}" >&2
    rm -f "${keys_json}" "${hf}"
    return 1
  }
  rm -f "${hf}"
  if [ "${http_code}" != "200" ]; then
    echo "ERROR: keys API HTTP ${http_code} for ${SENTRY_ORG}/${SENTRY_PROJECT}" >&2
    rm -f "${keys_json}"
    return 1
  fi
  local dsn
  dsn="$(jq -r '
    ([.[] | select(.isActive == true) | .dsn.public] | first)
    // ([.[] | .dsn.public] | first)
    // empty
  ' <"${keys_json}")" || dsn=""
  rm -f "${keys_json}"
  if [ -z "${dsn}" ] || [ "${dsn}" = "null" ]; then
    echo "ERROR: no public DSN on ${SENTRY_ORG}/${SENTRY_PROJECT}" >&2
    return 1
  fi
  printf '%s' "${dsn}"
}

_dsn_to_cron_url() {
  local dsn="$1" slug="$2"
  local key host project
  if [[ ! "${dsn}" =~ ^https://([^@]+)@([^/]+)/(.+)$ ]]; then
    echo "ERROR: DSN is not a recognised https://key@host/project shape" >&2
    return 1
  fi
  key="${BASH_REMATCH[1]}"
  host="${BASH_REMATCH[2]}"
  project="${BASH_REMATCH[3]}"
  project="${project%%\?*}"
  project="${project%%#*}"
  printf 'https://%s/api/%s/cron/%s/%s/' "${host}" "${project}" "${slug}" "${key}"
}

main() {
  local dsn cron_url http_code
  _preflight_monitor
  dsn="$(_resolve_dsn)"
  cron_url="$(_dsn_to_cron_url "${dsn}" "${SENTRY_MONITOR_SLUG}")"

  echo "Sentry cron check-in: org=${SENTRY_ORG} project=${SENTRY_PROJECT} slug=${SENTRY_MONITOR_SLUG} status=${SENTRY_STATUS}"

  http_code="$(curl -sS --retry 3 --max-time 15 --connect-timeout 5 \
    -o /dev/null -w '%{http_code}' \
    "${cron_url}?status=${SENTRY_STATUS}")" || {
    echo "ERROR: curl failed posting check-in for ${SENTRY_MONITOR_SLUG}" >&2
    exit 1
  }

  case "${http_code}" in
    200|201|202)
      echo "Sentry cron check-in accepted (HTTP ${http_code}) for ${SENTRY_MONITOR_SLUG}"
      ;;
    *)
      echo "ERROR: Sentry cron check-in HTTP ${http_code} for ${SENTRY_MONITOR_SLUG}" >&2
      exit 1
      ;;
  esac
}

main "$@"
