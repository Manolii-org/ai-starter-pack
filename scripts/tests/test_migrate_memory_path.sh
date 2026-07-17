#!/usr/bin/env bash
set -euo pipefail

# Test suite for migrate-memory-path.sh
# Creates scratch environments and verifies:
# 1. already-migrated (symlink)
# 2. no .claude/memory
# 3. clean migrate (files + hidden dirs)
# 4. conflict case + idempotency

TEST_DIR=$(mktemp -d)
cleanup() {
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

TEST_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_SCRIPT_DIR/.." && pwd)"
MIGRATE_SCRIPT="$SCRIPTS_DIR/migrate-memory-path.sh"

passed=0
failed=0

# Helper function
assert_pass() {
    local test_name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "✓ $test_name"
        ((passed=passed+1))
    else
        echo "✗ $test_name"
        ((failed=failed+1))
    fi
}

# Test 1: already-migrated (symlink)
echo "=== Test 1: already-migrated (symlink) ==="
TEST_1="$TEST_DIR/test1"
mkdir -p "$TEST_1/.claude" "$TEST_1/.ai/memory"
ln -s "../.ai/memory" "$TEST_1/.claude/memory"
output=$("$MIGRATE_SCRIPT" "$TEST_1" 2>&1)
assert_pass "1a: output contains 'already-migrated'" bash -c "grep -q 'already-migrated' <<< '$output'"
assert_pass "1b: symlink still exists" test -L "$TEST_1/.claude/memory"

# Test 2: no .claude/memory
echo "=== Test 2: no .claude/memory ==="
TEST_2="$TEST_DIR/test2"
mkdir -p "$TEST_2/.claude" "$TEST_2/.ai"
output=$("$MIGRATE_SCRIPT" "$TEST_2" 2>&1)
assert_pass "2a: output contains 'no .claude/memory'" bash -c "grep -q 'no .claude/memory' <<< '$output'"
assert_pass "2b: .ai/memory was created" test -d "$TEST_2/.ai/memory"

# Test 3: clean migrate (files + hidden dirs)
echo "=== Test 3: clean migrate ==="
TEST_3="$TEST_DIR/test3"
mkdir -p "$TEST_3/.claude/memory/.hidden"
echo "file1 content" > "$TEST_3/.claude/memory/file1.txt"
echo "file2 content" > "$TEST_3/.claude/memory/file2.txt"
echo "hidden file" > "$TEST_3/.claude/memory/.hidden/dot.txt"
mkdir -p "$TEST_3/.ai"

output=$("$MIGRATE_SCRIPT" "$TEST_3" 2>&1)
assert_pass "3a: .claude/memory is now a symlink" test -L "$TEST_3/.claude/memory"
assert_pass "3b: file1.txt moved to .ai/memory" test -f "$TEST_3/.ai/memory/file1.txt"
assert_pass "3c: file2.txt moved to .ai/memory" test -f "$TEST_3/.ai/memory/file2.txt"
assert_pass "3d: hidden dir moved" test -d "$TEST_3/.ai/memory/.hidden"
assert_pass "3e: hidden file moved" test -f "$TEST_3/.ai/memory/.hidden/dot.txt"
assert_pass "3f: output shows moved count" bash -c "grep -q 'moved=' <<< '$output'"

# Verify file content integrity
if [[ -f "$TEST_3/.ai/memory/file1.txt" ]]; then
    content1=$(cat "$TEST_3/.ai/memory/file1.txt")
    assert_pass "3g: file1 content preserved" test "$content1" = "file1 content"
else
    echo "✗ 3g: file1 content preserved"
    ((failed=failed+1))
fi

# Verify symlink target is correct
symlink_target=$(readlink "$TEST_3/.claude/memory")
assert_pass "3h: symlink target is ../.ai/memory" test "$symlink_target" = "../.ai/memory"

# Test 4: conflict detection + idempotency
echo "=== Test 4: conflict + idempotency ==="
TEST_4="$TEST_DIR/test4"
mkdir -p "$TEST_4/.claude/memory" "$TEST_4/.ai/memory"
echo "original content in .ai" > "$TEST_4/.ai/memory/conflict.txt"
echo "different content in .claude" > "$TEST_4/.claude/memory/conflict.txt"

output=$("$MIGRATE_SCRIPT" "$TEST_4" 2>&1)
assert_pass "4a: output contains 'conflict'" bash -c "grep -q 'conflict' <<< '$output'"
assert_pass "4b: .claude/memory is now a symlink" test -L "$TEST_4/.claude/memory"
assert_pass "4c: archive directory exists" test -d "$TEST_4/.ai/memory/archive/migration-conflicts"

# Check that archive contains the conflicting file (using find)
archive_count=$(find "$TEST_4/.ai/memory/archive/migration-conflicts" -name 'conflict.txt.*' 2>/dev/null | wc -l)
assert_pass "4d: conflicting .claude copy archived" test "$archive_count" -gt 0

# Verify .ai/memory content was preserved (not overwritten)
if [[ -f "$TEST_4/.ai/memory/conflict.txt" ]]; then
    ai_content=$(cat "$TEST_4/.ai/memory/conflict.txt")
    assert_pass "4e: .ai/memory content preserved" test "$ai_content" = "original content in .ai"
else
    echo "✗ 4e: .ai/memory content preserved"
    ((failed=failed+1))
fi

# Idempotency check: count archives before and after re-run
archive_count_before=$(find "$TEST_4/.ai/memory/archive/migration-conflicts" -name 'conflict.txt.*' 2>/dev/null | wc -l)
output_rerun=$("$MIGRATE_SCRIPT" "$TEST_4" 2>&1)
archive_count_after=$(find "$TEST_4/.ai/memory/archive/migration-conflicts" -name 'conflict.txt.*' 2>/dev/null | wc -l)
assert_pass "4f: re-run is idempotent (no duplicate archives)" test "$archive_count_before" -eq "$archive_count_after"
assert_pass "4g: re-run detects already-migrated" bash -c "grep -q 'already-migrated' <<< '$output_rerun'"

# Summary
echo ""
echo "=========================================="
if [[ $failed -eq 0 ]]; then
    echo "PASS: All $passed tests passed"
    exit 0
else
    echo "FAIL: $failed failed, $passed passed"
    exit 1
fi
