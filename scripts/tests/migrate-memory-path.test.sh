#!/usr/bin/env bash
# Hermetic tests for scripts/migrate-memory-path.sh — fix #6 coverage.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MIGRATE="$SCRIPT_DIR/migrate-memory-path.sh"
FAIL=0
PASS=0

_report() {
    if [[ "$1" == "pass" ]]; then
        echo "  ✓ $2"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $2"
        FAIL=$((FAIL + 1))
    fi
}

_setup() {
    local d; d=$(mktemp -d)
    mkdir -p "$d/.claude" "$d/.ai"
    echo "$d"
}

_teardown() { rm -rf "$1"; }

# Test 1: no .claude/memory → creates compat symlink
t="creates compat symlink when .claude/memory absent"
d=$(_setup)
bash "$MIGRATE" "$d" >/dev/null
if [[ -L "$d/.claude/memory" ]]; then _report pass "$t"; else _report fail "$t"; fi
_teardown "$d"

# Test 2: idempotent — running twice is safe
t="idempotent second run"
d=$(_setup)
mkdir -p "$d/.claude/memory"
echo "hello" > "$d/.claude/memory/file.txt"
bash "$MIGRATE" "$d" >/dev/null
bash "$MIGRATE" "$d" >/dev/null
if [[ -L "$d/.claude/memory" && -f "$d/.ai/memory/file.txt" ]]; then _report pass "$t"; else _report fail "$t"; fi
_teardown "$d"

# Test 3: conflict archived to unique mktemp dir (fix #6)
t="conflict uses mktemp collision-proof archive dir"
d=$(_setup)
mkdir -p "$d/.claude/memory" "$d/.ai/memory"
echo "old" > "$d/.claude/memory/dup.txt"
echo "new" > "$d/.ai/memory/dup.txt"
bash "$MIGRATE" "$d" >/dev/null 2>&1
# Archive path must include the file name inside a per-run dir under archive/migration-conflicts
found=$(find "$d/.ai/memory/archive/migration-conflicts" -name 'dup.txt' 2>/dev/null | head -1 || true)
if [[ -n "$found" && "$(cat "$found")" == "old" && "$(cat "$d/.ai/memory/dup.txt")" == "new" ]]; then
    _report pass "$t"
else
    _report fail "$t (found=$found)"
fi
_teardown "$d"

# Test 4: dangling symlink at destination now caught as conflict (fix #6)
t="dangling symlink at destination triggers conflict path"
d=$(_setup)
mkdir -p "$d/.claude/memory" "$d/.ai/memory"
echo "src" > "$d/.claude/memory/link-target.txt"
ln -s /nonexistent/path "$d/.ai/memory/link-target.txt"
bash "$MIGRATE" "$d" >/dev/null 2>&1
# The dangling link stays AND the source is archived under
# .ai/memory/archive/migration-conflicts. Weaker assertions (only
# checking -L) let a broken migration that deletes the source pass.
archived=$(find "$d/.ai/memory/archive/migration-conflicts" -type f \
    -name 'link-target.txt' -print -quit 2>/dev/null || true)
if [[ -L "$d/.ai/memory/link-target.txt" && ! -e "$d/.ai/memory/link-target.txt" \
      && -n "$archived" && "$(cat "$archived")" == "src" ]]; then
    _report pass "$t"
else
    _report fail "$t (archived=$archived, link ok=$([[ -L "$d/.ai/memory/link-target.txt" ]] && echo y || echo n))"
fi
_teardown "$d"

# Test 5: symlink already present short-circuits
t="pre-existing symlink short-circuits"
d=$(_setup)
mkdir -p "$d/.ai/memory"
ln -sfn "../.ai/memory" "$d/.claude/memory"
out=$(bash "$MIGRATE" "$d")
if echo "$out" | grep -q "already-migrated"; then _report pass "$t"; else _report fail "$t"; fi
_teardown "$d"

# Test 6: rmdir failure hard-exits (fix #6)
t="unmovable .claude/memory contents fail hard"
d=$(_setup)
mkdir -p "$d/.claude/memory" "$d/.ai/memory"
# Force the migration's terminal rmdir to fail by shimming rmdir on PATH.
# This actually exercises the fix #6 hard-exit branch instead of the
# baseline sanity check the older test used.
fake_bin=$(mktemp -d)
printf '#!/usr/bin/env bash\nexit 1\n' > "$fake_bin/rmdir"
chmod +x "$fake_bin/rmdir"
if PATH="$fake_bin:$PATH" bash "$MIGRATE" "$d" >/dev/null 2>&1; then
    _report fail "$t (migration unexpectedly succeeded with rmdir shim)"
elif [[ -d "$d/.claude/memory" && ! -L "$d/.claude/memory" ]]; then
    _report pass "$t"
else
    _report fail "$t (post-state: -d=$([[ -d "$d/.claude/memory" ]] && echo y || echo n) -L=$([[ -L "$d/.claude/memory" ]] && echo y || echo n))"
fi
rm -rf "$fake_bin"
_teardown "$d"

# Test 7: pre-existing symlink pointing at the WRONG target is healed
t="wrong-target symlink is healed, not trusted"
d=$(_setup)
mkdir -p "$d/.ai/memory" "$d/somewhere/else"
ln -sfn "../somewhere/else" "$d/.claude/memory"
bash "$MIGRATE" "$d" >/dev/null
# After healing, the symlink must resolve to .ai/memory, not somewhere/else.
resolved=$(readlink "$d/.claude/memory")
if [[ "$resolved" == "../.ai/memory" ]]; then _report pass "$t"; else _report fail "$t (got: $resolved)"; fi
_teardown "$d"

# Test 7b: wrong-target symlink with NON-EMPTY target refuses to heal (Codex P1)
t="wrong-target symlink with contents refuses auto-heal"
d=$(_setup)
mkdir -p "$d/.ai/memory" "$d/somewhere/else"
echo "important" > "$d/somewhere/else/fact.jsonl"
ln -sfn "../somewhere/else" "$d/.claude/memory"
if bash "$MIGRATE" "$d" >/dev/null 2>&1; then
    _report fail "$t (migration should have refused non-empty wrong target)"
else
    resolved=$(readlink "$d/.claude/memory" 2>/dev/null || true)
    contents=$(cat "$d/somewhere/else/fact.jsonl" 2>/dev/null || true)
    if [[ "$resolved" == "../somewhere/else" ]] && [[ "$contents" == "important" ]]; then
        _report pass "$t"
    else
        _report fail "$t (resolved=$resolved contents=$contents)"
    fi
fi
_teardown "$d"

# Test 8: pre-existing dangling symlink is healed (target doesn't exist)
t="dangling compat symlink is healed"
d=$(_setup)
ln -sfn "../.ai/memory" "$d/.claude/memory"  # target doesn't exist yet
bash "$MIGRATE" "$d" >/dev/null
# After healing, target should exist and symlink should resolve correctly.
if [[ -d "$d/.ai/memory" ]] && [[ -L "$d/.claude/memory" ]]; then
    _report pass "$t"
else
    _report fail "$t"
fi
_teardown "$d"

echo
echo "migrate-memory-path.test.sh — passed: $PASS  failed: $FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
