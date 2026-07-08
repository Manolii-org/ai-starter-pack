#!/usr/bin/env bash
# Stop hook — outputs session-ending reminder checklist.
# Plain text stdout becomes additionalContext for Claude.

# Opt-in: run reflection script automatically on session end.
# Set CLAUDE_AUTO_REFLECT=1 in your shell environment to enable.
# Default is OFF — runs fast (no API call) but writes staged files that
# persist until /reflect is run to process them.
if [ "${CLAUDE_AUTO_REFLECT:-0}" = "1" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  python3 "${SCRIPT_DIR}/reflect-on-failures.py" > /dev/null 2>&1 || true
fi

# ── Session cost logger ───────────────────────────────────────────────────────
# Parses the active session JSONL for token usage, estimates cost, and appends
# one record to .ai/metrics/session-costs.jsonl. Silent on missing files.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Extract session_id from the Stop hook JSON payload on stdin (consumed once here).
_HOOK_INPUT=$(cat)
_SESSION_ID=$(printf '%s' "$_HOOK_INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || true)
python3 "${SCRIPT_DIR}/session-cost-logger.py" ${_SESSION_ID:+--session "$_SESSION_ID"} 2>/dev/null || true
unset _HOOK_INPUT _SESSION_ID

cat <<'EOF'
Session ending checklist:
- Run /extract-insights if decisions were made
- Run /session-summary for significant sessions
- Flag any unresolved issues for next session
- Check if .ai/memory/ changes should be committed
- Check .ai/reflections/ for any pending-review reflection proposals (or run /reflect)
EOF

# ── Branch fingerprint ───────────────────────────────────────────────────────
# One-line visibility of what this branch has done relative to main: files
# touched, lines changed, new files created. Surfaces the "bloats existing
# files, never creates new ones" pattern without enforcing anything.
# Scoped to the branch (not the session) — accurate framing because
# session boundaries are not reliably knowable from inside a hook.
_branch_fingerprint() {
    git rev-parse --git-dir >/dev/null 2>&1 || return 0
    # Pick the first ref that actually exists: BASE_BRANCH (if set), then
    # origin/main, then origin/master. Only fall through to origin/master
    # when origin/main is absent — never when merge-base fails against an
    # existing configured branch (that would silently switch the baseline).
    local target
    if [ -n "${BASE_BRANCH:-}" ] \
        && git rev-parse --verify --quiet "$BASE_BRANCH" >/dev/null; then
        target="$BASE_BRANCH"
    elif git rev-parse --verify --quiet origin/main >/dev/null; then
        target="origin/main"
    elif git rev-parse --verify --quiet origin/master >/dev/null; then
        target="origin/master"
    else
        return 0
    fi
    local base
    base=$(git merge-base HEAD "$target" 2>/dev/null) || return 0
    local stat
    stat=$(git diff --shortstat "$base" 2>/dev/null) || return 0
    [ -n "$stat" ] || return 0
    local new_files
    new_files=$(git diff --name-status --diff-filter=A "$base" 2>/dev/null | wc -l | tr -d ' ')
    echo "Branch fingerprint (vs ${target}): ${stat# }, ${new_files} new file(s)."
}
_branch_fingerprint

# ── Doctor: surface dysfunction signals from this session ────────────────────
# Runs the existing doctor-analyse.py over the last hour's transcripts and
# surfaces only warn-or-higher findings. Signals covered: edit-thrashing,
# error-loop, repeated-instructions, correction-heavy, rapid-corrections.
# Silent on clean sessions. Synchronous because doctor runs in <0.2s; capped
# at 4s to stay within the settings.json Stop-hook timeout of 5s —
# backgrounding with disown would lose stdout once the parent hook exits.
_doctor_silent() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "${script_dir}/doctor-analyse.py" ] || return 0
    # Pass path via env var (no heredoc interpolation into Python source) —
    # mirrors the safe pattern used by _run_intent_drift below.
    export _DOCTOR_SCRIPT="${script_dir}/doctor-analyse.py"
    python3 - <<'PYEOF' 2>/dev/null || true
import json, os, subprocess, sys
try:
    out = subprocess.run(
        ["python3", os.environ["_DOCTOR_SCRIPT"], "--since", "1h", "--json"],
        capture_output=True, text=True, timeout=4,
    )
    data = json.loads(out.stdout or "{}")
    by_sig = data.get("summary", {}).get("by_signal", {})
    if not by_sig:
        sys.exit(0)
    parts = [f"{n}× {sig}" for sig, n in sorted(by_sig.items(), key=lambda x: -x[1])]
    print("Doctor signals this session: " + ", ".join(parts) + ". Run /doctor for detail.")
except Exception:
    pass
PYEOF
    unset _DOCTOR_SCRIPT
}
_doctor_silent

