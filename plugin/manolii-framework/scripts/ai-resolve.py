#!/usr/bin/env python3
"""Resolve ai-manifest.yaml requirements into repo-local agent surfaces.

Reads the consumer repo's ai-manifest.yaml, enforces scope rules fail-closed
(a repo may only require platform/*, <its-own-universe>/*, and repo/personal
assets), then materialises plugin components into the paths agents already
read — materialise-in-place: distribution changes, consumption paths don't.

    skills/<n>/SKILL.md  ->  .claude/skills/<n>/SKILL.md
    agents/*.md          ->  .claude/agents/*.md
    commands/*.md        ->  .claude/commands/*.md

Every materialised file is recorded in .ai/capability-lock.json so
`--check` can detect drift and hand-edited files are never silently clobbered.
Hooks and MCP wiring are reported as advisory output only — they need a
settings.json merge step that is a later-phase concern.

Usage:
    ai-resolve.py [--manifest ai-manifest.yaml] [--registry <path>]
                  [--repo-root .] [--apply [--prune] | --check]

  Default mode is a dry run — prints the plan, writes nothing.
  --apply   materialise the resolved set
  --prune   with --apply, also remove lockfile-tracked files no longer required
  --check   exit 1 if materialised files differ from the registry source

Registry source: a local checkout containing a top-level registry/ dir (the
ai-starter-pack repo works). Remote fetch (github:org/repo@ref) lands in P1.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path



SCOPES = ("platform", "manolii", "buro", "impaktful", "cpdcheck", "repo", "personal")
LOCAL_SCOPES = {"repo", "personal"}
COMPONENT_TARGETS = {
    "skills": ".claude/skills",
    "agents": ".claude/agents",
    "commands": ".claude/commands",
}
# Resolver-owned subtrees — prune/removal may only ever touch paths rooted at
# one of these (e.g. .claude/settings.json is NOT resolver-owned and must
# never be unlinked by a lockfile entry).
OWNED_ROOTS = frozenset(COMPONENT_TARGETS.values())
LOCK_PATH = ".ai/capability-lock.json"
# Lock 'exec' records written by this resolver are tagged full masks
# (EXEC_TAG | mode&0o777). The tag keeps a genuine installed mode of 0 or
# 0o111 distinguishable from a legacy record, which only ever encoded
# any-exec on/off (see _exec_matches).
EXEC_TAG = 0o10000
REQUIRES_RE = re.compile(
    # \Z not $ — `$` also matches before one trailing '\n', which would
    # accept a keep-chomped `|+` plugin name carrying its terminator.
    r"^(platform|manolii|buro|impaktful|cpdcheck|repo|personal)/([a-z0-9][a-z0-9-]*)\Z"
)
SEMVER_REF = re.compile(r"v?\d+(?:\.\d+){0,2}")
# A materialised file that INVOKES a sibling script — the resolver does not
# materialise scripts/, so real invocations can never run in resolver mode.
# Only executable-invocation shapes match (interpreter call or ./exec); a
# bare `scripts/x.py` mention in prose or sample output is not a dependency.
SCRIPT_REF = re.compile(
    # (?<![\w-]) before the alternation — `source`/`exec` must be a command
    # word, not a suffix of `resource`/`outsource`/`oncexec`, and not the
    # tail of hyphenated prose like `open-source` or `re-exec`.
    # The shell set matches _STDIN_EXEC_HEADS — `dash scripts/x.sh` and
    # `ksh scripts/x.sh` execute exactly like `bash` (Devin on
    # vendored-resolver review). `cat` is here too: on its own it only
    # reads (the literal gate drops it), but `cat scripts/x.sh | sh`
    # executes the file's contents. grep/head/tail are the same shape —
    # they only read, so a bare `grep p scripts/x` stays inert, but
    # `eval "$(grep p scripts/x)"` and `grep p scripts/x | sh` run the
    # contents (Codex on #1370).
    # Separators are HORIZONTAL whitespace only — `\s` would let a command
    # word at end of one line join a `scripts/` path at the start of the
    # next (`source\nscripts/x.sh` is not an invocation).
    rb"(?<![\w-])(?:python(?:\d+(?:\.\d+)*)?|bash|dash|ksh|ash|sh|zsh|cat"
    rb"|node|npx|tsx|ts-node|deno"
    rb"|grep|egrep|fgrep|head|tail"
    rb"|ruby|perl|source|exec|bun|bunx|uv[ \t]+run|pipenv[ \t]+run"
    rb"|poetry[ \t]+run|pdm[ \t]+run|hatch[ \t]+run)"
    # Any extension counts — registry-lint permits regular script files
    # without an allowlist, so `bash scripts/setup.bash` is a real
    # invocation (`.bash` was whitelisting-out, Codex on #123).
    rb"[ \t]+[^\n|&;`]*?scripts/(?:[A-Za-z0-9_.-]+/)"
    rb"*(?:[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+|[A-Za-z0-9_-]+)"
    rb"(?![\w.])"
    # An interpreter held in a variable — `$PYTHON scripts/setup.py`,
    # `${NODE} scripts/x.js`, `"$PYTHON" scripts/x.py` (the double quotes
    # still expand) — runs the script just as a literal interpreter word
    # does, and the resolver cannot know the variable's value, so it
    # counts as a dependency (Codex on vendored-resolver review). Options
    # — each optionally binding one operand word, since the variable's
    # grammar is unknown (`$PYTHON -X dev scripts/x.py`) — may sit
    # between the head and the path; a loose `[^\n]*?` would turn prose
    # like `$VAR and scripts/x.sh` into an invocation.
    rb"|\"?\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)\"?"
    rb"[ \t]+(?:-[^\s|&;`]*[ \t]+"
    rb"(?:(?:\"[^\n\"]*\"|'[^\n']*'|[^\s|&;`'-][^\s|&;`]*)[ \t]+)?)*"
    rb"scripts/(?:[A-Za-z0-9_.-]+/)"
    rb"*(?:[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+|[A-Za-z0-9_-]+)"
    rb"(?![\w.])"
    rb"|\./scripts/(?:[A-Za-z0-9_.-]+/)"
    rb"*(?:[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+|[A-Za-z0-9_-]+)"
    rb"(?![\w.])"
    # POSIX `.` — the dot builtin sources a file just like `source`. The
    # lookbehind keeps `..`, `foo.` and `./` out; `. ` requires whitespace
    # after the dot, so `./scripts/x.sh` still binds only to the exec alt.
    rb"|(?<![\w./\\-])\.[ \t]+[^\n|&;`]*?scripts/(?:[A-Za-z0-9_.-]+/)"
    rb"*(?:[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+|[A-Za-z0-9_-]+)"
    rb"(?![\w.])"
    # `python -m scripts.check` — the module form invokes the same file
    # (scripts/check.py, or a package's __init__.py). Extraction maps the
    # dotted module name back to candidate scripts/ paths; a bare
    # `scripts/check.sh` with no invocation word stays a prose mention
    # (can't be told apart from "edit scripts/check.sh" without
    # over-blocking capabilities that merely document the path).
    # Boundary includes `-`: `python -m scripts-tools` is a DIFFERENT
    # module argument, not bare `-m scripts` (consumer review of the vendored resolver).
    rb"|(?<![\w-])python(?:\d+(?:\.\d+)*)?[ \t]+"
    # Interpreter options may precede `-m` — `python -u -m scripts.check`
    # and `python -X dev -m scripts.check` run the module just the same.
    # Each option token may bind the NEXT word as its operand (bare word
    # or quoted string); a token starting with `-` is never an operand,
    # so `-m` itself can't be swallowed.
    rb"(?:-{1,2}[^\s|&;`'\"()\\]+[ \t]+"
    rb"(?:(?:\"[^\n\"]*\"|'[^\n']*'"
    rb"|[^\s|&;`'\"()\\-][^\s|&;`'\"()\\]*)[ \t]+)?)*"
    rb"-m[ \t]+scripts"
    rb"(?:\.(?:[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*))?(?![\w.-])")
# Backticked `scripts/x.py` is NOT an invocation context — prose uses it for
# mentions. A real dependency that no interpreter/./ prefix expresses must be
# declared explicitly: `requires_scripts: [...]` in the file's frontmatter.
SCRIPT_DEP_KEYS = ("requires_scripts",)
# Frontmatter declaring the file's `scripts/x.py` references are CONSUMER-side
# (the consumer repo already owns them, or the file's own setup section has the
# consumer fetch them) — the only exemption that lets an unbundled script
# reference materialise instead of being skipped as an unsatisfiable dep.
CONSUMER_SCRIPT_KEYS = ("consumer_scripts",)
# Path extraction for SCRIPT_REF matches (relative to scripts/) — used to
# distinguish bundled plugin scripts (a real dependency) from
# consumer-repository commands.
SCRIPT_NAME = re.compile(
    rb"scripts/((?:[A-Za-z0-9_.-]+/)"
    rb"*(?:[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+|[A-Za-z0-9_-]+))"
    rb"(?![\w.])")
# `-m scripts.a.b` extraction — the dotted module resolves to
# scripts/a/b.py or the runnable scripts/a/b/__main__.py (`python -m`
# never executes a bare __init__.py — that file alone is no entry point).
# A bare `python -m scripts` (group 1 None) runs scripts/__main__.py.
MODULE_NAME = re.compile(
    rb"-m[ \t]+scripts(?:\.([A-Za-z0-9_.]+))?(?![\w.-])")
def _cmd_window(src: bytes, start: int) -> bytes:
    """The command region starting at `start` — up to the first UNQUOTED
    shell metacharacter (newline, |, &, ;) or comment. A '#' ends the
    region only at a word start (unquoted and preceded by whitespace) —
    `a#b` and `\"a#b\"` are literal text. Metacharacters inside quotes
    are literal too (a literal newline is legal inside either quote form
    and does NOT end the command); a backtick stays a command-context
    boundary even inside double quotes. POSIX continuations are joined
    by _join_continuations up front — inside single quotes a backslash
    is literal and never joins."""
    in_s = in_d = esc = False
    out = bytearray()
    i = start
    while i < len(src):
        c = src[i]
        if esc:
            esc = False
            if c == 0x0A:
                i += 1
                continue
            out += b"\\" + bytes([c])
        elif in_s:
            if c == 0x27:
                in_s = False
            out.append(c)
        elif in_d:
            if c == 0x5C:
                esc = True
            elif c == 0x60:
                break
            else:
                if c == 0x22:
                    in_d = False
                out.append(c)
        elif c == 0x22:
            in_d = True
            out.append(c)
        elif c == 0x27:
            in_s = True
            out.append(c)
        elif c == 0x5C:
            esc = True
        elif c == 0x26:  # &
            nxt = src[i + 1] if i + 1 < len(src) else 0
            prev = out[-1] if out else 0
            # `&&` and a bare `&` end the command. `&>`, `>&`, and `<&`
            # are redirections — keep them so the target can be ignored
            # without dropping a later real argument.
            if nxt != 0x26 and (nxt == 0x3E or prev in (0x3E, 0x3C)):
                out.append(c)
                i += 1
                continue
            break
        elif c == 0x7C:  # |
            if out and out[-1] == 0x3E:
                out.append(c)  # `>|` clobber, not a pipe
                i += 1
                continue
            break
        elif c in b"\n;`":
            break
        elif c == 0x23 and (not out or out[-1] in b" \t"):
            break
        else:
            out.append(c)
        i += 1
    return bytes(out)


def _join_continuations(src: bytes) -> bytes:
    """POSIX line-continuation join for the whole source: drop
    `\\<newline>` where the shell keeps the command's tokens contiguous —
    after an ODD-length backslash run outside quotes or inside double
    quotes. An even run's final backslash is itself escaped, so the
    newline there still terminates the command — the unconditional
    byte-replace this supersedes fused such separate commands into one
    window. Inside single quotes every backslash is literal, so a
    backslash-newline is NOT a join (`printf 'bash \\'` + newline +
    `scripts/missing.sh'` must not become an invocation). A newline
    inside quotes does not end `_cmd_window`, so an argument after the
    closing quote stays visible without joining the quoted text."""
    out = bytearray()
    in_s = in_d = False
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if in_s:
            # No escapes and no line continuation. Joining here turned
            # `printf 'bash \` + newline + `scripts/missing.sh'` into an
            # invocation the shell never runs.
            out.append(c)
            if c == 0x27:
                in_s = False
            i += 1
            continue
        if c == 0x5C:
            j = i
            while j < n and src[j] == 0x5C:
                j += 1
            run = j - i
            nl = src[j:j + 2]
            if run % 2 == 1 and (nl[:1] == b"\n" or nl == b"\r\n"):
                out += b"\\" * (run - 1)
                i = j + (2 if nl == b"\r\n" else 1)
                continue
            out += src[i:j]
            i = j
            continue
        if in_d:
            if c == 0x22:
                in_d = False
        elif c == 0x22:
            in_d = True
        elif c == 0x27:
            in_s = True
        out.append(c)
        i += 1
    return bytes(out)


# Longest match first so `>>`/`&>`/`<<-` etc. win over the single chars.
_REDIR_OPS = (
    b"&>>", b"<<<", b"<<-", b">>", b"<<", b"<>", b">&", b"<&",
    b">|", b"&>", b">", b"<",
)
# Ops whose target is written, an fd word, or a heredoc/here-string
# delimiter — never a file the command READS. `<` and `<>` open the
# target for input, so those targets stay dependencies.
_REDIR_NO_DEP = frozenset(
    (b"&>>", b"<<<", b"<<-", b">>", b"<<", b">&", b"<&", b">|", b"&>",
     b">"))


def _redirection_target_spans(window: bytes) -> list[tuple[int, int]]:
    """Byte spans of words that are shell redirection targets.

    `python scripts/foo.py > scripts/generated.py` invokes foo.py; the
    generated path is not a dependency. An fd prefix (`2>`) is part of
    the operator. A quoted operator (`">"`) is an argument, not a
    redirect."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(window)
    in_s = in_d = False
    word_start: int | None = None
    expect_target = False

    def end_word(end: int) -> None:
        nonlocal word_start, expect_target
        if word_start is not None and expect_target and word_start < end:
            spans.append((word_start, end))
        if word_start is not None and expect_target:
            expect_target = False
        word_start = None

    while i < n:
        c = window[i]
        if in_s:
            if word_start is None:
                word_start = i
            if c == 0x27:
                in_s = False
            i += 1
            continue
        if in_d:
            if word_start is None:
                word_start = i
            if c == 0x5C and i + 1 < n:
                i += 2
                continue
            if c == 0x22:
                in_d = False
            i += 1
            continue
        if c == 0x5C and i + 1 < n:
            if word_start is None:
                word_start = i
            i += 2
            continue
        if c in b" \t":
            end_word(i)
            i += 1
            continue
        if c == 0x27:
            if word_start is None:
                word_start = i
            in_s = True
            i += 1
            continue
        if c == 0x22:
            if word_start is None:
                word_start = i
            in_d = True
            i += 1
            continue
        matched = next(
            (op for op in _REDIR_OPS if window.startswith(op, i)), None)
        if matched is not None:
            if (word_start is not None and word_start < i
                    and window[word_start:i].isdigit()):
                word_start = None  # `2>` — fd prefix, not a filename
            else:
                end_word(i)
            i += len(matched)
            expect_target = matched in _REDIR_NO_DEP
            continue
        if word_start is None:
            word_start = i
        i += 1
    end_word(n)
    return spans


# Single-letter interpreter flags that never take an operand. The next
# word stays positional (`python -u scripts/foo.py`, `bash -e scripts/x.sh`).
# `-d` is not here: it is the short form that takes a directory argument
# (`python -m http.server -d scripts/site`).
_BOOL_SHORT: dict[bytes, frozenset[int]] = {
    b"python": frozenset(b"bBEhiIOPqsSuvVx"),
    b"bash": frozenset(b"eEfhimpnuxv"),
    b"sh": frozenset(b"eEfhimpnuxv"),
    b"zsh": frozenset(b"eEfhimpnuxv"),
}
# Long options that do not take an operand. Anything else in `--opt` or
# `--opt=value` form binds the following word / the text after `=` — that
# path is not an invoked script (`--directory scripts/site`).
_BOOL_LONG = frozenset({
    b"--debug", b"--help", b"--interactive", b"--login", b"--noediting",
    b"--noprofile", b"--norc", b"--posix", b"--quiet", b"--silent",
    b"--verbose", b"--version", b"--yes",
})
_BOOL_LONG_PREFIXES = (b"--allow-", b"--experimental-", b"--no-")


def _shell_words(window: bytes) -> list[tuple[int, int]]:
    """Byte spans of shell words in `window`.

    Quotes and backslash escapes stay inside the word. A redirection
    operator with no surrounding space is part of the adjacent word;
    option classification only cares about words that start with `-`."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(window)
    in_s = in_d = False
    word_start: int | None = None

    def end_word(end: int) -> None:
        nonlocal word_start
        if word_start is not None and word_start < end:
            spans.append((word_start, end))
        word_start = None

    while i < n:
        c = window[i]
        if in_s:
            if word_start is None:
                word_start = i
            if c == 0x27:
                in_s = False
            i += 1
            continue
        if in_d:
            if word_start is None:
                word_start = i
            if c == 0x5C and i + 1 < n:
                i += 2
                continue
            if c == 0x22:
                in_d = False
            i += 1
            continue
        if c == 0x5C and i + 1 < n:
            if word_start is None:
                word_start = i
            i += 2
            continue
        if c in b" \t":
            end_word(i)
            i += 1
            continue
        if c == 0x27:
            if word_start is None:
                word_start = i
            in_s = True
            i += 1
            continue
        if c == 0x22:
            if word_start is None:
                word_start = i
            in_d = True
            i += 1
            continue
        if word_start is None:
            word_start = i
        i += 1
    end_word(n)
    return spans


def _quoted_spans(raw: bytes) -> list[tuple[int, int]]:
    """Spans inside quotes of one shell word. `a'bash x'c` — the quoted
    middle is operand text the command parses; an unterminated quote
    spans to the end (the shell reads the rest as quoted — and a
    fail-closed read of it as executable is the safe direction)."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(raw)
    q: int | None = None
    start = 0
    while i < n:
        c = raw[i]
        if q is None:
            if c == 0x5C and i + 1 < n:
                i += 2
                continue
            if c in (0x27, 0x22):
                q = c
                start = i + 1
        elif q == 0x27:
            if c == 0x27:
                spans.append((start, i))
                q = None
        else:
            if c == 0x5C and i + 1 < n:
                i += 2
                continue
            if c == 0x22:
                spans.append((start, i))
                q = None
        i += 1
    if q is not None:
        spans.append((start, n))
    return spans


def _mask_parens(src: bytes) -> bytes:
    """Length-preserving copy with UNQUOTED `(`/`)` masked to spaces —
    both are shell metacharacters (substitution bodies, grouping), never
    word content. Lets _shell_words split `$(bash x.sh)` body args."""
    out = bytearray(src)
    in_s = in_d = False
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if in_s:
            if c == 0x27:
                in_s = False
        elif in_d:
            if c == 0x5C:
                i += 1
            elif c == 0x22:
                in_d = False
        elif c == 0x5C:
            i += 1
        elif c == 0x22:
            in_d = True
        elif c == 0x27:
            in_s = True
        elif c in (0x28, 0x29):
            out[i] = 0x20
        i += 1
    return bytes(out)


def _word_text(raw: bytes) -> bytes:
    """Shell-unquoted text of one word, for option classification only."""
    out = bytearray()
    i = 0
    n = len(raw)
    in_s = in_d = False
    while i < n:
        c = raw[i]
        if in_s:
            if c == 0x27:
                in_s = False
            else:
                out.append(c)
            i += 1
            continue
        if in_d:
            if c == 0x5C and i + 1 < n:
                out.append(raw[i + 1])
                i += 2
                continue
            if c == 0x22:
                in_d = False
            else:
                out.append(c)
            i += 1
            continue
        if c == 0x27:
            in_s = True
        elif c == 0x22:
            in_d = True
        elif c == 0x5C and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
            continue
        else:
            out.append(c)
        i += 1
    return bytes(out)


def _command_key(word: bytes) -> bytes:
    base = _word_text(word).rsplit(b"/", 1)[-1]
    if base.startswith(b"python"):
        return b"python"
    return base


def _long_option_takes_value(text: bytes) -> bool:
    """True when `--opt` (no `=`) binds the following word."""
    if text in _BOOL_LONG:
        return False
    return not text.startswith(_BOOL_LONG_PREFIXES)


_SCRIPT_SUFFIXES = (
    b".py", b".sh", b".ts", b".js", b".mjs", b".cjs", b".rb", b".pl",
)

# Command heads whose arguments can never be executed — an interpreter +
# scripts/ path inside `echo "bash scripts/x.sh"` (or unquoted) only
# prints; it is documentation, not an invocation (Devin on
# vendored-resolver review). Kept to words that print or inspect: `eval`,
# `xargs`, `env`, `sudo`, `command`, `sh -c` DO run their arguments and
# are deliberately absent — and so are `sed` and `awk`: GNU sed's `e`
# command (`sed '1e bash scripts/x.sh'`) and awk's `system()`/`cmd |
# getline` execute text inside their program arguments, so their
# arguments are NOT provably inert (Codex + Devin on vendored-resolver
# review).
_NONEXEC_HEADS = frozenset({
    b"echo", b"printf", b"cat", b"head", b"tail", b"grep", b"egrep",
    b"fgrep", b"less", b"more", b"man", b"wc", b"diff", b"file",
    b"stat", b"ls", b"which", b"type", b"head", b"help",
})


def _command_start(src: bytes, pos: int) -> int:
    """Start of the command containing `pos` — the byte after the last
    UNQUOTED separator before it. Separators inside quotes are literal —
    `echo "note; bash x"` is still the echo command, so a backward scan
    (which cannot tell an opening from a closing quote) must not split
    there. Quote state is therefore computed by a forward scan. A
    backslash outside single quotes escapes the next byte; a backtick
    counts as a separator even inside double quotes (its substitution
    executes) but `$(` does NOT split — the enclosing command's head
    still governs, and `$(...)` regions are handled separately by
    _substitution_spans."""
    start = 0
    in_s = in_d = False
    i = 0
    while i < pos:
        c = src[i]
        if in_s:
            if c == 0x27:
                in_s = False
        elif in_d:
            if c == 0x5C:
                i += 1
            elif c == 0x60:
                start = i + 1
            elif c == 0x22:
                in_d = False
        elif c == 0x5C:
            i += 1
        elif c == 0x22:
            in_d = True
        elif c == 0x27:
            in_s = True
        elif c in b"\n|&;`":
            start = i + 1
        i += 1
    while start < pos and src[start] in b" \t":
        start += 1
    return start


