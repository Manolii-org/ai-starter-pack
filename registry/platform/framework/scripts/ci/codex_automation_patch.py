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
# Stricter than the publisher, never weaker: case-insensitive (case-folding
# checkouts), every dot-path segment (.claude/.cursor/.codex/.github/.vscode,
# .gitattributes, .pre-commit-config.yaml, ...) and files that tools execute or
# load as agent instructions. A repair to these needs a person.
PROTECTED_NAMES = frozenset({"claude.md", "claude.local.md", "agents.md", "gemini.md", "conftest.py",
                             "copilot-instructions.md", "codeowners", "makefile"})
SHA = re.compile(r"[0-9a-f]{40}\Z")
SUMMARY = re.compile(r"[A-Za-z0-9 .,:;()'/_+=-]{1,100}\Z")
CLOSING = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b", re.IGNORECASE)
MAX_PATCH_BYTES = 200 * 1024
MAX_FILES = 20
MAX_SUMMARY = 100
FORBIDDEN_HEADERS = ("deleted file mode", "old mode", "new mode", "rename from", "rename to",
                     "rename old", "rename new",
                     "copy from", "copy to", "similarity index", "dissimilarity index",
                     "GIT binary patch", "Binary files ")
HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
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
            or not SUMMARY.fullmatch(summary) or CLOSING.search(summary)):
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
    old_left = new_left = 0
    for line in patch.split("\n"):
        # Hunk content ("--- old comment" removing an SQL comment, "+++" added
        # text) is never a header. Git ends a hunk by its line counts, so do we:
        # anything after the counted lines is checked as a header again.
        if old_left or new_left:
            kind = line[:1]
            if kind == "\\":
                continue
            if kind == " " and old_left and new_left:
                old_left, new_left = old_left - 1, new_left - 1
                continue
            if kind == "-" and old_left:
                old_left -= 1
                continue
            if kind == "+" and new_left:
                new_left -= 1
                continue
            raise PatchRefused("patch_hunk_invalid")
        if (line[:1] == " " or (line[:1] in "+-" and line[:1] and not line.startswith(("--- ", "+++ ")))):
            # Content outside a counted hunk is malformed (git would refuse or
            # read it as another patch); never pass it through unchecked.
            raise PatchRefused("patch_hunk_invalid")
        if line.startswith("@@"):
            hunk = HUNK.match(line)
            if current is None or not hunk:
                raise PatchRefused("patch_hunk_invalid")
            old_left = int(hunk.group(1) if hunk.group(1) is not None else 1)
            new_left = int(hunk.group(2) if hunk.group(2) is not None else 1)
            continue
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not match or match.group(1) != match.group(2):
                raise PatchRefused("patch_header_invalid")
            current = match.group(1)
            parts = current.split("/")
            if (not SAFE_PATH.fullmatch(current) or any(p in {"", ".", ".."} for p in parts)
                    or any(p.startswith(".") for p in parts) or parts[-1].lower() in PROTECTED_NAMES
                    or INSTRUCTIONS.search(current) or INSTRUCTIONS.search(current.lower())):
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
    # Rename detection off: a rename must show as a deletion and be refused.
    status = _git(edit, "diff", "--cached", "--no-renames", "--name-status", "-z").decode().split("\0")
    entries = [item for item in status if item]
    pairs = list(zip(entries[0::2], entries[1::2]))
    if (len(entries) % 2 or any(kind not in {"A", "M"} for kind, _ in pairs)
            or sorted(path for _, path in pairs) != sorted(paths)):
        raise PatchRefused("patch_effect_mismatch")
    # Only regular files may result: an existing symlink's target is not code.
    staged = _git(edit, "ls-files", "-s", "-z", "--", *paths).decode().split("\0")
    if any(entry and entry.split(" ", 1)[0] not in {"100644", "100755"} for entry in staged):
        raise PatchRefused("patch_mode_refused")
    _git(edit, "commit", "--quiet", "--no-verify", "-m", "[autofix] " + proposal["summary"])
    candidate = _git(edit, "rev-parse", "HEAD").decode().strip()
    _git(edit, "update-ref", "refs/heads/implementation", candidate)
    bundle = work / "implementation.bundle"
    _git(edit, "bundle", "create", str(bundle), "refs/heads/implementation", "^" + base_sha)
    return {"candidate_sha": candidate, "paths": paths, "bundle": bundle}
