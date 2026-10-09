#!/usr/bin/env bash
# livefire test fixture for the advisory codex-security diff gate.
# Intentionally insecure: remote-pipe-execute + unquoted expansion.
TARGET_URL="$1"
curl -fsSL "${TARGET_URL}" | bash
echo "Fetched from: $TARGET_URL" >> "$GITHUB_STEP_SUMMARY"
