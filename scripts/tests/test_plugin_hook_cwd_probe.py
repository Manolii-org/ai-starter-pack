#!/usr/bin/env python3
"""Behavioural test for the plugin hooks' working-directory probe.

Why this exists: the generated plugin hooks used to open with a bare
`cd "${CLAUDE_PROJECT_DIR}" && …`. In Claude Code Web *multi-repo* sessions
`CLAUDE_PROJECT_DIR` is UNSET (verified live, CLI 2.1.239, 2026-08-22).

CORRECTION (Codex review, #71 — P1): an earlier version of this file claimed that
`cd ""` "lands in $HOME". It does not. Measured: `cd ""` is a NO-OP — rc=0, working
directory unchanged; only a bare `cd` with no argument goes to $HOME. The real
failure is subtler. The hook simply runs wherever Claude Code launched it, and per
the 2.1.239 changelog that is "the project root or home directory". When it is
$HOME, every hook reads and writes repo-relative state (.ai/memory/*,
.ai/guards.json, .ai/sessions/*) into the home directory. The Stop self-check is
the worst case: its `>> .ai/memory/...` sink is deliberately cwd-relative.

Codex also showed the first fix was incomplete: in $HOME, `git rev-parse` finds
nothing and this pack ships no `.ai/hook-root` marker, so the probe fell back to
$PWD — which IS $HOME. Resolving to a non-repository $HOME is now treated as
failure to resolve, and the hook skips (exit 0) rather than polluting.

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
    # An empty stdout means the probe short-circuited before `pwd` — i.e. it refused
    # to resolve a root. Surfaced as the sentinel "<skipped>" so cases can assert it.
    return r.stdout.strip() or "<skipped>"


def check_probe_never_lands_in_home_when_var_unset() -> None:
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


def check_probe_ignores_empty_var() -> None:
    """An explicitly empty value must be treated as absent, not as `cd ""`."""
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td).resolve()
        landed = _run(probe, scratch, {"CLAUDE_PROJECT_DIR": ""})
    if landed != str(scratch):
        fail("empty-var", f"empty CLAUDE_PROJECT_DIR should fall through to $PWD; landed in {landed}")


def check_probe_ignores_nonexistent_dir() -> None:
    """A stale path (deleted worktree) must fall through, not fail the hook."""
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td).resolve()
        landed = _run(probe, scratch, {"CLAUDE_PROJECT_DIR": str(scratch / "gone")})
    if landed != str(scratch):
        fail("stale-var", f"nonexistent CLAUDE_PROJECT_DIR should fall through; landed in {landed}")


def check_probe_honours_valid_var() -> None:
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        target = root / "proj"
        target.mkdir()
        (root / "elsewhere").mkdir()
        landed = _run(probe, root / "elsewhere", {"CLAUDE_PROJECT_DIR": str(target)})
    if landed != str(target):
        fail("valid-var", f"expected {target}, landed in {landed}")


def check_probe_finds_single_hook_root_marker() -> None:
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


def check_probe_is_ambiguous_safe_with_two_markers() -> None:
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


def check_refuses_to_run_in_a_non_repo_home() -> None:
    """Codex #71 P1: the fallback must LEAVE $HOME, not settle in it.

    In $HOME with no CLAUDE_PROJECT_DIR, `git rev-parse` returns nothing and this
    pack ships no `.ai/hook-root` marker, so steps 1-3 all fail and step 4 ($PWD)
    hands back $HOME itself. Running there is exactly the bug. The probe now treats
    that as failure-to-resolve and skips with exit 0 — fail-open on the permission
    decision, fail-safe on the filesystem.
    """
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        fake_home = Path(td).resolve()          # a $HOME that is NOT a git repo
        landed = _run(probe, fake_home, {"CLAUDE_PROJECT_DIR": None, "HOME": str(fake_home)})
    if landed != "<skipped>":
        fail("home-refusal", f"probe should skip rather than run in a non-repo $HOME; landed in {landed}")


def check_still_runs_when_home_is_itself_a_repo() -> None:
    """The refusal must not fire when $HOME genuinely IS the repository."""
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        home_repo = Path(td).resolve()
        (home_repo / ".git").mkdir()
        landed = _run(probe, home_repo, {"CLAUDE_PROJECT_DIR": None, "HOME": str(home_repo)})
    if landed != str(home_repo):
        fail("home-is-repo", f"a real repo at $HOME must still be used; landed in {landed}")


def check_still_runs_when_home_is_a_linked_worktree() -> None:
    """A worktree / submodule keeps `.git` as a FILE, not a directory.

    Codex #71 (P2): the refusal was `[ ! -d "$_R/.git" ]`, so a genuine repository at
    $HOME that happens to be a linked worktree or a submodule checkout was skipped —
    `git rev-parse --show-toplevel` resolved it correctly while `-d` did not. Verified
    with `git worktree add` on 2026-08-22: `.git` was a regular file containing
    `gitdir: …`. The guard now uses `-e` plus a `git -C` confirmation.
    """
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        main = root / "main-repo"
        main.mkdir()
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        def git(*a: str, cwd: Path = main) -> None:
            subprocess.run(["git", *a], cwd=cwd, check=True,
                           capture_output=True, env={**os.environ, **env})
        git("init", "-q", ".")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        (main / "a.txt").write_text("x")
        git("add", "-A")
        git("commit", "-qm", "init")
        wt = root / "linked"
        git("worktree", "add", "-q", str(wt), "-b", "wt")
        if (wt / ".git").is_dir():                     # guards the premise itself
            fail("home-worktree", "expected .git to be a FILE in a linked worktree")
            return
        landed = _run(probe, wt, {"CLAUDE_PROJECT_DIR": None, "HOME": str(wt)})
    if landed != str(wt):
        fail("home-worktree", f"a worktree at $HOME must still be used; landed in {landed}")


def check_exports_resolved_root_for_entrypoints() -> None:
    """Codex #71 (second P1): `cd` alone leaves CLAUDE_PROJECT_DIR unset.

    Three bundled entrypoints read the variable directly and fall back to their OWN
    __file__ location rather than cwd — hooks/post-tool.py:50,
    hooks/pre-compact.sh:14, scripts/system-self-check.py:22 (whose comment states it
    expects to run after `cd $CLAUDE_PROJECT_DIR`). Unset, all three resolve their
    state root to the PLUGIN INSTALL TREE, so compact state, transcript archives and
    self-checks target the wrong place. The probe must export the root it resolved.
    """
    probe = _probe()
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td).resolve()
        (repo / ".git").mkdir()
        r = subprocess.run(
            ["sh", "-c", f'{probe} && printf "%s" "$CLAUDE_PROJECT_DIR"'],
            cwd=repo, env={k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"},
            capture_output=True, text=True, timeout=30,
        )
    exported = r.stdout.strip()
    if not exported:
        fail("export-root", "probe resolved a root but left CLAUDE_PROJECT_DIR unset")
    elif exported != str(repo):
        fail("export-root", f"exported {exported!r}, expected {repo}")


def check_generated_hooks_all_use_the_probe() -> None:
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
        check_probe_never_lands_in_home_when_var_unset,
        check_probe_ignores_empty_var,
        check_probe_ignores_nonexistent_dir,
        check_probe_honours_valid_var,
        check_probe_finds_single_hook_root_marker,
        check_probe_is_ambiguous_safe_with_two_markers,
        check_refuses_to_run_in_a_non_repo_home,
        check_still_runs_when_home_is_itself_a_repo,
        check_still_runs_when_home_is_a_linked_worktree,
        check_exports_resolved_root_for_entrypoints,
        check_generated_hooks_all_use_the_probe,
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
    print("✔ plugin hook cwd probe: 11 cases pass (resolves, exports, never runs in a non-repo $HOME)")
    return 0


def test_all_checks_pass() -> None:
    """pytest entry point — the only test this module exposes to collection.

    CI runs `coverage run -m pytest scripts/tests/test_*.py`
    (.github/workflows/quality-base.yml:203). The checks above deliberately
    ACCUMULATE into `failures` so one CLI run reports every problem at once — but a
    function that only appends and returns None is invisible to pytest. Measured
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
