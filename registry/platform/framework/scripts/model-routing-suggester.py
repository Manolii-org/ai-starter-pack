#!/usr/bin/env python3
"""Main-thread tier suggester for the Sonnet-executor + Opus-advisor model.

Maps a prompt/context to one of four main-thread tiers:
  light-main   → Haiku solo         (simple mechanical tasks)
  medium-main  → Sonnet solo        (moderate multi-file work)
  heavy-main   → Sonnet + Opus advisor (complex, multi-repo, escalation patterns)
  fast-escape  → raw Opus           (user-invoked /fast only)

Naming note: these tiers (light-main/medium-main/heavy-main/fast-escape) are the
ADVISOR-TIER namespace. The UserPromptSubmit hook (scripts/classify-message.py)
uses a separate DISPLAY namespace (light/standard/heavy). They serve different
purposes and do not conflict at runtime.

Usage:
  python3 scripts/model-routing-suggester.py --text "read foo.py" --expect light-main
  python3 scripts/model-routing-suggester.py --text "design a migration strategy for..."
  python3 scripts/model-routing-suggester.py  # reads from stdin as JSON {text, context}
  from scripts.model-routing-suggester import suggest_tier

Logs to .ai/metrics/tier-suggestion.jsonl (append-only, optional — skipped if unwritable).
"""
import json
import re
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Escalation trigger patterns (mirrors policies.advisor_policy in model-routing.json)
# ---------------------------------------------------------------------------
_MULTI_REPO_RE = re.compile(
    r"\b(cross.repo|multi.repo|multiple repos?|all repos?)\b",
    re.I,
)
_SECURITY_RE = re.compile(
    r"\b(auth(?:entication|orisation|orization)?|crypto|payment|stripe|oauth|jwt|permission|rbac|tls|certificate|secret|api.?key|\.sql|migration|schema.change|rls|row.level.security)\b",
    re.I,
)
_CROSS_CUTTING_RE = re.compile(
    r"\b((?:refactor|rename|update|change|fix) (?:across|all|all files)|global rename|system.?wide|across the codebase)\b",
    re.I,
)
_SCHEMA_RE = re.compile(
    r"\b(\.sql|migration|schema|drizzle|alter table|add column|drop column|create table|supabase)\b",
    re.I,
)
_ROLLBACK_RE = re.compile(
    r"\b(rollback|roll back|revert commit|undo deploy|incident|outage|P0|P1|emergency)\b",
    re.I,
)
_COST_PERF_RE = re.compile(
    r"\b(cost.?trade.?off|performance.?trade.?off|scale|capacity plan|resource allocation|infrastructure decision)\b",
    re.I,
)
_POLICY_RE = re.compile(
    r"\b(license|data residency|client data|GDPR|HIPAA|SOC|privacy|compliance|contract)\b",
    re.I,
)
_ARCHITECTURE_RE = re.compile(
    r"\b(architect|design pattern|ADR|architecture decision|system design|service mesh|event.driven|CQRS|saga)\b",
    re.I,
)
# Governance / judgment phrasing — guards, blast-radius reasoning, auto-merge gates,
# data-sensitivity / safety-tier discussions. The failure mode on these prompts is
# rationalising existing config rather than reasoning blast-radius from first
# principles; escalating to heavy-main pulls in the stronger model for the judgment step.
# Non-word lookarounds (not plain \b) so dot-prefixed paths like ".ai/guards"
# still match after a space — \b would not fire between a space and a ".".
_GOVERNANCE_RE = re.compile(
    r"(?<!\w)("
    r"guard(?:ed)? paths?|guard entr(?:y|ies)|guards\.json|\.ai/guards|"
    r"unfreeze|blast.?radius|under.?protected|most.?protected|"
    r"auto.?merges?|merge.?protections?|branch.?protections?|CI.?gates?|"
    r"data.?sensitivit(?:y|ies)|safety.?tiers?|safety.?layers?|"
    r"should (?:we|i|they) (?:guard|protect|harden|secure|gate|escalate)"
    r")(?!\w)",
    re.I,
)
_FAST_ESCAPE_RE = re.compile(r"^/fast\b", re.I)  # /fast only; /heavy is NOT an escape hatch (/heavy matches heavy-main escalation patterns)

_HEAVY_PATTERNS = [
    ("multi_repo", _MULTI_REPO_RE),
    ("security_boundary", _SECURITY_RE),
    ("cross_cutting_refactor", _CROSS_CUTTING_RE),
    ("schema_migration", _SCHEMA_RE),
    ("rollback_incident", _ROLLBACK_RE),
    ("cost_perf_tradeoff", _COST_PERF_RE),
    ("policy_contract", _POLICY_RE),
    ("architecture_decision", _ARCHITECTURE_RE),
    ("governance_judgment", _GOVERNANCE_RE),
]

# Light-tier signals
_SINGLE_FILE_RE = re.compile(
    r"\b(read|cat|show|display|open|look at|check)\b.{0,40}\b(file|\.py|\.ts|\.json|\.md|\.sql|\.sh)\b",
    re.I,
)
_GREP_GLOB_RE = re.compile(
    r"\b(grep|find|glob|search for|list files|count|rename|format|lint|status|boilerplate)\b",
    re.I,
)


