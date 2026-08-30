#!/usr/bin/env python3
"""Path-level edit guards for the PreToolUse hook.

Reads .ai/guards.json and decides whether a proposed Edit/Write/NotebookEdit
or write-style Bash command targets a guarded path. Supports glob-based path
matching, region-level guards for JSON files, and session unfreezes.

See reports/gstack-adoption-plan/p5-path-guards.md for design rationale.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sys
from pathlib import Path
from typing import Any

GUARDS_FILE = ".ai/guards.json"

# Regex patterns for detecting write targets in Bash commands
_BASH_WRITE_PATTERNS = [
    re.compile(r'(?:^|[^>])>\s*(\S+)'),         # > path (negative lookbehind to avoid >>)
    re.compile(r'>>\s*(\S+)'),                   # >> path
    re.compile(r'\btee\s+(?:-a\s+)?(\S+)'),      # tee path | tee -a path
    # sed -i [flags] [pattern-token] path. The pattern token is optional after
    # quote-scrubbing (a quoted sed expression becomes whitespace), so the
    # (?:\S+\s+)? group is optional to capture the path either way.
    re.compile(r'\bsed\s+-i\S*\s+(?:\S+\s+)?(\S+)'),
    re.compile(r'\bdd\s+(?:if=\S+\s+)?of=(\S+)'),# dd of=path
    re.compile(r'\b(?:cp|mv|install)\s+(?:-\S+\s+)*\S+\s+(\S+)'),
    re.compile(r'\btruncate\s+(?:-\S+\s+)*(\S+)'),
]

# Applied to the raw command after heredoc bodies have been removed. The normal
# patterns intentionally scrub quoted strings to avoid matching prose, but a
# shell-quoted destination is still a real write target.
_BASH_QUOTED_WRITE_PATTERNS = [
    re.compile(r'(?:^|[^>])>\s*(["\'])(.+?)\1'),
    re.compile(r'>>\s*(["\'])(.+?)\1'),
    re.compile(r'\btee\s+(?:-a\s+)?(["\'])(.+?)\1'),
    re.compile(r'\b(?:cp|mv|install)\s+(?:-\S+\s+)*\S+\s+(["\'])(.+?)\1'),
    re.compile(r'\btruncate\s+(?:-\S+\s+)*(["\'])(.+?)\1'),
]


def _load_guards(repo_root: Path) -> dict[str, Any]:
    """Load guards.json, distinguishing absence from unreadable policy."""
    f = repo_root / GUARDS_FILE
    if not f.exists():
        return {"guards": [], "session_unfreezes": []}
    try:
        with f.open("r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, dict):
            raise ValueError("guard policy root must be a JSON object")
        return loaded
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(
            f"guard_check: {GUARDS_FILE} unreadable "
            f"({type(exc).__name__}: {exc}); failing closed",
            file=sys.stderr,
        )
        return {"guards": [], "session_unfreezes": [], "_load_failed": True}


def _path_matches(target_rel: str, patterns: list[str]) -> bool:
    """True if target_rel matches any glob pattern in patterns.

    Absolute targets (relativisation failed — caller's repo_root did not
    contain the path) are additionally tested with a "*/" prefix so in-repo
    files reached via a mismatched repo_root still match. Conservative-block
    beats silent-allow for guard-shaped paths (hook audit 2026-06-12 § 2.1).
    """
    for p in patterns:
        if fnmatch.fnmatch(target_rel, p):
            return True
        if target_rel.startswith("/") and fnmatch.fnmatch(target_rel, "*/" + p):
            return True
    return False


def _resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dotted json path. Returns None on missing key."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _region_changed(
    file_path: Path, json_path: str, edit_old: str | None, edit_new: str | None
) -> bool:
    """For JSON-region guards: True if the edit changes the value at json_path.

    Edit sends string fragments (old_string + new_string), so the full new
    file is reconstructed the same way the Edit tool will apply it before
    comparing parsed structures (hook audit 2026-06-12: fragment edits were
    unconditionally blocked, contradicting the guard's "other parts of the
    file are allowed" intent). Write sends full content (edit_old is None).
    Returns True (conservative block) on any reconstruction/parse failure.
    """
    if edit_new is None:
        return True  # no new content; assume changed

    try:
        old_text = file_path.read_text(encoding="utf-8") if file_path.exists() else "{}"
        old = json.loads(old_text)
        if edit_old is not None:
            if edit_old not in old_text:
                return True  # fragment not found; cannot reconstruct — block
            new = json.loads(old_text.replace(edit_old, edit_new, 1))
        else:
            new = json.loads(edit_new)
    except (json.JSONDecodeError, OSError):
        return True  # unparseable; conservative: block

    return _resolve_path(old, json_path) != _resolve_path(new, json_path)


def _decision_for_guard(guard: dict[str, Any], session_unfreezes: list[str]) -> dict[str, Any]:
    """Build decision dict for a matched guard."""
    if guard["id"] in session_unfreezes:
        return {
            "action": "allow_unfreeze",
            "guard_id": guard["id"],
            "operator_reason": "session_unfreeze_active",
        }
    return {
        "action": "block",
        "reason": guard.get("reason", "guarded path"),
        "guard_id": guard["id"],
    }


def check_edit(
    target_path: str,
    repo_root: Path,
    edit_old: str | None = None,
    edit_new: str | None = None,
) -> dict[str, Any]:
    """Check if an edit to target_path is allowed under current guards.

    Args:
        target_path: absolute or relative path to the target file
        repo_root: root of the repository
        edit_old: old content (only used for region matching context)
        edit_new: new content (used for region matching)

    Returns:
        {"action": "allow"} — not guarded
        {"action": "block", "reason": "...", "guard_id": "..."} — guarded
        {"action": "allow_unfreeze", "guard_id": "...", "operator_reason": "..."} — in session_unfreezes
    """
    cfg = _load_guards(repo_root)
    if cfg.get("_load_failed"):
        return {
            "action": "block",
            "reason": f"{GUARDS_FILE} could not be parsed; guards cannot be evaluated",
            "guard_id": "guards-config",
        }
    target_path_p = Path(target_path)

    # Normalise to an absolute, symlink-resolved path before relativising so
    # absolute tool paths (Edit/Write always send absolute paths) match the
    # relative globs in guards.json even when the caller's notion of repo_root
    # differs from the real toplevel (hook audit 2026-06-12 § 2.1).
    root_res = repo_root.resolve()
    target_abs = target_path_p if target_path_p.is_absolute() else (repo_root / target_path_p)
    try:
        target_abs = target_abs.resolve()
    except OSError:
        # resolve() can fail on dangling symlinks or permission edges; keep
        # the unresolved path — relative_to/suffix matching below still
        # guards it (conservative-block beats crashing the hook).
        pass
    try:
        target_rel = str(target_abs.relative_to(root_res))
    except ValueError:
        # Path is outside repo_root; keep the absolute form — _path_matches
        # falls back to "*/<pattern>" suffix matching for this case.
        target_rel = str(target_abs)

    session_unfreezes = cfg.get("session_unfreezes", [])
    if not isinstance(session_unfreezes, list) or not all(
        isinstance(guard_id, str) for guard_id in session_unfreezes
    ):
        return {
            "action": "block",
            "reason": f"{GUARDS_FILE} has an invalid session_unfreezes list",
            "guard_id": "guards-config",
        }

    # Edits to GUARDS_FILE itself that ONLY modify session_unfreezes are permitted.
    # Without this, the guards-config guard creates a circular chain: unfreezing any
    # other guard requires editing guards.json, which requires unfreezing
    # guards-config, which requires editing guards.json.
    #
    # Edit tool sends string fragments (old_string + new_string), not full JSON,
    # so we reconstruct the full new-file content before comparing parsed structures.
    # Write tool sends the full content as new_string (edit_old is None).
    if target_rel == GUARDS_FILE and edit_new is not None:
        try:
            guards_file = repo_root / GUARDS_FILE
            old_text = guards_file.read_text(encoding="utf-8") if guards_file.exists() else "{}"
            if edit_old is not None:
                if edit_old not in old_text:
                    raise ValueError("edit_old fragment not present in file")
                new_text = old_text.replace(edit_old, edit_new, 1)
            else:
                new_text = edit_new
            old = json.loads(old_text)
            new = json.loads(new_text)
            if not isinstance(old, dict) or not isinstance(new, dict):
                raise ValueError("guards.json root must be a JSON object")
            new_unfreezes = new.get("session_unfreezes", [])
            if not isinstance(new_unfreezes, list) or not all(
                isinstance(x, str) for x in new_unfreezes
            ):
                raise ValueError("session_unfreezes must be a list of strings")
            old_minus = {k: v for k, v in old.items() if k != "session_unfreezes"}
            new_minus = {k: v for k, v in new.items() if k != "session_unfreezes"}
            if old_minus == new_minus:
                return {
                    "action": "allow_unfreeze",
                    "guard_id": "guards-config",
                    "operator_reason": "session_unfreezes_only_edit",
                }
        except (json.JSONDecodeError, OSError, ValueError) as e:
            # Fast-path failed (malformed JSON, missing edit fragment, wrong shape,
            # invalid unfreeze type). Log to stderr so malformed bypass attempts are
            # auditable, then fall through to the normal guard check which will block.
            print(
                f"guard_check: session_unfreezes-only fast-path skipped: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    unfrozen_matches: list[str] = []
    for guard in cfg.get("guards", []):
        if (
            not isinstance(guard, dict)
            or not isinstance(guard.get("id"), str)
            or not isinstance(guard.get("paths"), list)
            or not all(isinstance(pattern, str) for pattern in guard.get("paths", []))
        ):
            return {
                "action": "block",
                "reason": f"{GUARDS_FILE} contains an invalid guard entry",
                "guard_id": "guards-config",
            }
        if not _path_matches(target_rel, guard.get("paths", [])):
            continue

        # Region-level check: if guard specifies JSON regions, check if value changed
        regions = guard.get("regions")
        if regions:
            file_p = repo_root / target_rel
            any_changed = any(
                _region_changed(file_p, r["json_path"], edit_old, edit_new)
                for r in regions
                if "json_path" in r
            )
            if not any_changed:
                continue  # Region untouched; this guard does not block

        decision = _decision_for_guard(guard, session_unfreezes)
        if decision["action"] == "block":
            return decision
        unfrozen_matches.append(decision["guard_id"])

    if unfrozen_matches:
        return {
            "action": "allow_unfreeze",
            "guard_id": unfrozen_matches[0],
            "guard_ids": unfrozen_matches,
            "operator_reason": "session_unfreeze_active",
        }

    return {"action": "allow"}


def _strip_quoted(command: str) -> str:
    """Replace single-quoted, double-quoted, and heredoc bodies with spaces.

    Preserves character offsets so the regex sees the same indices but no longer
    matches `> path` patterns inside quoted text (commit messages, doc strings,
    test fixtures). Conservative: heredoc body is stripped only when both opener
    and closer marker are matched.
    """
    out = list(command)
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c in ("'", '"'):
            quote = c
            j = i + 1
            while j < n and command[j] != quote:
                if quote == '"' and command[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            for k in range(i, min(j + 1, n)):
                if out[k] not in (" ", "\n", "\t"):
                    out[k] = " "
            i = j + 1
            continue
        if c == "<" and i + 1 < n and command[i + 1] == "<":
            m = re.match(r"<<-?\s*['\"]?(\w+)['\"]?", command[i:])
            if m:
                marker = m.group(1)
                end = command.find(f"\n{marker}", i + len(m.group(0)))
                if end > -1:
                    body_start = i + len(m.group(0))
                    for k in range(body_start, end):
                        if out[k] not in (" ", "\n", "\t"):
                            out[k] = " "
                    i = end
                    continue
        i += 1
    return "".join(out)


def _strip_heredoc_bodies(command: str) -> str:
    """Blank heredoc bodies while preserving command arguments and quotes."""
    out = list(command)
    for match in re.finditer(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n", command):
        marker = match.group(1)
        end = command.find(f"\n{marker}", match.end())
        if end < 0:
            continue
        for index in range(match.end(), end):
            if out[index] not in (" ", "\n", "\t"):
                out[index] = " "
    return "".join(out)


def check_bash(command: str, repo_root: Path) -> dict[str, Any]:
    """Heuristic: scan Bash command for write redirects and check each target.

    Quoted substrings and heredoc bodies are stripped before regex matching to
    avoid false positives on documentation/commit-message text that mentions
    `> path` patterns literally.

    Detects: > path, >> path, tee path, sed -i, dd of=path.
    Returns the same shape as check_edit (first violation encountered).
    """
    scrubbed = _strip_quoted(command)
    candidates: set[str] = set()

    for pat in _BASH_WRITE_PATTERNS:
        for m in pat.finditer(scrubbed):
            candidates.add(m.group(1).strip("'\""))

    without_heredocs = _strip_heredoc_bodies(command)
    for pattern in _BASH_QUOTED_WRITE_PATTERNS:
        for match in pattern.finditer(without_heredocs):
            candidates.add(match.group(2))

    unfrozen_guard_ids: list[str] = []
    for candidate in sorted(candidates):
        # Skip special files
        if candidate.startswith("/dev/") or candidate == "-":
            continue

        decision = check_edit(candidate, repo_root)
        if decision["action"] == "block":
            return decision
        if decision["action"] == "allow_unfreeze":
            unfrozen_guard_ids.extend(decision.get("guard_ids", [decision["guard_id"]]))

    if unfrozen_guard_ids:
        unique_ids = list(dict.fromkeys(unfrozen_guard_ids))
        return {
            "action": "allow_unfreeze",
            "guard_id": unique_ids[0],
            "guard_ids": unique_ids,
            "operator_reason": "session_unfreeze_active",
        }

    return {"action": "allow"}


def main() -> int:
    """CLI entry for hook invocation.

    Reads JSON from stdin with shape:
        {"tool": "Edit|Write|Bash|...", "args": {...}, "repo_root": "..."}

    Outputs decision JSON to stdout. Exit code 2 if action is "block".
    """
    payload = json.loads(sys.stdin.read())
    tool = payload.get("tool", "")
    args = payload.get("args", {})
    repo_root = Path(payload.get("repo_root", "."))

    if tool in ("Edit", "Write", "NotebookEdit"):
        target = args.get("file_path") or args.get("notebook_path", "")
        edit_new = args["new_string"] if "new_string" in args else args.get("content")
        decision = check_edit(
            target,
            repo_root,
            edit_old=args.get("old_string"),
            edit_new=edit_new,
        )
    elif tool == "Bash":
        decision = check_bash(args.get("command", ""), repo_root)
    else:
        decision = {"action": "allow"}

    print(json.dumps(decision))
    return 0 if decision["action"] != "block" else 2


if __name__ == "__main__":
    sys.exit(main())
