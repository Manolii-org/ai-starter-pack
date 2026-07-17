#!/usr/bin/env bash
set -euo pipefail

# Migrate .claude/memory/* → .ai/memory/
# Leave a symlink for backward compatibility
# Idempotent: safe to run multiple times

# Determine repo root from script location or first argument
if [[ -n "${1:-}" ]]; then
    REPO_ROOT="$1"
elif [[ -d "$(dirname "$0")/../.claude" ]]; then
    REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
else
    REPO_ROOT="$(pwd)"
fi

CLAUDE_MEM="$REPO_ROOT/.claude/memory"
AI_MEM="$REPO_ROOT/.ai/memory"
ARCHIVE="$AI_MEM/archive/migration-conflicts"

# Case 1: Already migrated (symlink exists)
if [[ -L "$CLAUDE_MEM" ]]; then
    echo "already-migrated (symlink)" >&1
    exit 0
fi

# Case 2: CLAUDE_MEM does not exist
if [[ ! -e "$CLAUDE_MEM" ]]; then
    mkdir -p "$AI_MEM"
    echo "no .claude/memory; nothing to migrate" >&1
    exit 0
fi

# Case 3: CLAUDE_MEM is a directory
if [[ -d "$CLAUDE_MEM" ]]; then
    mkdir -p "$AI_MEM" "$ARCHIVE"
    
    moved=0
    conflicts=0
    
    # Find all entries in CLAUDE_MEM (including hidden files), use null separator
    # Use temp file to avoid subshell issues with 'set -euo pipefail'
    if find "$CLAUDE_MEM" -mindepth 1 -maxdepth 1 -print0 > /tmp/.migrate_entries.$$ 2>/dev/null; then
        while IFS= read -r -d '' entry || [[ -n "$entry" ]]; do
            [[ -z "$entry" ]] && break
            name=$(basename "$entry")
            dest="$AI_MEM/$name"
            
            if [[ ! -e "$dest" ]]; then
                # Destination doesn't exist, move it
                mv "$entry" "$dest"
                ((++moved))
            else
                # Destination exists, archive the CLAUDE copy with epoch suffix
                epoch=$(date +%s)
                archive_dest="$ARCHIVE/$name.$epoch"
                mv "$entry" "$archive_dest"
                echo "conflict: kept .ai/memory/$name, archived .claude copy to $archive_dest" >&2
                ((++conflicts))
            fi
        done < /tmp/.migrate_entries.$$
        rm -f /tmp/.migrate_entries.$$
    fi
    
    # Remove CLAUDE_MEM directory so we can replace it with a symlink
    # (directory will be empty after moving all contents)
    rmdir "$CLAUDE_MEM" 2>/dev/null || true
    
    # Create relative symlink from .claude/memory → ../.ai/memory
    ln -sfn "../.ai/memory" "$CLAUDE_MEM"
    
    echo "moved=$moved conflicts=$conflicts"
    exit 0
fi

# If CLAUDE_MEM is something else (regular file?), error
echo "ERROR: $CLAUDE_MEM exists but is not a directory or symlink" >&2
exit 1
