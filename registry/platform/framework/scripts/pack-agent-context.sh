#!/usr/bin/env bash
# pack-agent-context.sh — pack repository context for sub-agent dispatch
#
# Usage:
#   pack-agent-context.sh <repo-dir> [file1] [file2] ...
#
# Output:
#   Writes a compact context file to /tmp/agent-context-XXXXX.txt
#   Prints the temp file path to stdout
#
# Token budget: ~5000 tokens (≈20,000 chars) — conservative to leave room for
#   the agent prompt and response. Truncates gracefully when exceeded.
#
# Security: explicitly excludes credential files (.env, .pem, .key, secrets.*)
#
# Example:
#   CONTEXT=$(bash scripts/pack-agent-context.sh /path/to/repo src/lib/assessment.ts)
#   # Then inject "$(cat $CONTEXT)" into your agent prompt

set -o pipefail
trap 'exit 1' ERR

TARGET_DIR="${1:-$(pwd)}"
shift
FILES=("$@")

MAX_CHARS=20000  # ~5000 tokens (conservative)
TREE_MAX_LINES=120

if [ ! -d "$TARGET_DIR" ]; then
    echo "[pack-agent-context] ERROR: directory not found: $TARGET_DIR" >&2
    exit 1
fi

# Create temp file — cleaned up by the caller or OS
OUTPUT_FILE=$(mktemp /tmp/agent-context-XXXXX.txt)

# ── 1. Header ──────────────────────────────────────────────────────────────────
{
    echo "=== AGENT CONTEXT SNAPSHOT ==="
    echo "Repository: $(basename "$TARGET_DIR")"
    echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo ""
} >> "$OUTPUT_FILE"

# ── 2. Directory tree (depth 2, exclude noise + credentials) ──────────────────
{
    echo "=== DIRECTORY STRUCTURE (depth 2) ==="
    find "$TARGET_DIR" -maxdepth 2 \
        -not -path '*/node_modules/*' \
        -not -path '*/.git/*' \
        -not -path '*/dist/*' \
        -not -path '*/.next/*' \
        -not -path '*/__pycache__/*' \
        -not -path '*/.turbo/*' \
        -not -name '.env' \
        -not -name '.env.*' \
        -not -name '*.pem' \
        -not -name '*.key' \
        -not -name 'secrets.*' \
        -not -name '*.secret' \
        2>/dev/null \
    | sort \
    | head -"$TREE_MAX_LINES" \
    | sed "s|${TARGET_DIR}/||g"
    echo ""
} >> "$OUTPUT_FILE"

# ── 3. File contents with token budget enforcement ────────────────────────────
# Check current size after tree, then add files until budget is reached.

_char_count() { wc -c < "$1" 2>/dev/null || echo 0; }

CHAR_COUNT=$(_char_count "$OUTPUT_FILE")
BUDGET_EXHAUSTED=false

if [ ${#FILES[@]} -gt 0 ]; then
    echo "=== FILE CONTENTS ===" >> "$OUTPUT_FILE"

    for FILE_PATH in "${FILES[@]}"; do
        # Resolve to absolute path
        if [[ "$FILE_PATH" != /* ]]; then
            ABS_PATH="$TARGET_DIR/$FILE_PATH"
        else
            ABS_PATH="$FILE_PATH"
        fi

        # Skip credential files regardless of how they were specified
        BASENAME=$(basename "$ABS_PATH")
        if [[ "$BASENAME" == .env || "$BASENAME" == .env.* || \
              "$BASENAME" == *.pem || "$BASENAME" == *.key || \
              "$BASENAME" == secrets.* || "$BASENAME" == *.secret ]]; then
            echo "[SKIPPED: credential file $BASENAME]" >> "$OUTPUT_FILE"
            continue
        fi

        if [ ! -f "$ABS_PATH" ]; then
            echo "[MISSING: $FILE_PATH]" >> "$OUTPUT_FILE"
            continue
        fi

        if [ "$BUDGET_EXHAUSTED" = true ]; then
            echo "[SKIPPED: $FILE_PATH — token budget exhausted]" >> "$OUTPUT_FILE"
            continue
        fi

        # Measure file size before appending
        FILE_SIZE=$(wc -c < "$ABS_PATH" 2>/dev/null || echo 0)
        if (( CHAR_COUNT + FILE_SIZE + 100 > MAX_CHARS )); then
            BUDGET_EXHAUSTED=true
            echo "[TRUNCATED: $FILE_PATH would exceed token budget — omitted]" >> "$OUTPUT_FILE"
            continue
        fi

        {
            echo ""
            echo "--- $FILE_PATH ---"
            cat -n "$ABS_PATH"
            echo ""
        } >> "$OUTPUT_FILE"

        CHAR_COUNT=$(_char_count "$OUTPUT_FILE")
    done
fi

# ── 4. Footer ─────────────────────────────────────────────────────────────────
{
    echo ""
    echo "=== END CONTEXT SNAPSHOT ==="
    echo "Total chars: $(_char_count "$OUTPUT_FILE") / $MAX_CHARS budget"
} >> "$OUTPUT_FILE"

echo "$OUTPUT_FILE"