def _substitution_spans(window: bytes) -> list[tuple[int, int]]:
    """Byte spans of `$(...)` and `<(`/`>(` substitutions in `window`.

    The shell runs a substitution's contents BEFORE the outer command —
    `echo "$(bash scripts/x.sh)"` and `cat <(bash scripts/x.sh)` both
    execute the script even though the outer head only prints or reads,
    so spans inside one are not literal text. An opener inside single
    quotes (or escaped) is literal and opens no span; double quotes do
    not disable it. Nested substitutions and balanced inner parens are
    tracked by depth; an unclosed opener runs to the end of the window
    (fail-closed)."""
    spans: list[tuple[int, int]] = []
    # [start, inner-paren depth, in_d before open] — inside `$(...)` the
    # shell parses quotes fresh, so the enclosing double-quote state is
    # saved at each open and restored at the matching close.
    stack: list[list] = []
    in_s = in_d = False
    i = 0
    n = len(window)
    while i < n:
        c = window[i]
        if in_s:
            if c == 0x27:
                in_s = False
            i += 1
            continue
        if c == 0x5C and i + 1 < n:
            i += 2
            continue
        if in_d:
            if c == 0x22:
                in_d = False
            elif c == 0x24 and window[i + 1:i + 2] == b"(":
                # `$(` expands inside double quotes; `<(`/`>(` do NOT —
                # `cat "<(bash x)"` passes literal text (Devin Review).
                stack.append([i, 0, True])
                in_d = False
                i += 2
                continue
            i += 1
            continue
        if c == 0x22:
            in_d = True
            i += 1
            continue
        if c == 0x27:
            in_s = True
            i += 1
            continue
        if (c == 0x24 and window[i + 1:i + 2] == b"(") or (
                # `<(cmd)`/`>(cmd)` process substitution — bash/zsh run the
                # body BEFORE the outer command sees its /dev/fd path, so
                # `cat <(bash scripts/x.sh)` executes the script the same
                # way `$(bash x)` does (Codex on vendored-resolver review).
                # The 2-char openers share the `$(`'s paren balancing.
                c in (0x3C, 0x3E) and window[i + 1:i + 2] == b"("
                # `<<`/`<>`/`>>` are redirections, not substitution — the
                # char before must not be another redirect char.
                and window[i - 1:i] not in (b"<", b">", b"&", b"|")):
            stack.append([i, 0, False])
            i += 2
            continue
        if stack:
            if c == 0x28:
                stack[-1][1] += 1
            elif c == 0x29:
                if stack[-1][1] == 0:
                    _s, _depth, outer_d = stack.pop()
                    spans.append((_s, i + 1))
                    in_d = outer_d
                else:
                    stack[-1][1] -= 1
        i += 1
    for s, _depth, _d in stack:
        spans.append((s, n))
    return spans


# Command heads that execute what they read on stdin — a non-executing
# head piped into one of these is NOT literal text (`printf 'bash x' | sh`
# runs it). xargs without a command runs echo — deliberately absent.
_STDIN_EXEC_HEADS = frozenset({
    b"sh", b"bash", b"dash", b"zsh", b"ksh", b"ash",
    b"python", b"perl", b"ruby", b"node", b"php", b"lua", b"tclsh",
    # `bc`/`dc` interpret stdin as PROGRAM text — `cat scripts/calc.bc | bc`
    # runs the file, and `bc`'s own output can carry code onward
    # (`print \"echo ran\"` | sh). Interpreters, not sinks (Codex on #125
    # and #1374).
    b"bc", b"dc",
})
# Heads that only prepare an environment and then exec the command
# behind them — `| env bash` and `| command bash` both launch bash on
# the pipe, so the real consumer is the word after the wrapper (Codex +
# Devin on vendored-resolver review). `sudo` and `nohup` are the same
# shape; `exec` replaces the shell with what follows.
_EXEC_WRAPPERS = frozenset({
    b"env", b"command", b"sudo", b"nohup", b"stdbuf", b"exec", b"time",
})
# Wrapper options that bind the FOLLOWING word — `sudo -u root bash`
# skips `root` before identifying `bash`; `env -C /tmp bash` and
# `stdbuf -o L bash` are the same shape (Devin Review on #1370).
_WRAPPER_OPT_OPERAND = {
    b"env": frozenset({b"-C", b"-S", b"-u",
                       b"--chdir", b"--unset", b"--split-string",
                       b"--argv0"}),
    b"sudo": frozenset({b"-u", b"-g", b"-h", b"-r", b"-t", b"-D", b"-R",
                        b"-p", b"-U", b"-T",
                        b"--user", b"--group", b"--host", b"--role",
                        b"--type", b"--chdir", b"--chroot", b"--prompt",
                        b"--other-user", b"--command-timeout"}),
    b"stdbuf": frozenset({b"-i", b"-o", b"-e",
                          b"--input", b"--output", b"--error"}),
    b"exec": frozenset({b"-a"}),
    b"time": frozenset({b"-o", b"-f", b"--output", b"--format"}),
    b"nohup": frozenset(),
    b"command": frozenset(),
}
# `command -v`/`-V` only describe a command — they never run it (Devin
# Review on #1370).
_WRAPPER_DESCRIBE = frozenset({b"-v", b"-V"})
# Heads whose output provably does NOT carry the input stream — a pipe
# into one ends the chain without executing anything downstream: `cat x
# | wc -l | sh` feeds sh a line count, not the script (Devin on #123).
# Kept to commands that REPLACE the stream: digest/count/printer tools.
# Filters and transformers (grep/sed/awk/tr/sort/uniq/tee/…) still emit
# script content — they are NOT here, so the walk continues past them.
_STDIN_SINK_HEADS = frozenset({
    b"wc", b"md5sum", b"sha1sum", b"sha224sum", b"sha256sum",
    b"sha384sum", b"sha512sum", b"b2sum", b"cksum", b"sum",
    b"echo", b"printf", b"yes", b"true", b"false", b"sleep",
    b"date", b"seq", b"pwd", b"uname", b"hostname", b"cal", b"factor",
    b"expr", b"env", b"printenv",
})
_ASSIGN_WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*=")
# Interpreter options whose OPERAND is the program — `sh -c 'x'` and
# `python -c 'x'` never read stdin, so piping a script into them is inert
# (Devin Review on #1953). `-e` means errexit for shells but the eval
# operand for perl/ruby/node/lua, so the table is per-head. `python -m`
# takes its program from the named module (`python -m json.tool` parses
# the pipe as DATA), so it ends the chain the same way.
_EXEC_OPERAND_FLAGS = {
    b"sh": frozenset({b"-c"}), b"bash": frozenset({b"-c"}),
    b"dash": frozenset({b"-c"}), b"zsh": frozenset({b"-c"}),
    b"ksh": frozenset({b"-c"}), b"ash": frozenset({b"-c"}),
    b"python": frozenset({b"-c", b"-m"}),
    b"perl": frozenset({b"-e", b"-E"}),
    b"ruby": frozenset({b"-e"}),
    b"node": frozenset({b"-e", b"--eval"}),
    b"php": frozenset({b"-r", b"-f"}),
    b"lua": frozenset({b"-e"}),
    b"tclsh": frozenset(),
}


def _effective_head(words: list, win: bytes) -> int | None:
    """Index of the effective command word — past `VAR=value` assignments
    and wrapper heads (env/sudo/command/…) with their operands. -1 for a
    describe-only `command -v`/`-V`; None when nothing is a head."""
    i = 0
    while i < len(words):
        text = _word_text(win[words[i][0]:words[i][1]])
        if _ASSIGN_WORD.match(text):
            i += 1
            continue
        key = _command_key(win[words[i][0]:words[i][1]])
        if key in _EXEC_WRAPPERS:
            i += 1
            takes_operand = _WRAPPER_OPT_OPERAND.get(key, frozenset())
            while i < len(words):
                t = _word_text(win[words[i][0]:words[i][1]])
                if _ASSIGN_WORD.match(t):
                    i += 1
                    continue
                if key == b"command" and t in _WRAPPER_DESCRIBE:
                    return -1
                if t.startswith(b"-"):
                    if (b"=" not in t and t in takes_operand
                            and i + 1 < len(words)):
                        i += 2
                    else:
                        i += 1
                    continue
                break
            continue
        return i
    return None


def _stdin_exec_head(win: bytes) -> str:
    """Classify `win`'s effective command as a pipe consumer.

    "exec"  — the head executes what it reads on stdin (`sh`, `python`).
    "sink"  — the head ends the stream without executing it: a
              describe-mode wrapper (`command -v`), an interpreter
              whose program comes from argv (`sh -c 'true'`,
              `python f.py` — the pipe's contents are ignored), or a
              head that replaces the stream (`wc -l`, `sha256sum` —
              downstream sees a count/digest, not the script).
    "other" — anything else; a `|` walk continues past it (a filter or
              an unknown head may still forward the script downstream)."""
    words = _shell_words(_mask_parens(win))
    # `env -S`/`--split-string` re-parses its operand into the command
    # line — `cat x | env -S 'bash -s'` execs bash on the pipe, quoted
    # operand or not (Codex on #1370). Classify the operand text as the
    # command; an operand that doesn't classify falls through to the
    # normal wrapper walk.
    for w in words:
        t = _word_text(win[w[0]:w[1]])
        if _ASSIGN_WORD.match(t):
            continue
        if _command_key(win[w[0]:w[1]]) == b"env":
            j = words.index(w) + 1
            found_cmd = False
            while j < len(words):
                tj = _word_text(win[words[j][0]:words[j][1]])
                if _ASSIGN_WORD.match(tj):
                    j += 1
                    continue
                operand = None
                consume = 0
                if tj in (b"-S", b"--split-string"):
                    if j + 1 < len(words):
                        operand = _word_text(
                            win[words[j + 1][0]:words[j + 1][1]])
                        consume = 2
                elif tj.startswith(b"--split-string="):
                    operand = tj[len(b"--split-string="):]
                    consume = 1
                elif tj.startswith(b"-S") and len(tj) > 2:
                    operand = tj[2:]
                    consume = 1
                if operand is not None:
                    # `env -S 'sh' -c true` — argv after the operand is
                    # APPENDED to the split command, so a later `-c` or
                    # filename decides whether stdin is read (Devin
                    # BUG_0001 on #125 / BUG_0002 on #1374).
                    rest = b" ".join(
                        _word_text(win[words[k][0]:words[k][1]])
                        for k in range(j + consume, len(words)))
                    v = _stdin_exec_head(
                        operand + (b" " + rest if rest else b""))
                    if v != "other":
                        return v
                    break
                if tj.startswith(b"-"):
                    j += 2 if (b"=" not in tj
                               and tj in _WRAPPER_OPT_OPERAND[b"env"]
                               and j + 1 < len(words)) else 1
                    continue
                found_cmd = True
                break
            if not found_cmd:
                # `env` with only options/assignments prints the environ
                # and never forwards stdin — `cat x | env | sh` pipes
                # env vars, not the script (Devin BUG_0005 on #125).
                return "sink"
            break
        break
    hi = _effective_head(words, win)
    if hi == -1:
        return "sink"
    if hi is None:
        return "other"
    key = _command_key(win[words[hi][0]:words[hi][1]])
    if key in _STDIN_SINK_HEADS:
        return "sink"
    if key not in _STDIN_EXEC_HEADS:
        return "other"
    # The interpreter's own program operand ends the chain: `-c 'x'` runs
    # the operand, a positional runs that FILE — neither reads stdin.
    # `-`/`-s` explicitly mean "read stdin". Option operands are skipped
    # via _option_value_spans (`python -X dev` still reads stdin).
    sub = win[words[hi][0]:]
    operands = set(_option_value_spans(sub))
    # Unmasked words — _option_value_spans computed spans without paren
    # masking, so `$(...)` words keep their coordinates.
    sub_words = _shell_words(sub)
    flags = _EXEC_OPERAND_FLAGS.get(key, frozenset())
    for w in sub_words[1:]:
        if w in operands:
            continue
        t = _word_text(sub[w[0]:w[1]])
        if t in (b"-", b"-s"):
            break
        if t in flags or not t.startswith(b"-"):
            return "sink"
    return "exec"


def _pipe_to_exec(src: bytes, pos: int) -> bool:
    """True when an unquoted `|` (or `|&`) at `pos` starts a pipeline
    whose contents reach a command that executes stdin.

    The walk continues past forwarding/filter heads — `cat x | tee
    /dev/stderr | sh` runs the script through two hops (Devin Review on
    #123). A "sink" segment (a describe-mode wrapper or an interpreter
    taking its program from argv, like `sh -c 'true'`) breaks the chain:
    the pipe's contents are never executed downstream of it."""
    j = pos
    while j < len(src):
        if src[j:j + 1] != b"|" or src[j + 1:j + 2] == b"|":
            return False
        j += 1
        if src[j:j + 1] == b"&":
            j += 1
        while j < len(src) and src[j] in b" \t":
            j += 1
        win = _cmd_window(src, j)
        verdict = _stdin_exec_head(win)
        if verdict == "exec":
            return True
        if verdict == "sink":
            return False
        j += len(win)
    return False


_HEREDOC_DELIM = re.compile(rb"['\"]?([A-Za-z0-9_.-]+)['\"]?")


def _heredoc_ops(line: bytes) -> list[tuple[bytes, bool, bool, int]]:
    """`<<` openers in one line — (delim, strip_tabs, quoted_delim, pos).
    Quote-aware: `<<` inside quotes or comments is text, not an operator.
    `<<<` (here-string) carries its input on the same line — skipped."""
    ops: list[tuple[bytes, bool, bool, int]] = []
    in_s = in_d = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_s:
            if c == 0x27:
                in_s = False
        elif in_d:
            if c == 0x5C:
                i += 1
            elif c == 0x22:
                in_d = False
        elif c == 0x5C:
            i += 1
        elif c == 0x27:
            in_s = True
        elif c == 0x22:
            in_d = True
        elif c == 0x23 and (i == 0 or line[i - 1] in b" \t"):
            break
        elif c == 0x3C and line[i + 1:i + 2] == b"<" \
                and line[i + 2:i + 3] != b"<":
            j = i + 2
            strip = False
            if line[j:j + 1] in (b"-", b"~"):
                strip = True
                j += 1
            while line[j:j + 1] in (b" ", b"\t"):
                j += 1
            m = _HEREDOC_DELIM.match(line, j)
            if m:
                ops.append((m.group(1), strip,
                            line[j:j + 1] in (b"'", b'"'), i))
                i = m.end()
                continue
        i += 1
    return ops


_HD_LITERAL = 0  # body is inert data end to end
_HD_EXEC = 1     # head (or a pipe consumer) executes the body
_HD_EXPAND = 2   # inert head, but unquoted body — $( ) / ` ` still expand


def _heredoc_spans(src: bytes) -> list[tuple[int, int, int]]:
    """(start, end, mode) spans of heredoc bodies in `src`.

    A `<<[-~]DELIM` opener queues body lines — stdin to its command — until
    a line equal to DELIM (`<<-`/`<<~` allow leading tabs). The body is
    inert data (_HD_LITERAL) only when the owning command's head cannot
    execute (`cat`, `echo`, …), its output is not piped into an
    interpreter, AND the body cannot expand a substitution — a QUOTED
    delimiter disables expansion entirely; an unquoted body with no
    `$(` or backtick is inert too, otherwise it is _HD_EXPAND (only the
    substitution regions inside it execute). `sh <<E` and `cat <<E | sh`
    both run the body (_HD_EXEC). An unterminated heredoc opens no span —
    its tail keeps normal (fail-closed) treatment."""
    spans: list[tuple[int, int, int]] = []
    lines = src.split(b"\n")
    offs: list[int] = []
    o = 0
    for ln in lines:
        offs.append(o)
        o += len(ln) + 1
    pending: list[list] = []  # [delim, strip_tabs, quoted, lit, start_off]
    i = 0
    while i < len(lines):
        ln = lines[i]
        if pending:
            delim, strip, quoted, lit, start_off = pending[0]
            chk = ln.lstrip(b"\t") if strip else ln
            if chk == delim:
                body = src[start_off:offs[i]]
                mode = (_HD_EXEC if not lit
                        else _HD_LITERAL if quoted or (
                            b"$(" not in body and b"`" not in body)
                        else _HD_EXPAND)
                spans.append((start_off, offs[i], mode))
                pending.pop(0)
                if pending:
                    # The next queued heredoc's body starts on the line
                    # AFTER this delimiter — `cat <<A; sh <<B` would
                    # otherwise attribute A's body to B's command too
                    # (Devin on #1370).
                    pending[0][4] = offs[i] + len(ln) + 1
            i += 1
            continue
        for delim, strip, quoted, op_pos in _heredoc_ops(ln):
            cs = _command_start(src, offs[i] + op_pos)
            win = _cmd_window(src, cs)
            w = _shell_words(_mask_parens(win))
            head = _command_key(win[w[0][0]:w[0][1]]) if w else b""
            lit = (head in _NONEXEC_HEADS
                   and not _pipe_to_exec(src, cs + len(win)))
            pending.append(
                [delim, strip, quoted, lit, offs[i] + len(ln) + 1])
        i += 1
    return spans


def _pipe_pos(src: bytes, pos: int) -> int:
    """Index of the first UNQUOTED `|`/`|&` at or after `pos`, or -1.

    The enclosing command ends at `;`, `&&`, `||`, a newline, a bare
    paren, or a word-start `#` comment — nothing downstream then executes
    this command's output. A stdout redirect (`>`, `&>` — `echo "$(x)"
    >/dev/null | sh` hands sh an EMPTY pipe, Devin on #125) also ends it;
    fd>1 redirects (`2>&1`) and input redirects (`<`) do not. Later
    `$(...)`, `<(`/`>(`, and backtick substitutions are balanced spans —
    arguments, not separators — and are SKIPPED so their inner quotes
    never leak into the outer quote state (Devin BUG_0002/0003 on #125)."""
    subs = _substitution_spans(src)

    def span_end(j: int) -> int:
        """End of the substitution span OPENING at j, else -1."""
        for a, b in subs:
            if a == j:
                return b
            if a > j:
                break
        return -1

    def tick_end(j: int) -> int:
        """End of a backtick pair OPENING at j, else -1 (approximate —
        inside a pair, `\\`` escapes the tick)."""
        k = j + 1
        while True:
            k = src.find(b"`", k)
            if k < 0:
                return -1
            if src[k - 1:k] != b"\\":
                return k + 1
            k += 1

    in_s = in_d = esc = False
    # Learn the quote state AT pos first — `pos` typically sits right
    # after a substitution close, so `"` or `'` there may be CLOSING a
    # quote, not opening one (`"$(x)" | sh`). Substitution and backtick
    # bodies are skipped whole: their quotes bind inside the sub, never
    # to the enclosing command.
    j = 0
    while j < pos:
        e = span_end(j)
        if e > 0:
            j = e
            continue
        c = src[j]
        if esc:
            esc = False
        elif c == 0x5C and not in_s:
            esc = True
        elif c == 0x60 and not in_s:
            e = tick_end(j)
            j = e if e > 0 else pos
        elif c == 0x27 and not in_d:
            in_s = not in_s
        elif c == 0x22 and not in_s:
            in_d = not in_d
        j += 1
    while j < len(src):
        e = span_end(j)
        if e > 0:
            j = e
            continue
        c = src[j]
        if esc:
            esc = False
        elif c == 0x5C:
            esc = True
        elif c == 0x27 and not in_d:
            in_s = not in_s
        elif c == 0x22 and not in_s:
            in_d = not in_d
        elif in_s or in_d:
            pass
        elif c == 0x60:
            e = tick_end(j)
            if e < 0:
                return -1
            j = e
            continue
        elif c == 0x7C:
            if src[j + 1:j + 2] == b"|":
                return -1
            return j
        elif c == 0x3E:  # `>`/`>>`/`>&` — the fd decides
            k = j - 1
            while k >= 0 and src[k:k + 1].isdigit():
                k -= 1
            if int(src[k + 1:j] or b"1") == 1:
                return -1  # stdout redirected — the pipe carries nothing
        elif c == 0x26:
            prev = src[j - 1:j]
            nxt = src[j + 1:j + 2]
            if prev in (b"<", b">"):
                pass  # redirect target (`2>&1`, `>&2`, `<&0`)
            else:
                return -1  # `&>`/`&>>` redirect fd1; `&&`/`& ` separate
        elif c in (0x3B, 0x0A, 0x28, 0x29):
            return -1
        elif c == 0x23 and src[j - 1:j] in b" \t\n;&|":
            return -1  # unquoted word-start `#` — a comment to EOL
        j += 1
    return -1


def _span_output_exec(src: bytes, a: int, after: int | None = None) -> bool:
    """True when the substitution starting at `a` produces output the
    enclosing command EXECUTES — so a reader inside it (`cat scripts/x`)
    supplies code, not data (Codex on #123).

    - `<(`/`>(` operands are fd-paths the head opens and runs:
      `bash <(cat x)` and `source <(cat x)` execute x's contents.
    - `$(` output becomes code as `eval`'s argument
      (`eval "$(cat x)"`), the operand of a `-c`/`-e`-style program
      flag (`bash -c "$(cat x)"`, `node -e "$(cat x)"`), or as the
      enclosing command's output piped to an executor
      (`echo "$(cat x)" | sh` — Devin on the consumer PRs)."""
    procsub = src[a] in (0x3C, 0x3E)
    cs = _command_start(src, a)
    win = _cmd_window(src, cs)
    words = _shell_words(_mask_parens(win))
    hi = _effective_head(words, win)
    if hi is None or hi < 0:
        return False
    key = _command_key(win[words[hi][0]:words[hi][1]])
    if procsub:
        return key in _STDIN_EXEC_HEADS or key in (b"source", b".")
    if key == b"eval":
        return True
    # The enclosing command's window stops AT the substitution opener,
    # so the pipe check scans from `after` — just past the
    # substitution's close — to the next unquoted `|` (`echo "$(cat x)"
    # | sh` — Devin on #6).
    p = (cs + len(win)) if after is None else _pipe_pos(src, after)
    if p >= 0 and _pipe_to_exec(src, p):
        return True
    flags = _EXEC_OPERAND_FLAGS.get(key, frozenset())
    if not flags:
        return False
    # `$(` is code when it sits in the operand word of a program flag.
    rel = a - cs
    for k in range(len(words)):
        if words[k][0] <= rel < words[k][1]:
            return any(
                _word_text(win[words[j][0]:words[j][1]]) in flags
                for j in range(hi + 1, k))
    return False


