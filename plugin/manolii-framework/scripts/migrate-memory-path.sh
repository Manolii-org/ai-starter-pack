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

# Case 1: Already migrated (symlink exists) — but only trust it if it points
# at the canonical .ai/memory directory. A stale/wrong-target/dangling symlink
# from a prior migration attempt would silently mislocate memory writes; heal
# it in place instead of exiting green.
if [[ -L "$CLAUDE_MEM" ]]; then
    # Resolve the target relative to the symlink's parent directory so that
    # the common "../.ai/memory" form resolves to $AI_MEM.
    link_target=$(readlink "$CLAUDE_MEM")
    case "$link_target" in
        /*) resolved="$link_target" ;;
        *)  resolved="$(dirname "$CLAUDE_MEM")/$link_target" ;;
    esac
    # Compare canonical paths; if realpath is missing OR either invocation
    # fails, apply the SAME python-based fallback to BOTH sides so the
    # legitimate compat symlink ".claude/../.ai/memory" still equals
    # ".ai/memory". Asymmetric normalization would heal a valid symlink
    # on every run.
    _normalize() {
        if command -v realpath >/dev/null 2>&1; then
            local n; n=$(realpath -m "$1" 2>/dev/null || true)
            [[ -n "$n" ]] && { printf '%s\n' "$n"; return 0; }
        fi
        if command -v python3 >/dev/null 2>&1; then
            python3 -c "import os,sys; print(os.path.normpath(sys.argv[1]))" "$1"
        else
            printf '%s\n' "$1"
        fi
    }
    actual=$(_normalize "$resolved")
    expected=$(_normalize "$AI_MEM")
    if [[ "$actual" == "$expected" ]] && [[ -d "$expected" ]]; then
        echo "already-migrated (symlink)" >&1
        exit 0
    fi
    # Codex P1 2026-07-19: if the wrong target is a non-empty directory,
    # blindly replacing the symlink would orphan whatever memory lives
    # there (e.g. .claude/memory -> ../old-memory with fact.jsonl inside).
    # Refuse the automatic heal and let the operator merge the tree.
    if [[ -e "$actual" && -d "$actual" ]]; then
        if [[ -n "$(ls -A "$actual" 2>/dev/null)" ]]; then
            echo "ERROR: refusing to heal $CLAUDE_MEM — its current target $actual is non-empty" >&2
            echo "       Move the contents into $AI_MEM manually (or delete them) and rerun." >&2
            exit 1
        fi
    fi
    echo "healing stale symlink: $CLAUDE_MEM -> $link_target" >&2
    mkdir -p "$AI_MEM"
    ln -sfn "../.ai/memory" "$CLAUDE_MEM"
    echo "healed compat symlink" >&1
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
            elif [[ -f "$dest" && ! -s "$dest" && -f "$entry" && -s "$entry" ]]; then
                # Codex P2 2026-07-19: destination is a seeded EMPTY file
                # (e.g. tracked .ai/memory/facts.jsonl scaffolded by the
                # pack) and the legacy source is non-empty. Archiving the
                # legacy file would hide the operator's real facts behind
                # the empty scaffold. Prefer the legacy file — overwrite
                # the empty destination.
                mv -f "$entry" "$dest"
                echo "preferred legacy .claude/memory/$name over empty seeded destination" >&2
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
