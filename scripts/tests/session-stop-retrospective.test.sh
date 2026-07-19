#!/usr/bin/env bash
# Hermetic tests for scripts/session-stop-retrospective.sh — fix #5 coverage.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WRAPPER="$REPO_ROOT/scripts/session-stop-retrospective.sh"
FAIL=0; PASS=0
_pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
_fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

_setup() {
    local d; d=$(mktemp -d)
    mkdir -p "$d/scripts/lib"
    # Fake collector that just logs argv to a well-known file so tests can inspect it.
    cat > "$d/scripts/session-retrospective.py" <<'PY'
#!/usr/bin/env python3
import sys, os, json, pathlib
out = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")) / "collector-argv.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
# Exit non-zero when explicitly asked (used by failure-marker test).
if os.environ.get("_FAIL_MODE") == "1":
    sys.exit(3)
sys.exit(0)
PY
    chmod +x "$d/scripts/session-retrospective.py"
    ( cd "$d" && git init -q 2>/dev/null || true )
    echo "$d"
}

_teardown() { rm -rf "$1"; }

# The wrapper resolves its repo root via `git rev-parse --show-toplevel`,
# so tests must `cd` into the tmp dir (which _setup git-init's) — the
# wrapper then loads its OWN copy of the collector from the tmp dir.
_run_wrapper() {
    local d="$1"; shift
    ( cd "$d" && CLAUDE_PROJECT_DIR="$d" "$@" bash "$WRAPPER" )
}

# Test 1: no stdin → still runs (empty payload path)
t="runs with empty stdin"
d=$(_setup)
_run_wrapper "$d" </dev/null >/dev/null
[[ -f "$d/collector-argv.jsonl" ]] && _pass "$t" || _fail "$t"
_teardown "$d"

# Test 2: parses session_id + transcript_path from JSON stdin
t="parses session_id and transcript_path from payload"
d=$(_setup)
echo '{"session_id":"SID-42","transcript_path":"/tmp/x.jsonl"}' | _run_wrapper "$d" >/dev/null
if grep -q "SID-42" "$d/collector-argv.jsonl" && grep -q "x.jsonl" "$d/collector-argv.jsonl"; then
    _pass "$t"
else
    _fail "$t"
fi
_teardown "$d"

# Test 3: malformed JSON payload → fail-open (no crash, still 0)
t="malformed payload does not fail the hook"
d=$(_setup)
if echo 'this is not json' | _run_wrapper "$d" >/dev/null 2>&1; then _pass "$t"; else _fail "$t"; fi
_teardown "$d"

# Test 4: collector failure writes failure marker AND wrapper still exits 0
t="collector non-zero writes .last-capture-failed and exits 0"
d=$(_setup)
if ( cd "$d" && CLAUDE_PROJECT_DIR="$d" _FAIL_MODE=1 bash "$WRAPPER" </dev/null >/dev/null 2>&1 ); then
    marker="$d/.ai/memory/retrospectives/.last-capture-failed"
    if [[ -f "$marker" ]] && grep -q '"exit_code":3' "$marker"; then _pass "$t"; else _fail "$t (no marker)"; fi
else
    _fail "$t (wrapper exited non-zero)"
fi
_teardown "$d"

# Test 5: collector success clears an existing failure marker
t="collector success clears stale failure marker"
d=$(_setup)
mkdir -p "$d/.ai/memory/retrospectives"
echo "stale" > "$d/.ai/memory/retrospectives/.last-capture-failed"
_run_wrapper "$d" </dev/null >/dev/null
[[ ! -f "$d/.ai/memory/retrospectives/.last-capture-failed" ]] && _pass "$t" || _fail "$t"
_teardown "$d"

# Test 6: --local-only is always injected (Stop budget)
t="wrapper always passes --local-only to collector"
d=$(_setup)
_run_wrapper "$d" </dev/null >/dev/null
if grep -q '"--local-only"' "$d/collector-argv.jsonl"; then _pass "$t"; else _fail "$t"; fi
_teardown "$d"

# Test 7: wrapper is a no-op if collector script missing
t="no-op when collector script absent"
d=$(mktemp -d)
mkdir -p "$d/scripts"
( cd "$d" && git init -q 2>/dev/null || true )
if _run_wrapper "$d" </dev/null >/dev/null 2>&1; then _pass "$t"; else _fail "$t"; fi
rm -rf "$d"

# Test 8: CLAUDE_PLUGIN_ROOT — collector resolved from plugin install, not consumer (Codex P2 2026-07-19)
t="resolves collector from CLAUDE_PLUGIN_ROOT when consumer has no scripts/session-retrospective.py"
consumer=$(mktemp -d)
mkdir -p "$consumer/scripts"
( cd "$consumer" && git init -q 2>/dev/null || true )
plugin=$(mktemp -d)
mkdir -p "$plugin/scripts"
cat > "$plugin/scripts/session-retrospective.py" <<'PY'
#!/usr/bin/env python3
import sys, os, pathlib
pathlib.Path(os.environ["CLAUDE_PROJECT_DIR"], "plugin-collector-fired").write_text("yes")
sys.exit(0)
PY
chmod +x "$plugin/scripts/session-retrospective.py"
( cd "$consumer" && CLAUDE_PROJECT_DIR="$consumer" CLAUDE_PLUGIN_ROOT="$plugin" \
    bash "$WRAPPER" </dev/null >/dev/null 2>&1 )
if [[ -f "$consumer/plugin-collector-fired" ]]; then _pass "$t"; else _fail "$t (plugin collector did not run)"; fi
rm -rf "$consumer" "$plugin"

echo
echo "session-stop-retrospective.test.sh — passed: $PASS  failed: $FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