def _descend_sub(src: bytes, pos: int,
                 region: tuple[int, int] | None = None,
                 output_exec: bool = False) -> bool | None:
    """If `pos` is inside the innermost `$(...)` (or a backtick pair) of
    `src` (restricted to `region` when given), classify it inside that
    substitution's body. Returns None when pos is in no substitution."""
    a0, b0 = region if region is not None else (0, len(src))
    # The span must OPEN inside the region; it may run past the end —
    # `$(` inside an unquoted heredoc body still parses to its closing
    # paren even when that lies beyond the delimiter line.
    inner = [s for s in _substitution_spans(src)
             if s[0] <= pos < s[1] and a0 <= s[0]]
    if inner:
        a, b = min(inner, key=lambda s: s[1] - s[0])
        body_end = b - 1 if src[b - 1:b] == b")" else b
        return _command_literal(
            src[a + 2:body_end], pos - a - 2,
            output_exec or _span_output_exec(src, a, b))
    # Backtick pairing is quote-aware: ticks inside a single-quoted span
    # are literal (`'grep `x` y'` is an argument, not a substitution —
    # Devin BUG_0002 on #1953), as is a backslash-escaped tick; inside
    # double quotes a tick still opens a substitution.
    # Quotes toggle only OUTSIDE heredoc regions — in an unquoted
    # heredoc body `'it\'s'` is literal text yet backticks still expand,
    # so an apostrophe must not hide a `sh scripts/x.sh` pair
    # (CodeRabbit on #125). Backslash escapes still apply in either
    # context.
    quote_aware = region is None
    in_s = in_d = esc = False
    ticks: list[int] = []
    for t in range(a0, b0):
        c = src[t]
        if esc:
            esc = False
            continue
        if c == 0x5C and not in_s:  # backslash
            esc = True
            continue
        if quote_aware and c == 0x27 and not in_d:  # single quote
            in_s = not in_s
            continue
        if quote_aware and c == 0x22 and not in_s:  # double quote
            in_d = not in_d
            continue
        if c == 0x60 and not in_s:
            ticks.append(t)
    for t1, t2 in zip(ticks[::2], ticks[1::2]):
        if t1 < pos < t2:
            # `eval \`cmd\`` / `bash -c \`cmd\`` execute the output the
            # same way the `$(` forms do (backtick is not procsub).
            return _command_literal(
                src[t1 + 1:t2], pos - t1 - 1,
                output_exec or _span_output_exec(src, t1, t2 + 1))
    return None


def _command_literal(src: bytes, pos: int,
                     output_exec: bool = False) -> bool:
    """True when `pos` sits in text the shell does NOT execute.

    Literal zones: arguments of a head that cannot run its arguments
    (`echo`, `printf`, `cat`, …) — unless that output pipes into an
    interpreter; inert heredoc bodies; and the trailing comment
    `_cmd_window` stopped at. `$(...)` bodies and backticks recurse —
    each substitution's OWN head decides (`$(echo bash x)` prints,
    `$(bash x)` runs), nesting descending to the innermost command.

    `output_exec` marks bodies whose OUTPUT becomes code (`bash -c
    "$(cat x)"`): a print/read head's operands are code-bound too."""
    for a, b, mode in _heredoc_spans(src):
        if a <= pos < b:
            if mode == _HD_LITERAL:
                return True
            if mode == _HD_EXPAND:
                r = _descend_sub(src, pos, (a, b), output_exec)
                return True if r is None else r
            break  # _HD_EXEC — the body is a script; classify it directly
    r = _descend_sub(src, pos, output_exec=output_exec)
    if r is not None:
        return r
    cs = _command_start(src, pos)
    win = _cmd_window(src, cs)
    if pos >= cs + len(win):
        return True  # trailing comment — documentation, not code
    words = _shell_words(_mask_parens(win))
    if not words:
        return False
    if _command_key(win[words[0][0]:words[0][1]]) not in _NONEXEC_HEADS:
        return False
    if output_exec:
        return False  # the command's output is code — nothing is inert
    return not _pipe_to_exec(src, cs + len(win))


def _glued_short_hides_path(text: bytes) -> bool:
    """A short option with the operand glued on (`-dscripts/site`).

    `-d` always takes that operand. Any other flag hides the path only
    when it is not itself a script file — `node -rscripts/preload.js`
    still counts as an invocation. Any dotted extension counts —
    scripts/ has no allowlist (`-rscripts/x.bash` is an invocation)."""
    if len(text) < 3 or text[1:2] == b"d":
        return len(text) >= 3
    name = text[2:].rsplit(b"/", 1)[-1].rsplit(b"=", 1)[-1]
    return not re.search(rb"\.[A-Za-z0-9_-]+$", name)


def _option_value_spans(window: bytes) -> list[tuple[int, int]]:
    """Byte spans of words that are option operands, not invoked scripts.

    `python -m http.server --directory scripts/site` serves that directory;
    it does not execute `scripts/site`. The same goes for `--directory=…`,
    `-d scripts/site`, and a path glued onto the flag (`-dscripts/site`).
    A later positional (`--directory scripts/site scripts/serve.py`) is
    still an invocation. `--` ends option parsing. Interpreter flags that
    never take an operand (`python -u`, `bash -e`, `bash --posix`) leave
    the next word positional."""
    words = _shell_words(window)
    if not words:
        return []
    bools = _BOOL_SHORT.get(_command_key(window[words[0][0]:words[0][1]]),
                            frozenset())
    spans: list[tuple[int, int]] = []
    i = 0
    ended = False
    while i < len(words):
        start, end = words[i]
        text = _word_text(window[start:end])
        if ended or text == b"-" or not text.startswith(b"-"):
            i += 1
            continue
        if text == b"--":
            ended = True
            i += 1
            continue
        if text.startswith(b"--"):
            eq = window.find(b"=", start, end)
            if eq != -1:
                if eq + 1 < end:
                    spans.append((eq + 1, end))
                i += 1
                continue
            if _long_option_takes_value(text) and i + 1 < len(words):
                spans.append(words[i + 1])
                i += 2
                continue
            i += 1
            continue
        # Short option. A pure letter cluster (`-euo`, `-OO`) is flags,
        # not an attached operand. A tail with a path or `=` is the value
        # (`-dscripts/site`, `-d=scripts/site`). A separate following word
        # is an operand only for interpreters whose flags we know (`-d`,
        # `-o`, `-W`); other commands stay fail-closed so
        # `node -r scripts/preload.js` is still an invocation.
        if len(text) >= 2 and (65 <= text[1] <= 90 or 97 <= text[1] <= 122):
            tail = text[2:]
            if tail and not tail.isalpha():
                if b"scripts/" in text and _glued_short_hides_path(text):
                    spans.append((start, end))
                i += 1
                continue
            if (not tail and bools and text[1] not in bools
                    and i + 1 < len(words)):
                spans.append(words[i + 1])
                i += 2
                continue
        i += 1
    return spans


# A `|`/`>` block-scalar indicator with optional chomping (+/-) and
# explicit-indentation (1-9) modifiers in either order: `|`, `>+`, `|-`,
# `|2`, `|2-`, `|-2`, `|+2`, `|2+`. `|0` is not legal YAML (digit is 1-9)
# and fails closed through the plain-scalar path.
BLOCK_IND_RE = re.compile(r"[>|](?:[1-9]?[+-]?|[+-][1-9])")


_DQ_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
    "L": "\u2028", "P": "\u2029",
    " ": " ",  # YAML's standard \-space escape (PyYAML accepts it)
}


def _dq_decode(s: str) -> str:
    """YAML double-quoted scalar escapes — \\xNN, \\uNNNN, \\UNNNNNNNN and the
    single-char set. Unknown escapes raise rather than reinterpret."""
    out = []
    i = 0
    while i < len(s):
        if s[i] != "\\":
            out.append(s[i])
            i += 1
            continue
        i += 1
        if i >= len(s):
            raise ValueError("trailing backslash in double-quoted scalar")
        c = s[i]
        i += 1
        if c in _DQ_ESCAPES:
            out.append(_DQ_ESCAPES[c])
            continue
        n = {"x": 2, "u": 4, "U": 8}.get(c)
        if n is None:
            raise ValueError(f"unsupported escape \\{c}")
        hexs = s[i:i + n]
        if len(hexs) != n or not re.fullmatch(r"[0-9a-fA-F]+", hexs):
            raise ValueError(f"malformed escape \\{c}{hexs}")
        out.append(chr(int(hexs, 16)))
        i += n
    return "".join(out)


