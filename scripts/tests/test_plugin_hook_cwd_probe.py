#!/usr/bin/env python3
"""Behavioural test for the plugin hooks' working-directory probe.

Why this exists: the generated plugin hooks used to open with a bare
`cd "${CLAUDE_PROJECT_DIR}" && …`. In Claude Code Web *multi-repo* sessions
`CLAUDE_PROJECT_DIR` is UNSET (verified live, CLI 2.1.239, 2026-08-22), so that
degrades to `cd ""` — which succeeds and lands in $HOME. Every hook then reads and
writes repo-relative state (.ai/memory/*, .ai/guards.json, .ai/sessions/*) outside
any repository. The Stop self-check is the worst case: its `>> .ai/memory/...`
sink is deliberately cwd-relative.

This asserts behaviour, not string shape: each case actually EXECUTES the probe
under a controlled environment and checks where it lands. A shape-only assertion
would pass on a probe that still resolves to $HOME.

Run: python3 scripts/tests/test_plugin_hook_cwd_probe.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

failures: list[str] = []


def fail(case: str, detail: str) -> None:
    failures.append(f"[{case}] {detail}")


def _probe() -> str:
    """Extract the probe from the builder without importing its whole CLI."""
    src = (REPO / "scripts" / "build-plugin.py").read_text(encoding="utf-8")
    start = src.find("CWD_PROBE = (")
    if start == -1:
        raise AssertionError("CWD_PROBE not found in build-plugin.py")
    end = src.find("\n    )", start)
    body = src[start:end]
    parts = [
        line.strip().removeprefix("CWD_PROBE = (").strip()
        for line in body.splitlines()
    ]
    out = "".join(p[1:-1] for p in parts if p.startswith(("'", '"')) and len(p) > 1)
    if not out:
        raise AssertionError("could not reassemble CWD_PROBE literal")
    return out


def _run(probe: str, cwd: Path, env_extra: dict[str, str | None]) -> str:
    """Execute `<probe> && pwd -P` and return the resulting directory."""
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    r = subprocess.run(
        ["sh", "-c", f"{probe} && pwd -P"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise AssertionError(f"probe exited {r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def test_probe_never_lands_in_home_when_var_unset() -> None:
    """The regression itself: unset CLAUDE_PROJECT_DIR, non-git cwd, no marker."""
    probe = _probe()
    home = str(Path.home().resolve())
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td).resolve()
        landed = _run(probe, scratch, {"CLAUDE_PROJECT_DIR": None})
    if landed == home:
        fail("unset-var", f"probe landed in $HOME ({home}) — the original bug")
    if landed != str(scratch):
        fail("unset-var", f"expected fallback to $PWD ({scratch}), landed in {landed}")


def test_probe_ignores_empty_var() -> None:
    """An explicitly empty value must be treated as absent, not as `cd ""`."""
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td).resolve()
        landed = _run(probe, scratch, {"CLAUDE_PROJECT_DIR": ""})
    if landed != str(scratch):
        fail("empty-var", f"empty CLAUDE_PROJECT_DIR should fall through to $PWD; landed in {landed}")


def test_probe_ignores_nonexistent_dir() -> None:
    """A stale path (deleted worktree) must fall through, not fail the hook."""
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td).resolve()
        landed = _run(probe, scratch, {"CLAUDE_PROJECT_DIR": str(scratch / "gone")})
    if landed != str(scratch):
        fail("stale-var", f"nonexistent CLAUDE_PROJECT_DIR should fall through; landed in {landed}")


def test_probe_honours_valid_var() -> None:
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        target = root / "proj"
        target.mkdir()
        (root / "elsewhere").mkdir()
        landed = _run(probe, root / "elsewhere", {"CLAUDE_PROJECT_DIR": str(target)})
    if landed != str(target):
        fail("valid-var", f"expected {target}, landed in {landed}")


def test_probe_finds_single_hook_root_marker() -> None:
    """Multi-repo case: exactly one sibling carries .ai/hook-root."""
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        marked = root / "master"
        (marked / ".ai").mkdir(parents=True)
        (marked / ".ai" / "hook-root").write_text("", encoding="utf-8")
        (root / "other-repo").mkdir()
        landed = _run(probe, root, {"CLAUDE_PROJECT_DIR": None})
    if landed != str(marked):
        fail("single-marker", f"expected the marked repo {marked}, landed in {landed}")


def test_probe_is_ambiguous_safe_with_two_markers() -> None:
    """Two markers is ambiguous — must fall back to $PWD, not guess."""
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        for name in ("a", "b"):
            (root / name / ".ai").mkdir(parents=True)
            (root / name / ".ai" / "hook-root").write_text("", encoding="utf-8")
        landed = _run(probe, root, {"CLAUDE_PROJECT_DIR": None})
    if landed != str(root):
        fail("two-markers", f"ambiguous marker set should fall back to $PWD ({root}); landed in {landed}")


def test_generated_hooks_all_use_the_probe() -> None:
    """No generated hook command may keep the unguarded bare cd."""
    committed = REPO / "plugin" / "manolii-framework" / "hooks" / "hooks.json"
    if not committed.exists():
        return  # artifact not built in this checkout; builder tests above still apply
    blob = json.dumps(json.loads(committed.read_text(encoding="utf-8")))
    if 'cd \\"${CLAUDE_PROJECT_DIR}\\" &&' in blob or 'cd "${CLAUDE_PROJECT_DIR}" &&' in blob:
        fail(
            "artifact-stale",
            "plugin/manolii-framework/hooks/hooks.json still contains a bare "
            "`cd \"${CLAUDE_PROJECT_DIR}\" &&`. Rebuild it: python3 scripts/build-plugin.py",
        )


def main() -> int:
    for fn in (
        test_probe_never_lands_in_home_when_var_unset,
        test_probe_ignores_empty_var,
        test_probe_ignores_nonexistent_dir,
        test_probe_honours_valid_var,
        test_probe_finds_single_hook_root_marker,
        test_probe_is_ambiguous_safe_with_two_markers,
        test_generated_hooks_all_use_the_probe,
    ):
        try:
            fn()
        except Exception as exc:  # a broken harness is a failure, not a pass
            fail(fn.__name__, f"{type(exc).__name__}: {exc}")

    if failures:
        print("Plugin hook cwd-probe check FAILED:\n")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("✔ plugin hook cwd probe: 7 cases pass (never lands in $HOME)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
