"""Patch-proposal contract for the subscription autofix guest pass.

The model's tool-free text must be one JSON object ``{"summary": str, "patch":
str}`` whose patch is a plain unified Git diff. Only modifications and new
regular files are admitted: no deletions, renames, copies, mode changes,
binary data, symlinks/submodules, path traversal or instruction surfaces. The
patch is applied with `git apply` in a scratch clone of the exact head and
committed without hooks under a fixed identity with an ``[autofix]`` subject.

This module grants no publication authority. The accepted publisher
(`autofix-publish.py`) independently re-checks ancestry, subjects, instruction
surfaces, native PR state, custody, head protection and its credential.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# Must equal autofix-publish.py's INSTRUCTIONS (enforced by tests).
INSTRUCTIONS = re.compile(r"(^|/)(\.claude(?:/|$)|CLAUDE\.md$|CLAUDE\.local\.md$|AGENTS\.md$)|^(\.mcp\.json|\.claude\.json|\.gitmodules|\.ripgreprc)$|^\.husky(?:/|$)|^\.github(?:$|/(workflows|actions)(?:/|$))")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_PATCH_BYTES = 200 * 1024
MAX_FILES = 20
MAX_SUMMARY = 100
FORBIDDEN_HEADERS = ("deleted file mode", "old mode", "new mode", "rename from", "rename to",
                     "copy from", "copy to", "similarity index", "dissimilarity index",
                     "GIT binary patch", "Binary files ")
SAFE_PATH = re.compile(r"[A-Za-z0-9_.@+\-][A-Za-z0-9_.@+\-/]{0,255}\Z")
IDENTITY = {"GIT_AUTHOR_NAME": "subscription-automation", "GIT_AUTHOR_EMAIL": "automation@invalid",
            "GIT_COMMITTER_NAME": "subscription-automation", "GIT_COMMITTER_EMAIL": "automation@invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}


class PatchRefused(ValueError):
    """The proposal cannot become a candidate commit; never repair or retry it."""


def parse_proposal(text: str) -> dict:
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise PatchRefused("proposal_duplicate_key")
            out[key] = value
        return out
    try:
        value = json.loads(text.strip(), object_pairs_hook=unique)
    except (ValueError, TypeError):
        raise PatchRefused("proposal_not_json") from None
    if not isinstance(value, dict) or set(value) != {"summary", "patch"}:
        raise PatchRefused("proposal_shape_invalid")
    summary, patch = value["summary"], value["patch"]
    if (not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY
            or any(ord(c) < 32 or ord(c) == 127 for c in summary)):
        raise PatchRefused("proposal_summary_invalid")
    if not isinstance(patch, str) or not patch.strip() or len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchRefused("proposal_patch_invalid")
    return {"summary": summary.strip(), "patch": patch if patch.endswith("\n") else patch + "\n"}


def validate_patch(patch: str) -> list[str]:
    """Return the touched paths, refusing everything outside the contract."""
    if "\x00" in patch or "\r" in patch:
        raise PatchRefused("patch_control_characters")
    paths = []
    current = None
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not match or match.group(1) != match.group(2):
                raise PatchRefused("patch_header_invalid")
            current = match.group(1)
            parts = current.split("/")
            if (not SAFE_PATH.fullmatch(current) or any(p in {"", ".", ".."} for p in parts)
                    or parts[0] == ".git" or INSTRUCTIONS.search(current)):
                raise PatchRefused("patch_path_refused")
            if current in paths:
                raise PatchRefused("patch_path_repeated")
            paths.append(current)
        elif line.startswith(FORBIDDEN_HEADERS):
            raise PatchRefused("patch_operation_refused")
        elif line.startswith("new file mode") and line != "new file mode 100644":
            raise PatchRefused("patch_mode_refused")
        elif line.startswith(("--- ", "+++ ")) and current is not None:
            side = line[4:]
            if side != "/dev/null" and side not in {f"a/{current}", f"b/{current}"}:
                raise PatchRefused("patch_header_invalid")
    if not paths or len(paths) > MAX_FILES:
        raise PatchRefused("patch_scope_invalid")
    return paths


def _env():
    return {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/state"), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", **IDENTITY}


def _git(cwd, *args, stdin=None, timeout=60):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=cwd, env=_env(), input=stdin, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise PatchRefused("git_" + args[0].replace("-", "_") + "_failed")
    return result.stdout


def build_candidate(source: Path, work: Path, base_sha: str, proposal: dict) -> dict:
    """Clone the exact head, apply the admitted patch, commit and bundle it."""
    if not SHA.fullmatch(base_sha):
        raise PatchRefused("base_invalid")
    paths = validate_patch(proposal["patch"])
    edit = work / "edit"
    _git(work, "clone", "--quiet", "--no-checkout", "--no-hardlinks", str(source), str(edit))
    _git(edit, "checkout", "--quiet", "--detach", base_sha)
    patch = proposal["patch"].encode("utf-8")
    _git(edit, "apply", "--check", "--whitespace=nowarn", "-", stdin=patch)
    _git(edit, "apply", "--index", "--whitespace=nowarn", "-", stdin=patch)
    changed = _git(edit, "diff", "--cached", "--name-only", "-z").decode().split("\0")
    if sorted(p for p in changed if p) != sorted(paths):
        raise PatchRefused("patch_effect_mismatch")
    _git(edit, "commit", "--quiet", "--no-verify", "-m", "[autofix] " + proposal["summary"])
    candidate = _git(edit, "rev-parse", "HEAD").decode().strip()
    _git(edit, "update-ref", "refs/heads/implementation", candidate)
    bundle = work / "implementation.bundle"
    _git(edit, "bundle", "create", str(bundle), "refs/heads/implementation", "^" + base_sha)
    return {"candidate_sha": candidate, "paths": paths, "bundle": bundle}