def _mini_yaml(text: str):
    """Stdlib-only YAML subset so the vendored resolver runs without PyYAML.

    Covers the ai-manifest + plugin-frontmatter grammar: block mappings,
    block sequences (scalar items and `- key: value` inline maps continued
    by deeper-indented keys), flow `[a, b]` lists and `{k: v}` maps —
    nested collections and collections folded across lines included —
    comments, `|`/`>` block scalars (literal content, '#' lines included),
    single-document `---`/`...` markers, and plain/quoted/int/float/bool/
    null scalars. Anything richer (anchors, multi-doc, tabs) raises
    ValueError — callers must fail closed, never guess."""
    def strip_comment(s: str) -> str:
        # ' #' starts a comment only outside quotes. Track quote state
        # properly — an apostrophe inside a double-quoted scalar (or a
        # backslash escape) must not corrupt the balance check.
        in_s = in_d = False
        sep = 0
        i = 0
        while i < len(s):
            ch = s[i]
            if in_d:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_d = False
            elif in_s:
                if ch == "'":
                    if s[i + 1:i + 2] == "'":
                        i += 2   # doubled '' is an escaped quote
                        continue
                    in_s = False
            elif ch == '"' and not s[sep:i].strip():
                in_d = True
            elif ch == "'" and not s[sep:i].strip():
                in_s = True
            elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
                return s[:i]
            else:
                # Quotes open a region only at a token boundary — a quote
                # inside an already-started scalar (an apostrophe like
                # "don't") is plain text, not a quote opener. A '-' counts
                # only as the block-sequence indicator — at a token start
                # AND followed by whitespace; 'editor-'s' is one scalar.
                if (ch in ":,[{"
                        or (ch == "-" and not s[sep:i].strip()
                            and s[i + 1:i + 2] in (" ", "\t", ""))):
                    sep = i + 1
            i += 1
        return s

    def flow_depth(s: str) -> int:
        # Bracket depth outside quoted regions — used to fold a flow
        # collection that continues on the next line(s). Quote gating
        # matches strip_comment (quotes only open at a token boundary).
        depth = 0
        in_s = in_d = False
        sep = i = 0
        while i < len(s):
            ch = s[i]
            if in_d:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_d = False
            elif in_s:
                if ch == "'":
                    if s[i + 1:i + 2] == "'":
                        i += 2
                        continue
                    in_s = False
            elif ch == '"' and not s[sep:i].strip():
                in_d = True
            elif ch == "'" and not s[sep:i].strip():
                in_s = True
            else:
                if ch in "[{":
                    depth += 1
                    sep = i + 1
                elif ch in "]}":
                    depth -= 1
                elif ch in ":,":
                    sep = i + 1
            i += 1
        return depth

    def map_colon(v: str) -> int:
        # Index of the `:` that separates a mapping key from its value,
        # or -1. The key may be quoted — a `:` inside quotes is key
        # content, not the separator. Quotes open a region only at a
        # token boundary (same rule as strip_comment/flow_depth) — an
        # apostrophe inside a plain scalar ("author's:") is text.
        in_s = in_d = False
        sep = 0
        i = 0
        while i < len(v):
            ch = v[i]
            if in_d:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_d = False
            elif in_s:
                if ch == "'":
                    if v[i + 1:i + 2] == "'":
                        i += 2
                        continue
                    in_s = False
            elif ch == '"' and not v[sep:i].strip():
                in_d = True
            elif ch == "'" and not v[sep:i].strip():
                in_s = True
            elif (ch == ":"
                    and v[i + 1:i + 2] in (" ", "\t", "")):
                # The ':' separates key from value only when followed by
                # whitespace or EOL — `rollout:phase` is a legal plain
                # key (its ':' precedes 'p', not a separator).
                return i
            else:
                if (ch in ",[{"
                        or (ch == "-" and not v[sep:i].strip()
                            and v[i + 1:i + 2] in (" ", "\t", ""))):
                    sep = i + 1
            i += 1
        return -1

    def open_quote(s: str) -> str:
        # The quote char left open at the end of `s` ("'" or '"'), else ''.
        # Same token-boundary gating as strip_comment: a quote mid-scalar
        # ("don't", `x"y`) is plain text, not a quote opener.
        in_s = in_d = False
        sep = 0
        i = 0
        while i < len(s):
            ch = s[i]
            if in_d:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_d = False
            elif in_s:
                if ch == "'":
                    if s[i + 1:i + 2] == "'":
                        i += 2
                        continue
                    in_s = False
            elif ch == '"' and not s[sep:i].strip():
                in_d = True
            elif ch == "'" and not s[sep:i].strip():
                in_s = True
            else:
                if (ch in ":,[{"
                        or (ch == "-" and not s[sep:i].strip()
                            and s[i + 1:i + 2] in (" ", "\t", ""))):
                    sep = i + 1
            i += 1
        return '"' if in_d else ("'" if in_s else "")

    # Quoted keys are legal YAML in block mappings just as in flow maps —
    # `"version": 1` decodes to the same key as `version: 1`.
    key_re = re.compile(
        # Plain-scalar keys may contain a mid-word apostrophe (`author's`) —
        # but may NOT start with one: a leading quote opens a quoted scalar,
        # so `- 'setup: done'` is the string 'setup: done', not key 'setup.
        # The quoted alternatives are tried first, so a quoted key still
        # parses ('key': v). The plain alternative accepts every character
        # YAML allows in a plain scalar key — `rollout/phase`, `a+b`, `x=y`
        # are all legal keys — excluding only the indicator characters that
        # can never open one (- ? : , [ ] { } # & * ! | > ' " % @ `) and
        # whitespace. A ':' may appear INSIDE the key when not followed by
        # whitespace — `rollout:phase: true` keys on `rollout:phase`
        # (PyYAML agrees). '#' can't appear at all (comment territory).
        r"^(\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'"
        # A leading `-`/`?` is an indicator only when followed by space —
        # `-x: v` and `?x: v` are legal plain keys (PyYAML agrees). `#`
        # mid-token is plain content too (`rollout#phase` — a comment
        # needs a preceding space, which the tail's \s exclusion
        # already rules out) (Codex on vendored-resolver review).
        r"|(?:[-?](?=\S)|[^\s?:,\[\]{}#&*!|>'\"%@`-])"
        r"(?:[^\s\[\]{},:]|:(?=\S))*)"
        # The ':' separates a key only when followed by spaces or EOL — a
        # tab is not a separator (`key:\tv` is a ScannerError in PyYAML).
        # re.S: a folded multiline quoted value can carry a literal '\n'.
        r" *:(?: +(.*)|)$",
        re.S)

    lines = []
    raw_lines = text.splitlines()
    # Whether the document's last line carries a line break — a `|+`/`>+`
    # block whose final content line ends at an unterminated EOF must not
    # gain a phantom terminator that splitlines() can no longer see.
    last_term = not text or text[-1] in "\n\r"
    li = 0
    block_indent = None    # set: deeper lines are literal block content
    content_indent = None  # the block's dedent level (first content line)
    pending_ws = []        # leading all-spaces lines awaiting content_indent
    seen_doc_start = False

    def flush_pending_ws():
        # Leading all-spaces lines are blank — unless one sits deeper than
        # the first real content line, which is an error in YAML ("a leading
        # empty line may not be more indented than the first non-empty line").
        for pi, pt in pending_ws:
            if content_indent is not None and pi > content_indent:
                raise ValueError(
                    "leading empty line deeper than block content")
            lines.append((block_indent + 1, "", pt))
        pending_ws.clear()

    while li < len(raw_lines):
        raw = raw_lines[li]
        term = li < len(raw_lines) - 1 or last_term
        li += 1
        if block_indent is not None:
            # Inside a `|`/`>` block scalar — content is literal: '#' lines
            # are NOT comments and blank lines are content too. The block
            # ends at the first non-blank line no deeper than the key.
            if raw[:1] == "\t":
                # A tab can never start a token — PyYAML ScannerError, even
                # inside a block scalar's leading whitespace.
                raise ValueError("tab cannot start a block-scalar line")
            ind = len(raw) - len(raw.lstrip(" "))
            if raw[ind:]:
                if (raw[ind:ind + 1] == "\t"
                        and content_indent is not None
                        and ind < content_indent):
                    # A tab in the indentation region can never start a
                    # token — PyYAML ScannerError. Once the content
                    # indent is established a tab AT/AFTER it is content
                    # (`  \tb` -> `\tb`), and a tab-led first content
                    # line (`  \ta`) makes the tab content itself.
                    raise ValueError("tab in block-scalar indentation")
                # A comment line deeper than the key ends the block when it
                # sits shallower than the established content indent (or,
                # before any content, shallower than a deeper whitespace
                # line already seen) — the line itself stays a comment,
                # not content. At/above the content indent it is literal.
                comment_ends = (
                    ind > block_indent
                    and raw[ind:].startswith("#")
                    and ind < (content_indent
                               if content_indent is not None
                               else max((pi for pi, _ in pending_ws),
                                        default=block_indent + 1)))
                # Non-blank — indentation is the leading SPACES only; a tab
                # after the spaces is content, not a token start.
                if ind > block_indent and not comment_ends:
                    if content_indent is None:
                        content_indent = ind
                        flush_pending_ws()
                    if ind < content_indent:
                        # Less indented than the first content line but
                        # deeper than the key — invalid YAML, not quieter
                        # content.
                        raise ValueError(
                            "inconsistent block scalar indentation")
                    # YAML dedents block content by the first line's
                    # indent — deeper lines keep their extra (relative)
                    # indentation, and trailing spaces are literal
                    # content too.
                    lines.append((ind, raw[content_indent:], term))
                    continue
            else:
                # Whitespace-only line (all spaces — a tab already raised).
                # A blank line NEVER ends a block, whatever its indent.
                if content_indent is None:
                    # BEFORE the first content line its validity depends on
                    # the dedent level the first content line will set —
                    # defer the decision.
                    pending_ws.append((ind, term))
                elif ind > content_indent:
                    # Interior whitespace-only line deeper than the dedent
                    # level — its excess spaces are significant content.
                    lines.append((ind, raw[content_indent:], term))
                else:
                    lines.append((block_indent + 1, "", term))
                continue
            flush_pending_ws()
            block_indent = None
            content_indent = None
        if not raw.strip() or raw.lstrip().startswith("#"):
            # Blank lines and comments are invisible to structure, but NOT
            # to a plain-scalar continuation: a blank line folds to '\n'
            # ('x:\n  a\n\n  b' -> 'a\nb') while a comment line ENDS the
            # scalar ('x: a\n# c\n  b' is a PyYAML error). Blanks emit ""
            # and comments emit "\x00" so parse can tell them apart; both
            # carry their own indent so a stray ind can't reshape the doc.
            kind = "" if not raw.strip() else "\x00"
            lines.append(
                (len(raw) - len(raw.lstrip(" ")), kind, term))
            continue
        rstripped = raw.rstrip()
        body = strip_comment(rstripped)
        if not body.strip():
            continue
        # An inline comment still ENDS the scalar it trails — `k: v # c`
        # followed by a deeper `more` line is a PyYAML error, not the
        # value 'v more'. Mark the boundary the same way a standalone
        # comment does, so a later continuation can't fold across it
        # (Devin on vendored-resolver review).
        inline_comment = len(body) != len(rstripped)
        # Single-document markers: exactly one leading `---` is boilerplate;
        # an EMPTY first document still counts, so a second `---` is a new
        # document and raises. `...` ends the document — anything after it
        # is trailing garbage. Markers count ONLY at column zero — an
        # indented `---` is scalar content, not a document boundary.
        if body == body.lstrip() and body.strip() == "---":
            if (seen_doc_start
                    or any(ln[1] not in ("", "\x00") for ln in lines)):
                raise ValueError(
                    "multiple YAML documents are not supported")
            seen_doc_start = True
            continue
        if (body == body.lstrip() and body.strip().startswith("---")
                and body.strip()[3] in " \t"):
            # `--- <node>` — the root node may share the marker line
            # (`--- {version: 1, ...}`). The marker still counts as the
            # document start; the remainder parses as the line's content.
            if (seen_doc_start
                    or any(ln[1] not in ("", "\x00") for ln in lines)):
                raise ValueError(
                    "multiple YAML documents are not supported")
            seen_doc_start = True
            body = body.strip()[3:].strip()
            if not body:
                continue
            if body.startswith("- ") or body == "-":
                # `--- - 1` — a seq entry directly on the marker line is a
                # ScannerError in PyYAML, not a document.
                raise ValueError(
                    "sequence entries are not allowed on a '---' line")
            if key_re.match(body):
                # `--- key: v` — the marker line's node must be COMPLETE:
                # a flow collection, scalar, or block-scalar indicator. A
                # block mapping entry cannot share the `---` line — PyYAML
                # raises "mapping values are not allowed here".
                raise ValueError(
                    "mapping entries are not allowed on a '---' line")
        if body == body.lstrip() and body.strip() == "...":
            for rest in raw_lines[li:]:
                if rest.strip() and not rest.lstrip().startswith("#"):
                    raise ValueError("content after document end '...'")
            break
        # A quoted scalar may continue on deeper lines — YAML folds the
        # break to one space and strips the continuation's indent; a blank
        # continuation line folds to a real '\n' (the space join resumes
        # only after a non-blank line). Consumed lines are literal inside
        # the quote — '#'/indents carry no syntax there.
        while open_quote(body) and li < len(raw_lines):
            nxt = raw_lines[li]
            li += 1
            # In a double-quoted scalar an ODD-length run of trailing
            # backslashes escapes the line break itself: the last '\' is
            # consumed and the continuation joins with NO separator. An
            # even run is an escaped backslash then an ordinary folded
            # break ('a\\\n  b' -> 'a\\ b'). Single-quoted scalars have
            # no escapes — '\' is literal there.
            if (open_quote(body) == '"'
                    and (len(body) - len(body.rstrip("\\"))) % 2 == 1):
                body = body[:-1]
                if nxt.strip():
                    body += nxt.strip()
                else:
                    body += "\n"
            elif nxt.strip():
                body += ("" if body.endswith("\n") else " ") + nxt.strip()
            else:
                body += "\n"
        if not open_quote(body):
            # Once the quote closes, the rest of that line regains YAML
            # syntax — a trailing comment must come off (it is text, not
            # scalar content), while a '#' inside the folded quote stays.
            body = strip_comment(body)
        # A flow collection may continue on deeper lines — fold each
        # (comment-stripped) line in with one space until the brackets
        # balance. Folding is gated on the VALUE actually opening a
        # collection: a '[' or '{' inside a plain scalar ('description:
        # Use [ to open') is ordinary text, not a bracket to balance.
        value = body.lstrip()
        seq_item = value[:1] == "-"
        if seq_item:
            value = value[1:].lstrip()
        fold = value[:1] in "[{"
        if not fold:
            ci = map_colon(value)
            if ci != -1:
                fold = value[ci + 1:].lstrip()[:1] in "[{"
        if fold:
            while flow_depth(body) > 0 and li < len(raw_lines):
                part = strip_comment(raw_lines[li].strip())
                li += 1
                if part.strip():
                    body += " " + part
            if flow_depth(body) != 0:
                raise ValueError("unterminated flow collection")
        indent = len(body) - len(body.lstrip(" "))
        if body[indent:indent + 1] == "\t":
            # Only spaces may indent — a tab can never start a token.
            raise ValueError("tab indentation is not supported")
        # A lone `|`/`>` on the line AFTER an empty value (`key:`, `-`,
        # `- key:`) still opens a block scalar — scoped by the KEY's
        # indent (or the item's key column for `- key:`), not by the
        # indicator line's own column. Emit a \x01-tagged header at the
        # effective block indent so parse treats it as block content; a
        # bare `|` at document root works the same way.
        lone_ind = BLOCK_IND_RE.fullmatch(body.lstrip())
        if lone_ind:
            # The preceding line for block discovery is the last REAL
            # line — blank/comment markers between `x:` and the `|` do
            # not detach it.
            prev = next((ln for ln in reversed(lines)
                         if ln[1] not in ("", "\x00")), None)
            # `key: # note` strips to `key: ` — the emptiness test must
            # ignore the whitespace strip_comment left after the colon.
            if (prev is None
                    or (prev[0] <= indent
                        and (prev[1].rstrip() == "-"
                             or prev[1].rstrip().endswith(":")))):
                block_indent = (prev[0] + 2
                                if prev is not None
                                and prev[1].startswith("- ")
                                else (prev[0] if prev is not None
                                      else indent))
                lines.append(
                    (block_indent, "\x01" + body.lstrip(), True))
                d = re.search(r"[1-9]", lone_ind.group(0))
                content_indent = (block_indent + int(d.group(0))
                                  if d else None)
                continue
        lines.append((indent, body.lstrip(), True))
        # A `|`/`>` value opens a literal block on the deeper lines that
        # follow — `- |`/`- key: |` seq items too (the value after the dash
        # is the indicator itself). The block's scope differs: `key: |`
        # content must sit deeper than the KEY (the key's column in a
        # mapping, or the item's logical indent+2 for `- key: |` — a `tag:`
        # sibling at that level is a key, not content); a bare `- |`
        # scalar item is scoped by the dash's own indent — any deeper
        # line is content, matching PyYAML. An explicit digit (`|2-`)
        # fixes the dedent level at block_indent + d instead of the first
        # content line's indent.
        bci = map_colon(value)
        # The ':' separator only counts when followed by a space or EOL —
        # `key:|`/`key:\t|` are not `key: <indicator>` pairs at all.
        bind = (
            BLOCK_IND_RE.fullmatch(value[bci + 1:].strip())
            if bci != -1 and value[bci + 1:bci + 2] in (" ", "")
            else (None if bci != -1 else BLOCK_IND_RE.fullmatch(value)))
        if inline_comment and not bind:
            # `ref: |- # pin` is legal — the comment trails the indicator
            # and deeper lines are still block content, so the marker
            # must not terminate them (Codex on vendored-resolver review).
            lines.append((indent, "\x00", True))
        if bind:
            block_indent = (indent + 2
                            if seq_item and bci != -1 else indent)
            d = re.search(r"[1-9]", bind.group(0))
            content_indent = (block_indent + int(d.group(0))
                              if d else None)

    if block_indent is not None:
        # A block scalar running to EOF — deferred whitespace-only lines
        # still belong to it (all blank when no content line ever came).
        flush_pending_ws()

    pos = [0]

    def key_of(tok: str):
        return scalar(tok) if tok[:1] in "\"'" else tok

    # "" = blank-line marker, "\x00" = comment marker — both invisible to
    # structure but load-bearing inside a plain-scalar continuation.
    def skip_markers():
        while pos[0] < len(lines) and lines[pos[0]][1] in ("", "\x00"):
            pos[0] += 1

    def block_marker():
        """Consume a \x01-tagged lone-indicator header + its content lines.

        The header carries the effective block indent as its tuple indent —
        content is every line deeper than that."""
        bi, ind = lines[pos[0]][0], lines[pos[0]][1][1:]
        pos[0] += 1
        vals = []
        while (pos[0] < len(lines) and lines[pos[0]][0] > bi
               and lines[pos[0]][1] != "\x00"):
            vals.append((lines[pos[0]][1], lines[pos[0]][2]))
            pos[0] += 1
        return block_scalar(ind, vals)

    def fold_scalar(acc, threshold):
        """Fold plain-scalar continuation lines into acc.

        Non-key content deeper than `threshold` joins with one space.
        A blank ("") line folds to a real '\n' — but only when content
        follows it (trailing blanks before the scalar ends are dropped).
        A "\x00" comment line ENDS the scalar (PyYAML: the comment breaks
        the continuation, so a following indented line is an error).
        A key-shaped line in the continuation region is 'mapping values
        are not allowed here'."""
        pending_nl = 0
        while pos[0] < len(lines):
            txt = lines[pos[0]][1]
            if txt == "":
                pending_nl += 1
                pos[0] += 1
                continue
            if txt == "\x00" or lines[pos[0]][0] <= threshold:
                break
            if key_re.match(txt):
                raise ValueError("mapping values are not allowed here")
            acc = (str(acc) + "\n" * pending_nl
                   + ("" if pending_nl or str(acc).endswith("\n")
                      else " ") + txt)
            pending_nl = 0
            pos[0] += 1
        return acc

    def flow_items(inner: str) -> list[str]:
        # Split a flow list's item text on top-level commas only — a comma
        # inside a quoted scalar or a nested flow belongs to the item.
        items, depth = [], 0
        in_s = in_d = esc = False
        start = sep = 0
        for i, ch in enumerate(inner):
            if esc:
                esc = False
            elif in_d and ch == "\\":
                esc = True
            elif in_d and ch == '"':
                in_d = False
            elif in_s and ch == "'":
                if inner[i + 1:i + 2] == "'":
                    esc = True  # '' escape — skip the second quote too
                else:
                    in_s = False
            elif ch == '"' and not in_s and not inner[sep:i].strip():
                # Quotes only open a quoted region at an item boundary — a
                # " inside an already-started plain scalar is plain text.
                # `sep` tracks the most recent structural opener/comma at
                # ANY depth, so a quote right after `[` or a nested `,`
                # opens a region too.
                in_d = True
            elif ch == "'" and not in_d and not inner[sep:i].strip():
                # Same for ': YAML allows apostrophes in plain scalars, so
                # `editor's-tool` mid-item must not swallow the comma.
                in_s = True
            elif not in_s and not in_d:
                if ch in "[{":
                    depth += 1
                    sep = i + 1
                elif ch in "]}":
                    depth -= 1
                elif ch == ":":
                    sep = i + 1
                elif ch == ",":
                    sep = i + 1
                    if depth == 0:
                        items.append(inner[start:i])
                        start = i + 1
        items.append(inner[start:])
        return items

    def scalar(tok: str):
        tok = tok.strip()
        if not tok:
            raise ValueError("empty scalar")
        if tok.startswith("[") and tok.endswith("]"):
            inner = tok[1:-1].strip()
            if not inner:
                return []
            items = flow_items(inner)
            # A trailing comma is legal YAML — the text after it is empty,
            # not an item. A bare empty item mid-list still fails closed.
            if not items[-1].strip():
                items = items[:-1]
            return [scalar(p) for p in items]
        if tok == "{}":
            return {}
        if tok == "[]":
            return []
        if tok.startswith("{") and tok.endswith("}"):
            # Flow map — `{k: v, ...}`; keys are bare scalars in the
            # supported subset (quoted keys stay fail-closed).
            out_map: dict = {}
            map_items = flow_items(tok[1:-1].strip())
            if map_items and not map_items[-1].strip():
                map_items = map_items[:-1]  # legal trailing comma
            for item in map_items:
                item = item.strip()
                # Quoted keys take JSON form (`"k":v` — no space needed);
                # a PLAIN key's ':' only separates when followed by
                # whitespace or the item's end — `{key:v}` is the single
                # scalar key 'key:v' in YAML, not a mapping.
                m = re.match(
                    r"^(\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*')\s*:(.*)$",
                    item, re.S)
                if m:
                    key = scalar(m.group(1))
                    val_tok = m.group(2)
                else:
                    # A plain key is everything up to the ':' separator —
                    # any character except the flow indicators ([ ] { } ,
                    # and the separator itself). `rollout/phase: true` and
                    # `a:b: v` (key 'a:b') are legal YAML; `{key:v}` still
                    # fails to match here because its ':' is followed by
                    # 'v', not whitespace/EOL.
                    m = re.match(
                        r"^([^\[\]{},]+?)\s*:(?:\s+(.*)|)$",
                        item, re.S)
                    if not m:
                        raise ValueError(
                            f"unsupported flow-map item {item!r}")
                    key, val_tok = m.group(1).strip(), m.group(2)
                    if not key:
                        raise ValueError(
                            f"unsupported flow-map item {item!r}")
                if key in out_map:
                    raise ValueError(
                        f"duplicate key {key!r} in flow map")
                out_map[key] = (None if val_tok is None
                                or not val_tok.strip()
                                else scalar(val_tok))
            return out_map
        if tok[0] == '"':
            if not (len(tok) > 1 and tok.endswith('"')):
                raise ValueError(f"unterminated quoted scalar {tok!r}")
            return _dq_decode(tok[1:-1])
        if tok[0] == "'":
            if not (len(tok) > 1 and tok.endswith("'")):
                raise ValueError(f"unterminated quoted scalar {tok!r}")
            return tok[1:-1].replace("''", "'")
        # PyYAML numeric grammar — the resolver's own regexes verbatim.
        # Ints: binary `0b`, legacy `0…` octal, decimal, hex `0x`,
        # sexagesimal `1:2:3` — `0o`/`0X`/`0B` are strings. Floats: the
        # mantissa MUST carry a `.` (so `1e3`/`1e+3` stay strings), the
        # exponent sign is mandatory, `.inf`/`.Inf`/`.INF` are signed,
        # `.nan`/`.NaN`/`.NAN` unsigned (`.Nan`, `-.nan` stay strings).
        sign = -1 if tok[:1] == "-" else 1
        if re.fullmatch(r"[-+]?0b[0-1_]+", tok):
            return sign * int(tok.lstrip("+-").replace("_", ""), 2)
        if re.fullmatch(r"[-+]?0[0-7_]+", tok):
            return sign * int(tok.lstrip("+-").replace("_", ""), 8)
        if re.fullmatch(r"[-+]?(?:0|[1-9][0-9_]*)", tok):
            return sign * int(tok.lstrip("+-").replace("_", ""))
        if re.fullmatch(r"[-+]?0x[0-9a-fA-F_]+", tok):
            return sign * int(tok.lstrip("+-").replace("_", ""), 16)
        if re.fullmatch(r"[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+", tok):
            v = 0
            for part in tok.lstrip("+-").split(":"):
                v = v * 60 + int(part.replace("_", ""))
            return sign * v
        if re.fullmatch(
                r"[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?"
                r"|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?", tok):
            return sign * float(tok.lstrip("+-").replace("_", ""))
        if re.fullmatch(r"[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*",
                        tok):
            # Every field but the LAST is scaled 60^k — the final field
            # stays at unit scale: `1:20.5` = 80.5, `1:02:03.5` = 3723.5
            # (PyYAML's constructor; consumer review of the vendored resolver).
            v = 0.0
            for part in tok.lstrip("+-").split(":"):
                v = v * 60 + float(part.replace("_", ""))
            return sign * v
        if tok.lstrip("+-") in (".inf", ".Inf", ".INF"):
            return math.inf * sign
        if tok in (".nan", ".NaN", ".NAN"):
            return math.nan
        # YAML 1.1 booleans/nulls — PyYAML's resolver regexes accept only
        # lower/Title/UPPER spellings (yes|Yes|YES|…), NOT arbitrary case:
        # `tRuE` and `nUll` stay strings (Devin on #123). Single-letter
        # y/n stay strings too. `requires_scripts: off` still reads False.
        if tok in ("true", "True", "TRUE",
                   "yes", "Yes", "YES", "on", "On", "ON"):
            return True
        if tok in ("false", "False", "FALSE",
                   "no", "No", "NO", "off", "Off", "OFF"):
            return False
        if tok in ("null", "Null", "NULL", "~"):
            return None
        if tok[0] in "[{|>&!%@`":
            raise ValueError(f"unsupported scalar {tok!r}")
        if re.search(r":(?:\s|$)", tok):
            # `a: b` inside a plain scalar is a nested mapping value —
            # PyYAML scanner error, never text ('description: foo: bar').
            raise ValueError(
                f"mapping values are not allowed in a plain scalar: {tok!r}")
        return tok

    def block_scalar(indicator: str, vals: list) -> str:
        # `vals` is (content, terminated) per line — `terminated` is False
        # only for a last line that reached an unterminated EOF, so `|+`/
        # `>+` don't invent a break the source did not carry.
        #
        # Literal `|` joins lines verbatim. Folded `>`: a break between
        # two ordinary non-blank lines becomes a space; every blank line
        # contributes one '\n', but an interior blank followed by an
        # ordinary line shares the break already emitted before it
        # (a\n\nb -> 'a\nb') — it keeps its own only when it is leading,
        # follows a blank or more-indented line, or precedes a blank or
        # more-indented line or the block's end. Breaks adjacent to
        # more-indented lines (still leading-space after dedent) stay
        # newlines. Chomping: `x-` strips trailing newlines, `x+` keeps
        # them all, plain `x` clips to exactly one — and an all-blank
        # block clips to "" (trailing blanks are chomped first).
        if indicator.startswith(">"):
            parts: list[str] = []
            last = len(vals) - 1
            # Whether the nearest non-blank line before the current blank
            # run is an ORDINARY line (not more-indented). The last blank
            # of an interior run shares the break the run-start's line
            # break already supplied; blanks after a more-indented line
            # or at the start of the block keep their own.
            prev_nb_ordinary = False
            for i, (v, term) in enumerate(vals):
                brk = "\n" if term else ""
                if not v:
                    nxt = vals[i + 1][0] if i < last else None
                    nxt_ordinary = bool(nxt) and nxt[:1] not in " \t"
                    if not (nxt_ordinary and prev_nb_ordinary):
                        parts.append(brk)
                else:
                    parts.append(v)
                    if i == last:
                        parts.append(brk)
                    elif not vals[i + 1][0] or v[:1] in " \t" \
                            or vals[i + 1][0][:1] in " \t":
                        parts.append("\n")
                    else:
                        parts.append(" ")
                    prev_nb_ordinary = v[:1] not in " \t"
            text = "".join(parts)
        else:
            text = "".join(v + ("\n" if t else "") for v, t in vals)
        if "-" in indicator[1:]:
            return text.rstrip("\n")
        if "+" in indicator[1:]:
            return text
        # Clip: one trailing break iff the last non-blank content line was
        # actually terminated — `x: |\n  a` (unterminated EOF) yields 'a',
        # while `x: |\n  a\n  ` still yields 'a\n' (a's own break).
        stripped = text.rstrip("\n")
        if not stripped:
            return ""
        last_nl = next((t for v, t in reversed(vals) if v), False)
        return stripped + ("\n" if last_nl else "")

    def parse(indent: int):
        if lines[pos[0]][0] != indent:
            raise ValueError("inconsistent indentation")
        first = lines[pos[0]][1]
        if first.startswith("\x01"):
            # Document rooted at a lone `|`/`>` block scalar.
            return block_marker()
        if first == "-" or first.startswith("- "):
            seq = []
            while pos[0] < len(lines):
                if lines[pos[0]][1] in ("", "\x00"):
                    pos[0] += 1
                    continue
                if (lines[pos[0]][0] != indent
                        or not (lines[pos[0]][1] == "-"
                                or lines[pos[0]][1].startswith("- "))):
                    break
                item = lines[pos[0]][1][1:].lstrip()
                pos[0] += 1
                if not item:
                    skip_markers()
                    if (pos[0] < len(lines)
                            and lines[pos[0]][1].startswith("\x01")):
                        seq.append(block_marker())
                    elif (pos[0] < len(lines)
                          and lines[pos[0]][0] > indent):
                        nxt = lines[pos[0]][1]
                        if (key_re.match(nxt) or nxt == "-"
                                or nxt.startswith("- ")):
                            seq.append(parse(lines[pos[0]][0]))
                        else:
                            # `-` bare item + deeper text — the node may be
                            # a flow collection (`-\n  {k: v}`), a quoted
                            # scalar, or folded plain text. scalar() handles
                            # all three: flow/quoted decode, plain text
                            # returns as-is, `5`/`yes` keep their type.
                            acc = lines[pos[0]][1]
                            pos[0] += 1
                            seq.append(
                                scalar(fold_scalar(acc, indent)))
                            skip_markers()
                            if (pos[0] < len(lines)
                                    and lines[pos[0]][0] > indent):
                                raise ValueError(
                                    "nested structure after scalar")
                    else:
                        seq.append(None)
                    continue
                if BLOCK_IND_RE.fullmatch(item):
                    # `- |` — the scalar item's block is every line deeper
                    # than the dash itself (how the preprocessor scoped
                    # it, and how PyYAML reads it). A comment marker ends
                    # the block — comments are not content.
                    vals = []
                    while (pos[0] < len(lines)
                           and lines[pos[0]][0] > indent
                           and lines[pos[0]][1] != "\x00"):
                        vals.append((lines[pos[0]][1], lines[pos[0]][2]))
                        pos[0] += 1
                    seq.append(block_scalar(item, vals))
                    continue
                km = key_re.match(item)
                if km:
                    d = {}
                    ikey = key_of(km.group(1))
                    iv = km.group(2)
                    if iv is not None and not iv.strip():
                        iv = None  # `- key: # comment` — no scalar value
                    if iv is not None and BLOCK_IND_RE.fullmatch(
                            iv.strip()):
                        # `- key: |` — same indent+2 block scoping as `- |`.
                        vals = []
                        while (pos[0] < len(lines)
                               and lines[pos[0]][0] > indent + 2
                               and lines[pos[0]][1] != "\x00"):
                            vals.append((lines[pos[0]][1], lines[pos[0]][2]))
                            pos[0] += 1
                        d[ikey] = block_scalar(iv.strip(), vals)
                    elif iv is not None:
                        # A multiline plain scalar folds each continuation
                        # line DEEPER THAN THE KEY'S COLUMN (indent+2) into
                        # the value — a sibling key sits AT indent+2.
                        d[ikey] = fold_scalar(scalar(iv), indent + 2)
                    else:
                        skip_markers()
                        if (pos[0] < len(lines)
                                and lines[pos[0]][1].startswith("\x01")):
                            d[ikey] = block_marker()
                        elif (pos[0] < len(lines)
                              and lines[pos[0]][0] > indent):
                            ni, nxt = lines[pos[0]][0], lines[pos[0]][1]
                            if ((nxt == "-" or nxt.startswith("- "))
                                    and ni >= indent + 2):
                                # `- key:` + `- x` at the key column or
                                # deeper — a nested seq value.
                                d[ikey] = parse(ni)
                            elif ni > indent + 2:
                                if key_re.match(nxt):
                                    d[ikey] = parse(ni)
                                else:
                                    # `- key:` + deeper plain text — a
                                    # folded scalar; a leftover at or below
                                    # the key column is a sibling (or an
                                    # orphan the sibling loop rejects).
                                    acc = nxt
                                    pos[0] += 1
                                    # Still a typed node — `- key:\n    1`
                                    # is int 1, not '1'.
                                    d[ikey] = scalar(
                                        fold_scalar(acc, indent + 2))
                            else:
                                # Key at the item's key column — a
                                # sibling, not the value.
                                d[ikey] = None
                        else:
                            d[ikey] = None
                    while pos[0] < len(lines):
                        if lines[pos[0]][1] in ("", "\x00"):
                            pos[0] += 1
                            continue
                        if lines[pos[0]][0] <= indent:
                            break
                        more = parse(lines[pos[0]][0])
                        if not isinstance(more, dict):
                            raise ValueError("nested sequence inside item map")
                        dup = d.keys() & more.keys()
                        if dup:
                            raise ValueError(
                                f"duplicate key {sorted(dup)[0]!r}")
                        d.update(more)
                    seq.append(d)
                else:
                    # `- foo` plain item folds any deeper continuation
                    # line into the scalar (deeper than the DASH's indent).
                    seq.append(fold_scalar(scalar(item), indent))
            return seq
        out = {}
        while pos[0] < len(lines):
            if lines[pos[0]][1] in ("", "\x00"):
                pos[0] += 1
                continue
            if lines[pos[0]][0] != indent:
                break
            km = key_re.match(lines[pos[0]][1])
            if not km:
                raise ValueError(f"unsupported line {lines[pos[0]][1]!r}")
            k, v = key_of(km.group(1)), km.group(2)
            # `key: # comment` strips to `key: ` — a whitespace-only value
            # is NO value (a nested block or null follows), not an empty
            # scalar.
            if v is not None and not v.strip():
                v = None
            if k in out:
                # Duplicate keys silently keep the last value in YAML — a
                # manifest that states a ref twice must be a parse error,
                # not a coin flip on which value won.
                raise ValueError(f"duplicate key {k!r}")
            pos[0] += 1
            if v is not None:
                if BLOCK_IND_RE.fullmatch(v.strip()):
                    # Block scalar: every deeper-indented line is literal
                    # content (even lines shaped like keys or seq items).
                    # A comment marker ends the block.
                    vals = []
                    while (pos[0] < len(lines)
                           and lines[pos[0]][0] > indent
                           and lines[pos[0]][1] != "\x00"):
                        vals.append((lines[pos[0]][1], lines[pos[0]][2]))
                        pos[0] += 1
                    out[k] = block_scalar(v.strip(), vals)
                else:
                    # A plain scalar continues on deeper lines — YAML
                    # folds them with one space (a blank line folds to
                    # '\n'). A leftover deeper line afterwards is mixed
                    # content: reject.
                    out[k] = fold_scalar(scalar(v), indent)
                    skip_markers()
                    if (pos[0] < len(lines) and lines[pos[0]][0] > indent):
                        raise ValueError("nested structure after scalar value")
            else:
                skip_markers()
            if v is None and (pos[0] < len(lines)
                              and lines[pos[0]][1].startswith("\x01")):
                out[k] = block_marker()
            elif v is None and (pos[0] < len(lines)
                                and lines[pos[0]][0] == indent
                                and (lines[pos[0]][1] == "-"
                                     or lines[pos[0]][1]
                                     .startswith("- "))):
                # Indentationless block sequence: `key:` followed by `-`
                # items at the SAME indent is valid YAML (yaml.dump emits
                # it). The seq parser stops at the next non-dash line, so
                # sibling mapping keys are not consumed.
                out[k] = parse(indent)
            elif v is None and pos[0] < len(lines) and lines[pos[0]][0] > indent:
                # `key:` empty followed by deeper plain text is a folded
                # scalar in YAML; a deeper key/seq is a nested structure.
                # `- `-shaped lines only open a sequence in FIRST position
                # — inside a scalar continuation they are literal text.
                if (key_re.match(lines[pos[0]][1])
                        or lines[pos[0]][1] == "-"
                        or lines[pos[0]][1].startswith("- ")):
                    out[k] = parse(lines[pos[0]][0])
                else:
                    acc = lines[pos[0]][1]
                    pos[0] += 1
                    if acc[:1] in "[{\"'":
                        # A deeper flow collection or quoted scalar IS the
                        # value — `requires:\n  [{plugin: ...}]` parses the
                        # list, it is not folded plain text.
                        out[k] = scalar(acc)
                    else:
                        # The scalar's first line sets the content column
                        # but continuations fold at any depth past the
                        # KEY's own indent (`x:\n  a\n a` -> 'a a'). A
                        # next-line scalar is still a typed node —
                        # `version:\n  1` is int 1, `...\n  on` is True —
                        # so the folded text goes through scalar() like
                        # an inline value.
                        out[k] = scalar(fold_scalar(acc, indent))
                    skip_markers()
                    if (pos[0] < len(lines)
                            and lines[pos[0]][0] > indent):
                        raise ValueError("nested structure after scalar")
            elif v is None:
                out[k] = None
        return out

    if not lines:
        return {}
    skip_markers()
    if pos[0] == len(lines):
        return {}
    if lines[pos[0]][1][:1] in "[{":
        # A whole-document flow collection — e.g. frontmatter written as a
        # single `{k: v}` line — parses through scalar directly.
        if any(ln[1] not in ("", "\x00") for ln in lines[pos[0] + 1:]):
            raise ValueError("trailing unparseable structure")
        return scalar(lines[pos[0]][1])
    root = parse(lines[pos[0]][0])
    skip_markers()
    if pos[0] != len(lines):
        raise ValueError("trailing unparseable structure")
    return root