# ── Intent-drift check ───────────────────────────────────────────────────────
# Compares the session diff against the original user prompt (if available).
# Output goes to .ai/drift-alerts/ for later review — never blocks Stop.
_run_intent_drift() {
    local prompt_file=".ai/session-logs/current/user-prompt.txt"
    [ -f "$prompt_file" ] || return 0

    local diff_summary
    diff_summary=$(git diff "$(git merge-base HEAD origin/main 2>/dev/null || echo HEAD~1)" --stat 2>/dev/null | tail -5 || true)
    [ -n "$diff_summary" ] || return 0

    local original_prompt
    original_prompt=$(head -c 500 "$prompt_file" 2>/dev/null || true)
    [ -n "$original_prompt" ] || return 0

    # Only run if API key available and drift check enabled
    [ -n "${ANTHROPIC_API_KEY:-}" ] || return 0
    [ "${PR_ASSESSMENT_DRIFT_ENABLED:-0}" = "1" ] || return 0

    # Export as env vars — avoids shell injection into Python source (triple-quote termination,
    # backslash sequences, etc. that would occur with unquoted <<PYEOF interpolation).
    export _DRIFT_PROMPT="$original_prompt"
    export _DRIFT_STATS="$diff_summary"

    python3 - <<'PYEOF' 2>/dev/null || true
import anthropic, json, os
from datetime import datetime, timezone

_original_prompt = os.environ.get("_DRIFT_PROMPT", "")
_diff_stats = os.environ.get("_DRIFT_STATS", "")

# Explicitly route to Anthropic direct — never proxy.
# ANTHROPIC_BASE_URL may point to the LiteLLM proxy; clearing it here
# prevents silent rerouting of this restricted-intent call to an OSS backend.
os.environ.pop("ANTHROPIC_BASE_URL", None)
client = anthropic.Anthropic(base_url="https://api.anthropic.com")
_prompt_text = (
    "Original intent: " + _original_prompt + "\n\n"
    "Session diff stats:\n" + _diff_stats + "\n\n"
    "Classify scope drift: on-scope | minor-expansion | significant-drift | scope-creep\n"
    'Return JSON only: {"classification": "...", "reason": "one sentence"}'
)

try:
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        timeout=15.0,
        messages=[{"role": "user", "content": _prompt_text}]
    )
    # Reasoning models (e.g. DeepSeek via a LiteLLM proxy alias) prepend a
    # `thinking` block — select text blocks, not content[0].
    _text = "".join(
        getattr(_b, "text", "") for _b in r.content
        if getattr(_b, "type", "text") == "text"
    )
    _result = json.loads(_text)
    if _result.get("classification") in ("significant-drift", "scope-creep"):
        os.makedirs(".ai/drift-alerts", exist_ok=True)
        _fname = f".ai/drift-alerts/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
        with open(_fname, "w") as _f:
            json.dump({
                "classification": _result["classification"],
                "reason": _result["reason"],
                "diff_stats": _diff_stats,
            }, _f, indent=2)
        print(f"[intent-drift] {_result['classification']}: {_result['reason']}")
        print(f"[intent-drift] Alert written to {_fname}")
except Exception:
    pass
PYEOF
}

# Run in detached background — Stop hook timeout is 5s; an API call takes 2-10s.
# Subshell isolates the exported env vars (_DRIFT_PROMPT, _DRIFT_STATS).
# disown prevents SIGHUP when the parent shell exits.
( _run_intent_drift ) &
disown $! 2>/dev/null || true
