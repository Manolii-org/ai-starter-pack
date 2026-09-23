#!/usr/bin/env bash
# Generates OTLP auth headers for Langfuse.
# Used by otelHeadersHelper in .claude/settings.json for dynamic OTEL auth.
# Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in your environment.
# Output: JSON object {"Authorization": "Basic <base64(pk:sk)>"}
set -euo pipefail

PK="${LANGFUSE_PUBLIC_KEY:-}"
SK="${LANGFUSE_SECRET_KEY:-}"

if [ -z "$PK" ] || [ -z "$SK" ]; then
    echo '{}'
    exit 0
fi

AUTH=$(printf '%s:%s' "$PK" "$SK" | base64 | tr -d '\n')
printf '{"Authorization": "Basic %s", "x-langfuse-ingestion-version": "4"}\n' "$AUTH"