def _yaml_load(text: str):
    """One YAML grammar in every environment: the restricted _mini_yaml
    subset — the vendored resolver has no runtime deps and parses the same
    installed set whether or not PyYAML happens to be present. Richer YAML
    fails closed, never parses differently. A single leading UTF-8 BOM is
    a signature, not content — Windows editors write one and PyYAML skips
    it; leaving it would corrupt the first key (Codex on
    vendored-resolver review)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return _mini_yaml(text)


def _frontmatter(src_bytes: bytes) -> dict:
    m = re.match(rb"\A---\s*\n(.*?)\n---\s*\n", src_bytes, re.S)
    if not m:
        return {}
    try:
        fm = _yaml_load(m.group(1).decode("utf-8", errors="ignore")) or {}
    except Exception:
        # Frontmatter that fails to parse must not read as 'no deps declared' —
        # fail closed so the file is skipped rather than shipped half-gated.
        return {"_unparseable": True}
    return fm if isinstance(fm, dict) else {"_unparseable": True}


def declares_script_deps(src_bytes: bytes) -> bool:
    """True when the file's YAML frontmatter declares script dependencies —
    explicit metadata, since prose heuristics can't distinguish
    "run `scripts/x.py`" from "routing uses `scripts/x.py`"."""
    fm = _frontmatter(src_bytes)
    return fm.get("_unparseable", False) or any(
        fm.get(k) for k in SCRIPT_DEP_KEYS)


def declared_consumer_scripts(src_bytes: bytes) -> set[str]:
    """The set of script paths the file declares are consumer-repository
    provided (consumer_scripts: [...]) — every unbundled scripts/x.py
    invocation must be explicitly listed here to be exempt."""
    fm = _frontmatter(src_bytes)
    out: set[str] = set()
    for k in CONSUMER_SCRIPT_KEYS:
        v = fm.get(k)
        if isinstance(v, (list, tuple)):
            out |= {str(x) for x in v}
    return out


def script_dep_block(plugin_dir: Path, src_bytes: bytes,
                     pinned_scripts: set[str] | None = None) -> bool:
    """True when the file's script usage cannot run under a resolver install:
    a bundled plugin script (scripts/ isn't materialised), an explicit
    requires_scripts dep, or an unbundled invocation that isn't listed in
    consumer_scripts.

    Under a tag:/sha: pin, `pinned_scripts` carries the scripts/-relative
    paths the PINNED git tree holds — the worktree's is_file() would honour
    ignored/untracked plants and index-hidden deletions the pin never saw."""
    sdir = plugin_dir / "scripts"
    declared = declared_consumer_scripts(src_bytes)
    # A POSIX backslash-newline continuation is part of one logical
    # command — remove it up front or `python3 \` + `scripts/x.py` on the
    # next line never matches the invocation regex. The join must be
    # quote- and parity-aware: inside single quotes a backslash is
    # literal, and an even run's last backslash is itself escaped — in
    # both cases the newline still ends the command, and joining anyway
    # would fuse two separate commands' arguments into one window.
    scan = _join_continuations(src_bytes)
    for m in SCRIPT_REF.finditer(scan):
        # A single invocation may carry SEVERAL scripts/ arguments —
        # `bash scripts/first.sh scripts/second.sh` ends its regex match
        # at first.sh, but second.sh is just as much a dependency. Scan
        # the whole command region (bounded by UNQUOTED shell metachars
        # and word-start comments — quote-aware, so "a#b" hides nothing)
        # so every argument reaches the bundled/declared checks.
        window = _cmd_window(scan, m.start())
        # Each ref carries (declared alternatives, bundled probes): any
        # bundled probe hit means the dep is bundled (the resolver cannot
        # materialise scripts/); otherwise at least one declared
        # alternative must appear in consumer_scripts.
        refs: list[tuple[list[str], list[str]]] = []
        # Redirection targets are not dependencies — output/fd/heredoc
        # operators create the target or take a word (`>`, `>>`, `>|`,
        # `>&`, `&>`, `&>>`, `2>`, `<<`, `<<-`, `<<<`, `<&`). An input
        # redirect (`<`, `<>`) still counts — the command reads it.
        redir_spans = _redirection_target_spans(window)
        # Option operands are not invoked scripts — `--directory
        # scripts/site`, `-d scripts/site`, `-dscripts/site` name a
        # directory to serve. Applied to EXTENSIONLESS names only: an
        # operand with a script extension could still be an input file
        # (`--input scripts/data.py`), so it stays gated (fail-closed).
        opt_spans = _option_value_spans(window)
        # Literal text is not an invocation. The match's window starts at
        # the interpreter keyword, so `echo "bash scripts/x.sh"` and
        # `# bash scripts/x.sh` would otherwise pin deps on arguments that
        # only print. `enclosing` is the full command containing the match;
        # a position past its end sits in the trailing comment _cmd_window
        # stopped at, and a head that cannot execute (`echo`, `printf`,
        # `cat`, `grep`, …) makes every argument literal (Devin on
        # vendored-resolver review).
        cs = _command_start(scan, m.start())
        enclosing = _cmd_window(scan, cs)
        # Literal text is not an invocation. `echo "bash scripts/x.sh"`,
        # `# bash scripts/x.sh`, `cat <<E … bash x.sh … E` (inert heredoc
        # bodies) and `$(echo bash x)` all only print or feed text; while
        # `$(bash x)`, `printf 'x' | sh`, `sh <<E` and `python <x.py`
        # DO run — _command_literal descends into each substitution's own
        # head and checks heredoc/pipe context to tell them apart.
        def literal(pos: int) -> bool:
            return _command_literal(scan, pos)

        # A scripts/ path only invokes when it IS the whole shell word —
        # `python -m scripts'-tools'` concatenates to `scripts-tools`, a
        # different argument (Codex on vendored review). Parens are
        # masked before word-splitting so a path inside `$(...)` splits
        # cleanly. A match inside a QUOTED operand still counts: the
        # quote's content is program/source text the command interprets
        # — `sh -c 'bash x'`, `ssh host 'bash x'`, `sed '1e bash x'`,
        # `awk 'BEGIN{system("bash x")}'` all execute what the quoted
        # argument says.
        enc_words = _shell_words(_mask_parens(enclosing))

        def word_member(epos: int, text: bytes) -> bool:
            w = next((w for w in enc_words if w[0] <= epos < w[1]), None)
            if w is None:
                return False
            raw = enclosing[w[0]:w[1]]
            tw = _word_text(raw)
            # Input-redirect glue stays inside the word (`<scripts/x.py`,
            # `<>…`, `0<…`) — `python <scripts/x.py` reads the script —
            # and so does a TRAILING redirect (`scripts/x.py<in`). Strip
            # both before comparing (Devin on vendored-resolver review).
            m_in = re.match(rb"\d*<>?", tw)
            cand = tw[m_in.end():] if m_in else tw
            cand = cand.split(b"<", 1)[0].split(b">", 1)[0]
            # `$PWD/scripts/x.py`/`${P}/…` and any deeper path tail still
            # names the script — only a bare-prefix concat like
            # `xscripts/` is rejected.
            if cand in (text, b"./" + text) or cand.endswith(b"/" + text):
                return True
            # A glued non-`-d` short-option operand whose tail is a script
            # IS the invocation (`node -rscripts/preload.js`) — the same
            # predicate the option-operand suppressor uses, inverted
            # (Codex on vendored-resolver review).
            if (tw.startswith(b"-") and not tw.startswith(b"--")
                    and len(tw) > 2 and tw[1:2].isalpha()
                    and b"scripts/" in tw
                    and not _glued_short_hides_path(tw)):
                return True
            if raw[:1] in (b"'", b'"'):
                return True
            return any(a <= epos - w[0] < b
                       for a, b in _quoted_spans(raw))

        for n in SCRIPT_NAME.finditer(window):
            if literal(m.start() + n.start()):
                continue
            if any(a <= n.start() and n.end() <= b
                   for a, b in redir_spans):
                continue
            if not word_member(m.start() + n.start() - cs,
                               window[n.start():n.end()]):
                continue
            p = n.group(1).decode("utf-8", errors="ignore")
            if (not re.search(r"\.[A-Za-z0-9_-]+$", p)
                    and any(a <= n.start() and n.end() <= b
                            for a, b in opt_spans)):
                continue
            refs.append(([p], [p]))
        for mod in MODULE_NAME.finditer(window):
            if literal(m.start() + mod.start()):
                continue
            arg_start = (mod.start()
                         + mod.group(0).find(b"scripts"))
            arg_text = (b"scripts." + mod.group(1)
                        if mod.group(1) is not None else b"scripts")
            if not word_member(m.start() + arg_start - cs, arg_text):
                continue
            # `python -m scripts.a.b` runs a/b.py or the package entry
            # a/b/__main__.py — either satisfies the invocation. A bare
            # __init__.py is no entry point: `python -m pkg` needs
            # __main__.py, so only those two paths gate the dep. A bare
            # `python -m scripts` runs scripts/__main__.py directly.
            if mod.group(1) is None:
                refs.append((["__main__.py"], ["__main__.py"]))
            else:
                base = mod.group(1).decode("utf-8", errors="ignore")
                base = base.replace(".", "/")
                refs.append(([base + ".py", base + "/__main__.py"],
                             [base + ".py", base + "/__main__.py"]))
        if not refs:
            continue
        for declared_alts, bundled_probes in refs:
            if any(
                    (name in pinned_scripts
                     if pinned_scripts is not None
                     else (sdir / name).is_file())
                    for name in bundled_probes):
                return True  # bundled dep — resolver cannot satisfy it
            if not any(
                    f"scripts/{name}" in declared
                    or f"./scripts/{name}" in declared
                    or name in declared
                    for name in declared_alts):
                return True  # unbundled + undeclared — would ship broken
    return False


def _stale_skip(repo_root: Path, plan: "Plan", snapshot) -> str | None:
    """rel_dst of a skipped destination that changed since planning, else
    None. An identical-skip's digest enters the lock verbatim — a file
    edited in between must abort the apply rather than record a stale
    ownership entry (consumer review of the vendored resolver)."""
    want_dig = {rel: d for r in plan.resolved
                for rel, d in r["files"].items()}
    want_mode = {rel: m for r in plan.resolved
                 for rel, m in r["exec"].items()}
    for dst, _why in plan.skips:
        rel_dst = dst.relative_to(repo_root).as_posix()
        want = want_dig.get(rel_dst)
        snap = snapshot(dst)
        if (snap is None or want is None
                or hashlib.sha256(snap[0]).hexdigest() != want
                or snap[1] != want_mode.get(rel_dst)):
            return rel_dst
    return None


@dataclass
class Plan:
    writes: list[tuple[Path, Path]] = field(default_factory=list)   # (src, dst)
    skips: list[tuple[Path, str]] = field(default_factory=list)     # (dst, why)
    conflicts: list[tuple[Path, str]] = field(default_factory=list) # (dst, why)
    removals: list[Path] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    resolved: list[dict] = field(default_factory=list)
    # rel_dst -> (src_sha256, plugin_req, src mode, installed mode) for
    # every planned output — cross-plugin output-path collision detection
    # compares SOURCE modes (the last writer must never win silently);
    # the installed mode is what the lock records, which differs from
    # the source's on the adopt-on-match path.
    planned: dict[str, tuple[str, str, int, int]] = field(
        default_factory=dict)
    # rel_dst -> verified source bytes, captured at plan time so --apply
    # writes what was checksummed instead of re-reading a mutable registry.
    payload: dict[str, bytes] = field(default_factory=dict)
    # rel_dst -> (atime_ns, mtime_ns) captured with the payload — apply
    # must never re-stat a registry source that could vanish mid-apply.
    times: dict[str, tuple[int, int]] = field(default_factory=dict)
    # rel_dsts whose destination was ABSENT at plan time. Apply refuses a
    # tracked destination missing since planning only when planning saw
    # it present — an installed file the consumer deleted is a planned
    # restore, not a concurrent edit (Devin on #123).
    absent: set[str] = field(default_factory=set)


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        sys.stderr.write(f"FAIL: manifest not found: {path}\n")
        sys.exit(2)
    try:
        data = _yaml_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        sys.stderr.write(f"FAIL: cannot parse ai-manifest.yaml: {e}\n")
        sys.exit(2)
    if not isinstance(data, dict):
        sys.stderr.write("FAIL: ai-manifest.yaml must be a mapping\n")
        sys.exit(2)
    for key in ("version", "universe", "requires"):
        if key not in data:
            sys.stderr.write(f"FAIL: ai-manifest.yaml missing required key: {key}\n")
            sys.exit(2)
    # bool True and float 1.0 both == 1 in Python — the schema version is
    # a literal int or the manifest is malformed, not merely unsupported.
    if type(data["version"]) is not int or data["version"] != 1:
        sys.stderr.write(f"FAIL: unsupported manifest version: {data['version']}\n")
        sys.exit(2)
    if not isinstance(data["requires"], list):
        sys.stderr.write("FAIL: requires must be a list\n")
        sys.exit(2)
    for i, entry in enumerate(data["requires"]):
        # Each entry must be a {plugin: str, ref: str} map — a non-mapping
        # crashes plan_requirement later, and an unquoted `ref: 1.10` parses
        # as the float 1.1, silently resolving a different requirement.
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("plugin"), str)
                or not isinstance(entry.get("ref"), str)):
            sys.stderr.write(
                f"FAIL: requires[{i}] must map string 'plugin' and 'ref' "
                "(quote numeric-looking refs, e.g. ref: \"1.10\")\n")
            sys.exit(2)
        # A block-scalar ref (`ref: |-`/`ref: >`) may carry the clip
        # newline; the version grammar is whitespace-free, so normalise
        # at the boundary instead of rejecting the valid manifest.
        entry["ref"] = entry["ref"].strip()
    surfaces = data.get("surfaces")
    if surfaces is not None and (
            not isinstance(surfaces, list)
            or not all(isinstance(s, str) for s in surfaces)):
        sys.stderr.write("FAIL: surfaces must be a list of strings\n")
        sys.exit(2)
    return data


def find_registry_root(path: Path) -> Path:
    """Accept a bare registry/ dir or a checkout that contains one."""
    for cand in (path, path / "registry"):
        if (cand / "plugins.json").is_file():
            return cand
    sys.stderr.write(f"FAIL: no registry/plugins.json under {path}\n")
    sys.exit(2)


def load_plugins_index(registry_root: Path) -> dict[tuple[str, str], dict]:
    index = json.loads((registry_root / "plugins.json").read_text(encoding="utf-8"))
    return {(p["scope"], p["name"]): p for p in index.get("plugins", [])}


def version_satisfies(version: str, ref: str) -> bool:
    """Exact semver or caret range (^x.y -> same major, >=).

    tag:/sha: pins never reach here — they are verified against the actual
    checkout revision in plan_requirement, so a checkout at the wrong commit
    cannot silently satisfy a pin."""
    def parse(v: str) -> tuple[int, ...]:
        parts = v.lstrip("v").split(".")
        return tuple(int(p) for p in parts if p.isdigit())

    def pad(t: tuple[int, ...]) -> tuple[int, int, int]:
        return (t + (0, 0, 0))[:3]

    if ref.startswith("^"):
        want = parse(ref[1:])
        # SemVer caret: the upper bound is the first NONZERO component +1 —
        # ^1.4 → <2.0.0, ^0.1 → <0.2.0, ^0.0.3 → <0.0.4. All-zero constraints
        # bound at their declared width: ^0 → <1.0.0, ^0.0 → <0.1.0.
        padded = pad(want)
        upper = None
        for i, c in enumerate(padded):
            if c:
                upper = padded[:i] + (c + 1,)
                break
        if upper is None:
            upper = tuple(1 if j == len(want) - 1 else 0 for j in range(3))
        have = pad(parse(version))
        return pad(want) <= have < upper
    # Exact refs are padded like caret bounds — the grammar accepts x[.y[.z]],
    # so '1.14' must satisfy a plugin reporting '1.14.0'.
    return pad(parse(version)) == pad(parse(ref))


def git_rev(repo: Path, rev: str) -> str | None:
    """Resolve `rev` to a commit sha inside `repo`, or None when the path is
    not a git checkout (or the rev does not exist)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{rev}^{{commit}}"],
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def collect_component_files(plugin_dir: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for comp in COMPONENT_TARGETS:
        d = plugin_dir / comp
        if d.is_dir():
            out[comp] = sorted(p for p in d.rglob("*") if p.is_file())
    return out


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Descriptor-relative file ops (O_DIRECTORY/O_NOFOLLOW + dir_fd) — POSIX
# only; Windows takes the path-based fallback.
_HAS_DIRFD = (os.name == "posix" and hasattr(os, "O_DIRECTORY")
              and hasattr(os, "O_NOFOLLOW") and os.supports_dir_fd
              >= {os.open, os.mkdir, os.rename, os.utime, os.chmod,
                  os.unlink, os.stat})


def secure_dir_fd(root: Path, rel: str) -> int:
    """A dir_fd for `rel` beneath `root`, creating missing components.

    Every component is opened O_DIRECTORY|O_NOFOLLOW relative to the fd of
    the component above it — a symlink planted (or swapped in) at ANY level
    raises instead of letting a later write escape the tree. Callers own
    the returned fd. POSIX-only; callers must gate on _HAS_DIRFD."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in rel.split("/") if rel and rel != "." else []:
            try:
                os.mkdir(part, 0o777, dir_fd=fd)
            except FileExistsError:
                pass
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_source(root: Path, rel: str) -> tuple[bytes, os.stat_result] | None:
    """Read `rel` under `root` without following ANY raced link.

    The resolve-time `resolved_src` check runs BEFORE this read — a
    process that can modify the registry checkout can swap the path (or
    an ANCESTOR dir) for a symlink in between, and `Path.read_bytes()`
    would then ship the link target's bytes (Codex P1 on #1953 and on
    consumer review). Every ancestor is opened `O_DIRECTORY|O_NOFOLLOW`
    descriptor-relative (same walk as destination handling); the leaf
    open uses `O_NOFOLLOW` (`ELOOP` on a swap) + `O_NONBLOCK` (a fifo
    open blocks for a writer BEFORE fstat can reject it). Returns
    (bytes, fstat) — the caller's mode/timestamps come from the SAME
    validated descriptor, so a post-close swap can't mix snapshots
    (CodeRabbit + Codex on #125). None on any failure — the caller
    surfaces a plan conflict."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        if _HAS_DIRFD:
            parts = rel.split("/")
            dfd = os.open(str(root),
                          os.O_RDONLY | os.O_DIRECTORY | nofollow)
            try:
                for part in parts[:-1]:
                    nfd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | nofollow,
                        dir_fd=dfd)
                    os.close(dfd)
                    dfd = nfd
                fd = os.open(parts[-1],
                             os.O_RDONLY | os.O_NONBLOCK | nofollow,
                             dir_fd=dfd)
            finally:
                os.close(dfd)
        else:
            fd = os.open(str(root / rel),
                         os.O_RDONLY | os.O_NONBLOCK | nofollow)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), st
    except OSError:
        return None
    finally:
        os.close(fd)