def suggest_tier(prompt_text: str, context_signal: str = "") -> dict:
    """Return {tier, confidence, reason, matched_patterns}.

    Args:
        prompt_text: The user's prompt text.
        context_signal: Optional extra signal (e.g., filenames, repo context).

    Returns:
        dict with keys: tier (str), confidence (float 0-1), reason (str),
                        matched_patterns (list[str]).
    """
    combined = f"{prompt_text} {context_signal}".strip()
    char_count = len(combined)

    # fast-escape: explicit /fast prefix only (/heavy routes to heavy-main)
    if _FAST_ESCAPE_RE.match(prompt_text.strip()):
        return {
            "tier": "fast-escape",
            "confidence": 0.99,
            "reason": "Explicit /fast user command — raw Opus bypass.",
            "matched_patterns": ["fast_escape_prefix"],
        }

    # heavy-main: any escalation pattern match
    matched = []
    for name, pattern in _HEAVY_PATTERNS:
        if pattern.search(combined):
            matched.append(name)

    if matched:
        confidence = min(0.95, 0.75 + len(matched) * 0.05)
        return {
            "tier": "heavy-main",
            "confidence": round(confidence, 2),
            "reason": f"Escalation pattern(s) detected: {', '.join(matched)}",
            "matched_patterns": matched,
            "recommended_oss_tier": "tier-0-oss-heavy",
        }

    # Char-count tiebreaker for remaining cases
    if char_count > 2000:
        return {
            "tier": "heavy-main",
            "confidence": 0.72,
            "reason": f"Prompt length {char_count} chars exceeds 2000 threshold.",
            "matched_patterns": ["char_count_2000+"],
            "recommended_oss_tier": "tier-0-oss-heavy",
        }

    # light-main: <500 chars + single-file/grep/glob signals
    if char_count < 500 and (_SINGLE_FILE_RE.search(combined) or _GREP_GLOB_RE.search(combined)):
        # Grep/glob/extraction tasks → cheapest extraction tier
        is_extraction = _GREP_GLOB_RE.search(combined) and not re.search(r"\b(rename|boilerplate)\b", combined, re.I)
        oss_tier = "tier-4-extract" if is_extraction else "tier-1-fast"
        return {
            "tier": "light-main",
            "confidence": 0.88,
            "reason": f"Short prompt ({char_count} chars) with single-file or search signal.",
            "matched_patterns": ["char_count_lt500", "single_file_or_search"],
            "recommended_oss_tier": oss_tier,
        }
    if char_count < 500:
        return {
            "tier": "light-main",
            "confidence": 0.80,
            "reason": f"Short prompt ({char_count} chars), no escalation patterns.",
            "matched_patterns": ["char_count_lt500"],
            "recommended_oss_tier": "tier-1-fast",
        }

    # medium-main: 500-2000 chars, no escalation patterns
    return {
        "tier": "medium-main",
        "confidence": 0.82,
        "reason": f"Moderate prompt ({char_count} chars), no escalation patterns detected.",
        "matched_patterns": ["char_count_500_2000"],
        "recommended_oss_tier": "tier-1-fast",
    }


def _log_suggestion(result: dict, prompt_text: str) -> None:
    log_path = REPO_ROOT / ".ai" / "metrics" / "tier-suggestion.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tier": result["tier"],
            "confidence": result["confidence"],
            "reason": result["reason"],
            "prompt_hash": hashlib.sha256(prompt_text.encode()).hexdigest()[:12],
            "prompt_chars": len(prompt_text),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"WARN: tier-suggestion log write failed: {e}", file=sys.stderr)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text", help="Prompt text to classify")
    parser.add_argument("--context", default="", help="Optional context signal")
    parser.add_argument("--expect", help="Expected tier (for smoke-test mode — exits 1 if mismatch)")
    parser.add_argument("--no-log", action="store_true", help="Skip writing to tier-suggestion.jsonl")
    args = parser.parse_args()

    if args.text:
        text = args.text
        context = args.context
    else:
        raw = sys.stdin.read().strip()
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                text = data.get("text", data.get("prompt", ""))
                context = data.get("context", "")
            except json.JSONDecodeError:
                text = raw
                context = ""
        else:
            text = raw
            context = ""

    result = suggest_tier(text, context)

    if not args.no_log:
        _log_suggestion(result, text)

    print(json.dumps(result, indent=2))

    if args.expect:
        if result["tier"] != args.expect:
            print(
                f"\nSMOKE TEST FAILED: expected tier '{args.expect}', got '{result['tier']}'",
                file=sys.stderr,
            )
            sys.exit(1)
        elif result["confidence"] < 0.8:
            print(
                f"\nWARNING: correct tier '{result['tier']}' but confidence {result['confidence']} < 0.8",
                file=sys.stderr,
            )
        else:
            print(f"\nSMOKE TEST PASSED: tier='{result['tier']}', confidence={result['confidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
