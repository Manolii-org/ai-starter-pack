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

# Case 2: CLAUDE_MEM does not exist — still create the compat symlink so
# existing consumers referencing .claude/memory/* find it.
if [[ ! -e "$CLAUDE_MEM" ]]; then
    mkdir -p "$AI_MEM" "$(dirname "$CLAUDE_MEM")"
    ln -sfn "../.ai/memory" "$CLAUDE_MEM"
    echo "no .claude/memory to migrate; created compat symlink" >&1
    exit 0
fi

# Case 3: CLAUDE_MEM is a directory
if [[ -d "$CLAUDE_MEM" ]]; then
    mkdir -p "$AI_MEM" "$ARCHIVE"
    
    moved=0
    conflicts=0
    
    # Find all entries in CLAUDE_MEM (including hidden files), use null separator.
    # Process substitution avoids the subshell-scoping issue AND the symlink-attack
    # risk of a predictable /tmp temp file.
    while IFS= read -r -d '' entry || [[ -n "$entry" ]]; do
        [[ -z "$entry" ]] && break
            name=$(basename "$entry")
            dest="$AI_MEM/$name"
            
            # Treat dangling symlinks as conflicts too — `-e` follows the link
            # and would report "doesn't exist" for a broken one, silently
            # clobbering the link on `mv`.
            if [[ ! -e "$dest" && ! -L "$dest" ]]; then
                # Destination doesn't exist, move it
                mv "$entry" "$dest"
                ((++moved))
            else
                # Destination exists, archive the CLAUDE copy into a unique
                # per-run directory (mktemp -d is collision-proof for the
                # rare case of multiple entries with the same basename in the
                # same second).
                epoch=$(date +%s)
                archive_dir=$(mktemp -d "$ARCHIVE/${name}.${epoch}.XXXXXX")
                archive_dest="$archive_dir/$name"
                mv "$entry" "$archive_dest"
                echo "conflict: kept .ai/memory/$name, archived .claude copy to $archive_dest" >&2
                ((++conflicts))
            fi
    done < <(find "$CLAUDE_MEM" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)

    # Remove CLAUDE_MEM directory so we can replace it with a symlink.
    # Fail hard if it can't be removed — silently ignoring means the
    # subsequent `ln -sfn` clobbers a directory that still holds unmovable
    # content, and we lose files. Better to surface the problem.
    if ! rmdir "$CLAUDE_MEM"; then
        echo "ERROR: unable to remove migrated legacy directory: $CLAUDE_MEM" >&2
        echo "       inspect its contents (unexpected files, permission issues) and retry." >&2
        exit 1
    fi

    # Create relative symlink from .claude/memory → ../.ai/memory
    ln -sfn "../.ai/memory" "$CLAUDE_MEM"
    
    echo "moved=$moved conflicts=$conflicts"
    exit 0
fi

# If CLAUDE_MEM is something else (regular file?), error
echo "ERROR: $CLAUDE_MEM exists but is not a directory or symlink" >&2
exit 1