def _git_dir(repo_root: Path) -> Path | None:
    """The resolved git metadata dir for `repo_root`, or None.

    `git rev-parse --git-dir` handles worktrees (where `.git` is a
    `gitdir:` FILE) and submodule layouts; a directory the caller can
    drop internal state into without dirtying the worktree (Codex P2 on
    #1374 — `.ai/capability-apply.lock` showed up as an
    untracked file in consumer checkouts)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-dir"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    p = Path(out.stdout.decode(errors="replace").strip())
    if not p.is_absolute():
        p = repo_root / p
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if p.is_dir() else None


@contextlib.contextmanager
def _repo_apply_mutex(repo_root: Path):
    """Serialize concurrent `--apply` runs against the same checkout.

    Two resolvers in one working tree interleave a lost update: both
    read the lock, each applies its own plan, and the last
    `atomic_replace` of the lockfile silently drops the other's
    installs (Devin BUG_0001 on #1953). An exclusive flock on
    `capability-apply.lock` under the GIT METADATA dir (untracked by
    design — the consumer worktree stays clean; `.ai/` is the fallback
    for non-git roots) is held across the whole plan→apply window.
    fcntl is POSIX-only — without it the mutex degrades to a no-op
    rather than refusing to run on that platform."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    git_dir = _git_dir(repo_root)
    if git_dir is not None:
        lock_dir = git_dir
    else:
        lock_dir = repo_root / ".ai"
        if lock_dir.is_symlink():
            # Never create the mutex file through a link — the apply's own
            # lockfile-destination check surfaces the conflict downstream;
            # serialize nothing here (there is no trusted dir to lock in).
            yield
            return
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            yield
            return
    try:
        fd = os.open(str(lock_dir / "capability-apply.lock"),
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                     0o644)
    except OSError:
        # lock path unroutable (the dir is a file, or the lock path is
        # itself a link) — the apply's checks will report it; proceed
        # unlocked.
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_replace(dst: Path, fill,
                   times: tuple[int, int] | None = None,
                   mode: int | None = None,
                   dfd: int | None = None) -> None:
    """Install `dst` through an exclusively-created sibling temp + rename.

    tempfile.mkstemp picks a random name with O_EXCL — a consumer cannot
    pre-plant a symlink or hard link there, so writes can never follow a
    link out of the tree. os.replace then unlinks any existing dst entry,
    so a destination hard-linked to a file outside the owned tree keeps
    its shared inode (and the external peer) untouched. Mode/times land on
    the temp BEFORE the swap — a metadata failure leaves the old
    destination intact rather than publishing a 0600 temp, and no
    post-rename chmod ever follows a swapped-in symlink.

    When `dfd` is given (from secure_dir_fd), every syscall is relative to
    the held-open verified directory — even a parent dir swapped for a
    symlink between planning and apply cannot redirect the write.

    `times` is the (atime_ns, mtime_ns) snapshot captured at PLAN time —
    apply must never re-stat the registry source: a source that
    disappears mid-apply would otherwise strand already-written files
    with no lock record."""
    if dfd is not None:
        # Descriptor-relative temp — same O_EXCL guarantee as mkstemp but
        # bound to the held-open parent instead of a rewalked path.
        while True:
            tmp_name = f".{dst.name}.{os.urandom(8).hex()}.tmp"
            try:
                tfd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                              0o600, dir_fd=dfd)
                break
            except FileExistsError:
                continue
        try:
            with os.fdopen(tfd, "wb") as f:
                fill(f)
            if times is not None:
                os.utime(tmp_name, ns=times, dir_fd=dfd,
                         follow_symlinks=False)
            if mode is not None:
                os.chmod(tmp_name, mode & 0o777, dir_fd=dfd,
                         follow_symlinks=False)
            os.rename(tmp_name, dst.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            try:
                os.unlink(tmp_name, dir_fd=dfd)
            except OSError:
                pass
        return
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent,
                                    prefix=f".{dst.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            fill(f)
        if times is not None:
            # Plan-time snapshot — copystat-style xattrs (including
            # security.capability) never propagate since times and mode
            # are set explicitly.
            os.utime(tmp, ns=times)
        if mode is not None:
            # The PLANNED mask — the source's mode may have drifted between
            # plan and apply; the installed file must match the lock record.
            os.chmod(tmp, mode & 0o777)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def plan_requirement(req: str, ref: str, universe: str, registry_root: Path,
                     index: dict, repo_root: Path, locked: dict, plan: Plan,
                     write_components: bool = True,
                     locked_exec: dict | None = None,
                     locked_prov: dict | None = None) -> None:
    locked_exec = locked_exec or {}
    locked_prov = locked_prov or {}
    m = REQUIRES_RE.match(req)
    if not m:
        plan.conflicts.append((repo_root / req, "malformed plugin reference"))
        return
    scope, name = m.group(1), m.group(2)

    # Fail-closed scope check — a repo can only reach platform, its own
    # universe, and the local scopes.
    allowed = {"platform", universe} | LOCAL_SCOPES
    if scope not in allowed:
        plan.conflicts.append((
            repo_root / req,
            f"scope '{scope}' is not reachable from universe '{universe}' "
            f"(allowed: platform, {universe}, repo, personal) — refusing",
        ))
        return
    if scope in LOCAL_SCOPES:
        plan.advisories.append(
            f"{req}: repo/personal assets are materialised in place — nothing to fetch")
        return

    pinned = ref.startswith(("sha:", "tag:"))
    if pinned:
        # Verify the pin BEFORE trusting the index — plugins.json is itself a
        # resolution input, and a dirty index could retarget a scope/name.
        want = ref.split(":", 1)[1]
        if ref.startswith("sha:"):
            # sha: must be a literal commit id — rev-parse accepts arbitrary
            # expressions, so sha:HEAD would otherwise always pass.
            if not re.fullmatch(r"[0-9a-fA-F]{7,40}", want):
                plan.conflicts.append((repo_root / req,
                    f"pinned ref '{ref}' — sha: requires a hexadecimal "
                    "commit id"))
                return
            pin_rev = want
        else:
            # git rev-parse accepts revision operators — tag:v2~1 would
            # resolve to refs/tags/v2's PARENT, not a tag named v2~1.
            # check-ref-format rejects ~ ^ : ? * [ \ spaces and "..".
            bad_name = subprocess.run(
                ["git", "check-ref-format", f"tags/{want}"],
                capture_output=True, timeout=10)
            if bad_name.returncode != 0:
                plan.conflicts.append((
                    repo_root / req,
                    f"pinned ref '{ref}' — tag: requires a valid git tag "
                    "name (no revision operators, '..', or spaces)",
                ))
                return
            # tag: resolves strictly under refs/tags/ — tag:main must not
            # satisfy against a branch.
            pin_rev = f"refs/tags/{want}"
        # The registry must BE the checkout (or its registry/ dir) —
        # rev-parse under a registry copied beneath an unrelated repo
        # resolves against the PARENT's refs, verifying the pin against
        # a tree it does not describe.
        top_r = subprocess.run(
            ["git", "-C", str(registry_root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10)
        if top_r.returncode != 0:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' needs a verifiable git checkout — "
                "the registry source is not a git repository",
            ))
            return
        rr = registry_root.resolve()
        top = Path(top_r.stdout.strip()).resolve()
        if rr != top and rr != top / "registry":
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' needs the registry at a checkout root "
                "or its registry/ dir — a registry nested inside another "
                "repository would verify the pin against the wrong tree",
            ))
            return
        head = git_rev(registry_root, "HEAD")
        if head is None:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' needs a verifiable git checkout — "
                "the registry source is not a git repository",
            ))
            return
        pinned_sha = git_rev(registry_root, pin_rev)
        if pinned_sha is None:
            kind = "tag" if ref.startswith("tag:") else "commit"
            plan.conflicts.append((
                repo_root / req,
                f"pinned {kind} '{want}' does not resolve in the registry "
                "checkout",
            ))
            return
        if pinned_sha != head:
            plan.conflicts.append((
                repo_root / req,
                f"registry checkout is not at the pinned ref '{want}' "
                f"(HEAD {head[:12]}) — check out the pin or use a version range",
            ))
            return
        # HEAD may equal the pin while the worktree is dirty — materialising
        # would copy uncommitted bytes while recording the pinned ref. The
        # check covers the whole registry: plugins.json and every plugin
        # tree are pin inputs.
        dirty = subprocess.run(
            ["git", "-C", str(registry_root), "status", "--porcelain",
             "--untracked-files=all", "--", "."],
            capture_output=True, text=True, timeout=10)
        if dirty.returncode != 0:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' cannot verify worktree cleanliness — "
                "git status failed; refusing to record a pin over "
                "unverifiable bytes",
            ))
            return
        if dirty.stdout.strip():
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' requires a clean worktree — uncommitted "
                "changes under the registry (index and plugin content are "
                "pin inputs)",
            ))
            return
        # porcelain is blind to skip-worktree / assume-unchanged edits —
        # the catalog is a resolution input, so verify the worktree copy
        # byte-for-byte against the pinned object before trusting `index`.
        cat = subprocess.run(
            ["git", "-C", str(registry_root), "show",
             f"{pinned_sha}:./plugins.json"],
            capture_output=True, timeout=10)
        if cat.returncode != 0:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' cannot verify the plugin catalog — "
                "git show <pin>:./plugins.json failed; refusing to resolve "
                "an index the pin cannot vouch for",
            ))
            return
        try:
            live_catalog = (registry_root / "plugins.json").read_bytes()
        except OSError:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}': registry/plugins.json unreadable",
            ))
            return
        if cat.stdout != live_catalog:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}': registry/plugins.json differs from "
                "the pinned object (skip-worktree/assume-unchanged hides "
                "worktree edits from git status) — refusing to install a "
                "catalog the pin never published",
            ))
            return
    else:
        body = ref[1:] if ref.startswith("^") else ref
        if not SEMVER_REF.fullmatch(body):
            plan.conflicts.append((repo_root / req, f"malformed ref '{ref}' — "
                "supported: ^x[.y[.z]], x[.y[.z]], tag:<tag>, sha:<sha>"))
            return

    entry = index.get((scope, name))
    if entry is None:
        plan.conflicts.append((repo_root / req, "not in registry/plugins.json"))
        return
    plugin_dir = registry_root.parent / entry["path"] if not Path(entry["path"]).is_absolute() else Path(entry["path"])
    if not plugin_dir.is_dir():
        plan.conflicts.append((repo_root / req, f"missing plugin dir: {entry['path']}"))
        return
    try:
        # Canonical paths both sides — with a symlinked registry ancestor
        # the lexical relative_to and the resolved tree disagree, so a
        # crafted path could read as inside while resolving outside.
        indexed_rel = plugin_dir.resolve().relative_to(
            registry_root.resolve())
    except ValueError:
        plan.conflicts.append((repo_root / req,
            f"indexed path '{entry['path']}' is outside the registry"))
        return
    if indexed_rel.parts != (scope, name):
        plan.conflicts.append((repo_root / req,
            f"indexed path '{entry['path']}' does not match the requested "
            f"scope/name '{scope}/{name}' — refusing to alias"))
        return
    if plugin_dir.resolve() != plugin_dir:
        # A symlinked plugin dir (or ancestor) aliases another scope's tree —
        # materialising it would distribute the target's content under this
        # plugin's name. Registry lint rejects the same shape at the gate.
        plan.conflicts.append((
            repo_root / req,
            "plugin dir contains a symlink — refusing to materialise through it",
        ))
        return

    manifest_file = plugin_dir / ".claude-plugin" / "plugin.json"
    if pinned:
        # The manifest feeds resolved_version + the lock's sha256 — it is a
        # pin input like any component file, so verify it against the pinned
        # git object too (skip-worktree on THIS file passes every other check
        # while recording modified metadata under the pin's name).
        rel_man = manifest_file.relative_to(registry_root).as_posix()
        try:
            man_blob = subprocess.run(
                ["git", "-C", str(registry_root), "show",
                 f"{pinned_sha}:./{rel_man}"],
                capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            man_blob = None
        if (man_blob is None or man_blob.returncode != 0
                or not manifest_file.is_file()
                or man_blob.stdout != manifest_file.read_bytes()):
            plan.conflicts.append((
                repo_root / req,
                f"{req}: {rel_man} differs from or is absent at the pinned "
                "revision — refusing to record pin metadata from unverifiable bytes",
            ))
            return
    version = None
    if manifest_file.is_file():
        try:
            version = json.loads(manifest_file.read_text(encoding="utf-8"))["version"]
        except (json.JSONDecodeError, KeyError):
            version = None
    # The manifest version is a resolution input: absent/malformed/non-semver
    # values must conflict — silently defaulting to 0.0.0 lets `ref: "0"`
    # satisfy a manifest-less plugin, and parse()'s permissive digit filter
    # would let "1.bad.2" satisfy "1.2".
    if (not isinstance(version, str)
            or not SEMVER_REF.fullmatch(version)):
        plan.conflicts.append((
            repo_root / req,
            f"{req}: .claude-plugin/plugin.json missing, unreadable, or "
            f"carries non-semver version {version!r} — refusing unverifiable "
            "resolution",
        ))
        return
    if not pinned and not version_satisfies(version, ref):
        plan.conflicts.append((
            repo_root / req,
            f"ref '{ref}' not satisfied by registry version {version}",
        ))
        return

    materialised: dict[str, str] = {}
    exec_modes: dict[str, int] = {}
    component_files = (collect_component_files(plugin_dir)
                       if write_components else {})
    pinned_scripts: set[str] | None = None
    pinned_modes: dict[str, str] = {}
    if pinned:
        # Worktree enumeration alone is not authoritative under a pin: a
        # tracked component DELETED while marked skip-worktree leaves status
        # clean and simply never appears in the rglob — the per-file git show
        # would never run on it, materialising an incomplete plugin under the
        # pin's name. Compare the git TREE's file list for each component dir
        # against what the worktree actually holds — enumerated independently
        # of component_files since surface selection may leave that empty.
        # Extra worktree files are already refused per-file below (untracked
        # → no pinned object).
        verify_files = collect_component_files(plugin_dir)
        for comp in COMPONENT_TARGETS:
            rel_dir = (plugin_dir / comp).relative_to(registry_root).as_posix()
            try:
                tree = subprocess.run(
                    ["git", "-C", str(registry_root), "ls-tree", "-r",
                     pinned_sha, "--", rel_dir],
                    capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                tree = None
            if tree is None or tree.returncode != 0:
                plan.conflicts.append((
                    repo_root / req,
                    f"pinned ref '{ref}' cannot enumerate the tracked plugin "
                    "tree — refusing to record a pin over unverifiable state",
                ))
                return
            # '<mode> <type> <sha>\t<path>' — modes are pin inputs too: a
            # skip-worktree exec-bit flip passes the blob comparison while
            # copystat ships the wrong permissions under the pin's name.
            tracked = set()
            for ln in tree.stdout.splitlines():
                if not ln:
                    continue
                meta, _, path = ln.partition("\t")
                tracked.add(path)
                pinned_modes[path] = meta.split(" ", 1)[0]
            present = {
                src.relative_to(registry_root).as_posix()
                for src in verify_files.get(comp, [])
            }
            missing = sorted(tracked - present)
            if missing:
                plan.conflicts.append((
                    repo_root / req,
                    f"{req}: {missing[0]} is tracked at the pinned revision "
                    "but absent from the worktree (skip-worktree deletion?) "
                    "— refusing an incomplete pin",
                ))
                return
        # The bundled-script check inside script_dep_block is a dep decision
        # the pin must also own: read scripts/ from the pinned git tree so an
        # ignored worktree plant or index-hidden deletion cannot flip it
        # while the lock records the same pin.
        rel_sdir = (plugin_dir / "scripts").relative_to(registry_root).as_posix()
        try:
            stree = subprocess.run(
                ["git", "-C", str(registry_root), "ls-tree", "-r",
                 "--name-only", pinned_sha, "--", rel_sdir],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            stree = None
        if stree is None or stree.returncode != 0:
            plan.conflicts.append((
                repo_root / req,
                f"pinned ref '{ref}' cannot enumerate the pinned scripts tree "
                "— refusing to make dep decisions on unverifiable state",
            ))
            return
        pinned_scripts = {
            ln[len(rel_sdir) + 1:] for ln in stree.stdout.splitlines()
            if ln and ln.startswith(rel_sdir + "/")
        }
    for comp, files in component_files.items():
        target_root = repo_root / COMPONENT_TARGETS[comp]
        for src in files:
            rel = src.relative_to(plugin_dir / comp)
            dst = target_root / rel
            rel_dst = dst.relative_to(repo_root).as_posix()
            resolved_src = src.resolve()
            if (resolved_src != src
                    or not resolved_src.is_relative_to(plugin_dir)):
                # A symlinked component file (or one behind a linked dir)
                # copies the link target's bytes into the consumer — e.g.
                # agents/leak.md -> /etc/passwd ships /etc/passwd.
                plan.conflicts.append((
                    dst,
                    f"{req}: {rel} is or resolves through a symlink — "
                    "refusing to materialise the link target"))
                continue
            got = _read_source(plugin_dir / comp, rel.as_posix())
            if got is None:
                plan.conflicts.append((
                    dst,
                    f"{req}: {rel} cannot be read without following a "
                    "link or non-regular file — refusing to materialise "
                    "unverifiable bytes"))
                continue
            src_bytes, src_st = got
            if pinned:
                # Compare against the pinned git OBJECT, not the index or
                # status output: --untracked-files=all is blind to ignored
                # files, and skip-worktree / assume-unchanged index flags
                # let a tracked file's modified worktree bytes pass both
                # checks while not being what the pinned revision holds.
                rel_src = src.relative_to(registry_root).as_posix()
                try:
                    blob = subprocess.run(
                        ["git", "-C", str(registry_root), "show",
                         f"{pinned_sha}:./{rel_src}"],
                        capture_output=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    plan.conflicts.append((
                        dst,
                        f"{req}: {rel} cannot be verified against the "
                        "pinned revision — git show failed",
                    ))
                    continue
                if blob.returncode != 0:
                    plan.conflicts.append((
                        dst,
                        f"{req}: {rel} is not tracked at the pinned revision "
                        "(ignored or untracked) — refusing to materialise "
                        "bytes outside the pin",
                    ))
                    continue
                if blob.stdout != src_bytes:
                    plan.conflicts.append((
                        dst,
                        f"{req}: {rel} differs from the pinned git object "
                        "(index flags like skip-worktree can hide the "
                        "divergence) — refusing to materialise",
                    ))
                    continue
                if ((pinned_modes.get(rel_src) == "100755")
                        != bool(src_st.st_mode & 0o111)):
                    plan.conflicts.append((
                        dst,
                        f"{req}: {rel} exec bit differs from the pinned git "
                        "tree (a skip-worktree mode flip hides it from "
                        "status) — refusing to materialise",
                    ))
                    continue
            if (b"CLAUDE_PLUGIN_ROOT" in src_bytes
                    or declares_script_deps(src_bytes)
                    or script_dep_block(plugin_dir, src_bytes,
                                        pinned_scripts)):
                # Files depending on the plugin install root or on sibling
                # scripts/ cannot run in a resolver install — the resolver
                # does not materialise scripts (surface wiring is a later
                # phase). Shipping them would document commands/skills that
                # fail on invoke.
                plan.advisories.append(
                    f"{req}: {rel} depends on the plugin root or scripts/ — "
                    f"not runnable in resolver mode (needs marketplace install "
                    f"or script wiring); not materialised")
                continue
            # Any symlink in the destination chain — dst itself or an ancestor
            # — makes mkdir/copy2 write through it: outside the repo or across
            # to another locked capability. resolve() != dst proves a link
            # exists regardless of where it points; refuse to write through it.
            if dst.resolve() != dst:
                plan.conflicts.append((
                    dst,
                    "destination path contains a symlink — refusing to materialise "
                    "through it (replace the link with a real directory)",
                ))
                continue
            # Type-check ancestors too — .claude/agents as a plain FILE is
            # not a symlink, passes the checks above, and dst.exists() is
            # False for its children: --apply would copy every earlier file
            # then crash at mkdir(), leaving them materialised without a
            # lock. Reject non-directory ancestors at plan time.
            file_ancestor = None
            for anc in dst.parents:
                if anc == repo_root:
                    break
                if anc.exists() and not anc.is_dir():
                    file_ancestor = anc
                    break
            if file_ancestor is not None:
                plan.conflicts.append((
                    dst,
                    f"destination ancestor {file_ancestor.relative_to(repo_root)} "
                    "is not a directory — refusing to materialise through it",
                ))
                continue
            src_sha = hashlib.sha256(src_bytes).hexdigest()
            if pinned:
                # Under a pin the recorded mode is the pinned TREE's — a
                # worktree chmod on the r/w bits (0644 -> 0600) hides from
                # status AND the blob compare but is not what the pin
                # holds; normalising keeps the install + lock record
                # pin-derived instead of unverified-worktree-derived.
                src_mode = (0o755 if pinned_modes.get(rel_src) == "100755"
                            else 0o644)
                src_exec = src_mode & 0o111
            else:
                src_exec = src_st.st_mode & 0o111
                src_mode = (src_st.st_mode & 0o666) | src_exec
            src_times = (src_st.st_atime_ns, src_st.st_mtime_ns)
            prior = plan.planned.get(rel_dst)
            if prior is not None:
                prior_sha, prior_req, prior_src_mode, prior_install = prior
                # Collide on the providers' SOURCE modes — identical bytes
                # with different masks are order-dependent output. The
                # installed mask is NOT the comparator: an adopted file's
                # entry holds the consumer's own mode, and comparing it
                # against a second provider's source would collide two
                # plugins that agree with each other.
                if prior_sha != src_sha or prior_src_mode != src_mode:
                    plan.conflicts.append((
                        dst,
                        f"output-path collision: {req} provides different content or "
                        f"mode for this path than {prior_req} — refusing to pick a winner",
                    ))
                else:
                    plan.skips.append((dst, f"identical — already provided by {prior_req}"))
                    materialised[rel_dst] = src_sha
                    # The installed file carries the FIRST provider's
                    # install mode — record that, not this provider's.
                    exec_modes[rel_dst] = prior_install
                continue
            # planned keys are file paths only: one plugin shipping
            # .claude/x while another ships .claude/x/y escapes the
            # exact-key check above — --apply would write the file then
            # fail mkdir() on the descendant mid-run, unlocked.
            overlap = next(
                (p for p in plan.planned
                 if p.startswith(rel_dst + "/")
                 or rel_dst.startswith(p + "/")),
                None)
            if overlap is not None:
                plan.conflicts.append((
                    dst,
                    f"output-path collision: {req} plans {rel_dst} which "
                    f"overlaps {overlap} (file vs directory prefix) — "
                    "refusing to materialise",
                ))
                continue
            if not dst.exists():
                plan.absent.add(rel_dst)
            if dst.exists() and not dst.is_file():
                # A directory (or FIFO/socket) at the destination —
                # read_bytes() would crash IsADirectoryError instead of
                # reporting a fail-closed conflict.
                plan.conflicts.append((
                    dst,
                    "destination exists as a non-regular file — refusing to "
                    "overwrite it (remove the directory and re-resolve)",
                ))
                continue
            if dst.exists():
                # 'identical' means bytes AND the any-exec state match — a
                # registry file that gained/lost +x must fall through to the
                # locked drift-repair path, not skip with stale permissions.
                # Exec class is the identity signal here; the full mask is
                # checked separately against the lock record below.
                dst_exec = dst.stat().st_mode & 0o111
                same_exec = bool(dst_exec) == bool(src_exec)
                bytes_match = dst.read_bytes() == src_bytes
                # For a tracked destination the on-disk mode must also match
                # the installed-mode record — a consumer chmod that
                # coincides with a registry chmod is still a local edit.
                rec_exec = locked_exec.get(rel_dst)
                rec_tagged = (isinstance(rec_exec, int)
                              and not isinstance(rec_exec, bool)
                              and rec_exec >= EXEC_TAG)
                exec_consistent = (rel_dst not in locked or rec_exec is None
                                   or _exec_matches(rec_exec,
                                                    dst.stat().st_mode))
                # 'identical' also requires the SOURCE's current mode to be
                # the installed-mode record: a permission-only registry
                # change (0644 -> 0444, same bytes) is registry mode drift,
                # not identity — falling through to the rewrite path keeps
                # the installed copy and the lock record in step. This only
                # gates files the resolver WROTE (in install_prov): their
                # record encodes the source's mode. An adopted file's record
                # encodes the consumer's own mode — comparing it against src
                # would schedule a permanent clobber of a file the consumer
                # owns. A TAGGED record carries the full mask; a legacy
                # untagged one only proves any-exec — under it, r/w-bit
                # drift can't be attributed, so identical requires the full
                # dst mask to equal the source's: ratify neither a possible
                # consumer chmod nor a silent rewrite that would revert it.
                mode_consistent = (
                    rel_dst not in locked or rec_exec is None
                    or rel_dst not in locked_prov
                    or (rec_tagged and _exec_matches(rec_exec, src_mode))
                    or (not rec_tagged
                        and (dst.stat().st_mode & 0o777)
                            == (src_mode & 0o777)))
                if (bytes_match and same_exec and exec_consistent
                        and mode_consistent
                        and rel_dst in locked
                        and locked[rel_dst] is not None
                        and locked[rel_dst] != src_sha):
                    # dst's bytes changed since install — they merely
                    # coincide with the new registry content. Recording
                    # the new digest would claim resolver ownership of a
                    # local edit and let a later --prune delete it; fail
                    # closed and let the operator decide instead.
                    plan.conflicts.append((
                        dst,
                        "materialised file modified since install — its "
                        "bytes now match the registry source, but the local "
                        "edit was never resolver-installed; refusing to "
                        "record ownership of it (delete the file and "
                        "re-resolve, or restore the installed bytes)",
                    ))
                    continue
                if (bytes_match and same_exec and exec_consistent
                        and mode_consistent):
                    plan.skips.append((dst, "identical"))
                    # The lock records the mode of the file actually on
                    # disk — dst's real mask, not the registry source's.
                    # For an adopted untracked file dst is the only truth;
                    # for a tracked identical file dst == record == src.
                    dst_mode = dst.stat().st_mode & 0o777
                    plan.planned[rel_dst] = (src_sha, req, src_mode,
                                             dst_mode)
                    materialised[rel_dst] = src_sha
                    exec_modes[rel_dst] = dst_mode
                elif rel_dst not in locked:
                    plan.conflicts.append((
                        dst,
                        "exists and differs — not lockfile-tracked, refusing to clobber a hand edit",
                    ))
                    continue
                elif locked[rel_dst] is None or sha256(dst) != locked[rel_dst]:
                    plan.conflicts.append((
                        dst,
                        "materialised file modified since install — refusing to clobber a hand "
                        "edit (restore it or delete it and re-resolve)",
                    ))
                    continue
                elif locked_prov.get(rel_dst) in (None, "unknown"):
                    # ADOPTED, or locked before provenance existed: nothing
                    # proves this resolver ever wrote the file — a legacy
                    # lock recorded adopted and installed files alike.
                    # Registry drift must not rewrite a possibly
                    # consumer-owned file nor stamp fresh provenance over
                    # it (a later --prune would then delete it).
                    plan.conflicts.append((
                        dst,
                        "file not provably resolver-installed — the "
                        "registry version changed; delete the file and "
                        "re-resolve to take the registry version, or "
                        "drop the requirement to keep yours",
                    ))
                    continue
                elif (not same_exec or not exec_consistent
                      or not mode_consistent):
                    # Bytes match the install record but the mode state
                    # differs from the source and/or the record — the lock's
                    # installed-mode record distinguishes a local chmod
                    # (refuse) from a registry mode change (repair by
                    # rewriting). No record means the drift cannot be
                    # attributed — fail closed.
                    if (rec_exec is None
                            or not _exec_matches(rec_exec,
                                                 dst.stat().st_mode)):
                        plan.conflicts.append((
                            dst,
                            "exec mode differs from the installed-mode record "
                            "— refusing to clobber a possible local chmod "
                            "(restore the mode or delete the file and "
                            "re-resolve)",
                        ))
                        continue
                    if (not rec_tagged and same_exec
                            and (dst.stat().st_mode & 0o777)
                                != (src_mode & 0o777)):
                        # A legacy any-exec record can't attribute
                        # read/write-bit drift — it only proves the exec
                        # class matched at install. Rewriting to the
                        # registry mask could revert a consumer chmod,
                        # recording 'identical' would ratify it: refuse.
                        plan.conflicts.append((
                            dst,
                            "mode drift beyond the legacy exec record's "
                            "any-executable tracking — the difference "
                            "cannot be attributed (restore the mode or "
                            "delete the file and re-resolve)",
                        ))
                        continue
                    else:
                        # dst mode still matches the install record — the
                        # registry changed mode; repair by rewriting.
                        plan.writes.append((src, dst))
                        plan.planned[rel_dst] = (src_sha, req, src_mode,
                                                 src_mode)
                        plan.payload[rel_dst] = src_bytes
                        plan.times[rel_dst] = src_times
                else:
                    plan.writes.append((src, dst))  # registry drift — update
                    plan.planned[rel_dst] = (src_sha, req, src_mode,
                                             src_mode)
                    plan.payload[rel_dst] = src_bytes
                    plan.times[rel_dst] = src_times
                materialised[rel_dst] = src_sha
                # The identical-skip branch records dst's real mask; every
                # other path installs the source's.
                exec_modes.setdefault(rel_dst, src_mode)
            else:
                plan.writes.append((src, dst))
                plan.planned[rel_dst] = (src_sha, req, src_mode, src_mode)
                plan.payload[rel_dst] = src_bytes
                plan.times[rel_dst] = src_times
                materialised[rel_dst] = src_sha
                exec_modes[rel_dst] = src_mode

    for comp in ("hooks", "scripts", "data", "telemetry"):
        if (plugin_dir / comp).is_dir():
            plan.advisories.append(
                f"{req}: '{comp}/' needs surface wiring (settings merge) — "
                f"not materialised by --apply; see docs/registry.md")

    plan.resolved.append({
        "plugin": req,
        "scope": scope,
        "ref": ref,
        "resolved_version": version,
        "source": entry["path"],
        "files": materialised,
        "exec": exec_modes,
        "sha256": sha256(manifest_file) if manifest_file.is_file() else None,
    })


def load_lock(repo_root: Path) -> tuple[dict, str | None]:
    """(lock doc, error) — a lock that exists but can't be parsed must never
    masquerade as an empty ownership map: --check would report OK while
    resolver-installed files stay behind, and the next --apply would
    overwrite the lock and lose their ownership permanently."""
    p = repo_root / LOCK_PATH
    if not p.is_file():
        return {}, None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {}, f"unparseable JSON: {e}"
    if not isinstance(doc, dict):
        return {}, "not a JSON object"
    # Structure validation — a syntactically valid but wrongly-typed lock
    # ({"files": null}, resolved:[1,2]) must fail closed with a repairable
    # conflict, not a TypeError traceback deep in locked_digests.
    files = doc.get("files")
    if "files" in doc:
        if isinstance(files, dict):
            if not all(isinstance(k, str)
                       and (v is None or isinstance(v, str))
                       for k, v in files.items()):
                return {}, "'files' entries must map paths to digests"
        elif isinstance(files, list):
            if not all(isinstance(f, str) for f in files):
                return {}, "'files' list entries must be path strings"
        else:
            return {}, "'files' must be an object or a list"
    resolved = doc.get("resolved")
    if "resolved" in doc and (
            not isinstance(resolved, list)
            or not all(isinstance(r, dict) for r in resolved)):
        return {}, "'resolved' must be a list of plugin objects"
    if isinstance(resolved, list):
        # Nested 'files' maps inside resolved entries feed locked_digests —
        # {"resolved":[{"files":null}]} is valid JSON that TypeErrors there.
        for r in resolved:
            if "files" not in r:
                continue
            rf = r["files"]
            if isinstance(rf, dict):
                ok = all(isinstance(k, str)
                         and (v is None or isinstance(v, str))
                         for k, v in rf.items())
            elif isinstance(rf, list):
                ok = all(isinstance(f, str) for f in rf)
            else:
                ok = False
            if not ok:
                return {}, ("resolved entry 'files' must map paths to "
                            "digests or list path strings")
    provenance = doc.get("provenance")
    if "provenance" in doc and (
            not isinstance(provenance, dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in provenance.items())):
        return {}, "'provenance' must map repo-relative paths to plugin names"
    execmap = doc.get("exec")
    if "exec" in doc and (
            not isinstance(execmap, dict)
            or not all(isinstance(k, str) and isinstance(v, int)
                       for k, v in execmap.items())):
        return {}, "'exec' must map repo-relative paths to exec masks (int)"
    return doc, None


def lock_provenance(lock: dict) -> dict[str, str]:
    """rel path -> the plugin req that installed it, for prune attribution.

    The 'provenance' map is the authority when present — locks written by
    provenance-aware code deliberately omit adopted-on-match files (a file
    the resolver never wrote is never prune-eligible). Backfill from
    resolved[].files only for locks that predate the field. A path present
    only in the top-level 'files' map (a forged or hand-written entry) has
    NO provenance — it is released from tracking, never pruned."""
    prov = lock.get("provenance")
    if isinstance(prov, dict):
        return dict(prov)
    out: dict[str, str] = {}
    # A legacy lock records digests for resolver-written AND adopted-on-
    # match files alike — resolved[].files cannot tell them apart either,
    # so every backfilled entry is 'unknown': prune refuses to unlink what
    # it cannot prove this resolver wrote.
    for r in lock.get("resolved", []):
        rf = r.get("files", {}) if isinstance(r, dict) else {}
        keys = rf.keys() if isinstance(rf, dict) else (
            rf if isinstance(rf, list) else ())
        for k in keys:
            if isinstance(k, str):
                out.setdefault(k, "unknown")
    # Locks written by the shipped resolver before 'provenance' existed hold
    # all ownership in the top-level 'files' map — their resolved[] entries
    # were already serialised without 'files'. Attribute those entries
    # (bounded to resolver-owned roots) so their files stay prune-eligible.
    # The shape is gated on a non-empty resolved[] so a minimal files-only
    # lock (v0/v1 or forged claim) keeps the fail-closed release treatment.
    files_map = lock.get("files")
    resolved_entries = lock.get("resolved")
    if (isinstance(files_map, dict) and isinstance(resolved_entries, list)
            and resolved_entries):
        for k in files_map:
            if (isinstance(k, str)
                    and "/".join(k.split("/")[:2]) in OWNED_ROOTS):
                out.setdefault(k, "unknown")
    return out


def lock_exec_modes(lock: dict) -> dict[str, int]:
    """rel path -> installed mode record, for chmod-drift attribution: the
    lock records the mode the resolver materialised, so a local chmod
    (dst mode != record) is distinguishable from a registry mode change
    (dst mode == record != src mode). Records written by this resolver
    are tagged (EXEC_TAG | full mask); legacy bool/int values are kept
    as-is — they express only an on/off exec state, so drift against
    them compares any-exec or conflicts rather than guesses."""
    execmap = lock.get("exec")
    return dict(execmap) if isinstance(execmap, dict) else {}


def _exec_matches(rec, mode: int) -> bool:
    """Match an installed-mode record against a live st_mode.

    Records this resolver writes are EXEC_TAG-tagged full masks — the
    tag exists so a genuine installed mode of 0 or 0o111 is not mistaken
    for a legacy record. Legacy records — bools and the old any-exec
    masks 0o111/0 — express only an on/off state (git reproduces
    100644/100755 and the umask picks the actual bits), so they compare
    by any-exec."""
    if (isinstance(rec, int) and not isinstance(rec, bool)
            and rec >= EXEC_TAG):
        return (mode & 0o777) == (rec & 0o777)
    return bool(mode & 0o111) == bool(rec)


def _exec_provable(rec, mode: int) -> bool:
    """A prune decision needs the FULL installed mask — a tagged record —
    not the legacy any-exec approximation, and not 'no record at all':
    only a tagged record that still matches the on-disk mode proves the
    file's permissions were never touched since install."""
    return (isinstance(rec, int) and not isinstance(rec, bool)
            and rec >= EXEC_TAG and (rec & 0o777) == (mode & 0o777))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default="ai-manifest.yaml")
    ap.add_argument("--registry", required=True,
                    help="path to a checkout containing registry/ (e.g. an ai-starter-pack clone)")
    ap.add_argument("--repo-root", default=".")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    # Explicit no-mutate spelling for wrappers/CI — dry run is the default
    # (absence of --apply/--check), but callers must not have to rely on an
    # implicit mode.
    mode.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="with --apply, also remove lockfile-tracked files no longer required")
    args = ap.parse_args()
    if args.prune and not args.apply:
        ap.error("--prune requires --apply")

    repo_root = Path(args.repo_root).resolve()
    if args.apply:
        # The mutex must be taken BEFORE the lock is read — the plan
        # that applies is the one the lock snapshot describes.
        with _repo_apply_mutex(repo_root):
            return _run(args, repo_root)
    return _run(args, repo_root)


def _run(args: argparse.Namespace, repo_root: Path) -> int:
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path
    manifest = load_manifest(manifest_path)
    universe = str(manifest["universe"])
    registry_root = find_registry_root(Path(args.registry).resolve())
    index = load_plugins_index(registry_root)
    lock, lock_err = load_lock(repo_root)

    locked_dig = locked_digests(lock)
    plan = Plan()
    # claude-code is the only implemented surface — anything else the
    # manifest selects is advisory until its renderer lands, and a manifest
    # that selects no claude-code must materialise no .claude/ output at all.
    surfaces = manifest.get("surfaces")
    claude_selected = not isinstance(surfaces, list) or "claude-code" in surfaces
    if isinstance(surfaces, list):
        for s in surfaces:
            if s != "claude-code":
                plan.advisories.append(
                    f"surface '{s}' is advisory-only — no renderer yet "
                    "(claude-code is the only implemented surface)")
    install_prov = lock_provenance(lock)
    locked_exec = lock_exec_modes(lock)
    for req in manifest["requires"]:
        plan_requirement(req["plugin"], req["ref"], universe,
                         registry_root, index, repo_root, locked_dig, plan,
                         write_components=claude_selected,
                         locked_exec=locked_exec,
                         locked_prov=install_prov)

    # Orphan detection: lockfile files no longer required. Without --prune an
    # orphan is simply kept (and stays lockfile-tracked) — modified or not.
    # Under --prune a file whose on-disk digest differs from the installed
    # digest (or whose install digest is unknown — v1 locks) may be a hand
    # edit: never unlink it silently, surface it as a conflict.
    current = set()
    for r in plan.resolved:
        current.update(r["files"])
    for f in sorted(locked_dig):
        if f in current:
            continue
        # Containment: the lockfile is data, not authority — a poisoned or
        # legacy-v1 entry (absolute path, .. escape, or anything outside the
        # resolver-owned subtrees — .claude/{skills,agents,commands}) must
        # never steer an unlink outside them. .claude/settings.json and
        # friends are not resolver-owned. Resolve it and fail closed.
        lexical = repo_root / f
        candidate = lexical.resolve()
        # The entry itself may still be a symlink — unlink removes the link,
        # never its target. But a symlink ANCESTOR (.claude/skills/alias ->
        # ../agents) makes unlink traverse the link and delete another
        # capability's file; the parent chain must resolve to itself.
        parent_resolved = lexical.parent.resolve()
        try:
            rel_c = candidate.relative_to(repo_root)
        except ValueError:
            rel_c = None
        if (Path(f).is_absolute() or parent_resolved != lexical.parent
                or rel_c is None
                or "/".join(rel_c.parts[:2]) not in OWNED_ROOTS):
            plan.conflicts.append((
                lexical,
                "lockfile path outside resolver-owned roots or behind a "
                "symlinked directory (.claude/{skills,agents,commands}) — "
                "refusing to act on it "
                "(repair .ai/capability-lock.json manually)",
            ))
            continue
        if f not in install_prov:
            # Never resolver-installed: a pre-existing identical file adopted
            # on sight, a forged claim, or a v1-lock entry. It is the
            # consumer's file — never a prune candidate, never an orphan the
            # resolver tracks; the next lock simply stops watching it.
            plan.advisories.append(
                f"{f}: not resolver-installed — releasing lock tracking "
                "(file kept)")
            continue
        digest = locked_dig[f]
        # Digest checks read THROUGH a symlink (that's what the lock recorded),
        # but removals act on the lexical path — unlinking a symlink entry must
        # remove the link, never its target.
        if (args.prune and install_prov[f] == "unknown"
                and os.path.lexists(lexical)):
            # Backfilled from a pre-provenance legacy lock — the top-level
            # files map never recorded installed-vs-adopted, so the entry may
            # be a consumer file the resolver never wrote. Deletion would be
            # irreversible: refuse and let a human remove it. An already-
            # deleted path falls through to plan.removals — clearing its lock
            # entry unlinks nothing.
            plan.conflicts.append((
                lexical,
                "legacy lock entry with unverifiable provenance — may be an "
                "adopted consumer file; refusing to prune (delete it "
                "manually, then re-resolve)",
            ))
            continue
        if args.prune:
            if (os.path.lexists(lexical) and not lexical.is_file()
                    and not lexical.is_symlink()):
                plan.conflicts.append((
                    lexical,
                    "lockfile-tracked path exists as a non-regular file "
                    "(directory/socket/…) — refusing to prune it",
                ))
            elif lexical.is_symlink():
                # The lock records a resolver-written regular file — a
                # link in its place is a type change even when it resolves
                # to identical bytes (the digest check follows links).
                plan.conflicts.append((
                    lexical,
                    "prune candidate replaced by a symlink — refusing to "
                    "remove a possibly hand-edited path (delete or "
                    "restore it manually, then re-resolve)",
                ))
            elif (lexical.is_file()
                    and (digest is None or sha256(lexical) != digest)):
                plan.conflicts.append((
                    lexical,
                    "prune candidate modified since install — refusing to remove a "
                    "possibly hand-edited file (delete or restore it manually, then re-resolve)",
                ))
            elif (lexical.is_file()
                    and not _exec_provable(locked_exec.get(f),
                                           lexical.stat().st_mode)):
                # Pruning is irreversible — it needs the FULL installed
                # mask, not the legacy any-exec approximation: a consumer
                # chmod of the non-exec bits (0644 -> 0600) leaves the
                # digest AND the any-exec state unchanged, so a missing
                # or untagged record can't prove the file is the one the
                # resolver installed.
                plan.conflicts.append((
                    lexical,
                    "prune candidate has no full installed-mode record to "
                    "verify against — a consumer chmod of the non-exec "
                    "bits is indistinguishable; refusing to remove it "
                    "(delete or restore it manually, then re-resolve)",
                ))
            else:
                plan.removals.append(lexical)
        else:
            plan.removals.append(lexical)

    # A symlinked .ai dir or lock file makes write_text follow the link out of
    # the repo — refuse before any materialisation applies.
    lock_file = repo_root / LOCK_PATH
    if lock_file.resolve() != lock_file:
        plan.conflicts.append((
            lock_file,
            "lockfile destination contains a symlink — refusing to write through it",
        ))
    # Type-check the destination too — --apply copies every planned file
    # BEFORE the lock write; if .ai is a plain file or the lock path is a
    # directory the copy succeeds and the lock fails, leaving materialised
    # files with no ownership record. Reject it at plan time.
    if lock_file.parent.exists() and not lock_file.parent.is_dir():
        plan.conflicts.append((
            lock_file,
            "lockfile parent .ai is not a directory — refusing to materialise "
            "without an ownership path",
        ))
    elif lock_file.exists() and not lock_file.is_file():
        plan.conflicts.append((
            lock_file,
            "lockfile destination is not a regular file — refusing to "
            "materialise without an ownership path",
        ))
    if lock_err:
        plan.conflicts.append((
            lock_file,
            f"capability lock is malformed ({lock_err}) — refusing to infer an "
            f"empty ownership map (repair or delete {LOCK_PATH})",
        ))

    # ---- report ----
    print(f"universe={universe}  registry={registry_root}")
    for r in plan.resolved:
        print(f"  resolve {r['plugin']}@{r['resolved_version']}  ({len(r['files'])} files)")
    for _, dst in plan.writes:
        print(f"  write   {dst}")
    for dst, why in plan.skips[:20]:
        print(f"  skip    {dst} ({why})")
    if len(plan.skips) > 20:
        print(f"  skip    … {len(plan.skips) - 20} more identical")
    for dst in plan.removals:
        print(f"  prune   {dst} {'(will remove)' if args.prune else '(kept — --prune to remove)'}")
    for a in plan.advisories:
        print(f"  note    {a}")
    for dst, why in plan.conflicts:
        print(f"  CONFLICT {dst}: {why}")

    if plan.conflicts:
        print(f"\n{len(plan.conflicts)} conflict(s) — fail-closed, nothing applied")
        return 1

    if args.check:
        drift = [d for _, d in plan.writes]
        # Verify the lock itself, not just file bytes — a consumer whose
        # files match the registry but whose lock is missing or stale has
        # no ownership record: CI would pass, then a later registry update
        # reads the files as untracked hand edits and refuses to update.
        # Expected doc mirrors exactly what --apply would write.
        expected_files = {rel: digest for r in plan.resolved
                          for rel, digest in r["files"].items()}
        for f in plan.removals:
            rel = f.relative_to(repo_root).as_posix()
            expected_files[rel] = locked_dig[rel]
        expected_resolved = [{k: v for k, v in r.items()
                              if k not in ("files", "exec")}
                             for r in plan.resolved]
        expected_written = {dst.relative_to(repo_root).as_posix()
                            for _, dst in plan.writes}
        expected_prov = {}
        for r in plan.resolved:
            for rel in r["files"]:
                if rel in expected_written:
                    expected_prov[rel] = r["plugin"]
                elif rel in install_prov:
                    # Carried record — including 'unknown' backfill, which
                    # must never silently upgrade to a real plugin name.
                    expected_prov[rel] = install_prov[rel]
        for f in plan.removals:
            rel = f.relative_to(repo_root).as_posix()
            if rel in install_prov:
                expected_prov[rel] = install_prov[rel]
        expected_exec = {rel: (mode | EXEC_TAG) for r in plan.resolved
                         for rel, mode in r["exec"].items()}
        for f in plan.removals:
            rel = f.relative_to(repo_root).as_posix()
            if rel in locked_exec:
                expected_exec[rel] = locked_exec[rel]
        lock_missing = not lock_file.is_file()
        expected_lock = {
            "version": 1,
            "universe": universe,
            "resolved": expected_resolved,
            "files": expected_files,
            "provenance": expected_prov,
            "exec": expected_exec,
        }
        lock_stale = lock != expected_lock
        # Kept orphans are still lockfile-tracked: a modified or deleted one
        # is drift, not a pass — the expected-lock comparison is
        # self-referential, so verify on-disk bytes against the recorded
        # digest. A v1-lock orphan with no recorded digest can only be
        # existence-verified; --apply upgrades it to a full record.
        orphan_drift = []
        for f in plan.removals:
            rel = f.relative_to(repo_root).as_posix()
            digest = locked_dig[rel]
            if not os.path.lexists(f):
                orphan_drift.append(f"{rel} (missing)")
                continue
            if f.is_symlink():
                # The lock records a resolver-written regular file — a link in
                # its place is a type change even when it resolves to identical
                # bytes (the digest check follows links; the exec check skips
                # them).
                orphan_drift.append(f"{rel} (replaced by a symlink)")
                continue
            if not f.is_file():
                # A fifo, device, socket or directory in the recorded file's
                # place IS drift — and hashing must never run here: opening
                # a fifo blocks until a writer shows up.
                orphan_drift.append(
                    f"{rel} (replaced by a non-regular file)")
                continue
            if digest is not None:
                try:
                    on_disk = sha256(f)
                except OSError:
                    on_disk = None
                if on_disk != digest:
                    orphan_drift.append(f"{rel} (modified)")
            rec = locked_exec.get(rel)
            if (rec is not None and f.is_file() and not f.is_symlink()
                    and not _exec_matches(rec, f.stat().st_mode)):
                orphan_drift.append(f"{rel} (exec mode changed)")
        if drift or orphan_drift or lock_missing or lock_stale:
            for od in orphan_drift:
                print(f"  drift   {od}")
            if lock_missing:
                print("\nDRIFT: capability lock missing — run --apply to "
                      "establish ownership")
            elif lock_stale:
                print("\nDRIFT: capability lock is stale — run --apply to "
                      "refresh ownership")
            print("\nDRIFT: materialised state differs from registry source")
            return 1
        print("\nOK: materialised state matches registry source")
        return 0

    if args.apply:
        # Probe the lock destination AND every write/removal parent BEFORE
        # materialising anything — a read-only target found mid-apply would
        # strand earlier writes with no ownership record, and the retry
        # would adopt them as local files (the next registry update then
        # conflicts instead of updating).
        probe_dirs = {lock_file.parent} | {dst.parent
                                         for _, dst in plan.writes}
        if args.prune:
            # A missing orphan needs no parent probe — unlinking it is a
            # no-op; only its lock entry is cleared. Probing a read-only
            # parent anyway would block pruning of unrelated files.
            probe_dirs |= {f.parent for f in plan.removals
                           if os.path.lexists(f)}
        pd = lock_file.parent
        dfds: dict[str, int] = {}
        try:
            for pd in sorted(probe_dirs):
                if _HAS_DIRFD:
                    rel_par = pd.relative_to(repo_root).as_posix()
                    # secure_dir_fd creates missing components AND holds the
                    # verified dir open — the write probe lands inside it
                    # without re-walking a path a symlink could redirect.
                    dfds[rel_par] = secure_dir_fd(repo_root, rel_par)
                    pn = f".write-probe.{os.urandom(4).hex()}"
                    pfd = os.open(pn, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                  0o600, dir_fd=dfds[rel_par])
                    os.close(pfd)
                    os.unlink(pn, dir_fd=dfds[rel_par])
                else:
                    pd.mkdir(parents=True, exist_ok=True)
                    probe_fd, probe_name = tempfile.mkstemp(
                        dir=pd, prefix=".write-probe.", suffix=".tmp")
                    os.close(probe_fd)
                    Path(probe_name).unlink()
        except OSError as e:
            for dfd in dfds.values():
                os.close(dfd)
            sys.stderr.write(f"FAIL: cannot write to {pd}: {e}\n")
            return 2

        def parent_fd(p: Path) -> int | None:
            if not _HAS_DIRFD:
                return None
            rel = p.relative_to(repo_root).as_posix()
            if rel not in dfds:
                dfds[rel] = secure_dir_fd(repo_root, rel)
            return dfds[rel]

        # Rollback journal — each entry is (dst, prior_bytes, prior_mode,
        # created_inode); prior_bytes None marks a destination that did
        # not exist before this apply, and created_inode pins its
        # deletion to the inode this resolver created — a name that now
        # resolves to a different file is not ours to unlink. A failure
        # AFTER the first materialised output (a mid write, or the lock
        # write itself) would otherwise leave files the lock never
        # recorded — the next run would adopt them without provenance
        # and a later registry update would conflict on files this
        # resolver actually put there. Restore every touched destination
        # instead: apply is all-or-nothing.
        undo: list[tuple[Path, bytes | None, int | None,
                         tuple[int, int] | None]] = []

        def rollback() -> None:
            for dst, prior, mode, ino, times in reversed(undo):
                try:
                    if prior is None:
                        if ino is not None:
                            try:
                                if _HAS_DIRFD:
                                    cur = os.stat(
                                        dst.name,
                                        dir_fd=parent_fd(dst.parent),
                                        follow_symlinks=False)
                                else:
                                    cur = os.stat(
                                        dst, follow_symlinks=False)
                            except OSError:
                                continue  # already gone — nothing to undo
                            if (cur.st_dev, cur.st_ino) != ino:
                                # The name was renamed/replaced since the
                                # write — unlinking it would delete a file
                                # this run did not create.
                                sys.stderr.write(
                                    "note: rollback left "
                                    f"{dst.relative_to(repo_root).as_posix()}"
                                    " in place — the path no longer resolves"
                                    " to the file this run created\n")
                                continue
                        if _HAS_DIRFD:
                            os.unlink(dst.name,
                                      dir_fd=parent_fd(dst.parent))
                        else:
                            dst.unlink(missing_ok=True)
                    else:
                        if ino is not None:
                            # Compare-and-restore: the inode this run
                            # installed must still own the name — a
                            # concurrent replacement after our write is
                            # not ours to overwrite (Codex on
                            # vendored-resolver review).
                            try:
                                if _HAS_DIRFD:
                                    cur = os.stat(
                                        dst.name,
                                        dir_fd=parent_fd(dst.parent),
                                        follow_symlinks=False)
                                else:
                                    cur = os.stat(
                                        dst, follow_symlinks=False)
                            except OSError:
                                continue
                            if (cur.st_dev, cur.st_ino) != ino:
                                sys.stderr.write(
                                    "note: rollback left "
                                    f"{dst.relative_to(repo_root).as_posix()}"
                                    " in place — the path no longer resolves"
                                    " to the file this run wrote\n")
                                continue
                        # Restore bytes, mode AND the original timestamps —
                        # a fresh inode without `times` would read as a
                        # change to timestamp-based builds and watchers
                        # even though the apply failed.
                        atomic_replace(
                            dst,
                            lambda f, b=prior: f.write(b),
                            times=times,
                            mode=mode,
                            dfd=parent_fd(dst.parent))
                except OSError as re:
                    # Best-effort restore — a write that already failed
                    # (e.g. ENOSPC) will likely fail here too; surface it
                    # but keep unwinding the rest of the journal.
                    sys.stderr.write(
                        f"note: rollback could not restore "
                        f"{dst.relative_to(repo_root).as_posix()}: {re}\n")

        # Pruned files are staged under `.ai-prune-<name>` and kept until
        # the lock commits: restoring is a RENAME BACK (no byte rewrite,
        # no free space needed — a rollback that recreated the file could
        # die on ENOSPC or clobber a path recreated after staging).
        staged: list[tuple[Path, Path, int | None]] = []

        def restore_staged() -> None:
            """Rename each staged prune back to its original name — only
            while that name is still free. A file recreated at the path
            after staging owns it and must not be overwritten."""
            for f_, tmp_, dfd_ in reversed(staged):
                try:
                    if os.path.lexists(f_):
                        continue
                    if _HAS_DIRFD and dfd_ is not None:
                        os.rename(tmp_.name, f_.name,
                                  src_dir_fd=dfd_, dst_dir_fd=dfd_)
                    else:
                        tmp_.rename(f_)
                except OSError:
                    pass

        try:
            def snapshot(d: Path):
                # (bytes, mode, (atime_ns, mtime_ns)) of d's current
                # content WITHOUT following links — None only when d is
                # confirmed ABSENT. A link or
                # non-regular file raises: recording one as 'absent' would
                # journal a bogus prior state, and --apply must abort on a
                # destination swapped in after planning rather than write
                # over it. Any OTHER open error (EACCES, EMFILE, ...)
                # propagates — journaling a failed read as 'absent' would
                # make rollback unlink an existing file. O_NONBLOCK: a
                # destination swapped to a FIFO must not hang the apply
                # waiting on a writer.
                if _HAS_DIRFD:
                    try:
                        fd = os.open(
                            d.name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent_fd(d.parent))
                    except OSError as e:
                        if e.errno == errno.ENOENT:
                            return None
                        if e.errno == errno.ELOOP:
                            raise OSError(
                                "destination is a symlink: "
                                f"{d.relative_to(repo_root).as_posix()}")
                        raise
                    with os.fdopen(fd, "rb") as fh:
                        st = os.fstat(fh.fileno())
                        if not stat.S_ISREG(st.st_mode):
                            raise OSError(
                                "destination is not a regular file: "
                                f"{d.relative_to(repo_root).as_posix()}")
                        return (fh.read(), st.st_mode & 0o777,
                                (st.st_atime_ns, st.st_mtime_ns))
                if d.is_symlink():
                    raise OSError(
                        "destination is a symlink: "
                        f"{d.relative_to(repo_root).as_posix()}")
                try:
                    if d.is_file():
                        st = d.stat()
                        return (d.read_bytes(), st.st_mode & 0o777,
                                (st.st_atime_ns, st.st_mtime_ns))
                except FileNotFoundError:
                    return None
                if os.path.lexists(d):
                    raise OSError(
                        "destination is not a regular file: "
                        f"{d.relative_to(repo_root).as_posix()}")
                return None

            # The registry digests the plan accepted for every written
            # destination — the state a legacy (digest-less) lock entry
            # compares against.
            plan_dig = {rel: d for r in plan.resolved
                        for rel, d in r["files"].items()}
            for src, dst in plan.writes:
                rel_dst = dst.relative_to(repo_root).as_posix()
                if not _HAS_DIRFD:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                snap = snapshot(dst)
                # The snapshot must equal the state the plan accepted:
                # tracked paths were verified byte-for-byte against the
                # lock (digest + installed-mode record), untracked ones
                # were verified absent. Anything else is a concurrent
                # edit — abort rather than silently clobber it.
                want = locked_dig.get(rel_dst)
                rec_mode = locked_exec.get(rel_dst)
                if snap is None:
                    if (rel_dst in locked_dig
                            and rel_dst not in plan.absent):
                        raise OSError(
                            "tracked destination vanished since planning: "
                            f"{rel_dst}")
                elif rel_dst not in locked_dig:
                    raise OSError(
                        "destination changed since planning — refusing to "
                        "clobber a possible concurrent edit: " f"{rel_dst}")
                elif want is None:
                    # Legacy lock rows carry no digest — the state the
                    # plan accepted is 'bytes identical to the registry
                    # source' (a mode-driven repair write). Compare
                    # against the plan's recorded digest, not the absent
                    # lock one; the mode is intentionally unchecked —
                    # differing mode is the reason this write exists
                    # (Devin on vendored-resolver review).
                    if (hashlib.sha256(snap[0]).hexdigest()
                            != plan_dig.get(rel_dst)):
                        raise OSError(
                            "destination changed since planning — refusing "
                            "to clobber a possible concurrent edit: "
                            f"{rel_dst}")
                elif (hashlib.sha256(snap[0]).hexdigest() != want
                        or not (rec_mode is None
                                or _exec_matches(rec_mode, snap[1]))):
                    raise OSError(
                        "destination changed since planning — refusing to "
                        "clobber a possible concurrent edit: " f"{rel_dst}")
                undo.append((dst,
                             snap[0] if snap else None,
                             snap[1] if snap else None, None,
                             snap[2] if snap else None))
                # Write the bytes the plan checksummed, not a fresh read of
                # src — a registry file swapped between plan and apply
                # would otherwise ship content the lock never digested.
                planned = plan.planned.get(rel_dst)
                # The FULL planned mask lands on the temp before the
                # rename — a post-rename chmod could follow a swapped-in
                # symlink.
                atomic_replace(
                    dst,
                    lambda f, b=plan.payload[rel_dst]: f.write(b),
                    times=plan.times.get(rel_dst),
                    mode=(planned[3] if planned is not None else None),
                    dfd=parent_fd(dst.parent))
                # Bind rollback to the inode this write installed —
                # a name swapped to a different file before a rollback
                # must be neither unlinked nor overwritten (new files get
                # compare-and-unlink; updates get compare-and-restore —
                # consumer review of the vendored resolver).
                if _HAS_DIRFD:
                    st = os.stat(dst.name,
                                 dir_fd=parent_fd(dst.parent),
                                 follow_symlinks=False)
                else:
                    st = os.stat(dst, follow_symlinks=False)
                undo[-1] = (dst,
                            snap[0] if snap else None,
                            snap[1] if snap else None,
                            (st.st_dev, st.st_ino),
                            snap[2] if snap else None)
            if args.prune:
                for f in plan.removals:
                    rel_f = f.relative_to(repo_root).as_posix()
                    if not os.path.lexists(f):
                        # Already gone — removal is a lock-entry clear only.
                        # Checking BEFORE snapshot matters twice: snapshot's
                        # parent_fd creates missing directories, so a pruned
                        # file under a deleted dir would resurrect the whole
                        # tree — and fail under a read-only ancestor, which
                        # a no-op removal never needed.
                        continue
                    # Regular files only — a symlink reached removals only
                    # through the kept-path branch, and unlink on it is
                    # never a resolver-approved deletion. Re-stat WITHOUT
                    # following links immediately before the unlink: a
                    # candidate swapped for a symlink or directory after
                    # planning must fail the whole apply (rollback), not
                    # be silently skipped while its lock entry drops.
                    # Bind verification to the directory entry actually
                    # deleted: the digest/mode check would otherwise run on
                    # one inode while os.unlink acts on whatever the name
                    # resolves to when it fires — a concurrent
                    # rename-replace could swap in an unverified file
                    # between the two. POSIX has no compare-and-unlink, so
                    # move the entry to a private name inside the same
                    # directory first: the moved inode is provably the
                    # verified one, and a failed check can put it back.
                    tmp = f.parent / (".ai-prune-" + f.name)
                    if os.path.lexists(tmp):
                        raise OSError(
                            "prune staging name already exists: "
                            f"{tmp.relative_to(repo_root).as_posix()}")
                    dfd = parent_fd(f.parent) if _HAS_DIRFD else None
                    if _HAS_DIRFD:
                        try:
                            os.rename(f.name, tmp.name,
                                      src_dir_fd=dfd, dst_dir_fd=dfd)
                        except OSError as e:
                            if e.errno == errno.ENOENT:
                                continue  # raced deletion — still a no-op
                            raise
                    else:
                        try:
                            f.rename(tmp)
                        except FileNotFoundError:
                            continue  # raced deletion — still a no-op

                    def _restore() -> None:
                        try:
                            # A newcomer that took the original name is
                            # not ours to clobber — leave the moved entry
                            # under its private name; the raise below
                            # aborts the apply either way.
                            if os.path.lexists(f):
                                return
                            if _HAS_DIRFD:
                                os.rename(tmp.name, f.name,
                                          src_dir_fd=dfd,
                                          dst_dir_fd=dfd)
                            else:
                                tmp.rename(f)
                        except OSError:
                            pass

                    try:
                        snap = snapshot(tmp)
                        if snap is not None:
                            # Re-verify against the lock BEFORE unlinking
                            # — the digest/mode check at plan time is a
                            # stale read by now: a hand edit between plan
                            # and apply must abort (and roll back), not
                            # delete the edited file.
                            want_dig = locked_dig.get(rel_f)
                            if (want_dig is None
                                    or hashlib.sha256(snap[0]).hexdigest()
                                    != want_dig
                                    or not _exec_provable(
                                        locked_exec.get(rel_f), snap[1])):
                                raise OSError(
                                    "prune target changed since planning "
                                    "(digest or mode drift) — refusing to "
                                    "remove a possibly hand-edited file: "
                                    f"{rel_f}")
                    except OSError:
                        # A read failure must not strand the file under
                        # its staging name — put it back before the abort
                        # unwinds the rest of the apply.
                        _restore()
                        raise
                    if snap is None:
                        continue  # raced deletion — still a no-op
                    # Keep the staged file until the lock commits — a
                    # failed apply restores it by RENAMING BACK (no byte
                    # rewrite, no free space needed, cannot overwrite a
                    # file recreated at the original name). It never
                    # joins `undo` — the byte-restore path is the bug
                    # class this avoids.
                    staged.append((f, tmp, dfd))
                    print(f"  removed "
                          f"{f.relative_to(repo_root).as_posix()}")
        except OSError as e:
            restore_staged()
            rollback()
            for dfd in dfds.values():
                os.close(dfd)
            sys.stderr.write(f"FAIL: cannot apply: {e}\n")
            return 2
        # Skipped (identical) destinations get their digest copied into the
        # lock without a write — re-verify each one still matches what
        # planning saw, or the lock records a stale ownership entry for a
        # file edited in between (consumer review of the vendored resolver).
        try:
            stale = _stale_skip(repo_root, plan, snapshot)
            if stale is not None:
                raise OSError(
                    "identical destination changed since planning — "
                    "refusing to record a stale lock entry: "
                    f"{stale}")
        except OSError as e:
            restore_staged()
            rollback()
            for dfd in dfds.values():
                os.close(dfd)
            sys.stderr.write(f"FAIL: cannot apply: {e}\n")
            return 2
        new_files = {rel: digest for r in plan.resolved
                     for rel, digest in r["files"].items()}
        if not args.prune:
            # Kept orphans stay lockfile-tracked so a later --apply --prune
            # (or --check) still knows they were installed by the resolver.
            for f in plan.removals:
                rel = f.relative_to(repo_root).as_posix()
                new_files[rel] = locked_dig[rel]
        written = {dst.relative_to(repo_root).as_posix()
                   for _, dst in plan.writes}
        # Provenance = files this resolver wrote THIS run or carried from an
        # earlier install record. A pre-existing file that merely matched the
        # registry content is adopted for drift-watching only — never
        # provenanced, so --prune can never unlink a file the resolver did
        # not put there.
        new_prov = {}
        for r in plan.resolved:
            for rel in r["files"]:
                if rel in written:
                    new_prov[rel] = r["plugin"]
                elif rel in install_prov:
                    # Carried record — 'unknown' legacy backfill stays
                    # 'unknown' until this resolver actually writes the file.
                    new_prov[rel] = install_prov[rel]
        if not args.prune:
            for f in plan.removals:
                rel = f.relative_to(repo_root).as_posix()
                if rel in install_prov:
                    new_prov[rel] = install_prov[rel]
        new_exec = {rel: (mode | EXEC_TAG) for r in plan.resolved
                    for rel, mode in r["exec"].items()}
        if not args.prune:
            for f in plan.removals:
                rel = f.relative_to(repo_root).as_posix()
                if rel in locked_exec:
                    new_exec[rel] = locked_exec[rel]
        lock_doc = {
            "version": 1,
            "universe": universe,
            "resolved": [{k: v for k, v in r.items()
                          if k not in ("files", "exec")} for r in plan.resolved],
            "files": new_files,
            "provenance": new_prov,
            "exec": new_exec,
        }
        lock_file = repo_root / LOCK_PATH
        # Atomic like the component writes — write_text truncates a
        # hard-linked lock's shared inode (external peer), and a crash
        # mid-write would leave a partial ownership record.
        try:
            if not _HAS_DIRFD:
                lock_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_replace(
                lock_file,
                lambda f: f.write(json.dumps(lock_doc, indent=2)
                                  .encode("utf-8") + b"\n"),
                dfd=parent_fd(lock_file.parent))
        except OSError as e:
            # Roll the outputs back too — leaving them on disk untracked
            # would let the next run adopt them without provenance.
            restore_staged()
            rollback()
            for dfd in dfds.values():
                os.close(dfd)
            sys.stderr.write(f"FAIL: cannot write {LOCK_PATH}: {e}\n")
            return 2
        # The lock is committed — staged prunes are now durable. Unlink
        # through the still-open verified dir FD: a parent renamed and
        # replaced by a symlink since staging can't redirect the delete
        # outside the repository (Codex on vendored-resolver review).
        for _f, tmp, _dfd in staged:
            try:
                if _HAS_DIRFD and _dfd is not None:
                    os.unlink(tmp.name, dir_fd=_dfd)
                else:
                    tmp.unlink()
            except OSError as e:
                print("  note: staged prune entry left at "
                      f"{tmp.relative_to(repo_root).as_posix()} ({e})")
        for dfd in dfds.values():
            os.close(dfd)
        if args.prune:
            # Only once the lock is durably written — pruning an emptied
            # parent dir BEFORE this point would leave rollback() unable
            # to recreate the file it is restoring.
            for f in plan.removals:
                d = f.parent
                try:
                    # Revalidate the WHOLE ancestor chain WITHOUT
                    # following links — the verified dir FDs are closed
                    # by now, and `islink(d)` only checks the final
                    # component: `.claude` swapped to a symlink would
                    # make is_dir/iterdir/rmdir act on a `commands`
                    # dir outside repo_root (Codex on vendored-resolver
                    # review).
                    chain = [d] + list(d.parents)
                    chain = chain[:chain.index(repo_root)]
                    while (d != repo_root
                           and not any(os.path.islink(a) for a in chain)
                           and d.is_dir()
                           and not any(d.iterdir())):
                        d.rmdir()
                        d = d.parent
                        chain = chain[1:]
                except OSError:
                    # Best-effort cleanup — a read-only parent (or a
                    # missing orphan's dir) is cosmetic; the lock is
                    # already correct.
                    print("  note: empty-directory cleanup skipped for "
                          f"{d.relative_to(repo_root).as_posix()} "
                          "(permission denied)")
        print(f"\napplied: {len(plan.writes)} write(s), lock -> {LOCK_PATH}")
        return 0

    print(f"\ndry-run: {len(plan.writes)} write(s), {len(plan.skips)} identical, "
          f"{len(plan.removals)} orphaned — pass --apply to materialise")
    return 0


def locked_digests(lock: dict) -> dict[str, str | None]:
    """All lockfile-tracked repo-relative paths -> installed sha256 (or None
    for v1 locks that recorded paths without digests)."""
    out: dict[str, str | None] = {}
    if not lock:
        return out
    files = lock.get("files", {})
    if isinstance(files, dict):
        out.update(files)
    else:
        out.update((f, None) for f in files)
    for r in lock.get("resolved", []):
        rf = r.get("files", {})
        if isinstance(rf, dict):
            for k in rf:
                out.setdefault(k, rf[k])
        else:
            for k in rf:
                out.setdefault(k, None)
    return out


if __name__ == "__main__":
    sys.exit(main())
