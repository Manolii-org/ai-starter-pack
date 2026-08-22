#!/usr/bin/env python3
"""Declared-subset check for the pack's PreToolUse hook.

`scripts/pre-tool-use.py` here is 398 lines; the orchestrator's copy in
manolii-org/master is 2,336 and carries a strictly larger guard set. Measured
2026-08-22:

    marker              pack   master
    OSS-GUARD              8        6
    BROAD-DISPATCH         1        1
    RETURN-CAP             0        1
    GUARD: (path guards)   0        3
    TOP-TIER-SUBAGENT      0        1
    DEPRECATED-AGENT       0        1
    TIER-MISMATCH          0        2
    guards.json            0        2

That gap may be deliberate — a pack shipped to external consumers has no reason
to carry Manolii's `.ai/guards.json` path guards or ecosystem-specific agent
routing. But nothing recorded the intent and nothing detected drift, so
"deliberate subset" and "silent rot" were indistinguishable. This file does not
force parity; it makes the subset EXPLICIT and monitored.

Two failure modes it catches:
  * the pack silently LOSES a guard it is supposed to ship (regression);
  * a guard appears here that the declaration says is out of scope, so the
    declaration and the code have drifted apart.

If the pack should adopt one of the omitted guards, move it from OMITTED to
REQUIRED here in the same commit that ports the code. Editing the declaration
alone will fail.

Run: python3 scripts/tests/test_hook_guard_coverage.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
HOOK = REPO / "scripts" / "pre-tool-use.py"

# Guards this pack ships. Marker -> minimum occurrences.
REQUIRED: dict[str, int] = {
    "OSS-GUARD": 1,        # model-routing tier enforcement on Agent dispatch
    "BROAD-DISPATCH": 1,   # breadth-signal nudge toward an orchestrator
}

# Guards deliberately NOT shipped, with the reason. Presence here is a signal to
# review the declaration, not automatically a bug.
OMITTED: dict[str, str] = {
    "RETURN-CAP": "return-cap discipline is orchestrator policy, not a pack default",
    "GUARD:": "path guards read .ai/guards.json, which is Manolii-ecosystem-specific",
    "TOP-TIER-SUBAGENT": "opus-pinning rules are orchestrator routing policy",
    "DEPRECATED-AGENT": "names agents that exist only in the orchestrator repo",
    "TIER-MISMATCH": "depends on the orchestrator's tier_definitions",
    "guards.json": "see GUARD: above",
}

failures: list[str] = []


def main() -> int:
    if not HOOK.exists():
        print(f"✗ {HOOK.relative_to(REPO)} is missing — the pack must ship a PreToolUse hook")
        return 1

    text = HOOK.read_text(encoding="utf-8")

    for marker, minimum in REQUIRED.items():
        n = text.count(marker)
        if n < minimum:
            failures.append(
                f"REQUIRED guard {marker!r} appears {n}x (expected >= {minimum}). "
                "The pack has lost a guard it is declared to ship."
            )

    for marker, reason in OMITTED.items():
        if marker in text:
            failures.append(
                f"guard {marker!r} is present but declared OMITTED ({reason}). "
                "If the pack now ships it, move it to REQUIRED in this file."
            )

    overlap = set(REQUIRED) & set(OMITTED)
    if overlap:
        failures.append(f"declaration is self-contradictory: {sorted(overlap)} in both REQUIRED and OMITTED")

    if failures:
        print("Pack hook guard-coverage check FAILED:\n")
        for f in failures:
            print(f"  ✗ {f}")
        print(
            "\nThis check records which guards the pack intends to ship. It does not\n"
            "require parity with manolii-org/master — it requires the declaration and\n"
            "the code to agree.\n"
        )
        return 1

    print(
        f"✔ pack hook guard coverage: {len(REQUIRED)} required present, "
        f"{len(OMITTED)} declared omissions absent"
    )
    return 0


def test_all_checks_pass() -> None:
    """pytest entry point — the only test this module exposes to collection.

    CI runs `coverage run -m pytest scripts/tests/test_*.py`
    (.github/workflows/quality-base.yml:203). Modules in this directory report through
    `main()`'s exit code rather than assertions, so without a collected test that
    checks it, pytest sees nothing to fail. Measured
    before this fix: `pytest -q test_fork_dispatch.py` reported **7 passed** while
    `python3 test_fork_dispatch.py` exited 1 with 4 real failures, and
    test_no_withdrawn_tool_instructions.py collected **zero** tests. Every guard in
    this directory was therefore inert in CI.

    Fix: the individual checks are named `check_*` (not collected), and this single
    collected test funnels them through `main()` so both invocation modes agree.
    Found by Codex review on PR #4433.
    """
    assert main() == 0, "see captured stdout for the failing checks"


if __name__ == "__main__":
    sys.exit(main())
