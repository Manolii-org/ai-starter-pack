"""Unit tests for the capability registry machinery — pack-internal, excluded
from rendered consumers (resolver/lint semantics, scope fail-closed rule)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RESOLVE = REPO / "scripts" / "ai-resolve.py"
LINT = REPO / "scripts" / "registry-lint.py"


def make_registry(root: Path, plugins: dict[str, list[tuple[str, str]]]) -> Path:
    """Build a minimal registry: plugins.json + scope dirs + plugin trees."""
    reg = root / "registry"
    reg.mkdir(parents=True)
    index = {"schema_version": 1, "scopes": {}, "plugins": []}
    for scope_path, files in plugins.items():
        scope, name = scope_path.split("/", 1)
        pdir = reg / scope / name
        (pdir / ".claude-plugin").mkdir(parents=True)
        (pdir / ".devin-plugin").mkdir(parents=True)
        (pdir / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": name, "version": "1.0.0", "description": "test"}))
        (pdir / ".devin-plugin" / "plugin.json").write_text(json.dumps(
            {"name": name, "version": "1.0.0"}))
        index["plugins"].append({
            "scope": scope, "name": name,
            "path": f"registry/{scope}/{name}",
        })
        for fname, content in files:
            fp = pdir / fname
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
    (reg / "plugins.json").write_text(json.dumps(index))
    return reg.parent  # checkout root containing registry/

def write_manifest(repo: Path, universe: str, requires: list[dict]) -> Path:
    import yaml
    m = repo / "ai-manifest.yaml"
    m.write_text(yaml.dump({
        "version": 1,
        "universe": universe,
        "requires": requires,
    }))
    return m

def run_resolver(manifest: Path, registry_root: Path, repo_root: Path, *flags: str):
    return subprocess.run(
        [sys.executable, str(RESOLVE), "--manifest", str(manifest),
         "--registry", str(registry_root), "--repo-root", str(repo_root), *flags],
        capture_output=True, text=True, timeout=120)

def seed_catalog(reg: Path, patterns: list[str] | None = None) -> None:
    p = reg / "platform" / "framework" / "data"
    p.mkdir(parents=True, exist_ok=True)
    (p / "token-shapes.json").write_text(json.dumps({
        "patterns": [{"name": "t", "regex": rx} for rx in (patterns or [])]}))

def load_lint_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("registry_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass field resolution needs the module registered
    spec.loader.exec_module(mod)
    return mod

def _bootstrap_env(tmp_path):
    """env for bootstrap subprocess calls: a `gh` stub reporting 'private'
    (the visibility check fails closed when gh can't answer), PATH
    including it, and MIRROR_TRUST_DIRS declaring the stub — a binary
    outside the system dirs is untrusted unless the operator declares
    the root."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho \"$@\" > \"" + str(tmp_path / "gh_args")
                  + "\"\necho private\n")
    gh.chmod(0o755)
    # Isolate the harness machine's GLOBAL git config — e.g. a Devin box
    # rewrites every github.com url through its auth proxy via
    # url.insteadOf, which would silently steer push-target checks.
    empty_cfg = tmp_path / "gitconfig.empty"
    empty_cfg.write_text("")
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                GIT_CONFIG_GLOBAL=str(empty_cfg),
                MIRROR_TRUST_DIRS=os.pathsep.join(filter(None,
                    (str(bin_dir), os.environ.get("MIRROR_TRUST_DIRS", "")))))

def _load_bootstrap():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bootstrap_mirror", "scripts/bootstrap-mirror.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def load_resolve_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ai_resolve", RESOLVE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ai_resolve"] = mod  # dataclass annotation lookup needs this
    spec.loader.exec_module(mod)
    return mod


def test_pipe_to_exec_compound_segments(tmp_path):
    """`if`/`{ }`/`( )` compounds inside a pipe forward the dep when any
    `;`-sibling emits it — `_cmd_window` stops at `;`, so the walk must
    step into compounds rather than end the statement (Codex + Devin on
    #1380/#1957, round-16 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # a forwarding sibling reaches fd1
            b"cat scripts/x.sh | if :; then cat; fi | sh",
            b"cat scripts/x.sh | if cat; then cat | wc; fi | sh",
            b"cat scripts/x.sh | while :; do cat; done | sh",
            b"cat scripts/x.sh | for i in a b; do cat; done | sh",
            b"cat scripts/x.sh | ( cat ) | sh",
            b"cat scripts/x.sh | { cat; } | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # every sibling emits own content — nothing dep on fd1
            b"cat scripts/x.sh | if true; then echo hi; fi | sh",
            b"cat scripts/x.sh | if cat | wc; fi | sh",
            b"cat scripts/x.sh | while :; do cat | wc; done | sh",
            b"cat scripts/x.sh | ( cat | wc ) | sh",
            # a non-`|` boundary after the compound ends the statement —
            # `sh` reads nothing
            b"cat scripts/x.sh | ( cat ) ; sh",
            b"cat scripts/x.sh | tee log; sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round17(tmp_path):
    """Round-17 review batch — substitution/fd-cluster/position edge
    cases verified against live bash behaviour (Devin + Codex on
    #9/#127/#1380/#1957)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # a `)` sharing a window still restores the group's
            # aggregate fd1 — `( cat; true )` emits the pipe (live bash)
            b"cat scripts/x.sh | ( cat; true ) | sh",
            b"cat scripts/x.sh | ( cat; echo safe ) | sh",
            # process substitution as redirect target / program /
            # reader operand — the inner body inherits the outer stdin
            b"cat scripts/x.sh | sh < <(cat)",
            b"cat scripts/x.sh | sh <(cat)",
            b"cat scripts/x.sh | sh $(cat)",
            b"cat scripts/x.sh | cat <(cat) | sh",
            b"cat scripts/x.sh | head <(cat) | sh",
            # a `$(` inside ANY argv executes the stream — `wc` takes
            # the expansion as a filename, but `sh` inside runs first
            b'cat scripts/x.sh | wc "$(sh)" | sh',
            b'cat scripts/x.sh | echo "$(sh)"',
            # clustered shell flags carry -s
            b"cat scripts/x.sh | bash -es foo",
            # `dd` operands are key=value — `status=none` still copies
            b"cat scripts/x.sh | dd status=none | sh",
            # `tee >()` still forwards its stdin on fd1
            b"cat scripts/x.sh | tee >(wc) | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # grep -c/-l/-L replace the stream like -q
            b"cat scripts/x.sh | grep -c . | sh",
            b"cat scripts/x.sh | grep -l . | sh",
            # dd if=FILE rebinds the input
            b"cat scripts/x.sh | dd if=/dev/null | sh",
            # a full-EOF reader leaves only EOF for `;` siblings
            b'cat scripts/x.sh | sh -c "cat >/dev/null; sh"',
            b'cat scripts/x.sh | sh -c "cat >/dev/null; cat"',
            b'cat scripts/x.sh | echo "$(if wc -l; then cat; fi)" | sh',
            b"cat scripts/x.sh | ( wc; sh ) | cat",
            b"cat scripts/x.sh | ( cat | wc; sh ) | cat",
            # an inner substitution nested in a capture whose body ends
            # the flow — `wc -l` swallows what `$(cat)` re-read
            b'cat scripts/x.sh | echo "$(printf "$(cat)" | wc -l)" | sh',
            b'cat scripts/x.sh | echo "$(cat >/dev/null)"',
            # an operand-taking head gets the expansion as a PATH, not
            # stdin — `wc "$(cat)"` reads a file named by the stream
            b'cat scripts/x.sh | wc "$(cat)" | sh',
            # procsub whose inner body does not read the stream
            b"cat scripts/x.sh | sh < <(printf hi)",
            b"cat scripts/x.sh | cat <(printf hi) | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round18(tmp_path):
    """Round-18 review batch — `eval` operand = executed command
    string; `cat f -` reads BOTH a file and the shared stdin
    (Devin on #127, Codex on #1380)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # eval's argv is program text run on the same stdin/stdout
            b"cat scripts/x.sh | eval cat | sh",
            b"cat scripts/x.sh | eval sh | sh",
            b"cat scripts/x.sh | echo \"$(eval cat)\" | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # a `-`/fd-path operand makes `cat` read the shared stdin
            # to EOF alongside its files — later `;` siblings see EOF
            b"cat scripts/x.sh | sh -c 'cat /etc/hosts -; sh'",
            b"cat scripts/x.sh | sh -c 'cat - /etc/hosts; sh'",
            b"cat scripts/x.sh | sh -c 'cat /dev/stdin /etc/hosts; sh'",
            # eval running a sink still replaces the stream
            b"cat scripts/x.sh | eval wc -l | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round19(tmp_path):
    """Round-19 review batch — eval's fd map bounds the inner command;
    bare eval emits nothing; `source /dev/stdin` executes the pipe;
    region bodies balance nested substitution parens; a quoted FILENAME
    operand is not program text (Devin + Codex on #127/#9/#1380)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "two.sh").write_bytes(b"a b")
    for line in (
            # `eval PROG` runs PROG on the same stdin/stdout
            b"cat scripts/x.sh | eval cat | sh",
            b"cat scripts/x.sh | eval sh | sh",
            # source of an fd-path operand executes the stream
            b"cat scripts/x.sh | source /dev/stdin",
            b"cat scripts/x.sh | . /dev/stdin",
            # nested procsub: inner `)` no longer truncates the body
            b"cat scripts/x.sh | echo \"$(cat <(cat))\" | sh",
            # a `<(cat)` codec operand re-reads the upstream pipe
            b"cat scripts/x.sh | python -m base64 -d <(cat) | sh",
            # nested `date +FORMAT` echoes the capture back out —
            # quoted-only since round 24 (unquoted +$( field-splits)
            b"cat scripts/x.sh | echo \"$(date \"+$(cat)\")\" | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # eval's own `< f` rebind feeds the inner command the FILE
            b"cat scripts/x.sh | eval cat </dev/null | sh",
            b"cat scripts/x.sh | eval sh </dev/null | sh",
            # bare/empty eval emits nothing
            b"cat scripts/x.sh | eval | sh",
            b"cat scripts/x.sh | eval '' | sh",
            # `source FILE` reads the file, not the pipe
            b"cat scripts/x.sh | source /etc/profile | sh",
            # inert inner procsub (`echo safe` does not read the pipe)
            b"cat scripts/x.sh | echo \"$(cat <(echo safe))\" | sh",
            b"cat scripts/x.sh | python -m base64 -d <(echo safe) | sh",
            # a quoted FILENAME operand names a different file
            b"bash 'scripts/x.sh;safe'",
            b'bash "scripts/absent.sh;safe"',
            # nested `date --date=` parses the capture — emits a
            # timestamp, never the script bytes (Devin on #1957)
            b"cat scripts/x.sh | echo \"$(date --date=$(cat))\" | sh",
            # gzip requires a *.gz filename — `/dev/fd/63` aborts the
            # module before it reads the stream (Devin on #128)
            b"cat scripts/x.sh | python -m gzip -d <(cat) | sh",
            # the `$(printf)` path lives outside the substitution — bash
            # opens the literal `x.sh;safe` name (Devin on #128)
            b'bash "scripts/x.sh;$(printf safe)"',
            # a quoted multi-word FILENAME is one literal name — the
            # whitespace shortcut only applies to program text (Devin
            # on #1957)
            b"bash 'scripts/x.sh safe'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # Program-text operands still detect — `sh -c`, eval, sed/awk
    # programs, ssh remote commands all interpret the quoted text.
    for line in (
            b"sh -c 'bash scripts/x.sh'",
            b"sh -c 'x;bash scripts/x.sh'",
            b"eval 'bash scripts/x.sh'",
            b"sed '1e bash scripts/x.sh' f",
            b"ssh h 'bash scripts/x.sh'",
            # round-20: emit heads echo a capture opened inside their
            # argv to fd1 (Devin on #128/#1382 + Codex on #11)
            b"cat scripts/x.sh | echo \"$(echo $(cat))\" | sh",
            b"cat scripts/x.sh | sh -c 'echo \"$(cat)\" | sh'",
            b"cat scripts/x.sh | sh -c 'echo $(cat)' | sh",
            # eval's `< f` rebind does not erase bytes its argument
            # substitution already captured (Devin on #1382)
            b"cat scripts/x.sh | sh -c 'eval \"$(cat)\" < /dev/null' | sh",
            # `source`/`.` execute a `<(BODY)` filename operand
            # (CodeRabbit on #128)
            b"cat scripts/x.sh | source <(cat) | sh",
            b"cat scripts/x.sh | . <(cat) | sh",
            # wrapped program heads — timeout execs its argv (with stdin
            # delegated), xargs execs it (stdin held for its own argv)
            # (Codex on #128/#1382)
            b"cat scripts/x.sh | timeout 1 sh -c 'bash scripts/x.sh'",
            b"cat scripts/x.sh | xargs sh -c 'bash scripts/x.sh'",
            b"cat scripts/x.sh | timeout 1 sh | sh",
            # `=`-attached program flags (Codex on #11)
            b"node --eval='bash scripts/x.sh'",
            b"sed --expression='1e bash scripts/x.sh' f"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # Round-20 negative cases — none of these executes the bundled
    # script's bytes.
    for line in (
            # `source` runs only its FIRST operand — later words are
            # the script's positional parameters (Codex + Devin on
            # #128/#1382)
            b"cat scripts/x.sh | source /dev/null /dev/stdin | sh",
            # xargs' own flag operands are not program text
            b"xargs -n 5 'bash scripts/x.sh'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # Round-21 positive cases — each executes the bundled script.
    for line in (
            # clustered program flags bind the next word (Codex on #128)
            b"sh -ec 'bash scripts/x.sh'",
            # `perl -we` — w flag + -e binds the NEXT word (-eE instead
            # glues program text "E" onto -e; negative case below —
            # CodeRabbit on #128, round-22; verified live)
            b"perl -we 'system(q(bash scripts/x.sh))'",
            # perl -E / node -E / awk --source take program text
            # (Devin + Codex on #128/#1382)
            b"perl -E 'system(q(bash scripts/x.sh))'",
            b"awk --source='BEGIN{system(\"bash scripts/x.sh\")}'",
            # quoted `)` inside `$(` does not close the substitution
            # (Devin on #128/#1382/#1957)
            b'bash "$(printf \')\'; bash scripts/x.sh)"',
            b"cat <<X\n$(printf ')'; bash scripts/x.sh)\nX\n",
            # gzip iterates operands: `-` before a bad name already
            # emitted decoded stdin (Devin on #1382/#1957)
            b"cat scripts/x.sh | python -m gzip -d - bad | sh",
            b"cat scripts/x.sh | python -m gzip -d f.gz - | sh",
            # `$(` args to eval/source expand in the PARENT's stdin
            # context before `< f` rebinds (Codex on #11/#1382)
            b"cat scripts/x.sh | eval \"$(cat)\" < /dev/null",
            b"cat scripts/x.sh | source <(cat) < /dev/null | sh",
            # no-operand xargs options don't consume the command head
            # (Codex + CodeRabbit + Devin on #128/#1382/#1957)
            b"xargs --show-limits sh -c 'bash scripts/x.sh'",
            b"xargs --eof sh -c 'bash scripts/x.sh'",
            b"xargs -l sh -c 'bash scripts/x.sh'",
            # timeout's DURATION is not the wrapped command — env -S
            # still splits into a shell (Devin on #128/#1382)
            b"cat scripts/x.sh | timeout 1 env -S 'sh'",
            # `+$(cat)` joins the format operand inside quotes;
            # the UNQUOTED form field-splits — a pipe-fed capture is
            # indeterminate (fail closed), a provable >=2-word file
            # capture errors (round-25 word-count gate; the round-24
            # flat reject missed the single-word emit)
            b'cat scripts/x.sh | date "+$(cat)" | sh',
            b'cat scripts/x.sh | date +$(cat) | sh',
            # backtick pairs are substitution words like `$(` — inside
            # and outside double quotes, in eval argv, and through an
            # emit head's output (round-21 backtick parity; _cmd_window
            # used to cut the window at the opener so `eval `cat`` saw
            # an empty program)
            b"cat scripts/x.sh | eval `cat` | sh",
            b"cat scripts/x.sh | echo `cat` | sh",
            b'cat scripts/x.sh | echo "`cat`" | sh',
            # a backtick arg sub expands in the PARENT stdin context
            # before eval's own `< f` rebind — same rule as `$(`
            b"cat scripts/x.sh | eval `cat` < /dev/null",
            b"cat scripts/x.sh | eval `cat` -n < /dev/null | sh",
            # a script ref inside a closed backtick sub pipes out
            b'echo "`cat scripts/x.sh`" | sh',
            b"echo `cat scripts/x.sh` | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # Round-21 negative cases.
    for line in (
            # UNQUOTED `date +$(cat F)` with a provable >=2-word
            # file capture field-splits into extra operands — date
            # errors, nothing reaches the exec (round-25 gate; the
            # round-24 flip was positive on a pipe-fed theory)
            b"cat scripts/x.sh | date +$(cat scripts/two.sh) | sh",
            b"date +$(cat scripts/two.sh) | sh",
            # evaluated program reads the REBOUND stdin, not the pipe
            # (Devin on #128/#1382/#1957)
            b"cat scripts/x.sh | eval sh </dev/null",
            b"cat scripts/x.sh | eval cat </dev/null | sh",
            # gzip positional file sink: invalid name before any `-`
            b"cat scripts/x.sh | python -m gzip -d bad | sh",
            # QUOTED heredoc body is literal — no expansions at all
            b"cat <<'X'\n$(bash scripts/x.sh)\nX\n",
            # `xargs --eof=END` attached operand is an option, but an
            # eof-set head still execs the following program (keep
            # positive); the bare `--eof` also takes no operand
            b"cat scripts/x.sh | eval 'bash f' </dev/null",
            # an emit head's constant/discarded backtick output is not
            # the script's bytes — `$(` parity throughout
            b"cat scripts/x.sh | echo `printf x` | sh",
            b'cat scripts/x.sh | echo "`printf x`" | sh',
            b'cat scripts/x.sh | echo "`cat`" >/dev/null | sh',
            # `$(` parity: bash's operand is the sub OUTPUT (a name),
            # not the script text — same hole as `bash $(echo …)`
            b"bash `echo scripts/x.sh`",
            # xargs execs its utility operand verbatim — a quoted
            # multi-word utility is a literal program name (ENOENT),
            # not program text
            b'xargs -n 5 "bash scripts/x.sh"'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # Round-22 positive cases — each executes the bundled script.
    for line in (
            # a `c` ANYWHERE in a sh option cluster binds the FIRST
            # POSITIONAL word as the command string — `-ce`, `-c -e`
            # and `-xce` all run P (CodeRabbit on #128; verified live)
            b"bash -ce 'bash scripts/x.sh'",
            b"sh -c -e 'bash scripts/x.sh'",
            b"bash -xec 'bash scripts/x.sh'",
            # node -p/--print evaluates its operand like -e (Codex) —
            # the operand must be VALID JavaScript (bare `bash x.sh`
            # is not; CodeRabbit on #128, round-23)
            b"node -p 'require(\"node:child_process\")"
            b".execSync(\"bash scripts/x.sh\")'",
            b"node --print 'require(\"node:child_process\")"
            b".execSync(\"bash scripts/x.sh\")'",
            # a substitution GENERATING eval's command is opaque —
            # `eval "$(printf sh)"` expands to `eval sh` and runs the
            # pipe (Devin on #1382; verified live)
            b'cat scripts/x.sh | eval "$(printf sh)"',
            b'cat scripts/x.sh | eval `printf sh`'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # Round-22 negative cases.
    for line in (
            # `perl -eE` glues program text "E" onto -e — the operand
            # is a filename, not program text (CodeRabbit on #128)
            b"perl -eE 'system(q(bash scripts/x.sh))'",
            # a wrapped command's FILENAME operand stays a literal
            # name through xargs — 'x.sh safe' ENOENTs, never runs
            # (Devin on #128)
            b"xargs bash 'scripts/x.sh safe'",
            # a backtick inside single quotes (or escaped) is literal
            # text, never a substitution (Devin + Codex on #128/#1382)
            b"cat scripts/x.sh | echo '`cat`' | sh",
            b"cat scripts/x.sh | echo \\`cat\\` | sh",
            # gzip COMPRESS mode emits transformed bytes, not the
            # stream — a `-` then any filename operand can't forward
            # the script (Devin on #128/#1382)
            b"cat scripts/x.sh | python -m gzip - | sh",
            b"cat scripts/x.sh | python -m gzip - bad | sh",
            b"cat scripts/x.sh | python -m gzip bad - | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # Round-23 positive cases — each executes the bundled script.
    for line in (
            # `env -S`/`--split-string` re-parses its operand INTO a
            # command line — the operand is program text even as the
            # LAST word (Codex on #128; verified live)
            b"cat scripts/x.sh | env -S 'bash scripts/x.sh'",
            b"cat scripts/x.sh | timeout 1 env -S 'bash scripts/x.sh'",
            b"cat scripts/x.sh | env --split-string 'bash scripts/x.sh'",
            # `-o`/`-O` consume their option name — `bash -o pipefail
            # -c P` still runs P (CodeRabbit on #128; verified live)
            b"bash -o pipefail -c 'bash scripts/x.sh'",
            b"cat scripts/x.sh | bash -o pipefail -c 'sh'",
            b"cat scripts/x.sh | bash -O extglob -c 'sh'",
            # a `c` flag mid-line: `sh -c -e sh` runs the next
            # NON-OPTION word on stdin (Devin on #1957; verified on
            # bash and dash)
            b"cat scripts/x.sh | sh -c -e sh",
            b"cat scripts/x.sh | bash -c -e sh",
            # bun's eval/print flags evaluate their operand like node
            # (Codex on #128)
            b"bun --eval 'require(\"./scripts/x.sh\")'",
            b"bun -e 'require(\"./scripts/x.sh\")'",
            # a backslash inside '…' is LITERAL — it cannot eat the
            # closing quote, so the tick after it substitutes
            # (CodeRabbit/Codex on #128/#1382; verified live)
            b"cat scripts/x.sh | echo 'a\\' `cat` | sh",
            # an apostrophe inside "…" is literal — it must NOT open
            # single-quote state, so the tick substitutes
            # (CodeRabbit on #128; verified live)
            b"cat scripts/x.sh | echo \"it's `cat`\" | sh",
            b"cat scripts/x.sh | eval 'x' `cat` | sh",
            # ssh operands AFTER the destination form the remote
            # command — option operands are consumed first (Devin on
            # #128; verified live)
            b"ssh -p 22 host 'bash scripts/x.sh'",
            b"ssh -p22 host 'bash scripts/x.sh'",
            b"ssh -o User=x -i id host 'bash scripts/x.sh'"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # Round-23 negative cases.
    for line in (
            # `node -r` preloads a MODULE file — a filename operand,
            # not program text (Devin on #128; verified live)
            b"node -r 'bash scripts/x.sh'",
            b"node --require 'bash scripts/x.sh'",
            # after `sed -e`/`awk -f` the positionals are INPUT
            # FILES, not the program (Devin on #128; verified live)
            b"sed -e p 'bash scripts/x.sh'",
            b"awk -f f 'bash scripts/x.sh'",
            b"sed --expression p 'bash scripts/x.sh'",
            # `xargs -a F` reads argv items from F — it does NOT
            # drain the shared stdin (Devin on #1957; verified live)
            b"cat scripts/x.sh | xargs -a /dev/null | sh",
            b"cat scripts/x.sh | xargs --arg-file=/dev/null | sh",
            # ssh's option operands are not the destination — `-p
            # 22` means the NEXT word is still not the remote
            # command (Devin on #128)
            b"ssh -p 22 'bash scripts/x.sh'",
            # a substitution that DRAINS stdin generates
            # stdin-independent command text for eval — `eval
            # "$(cat|wc -l)"` runs a count on EOF (Devin on #11)
            b"cat scripts/x.sh | eval \"$(cat | wc -l)\"",
            b"cat scripts/x.sh | eval `cat | wc -l`",
            b"cat scripts/x.sh | eval \"$(cat | head -n 0)\""):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round24(tmp_path):
    """Round-24 review batch — unquoted heredoc bodies are program text
    (`bash <<EOF` bodies execute; `'$(x)'` inside still expands); `xargs
    -a` fd-aliased operands read the pipe while real files leave the
    utility's own stdin classification; `sh -c > /dev/null CMD` skips
    redirect words; `mapfile -n`/`readarray -n` leave a tail; `nice`
    execs its COMMAND; formatter flag operands forward stdin; glued
    program-file flags (`sed -ep`, `awk -fprog`) name files so
    positionals stay input files; glued backtick suffixes glue a
    filename; UNQUOTED `date +$(…)`/`date +`…`` field-split and never
    reach a downstream exec (Devin + Codex + CodeRabbit on
    #128/#1382/#1957/#11 — verified live on bash and dash)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "two.sh").write_bytes(b"a b")
    for line in (
            # an _HD_EXEC heredoc body is the inner script — plain
            # lines run, and `'`/`"` do NOT suppress `$(` expansion
            # inside it (apostrophes are literal in the body)
            b"bash <<EOF\n'$(bash scripts/x.sh)'\nEOF",
            b"bash <<EOF\nbash scripts/x.sh\nEOF",
            b"bash <<EOF\nsh scripts/x.sh\nEOF",
            # `xargs -a -`/`--arg-file=-` read the PIPE as the item
            # source — the stream flows on like bare xargs
            b"cat scripts/x.sh | xargs -a - echo | sh",
            b"cat scripts/x.sh | xargs --arg-file=- echo | sh",
            # `xargs -a F` leaves the live pipe to the utility —
            # `sh -c 'sh'` executes it
            b"cat scripts/x.sh | xargs -a /dev/null sh -c 'sh'",
            b"cat scripts/x.sh | xargs --arg-file=/dev/null sh -c 'sh'",
            # a LATER `-a` replaces an earlier one (GNU semantics) —
            # `-` wins and the pipe is read as items again
            b"cat scripts/x.sh | xargs -a /dev/null -a - echo | sh",
            # `sh -c > /dev/null CMD` — redirect words are skipped;
            # the real command string follows
            b"cat scripts/x.sh | sh -c > /dev/null sh",
            b"cat scripts/x.sh | bash -c > /dev/null sh",
            # `mapfile -n N`/`readarray -n N` copy at most N records —
            # the tail still reaches a later `; sh`
            b"cat scripts/x.sh | sh -c 'mapfile -n 1; sh'",
            b"cat scripts/x.sh | sh -c 'readarray -n5; sh'",
            b"cat scripts/x.sh | sh -c 'mapfile --line-count 1; sh'",
            # `nice` is an exec wrapper — it runs its COMMAND operand
            # (with `-n`/`--adjustment` glued or separate)
            b"nice sh -c 'bash scripts/x.sh'",
            b"nice -n 10 sh -c 'bash scripts/x.sh'",
            b"nice -n10 sh -c 'bash scripts/x.sh'",
            b"nice --adjustment 5 bash -c 'bash scripts/x.sh'",
            b"cat scripts/x.sh | nice -n 10 sh -c 'sh'",
            # formatter flag operands are not file operands — stdin
            # still forwards to the exec
            b"cat scripts/x.sh | fmt -w 999 | sh",
            b"cat scripts/x.sh | expand -t 8 | sh",
            b"cat scripts/x.sh | sort --parallel 2 | sh",
            b"cat scripts/x.sh | unexpand --tabs 4 | sh",
            # operand-valued flags don't consume the program —
            # `sed -l 80 '1e x'` still treats the positional as the
            # sed program (got_prog only marks -e/-f/-file forms)
            b"sed -l 80 '1e bash scripts/x.sh'",
            b"sed --line-length 80 '1e bash scripts/x.sh'",
            b"awk -v x=1 '{system(\"bash scripts/x.sh\")}'"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # an _HD_EXEC heredoc body's NON-executing lines are data —
            # echo/printf lines emit bytes, not program text
            b"bash <<EOF\necho scripts/x.sh\nEOF",
            b"bash <<EOF\nprintf '%s' scripts/x.sh\nEOF",
            # `xargs -a F` + a non-executing utility — the default
            # `echo` sinks the live pipe
            b"cat scripts/x.sh | xargs -a /dev/null echo | sh",
            # a LATER `-a` replaces `-` with a real file — the items
            # are no longer the pipe, and `echo` sinks it
            b"cat scripts/x.sh | xargs -a - -a /dev/null echo | sh",
            # `mapfile`/`readarray` without `-n` drain stdin to EOF —
            # nothing left for `; sh`
            b"cat scripts/x.sh | sh -c 'mapfile; sh'",
            b"cat scripts/x.sh | sh -c 'readarray; sh'",
            # UNQUOTED `date +$(cat F)` on a provable >=2-word file
            # capture field-splits into extra operands — date errors,
            # nothing reaches the exec (quoted `"+$(…)` still emits;
            # a single-word or indeterminate capture joins — see
            # test_script_dep_date_unquoted_sub, round-25 gate)
            b"cat scripts/x.sh | date +$(cat scripts/two.sh) | sh",
            b"date +$(cat scripts/two.sh) | sh",
            # a balanced backtick pair glues the arg — words after its
            # closer stay inside echo's argv
            b"echo `printf ok` bash scripts/x.sh",
            # a glued backtick suffix is filename text — bash opens
            # `x.sh.bak`, not `x.sh`
            b"bash scripts/x.sh`printf .bak`",
            # a glued program-file flag names a file, so the
            # positional is an input file too — never program text
            b"sed -ep 'bash scripts/x.sh'",
            b"awk -fprog.awk 'bash scripts/x.sh'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round25(tmp_path):
    """Round-25 review batch — `setsid` is an exec wrapper; `find`
    `-exec`/`-execdir`/`-ok`/`-okdir` spawn argv the action execs;
    `flock L CMD…` and `flock L -c 'P'` run a command holding the lock;
    an emit head's quoted operand inside `<( )` lands on an fd that
    `source`/`.` executes; xargs option parsing ends at the utility
    argv so `xargs -a /dev/null echo -a -` is a plain echo; an xargs
    stage's own `< f` rebind severs the pipe from the utility; `sort
    --files0-from F` reads its file LIST from F — a real file leaves
    the pipe untouched while `-`/fd-0 aliases drain it as the list;
    UNQUOTED `date +$(cat F)` joins the format when the file provably
    holds one word (Codex + Devin on #128 — verified live on bash and
    dash)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "two.sh").write_bytes(b"a b")
    for line in (
            # `setsid` execs its program — plain and `--wait` forms
            # (Codex on #128; verified live)
            b"setsid sh -c 'bash scripts/x.sh'",
            b"setsid --wait sh -c 'bash scripts/x.sh'",
            # `find -exec CMD \;`/`+` runs the command argv per file —
            # -execdir/-ok/-okdir are the same action (Codex on #128)
            b"find . -exec sh -c 'bash scripts/x.sh' \\;",
            b"find . -execdir sh -c 'bash scripts/x.sh' \\;",
            b"find . -ok sh -c 'bash scripts/x.sh' \\;",
            b"find . -name x -exec sh -c 'bash scripts/x.sh' \\; -type f",
            # `flock L -c 'P'` binds P as the command string; `flock L
            # CMD…` runs the argv after the lockfile (Codex on #128)
            b"flock /tmp/l.lock -c 'bash scripts/x.sh'",
            b"flock /tmp/l.lock --command 'bash scripts/x.sh'",
            b"flock /tmp/l.lock sh -c 'bash scripts/x.sh'",
            # an emit head's quoted operand inside `<( )` is emitted
            # text on the fd `source`/`.` executes (Codex on #128)
            b"source <(printf '%s\\n' 'bash scripts/x.sh')",
            b"source <(printf 'bash scripts/x.sh')",
            b". <(echo 'bash scripts/x.sh')",
            # a single-word `cat` capture still joins the unquoted
            # `+FORMAT` — x.sh holds one word (Devin on #128; verified
            # live on GNU date)
            b"cat scripts/x.sh | date +$(cat) | sh",
            b"cat scripts/x.sh | date +`cat` | sh",
            b"cat scripts/x.sh | date +$(cat scripts/x.sh) | sh",
            # `--files0-from -` reads the filename LIST from stdin —
            # the named files' contents reach the exec (Devin on #128)
            b"cat scripts/x.sh | sort --files0-from - | sh",
            b"cat scripts/x.sh | sort --files0-from=/dev/stdin | sh",
            # a `--files0-from F` sort leaves the pipe live — a later
            # `; sh` sibling still reads it (Devin on #128)
            b"cat scripts/x.sh | sh -c 'sort --files0-from=/dev/null; sh'"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # xargs option parsing ends at the utility argv — `echo
            # -a -` are ECHO's args, not a second `-a` (Devin on #128)
            b"cat scripts/x.sh | xargs -a /dev/null echo -a - | sh",
            # the xargs stage's own `< /dev/null` rebinds the utility's
            # stdin — the pipe is never connected (Devin on #128)
            b"cat scripts/x.sh | xargs -a /dev/null cat </dev/null | sh",
            # `sort --files0-from F` reads the list from F — stdin is
            # unread and unrelated bytes are emitted (Devin on #128)
            b"cat scripts/x.sh | sort --files0-from /dev/null | sh",
            b"cat scripts/x.sh | sort --files0-from=/dev/null | sh",
            # a bare `+` ends `-exec` only right after `{}` — `+` alone
            # leaves the argv unterminated: find exits on the parse
            # error (`missing argument to `-exec`') and nothing runs
            # (round-49 — verified live on GNU find)
            b"find . -exec sh -c 'bash scripts/x.sh' +",
            # the stdin-alias form drains the pipe as the list —
            # `; sh` then sees EOF (Devin on #128)
            b"cat scripts/x.sh | sh -c 'sort --files0-from=-; sh'",
            # a provable >=2-word or empty `cat` capture errors inside
            # date — nothing reaches the exec (Devin on #128)
            b"date +$(cat scripts/two.sh) | sh",
            b"cat scripts/x.sh | date +$(cat scripts/two.sh) | sh",
            # `echo <(printf 'bash x')` prints the fd NAME — the emit
            # head never runs it (Codex on #128)
            b"echo <(printf 'bash scripts/x.sh')",
            # `find -exec sh 'bash x' \;` execs a literal program name
            # — the quoted operand is a filename, not program text
            b"find . -exec sh 'bash scripts/x.sh' \\;",
            # `flock L 'bash x'` — the operand is the lock argv's
            # literal name (ENOENT), not program text
            b"flock /tmp/l.lock 'bash scripts/x.sh'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round25b(tmp_path):
    """Round-25b review fixes: `fmt` goal/prefix flag operands; `&`
    glued to a redirect is not a command separator (`0<&0 sh -c`,
    `>&2`, `&>f`) (Codex on #1957)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r25b", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r25b"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"e\n")
    (pdir / "scripts" / "two.sh").write_bytes(b"a b\n")
    for line in (
            # `0<&0`/`>&2` fd-dups are redirects, not separators — the
            # head stays `sh` and the -c string executes (Codex on
            # #1957, round-25b review — verified live on bash+dash)
            b"0<&0 sh -c 'bash scripts/x.sh'",
            b'0<&0 sh -c "bash scripts/x.sh"',
            b"1>&2 sh -c 'bash scripts/x.sh'",
            b"0<&0 cat scripts/x.sh | sh",
            # `fmt -g GOAL`/`-p PREFIX` (and long forms) take operands —
            # the value is not a file operand, stdin is still the text
            # (Codex on #1957, round-25b review)
            b"cat scripts/x.sh | fmt -g 70 | sh",
            b"cat scripts/x.sh | fmt -p PRE | sh",
            b"cat scripts/x.sh | fmt --goal 70 | sh",
            b"cat scripts/x.sh | fmt --prefix=P | sh",
            # regressions — a real `&` separator still splits
            b"echo a & bash scripts/x.sh",
            b"echo a && bash scripts/x.sh",
            b"bash scripts/x.sh &",
            b"echo a &>f; bash scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # fmt diverted to a file emits nothing on stdout
            b"cat scripts/x.sh | fmt -g 70 > /dev/null | sh",
            b"0<&0 echo bash"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round25c(tmp_path):
    """Round-25c: `cut --output-delimiter` takes an operand — the value
    is not a file operand (Codex on #1957)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r25c", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r25c"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"e\n")
    for line in (
            b'cat scripts/x.sh | cut -f1 --output-delimiter " " | sh',
            b'cat scripts/x.sh | cut -f1 --output-delimiter=" " | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | cut -f1 --output-delimiter " " '
              b'> /dev/null | sh\n')

def test_pipe_to_exec_round25d(tmp_path):
    """Round-25d: an inner `$(`/backtick capture inherits its ENCLOSING
    `$(`'s output-exec context (nested date captures), and an xargs
    "other" utility's own file operands replace the live pipe (Devin
    on #11)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r25d", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r25d"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"e\n")
    for line in (
            # the inner `$(cat x)`/`"$(cat x)"` capture rides the outer
            # `$(…)` whose output joins date's +format and reaches `sh`
            # (Devin on #11, round-25d review — verified live)
            b'date +$(printf %s "$(cat scripts/x.sh)") | sh',
            b'date +$(echo "$(cat scripts/x.sh)") | sh',
            b'date +$(echo `cat scripts/x.sh`) | sh',
            b'date +$(printf "$(cat scripts/x.sh)") | sh',
            # output-exec contexts still reach through the nesting
            b'bash -c "$(echo "$(cat scripts/x.sh)")"',
            b'eval "$(echo "$(cat scripts/x.sh)")"',
            b'$(echo "$(cat scripts/x.sh)") | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # a truly inert nest prints the bytes only — no exec
            b'echo "$(printf %s "$(cat scripts/x.sh)")"',
            b'echo "$(cat scripts/x.sh)"',
            # the xargs utility's own file operand replaces the pipe —
            # `cat /dev/null` emits no script bytes (Devin on #11)
            b'cat scripts/x.sh | xargs -a /dev/null cat /dev/null | sh',
            b'cat scripts/x.sh | xargs -a /dev/null cat /etc/hostname | sh',
            b'date +$(printf %s "x") | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round26(tmp_path):
    """Round-26: find multi-action -exec emit heads, nested `env -S`
    wrapper chains, `&&>` separators, and ionice/taskset exec wrappers
    (Devin + Codex on #128)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r26", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r26"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"e\n")
    for line in (
            # every -exec action runs per match — an EMITTING action's
            # text reaches the pipe even when an earlier action is inert
            # (Devin on #128, round-26 review — verified live)
            b"find . -exec true \\; -exec echo 'bash scripts/x.sh' \\; | sh",
            b"find . -exec printf 'bash scripts/x.sh' \\; -exec true \\; | sh",
            # a wrapper chain may contain env again — `env env -S 'x'`
            # re-parses through the INNER env (Codex on #128 — live)
            b"env env -S 'bash scripts/x.sh'",
            b"timeout 1 env env -S 'bash scripts/x.sh'",
            b"env FOO=1 env -S 'bash scripts/x.sh'",
            # ionice/taskset exec their COMMAND (util-linux wrappers —
            # Codex on #128, round-26 review — verified live)
            b"ionice -c 3 sh -c 'bash scripts/x.sh'",
            b"ionice -c3 sh -c 'bash scripts/x.sh'",
            b"taskset -c 0 sh -c 'bash scripts/x.sh'",
            b"taskset --cpu-list 0 sh -c 'bash scripts/x.sh'",
            b"taskset 0x1 sh -c 'bash scripts/x.sh'",
            # `&&` separates even before a redirect — `x &&>f cmd`
            # runs cmd with its output redirected (Devin on #128,
            # round-26 review — verified in bash/dash)
            b"echo ok &&>out bash scripts/x.sh",
            b"echo ok &&>out bash scripts/x.sh >log"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # query-mode forms never exec a command
            b"taskset -p -c 0-3 1234",
            b"ionice -p 1234",
            # util-linux `-c` binds ONLY a separate operand — `-c0`
            # aborts "invalid option -- '0'" before the command
            # (Codex on #1393, round-52 review — verified live)
            b"taskset -c0 sh -c 'bash scripts/x.sh'",
            # an inert single action emits nothing executable
            b"find . -exec true \\; | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round27(tmp_path):
    """Round-27: an inner `$(` capture only rides the enclosing `$(`
    when its bytes survive the enclosing body's own stages — a sink
    like `wc -c` replaces them with a count (Devin on #11)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r27", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r27"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"e\n")
    for line in (
            # the inner capture's bytes are digested inside the body —
            # the outer capture emits only a count (Devin on #11,
            # round-27 review — `sh: 13: not found` live)
            b'echo $(echo "$(cat scripts/x.sh)" | wc -c) | sh',
            b'echo $(echo "$(cat scripts/x.sh)" | wc -l) | sh',
            # a non-emitting containing stage drops the bytes too —
            # `cat "$(cat x)"` treats them as a filename, `wc -l
            # <"$(cat x)"` digests them (modeled conservatively
            # pre-round-27; unchanged here)
            ):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # forwarding stages keep the bytes alive to the outer
            # capture and the exec after it (verified live)
            b'echo $(echo "$(cat scripts/x.sh)" | cat) | sh',
            b'echo $(echo "$(cat scripts/x.sh)" | grep x) | sh',
            b'echo $(echo "$(cat scripts/x.sh)" | sh) | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round28(tmp_path):
    """Round-28: chrt/unshare exec wrappers, a containing stage's `>`
    diversion drops capture bytes (inner subs, ticks, and emitted
    operands alike), and printf's format counts once (Devin + Codex
    on #128)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r28", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r28"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"e\n")
    for line in (
            # a `>` diversion on the containing stage drops the bytes
            # — outer capture, emitted operand, and inner sub alike
            # (Devin on #11/#128 — `sh` sees only a newline, live)
            b'echo "$(cat scripts/x.sh)" >/dev/null | sh',
            b'echo "$(cat scripts/x.sh)" >f | sh',
            b'echo $(echo "$(cat scripts/x.sh)" >/dev/null) | sh',
            b'echo $(echo `cat scripts/x.sh` >/dev/null) | sh',
            b'echo $(echo `cat scripts/x.sh` | wc -c) | sh',
            b"echo 'bash scripts/x.sh' >/dev/null | sh",
            b"printf 'bash scripts/x.sh' >/dev/null | sh",
            # query-mode forms never exec a command
            b"chrt -p 1234",
            b"chrt -o -p 0 1234"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # chrt [opts] PRIO CMD and unshare [opts] CMD exec their
            # command operand (Codex on #128 — verified live)
            b"chrt -o 0 sh -c 'bash scripts/x.sh'",
            b"chrt -f 20 sh -c 'bash scripts/x.sh'",
            b"unshare sh -c 'bash scripts/x.sh'",
            b"unshare --fork sh -c 'bash scripts/x.sh'",
            b"unshare -r sh -c 'bash scripts/x.sh'",
            b"unshare --wd /tmp sh -c 'bash scripts/x.sh'",
            # `2>` diverts only stderr — stdout still reaches the pipe
            b'echo "$(cat scripts/x.sh)" 2>f | sh',
            b'echo $(echo "$(cat scripts/x.sh)" 2>f) | sh',
            # program-flag consumption is redirect-independent —
            # `-c`/`eval` read the capture as program text, not stdout
            b'bash -c "$(cat scripts/x.sh)" >f',
            b'eval "$(cat scripts/x.sh)" >f',
            # live stages still propagate through the nesting
            b'echo $(echo `cat scripts/x.sh`) | sh',
            b'echo $(echo "$(cat scripts/x.sh)" | cat) | sh',
            b'echo $(echo "$(cat scripts/x.sh)" | sh) | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round29(tmp_path):
    """Round-29: a condition's own `>` diversion drops the stream
    (`if cat >/dev/null`), an unquoted `date +$(…)` from stdin only
    joins when the stream's words field-split to one, group-closer
    `)` is not a codec operand, and program-flag substitutions inside
    a date format still count as exec (Codex on #1957)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r29", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r29"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    (pdir / "scripts" / "single.sh").write_bytes(b"HIT\n")
    (pdir / "scripts" / "enc.sh").write_bytes(b"ZWNobyBISVQK\n")
    for line in (
            # the condition's own stdout is redirected — the stream
            # drains into the file, `then`/downstream see EOF
            b'cat scripts/x.sh | if cat >/dev/null; then :; fi | sh',
            b'cat scripts/x.sh | if cat >/dev/null; then cat; fi | sh',
            b'cat scripts/x.sh | if cat >/dev/null; then cat; fi '
            b'| cat | sh',
            # `date +$(cat)` from stdin: the script is 2 words → date
            # sees `+<fmt> <word2>` and errors — never a format
            b'cat scripts/x.sh | date +$(cat) | sh',
            b'cat scripts/x.sh | cat | date +$(cat) | sh',
            b'cat scripts/x.sh | tee | date +$(cat) | sh',
            b'cat scripts/x.sh | date +`cat` | sh',
            # a non-script stream stays indeterminate-count → fail
            # closed stays True only via provenance — `wc -l` emits
            # a count, not the bytes
            b'cat scripts/x.sh | wc -l | date +$(cat) | sh',
            # `date` non-format operand: the inner eval's emission is
            # never echoed, but the eval still executes the script —
            # counted via the program-flag path, not the echo path;
            # echo's capture re-emission is not a date format either
            b'date "$(echo "$(cat scripts/x.sh)")" | sh',
            b'date "$(echo "$(cat scripts/x.sh)" >/dev/null)" | sh',
            # an encoder (no -d) forwards script bytes, not decoded
            b'( cat scripts/x.sh | python -m base64 ) | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # the undiverted condition still forwards the stream
            b'cat scripts/x.sh | if cat; then cat >/dev/null; fi | sh',
            # a 1-word stream DOES field-join `+FMT` — date echoes it
            b'cat scripts/single.sh | date +$(cat) | sh',
            b'cat scripts/single.sh | cat | date +$(cat) | sh',
            b'cat scripts/single.sh | date +`cat` | sh',
            # quoted `+"$(…)"` joins regardless of word count
            b'date "+$(cat scripts/x.sh)" | sh',
            # a bare `)` is not a base64 operand — the group still
            # decodes+execs the script
            b'( cat scripts/enc.sh | python -m base64 -d ) | sh',
            b'( cat scripts/enc.sh | python3 -m base64 -d ) | sh',
            # `eval`/`bash -c` inside a date format run the script
            # before date reads its own argument — dep by exec, not
            # by echoed output
            b'date "$(eval "$(cat scripts/x.sh)")" | sh',
            b'date "$(bash -c "$(cat scripts/x.sh)")" | sh',
            b'echo "$(eval "$(cat scripts/x.sh)")" | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round30(tmp_path):
    """Round-30: sort's --sort and tac's -s/--separator consume a value
    word (GNU long prefixes too), and a capture AFTER a program flag's
    value is a positional ($0), not program text (Devin + Codex on
    #1957)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r30", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r30"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    for line in (
            # the capture sits AFTER the flag's own value — it is $0,
            # never program text (`bash -c 'true' "$(cat x)"` runs
            # `true`, verified live)
            b'bash -c "true" "$(cat scripts/x.sh)" | sh',
            b'bash -c "echo hi" "$(cat scripts/x.sh)" | sh',
            b'python -c x "$(cat scripts/x.sh)" | sh',
            b'perl -e x "$(cat scripts/x.sh)" | sh',
            # same positional rule inside a non-format date operand
            b'date "$(bash -c true "$(cat scripts/x.sh)")" | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # option values are not file operands — the stream flows
            # through sort/tac unchanged (verified live)
            b'cat scripts/x.sh | sort --sort numeric | sh',
            b'cat scripts/x.sh | sort --sort=numeric | sh',
            b'cat scripts/x.sh | tac --separator x | sh',
            b'cat scripts/x.sh | tac -s x | sh',
            # GNU unique long-prefix resolution consumes the value
            b'cat scripts/x.sh | tac --sep x | sh',
            # boolean tac flags leave stdin flowing
            b'cat scripts/x.sh | tac -r | sh',
            b'cat scripts/x.sh | tac -b | sh',
            # the capture IS the flag's value — program text either way
            b'bash -c "$(cat scripts/x.sh)" | sh',
            b'sh -c "$(cat scripts/x.sh)" | sh',
            b'sh -c -e "$(cat scripts/x.sh)" | sh',
            b'python -c "$(cat scripts/x.sh)" | sh',
            b'perl -e "$(cat scripts/x.sh)" | sh',
            # glued to the flag word itself
            b'bash -c$(cat scripts/x.sh) | sh',
            # `[=arg]`-optional and boolean unshare options never
            # consume the next word — `sh` stays the command head
            # (Devin on #11 — verified live)
            b'unshare --mount-proc sh -c "bash scripts/x.sh"',
            b'unshare --kill-child sh -c "bash scripts/x.sh"',
            b'unshare --map-auto sh -c "bash scripts/x.sh"',
            # while real separate-word operands keep consuming
            b'unshare --wd / sh -c "bash scripts/x.sh"',
            b'unshare --map-user 0 sh -c "bash scripts/x.sh"'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round31(tmp_path):
    """Round-31: `prlimit [opts] COMMAND` and `setpriv [opts] PROGRAM`
    are exec wrappers; `prlimit -p`/`setpriv --dump` are query modes
    that reject a trailing command (Codex on #128 — verified live)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r31", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r31"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    for line in (
            # prlimit resource limits attach `[=lim]` — the command
            # head survives to exec (verified live: `prlimit --cpu 1`
            # tries to exec `1`)
            b'prlimit --cpu=1 sh -c "bash scripts/x.sh"',
            b'prlimit --nofile=64 sh -c "bash scripts/x.sh"',
            b'prlimit sh -c "bash scripts/x.sh"',
            # `-o LIST` consumes the next word
            b'prlimit -o pid sh -c "bash scripts/x.sh"',
            # setpriv boolean flags leave PROGRAM as head
            b'setpriv --nnp sh -c "bash scripts/x.sh"',
            b'setpriv --no-new-privs sh -c "bash scripts/x.sh"',
            b'setpriv --reset-env sh -c "bash scripts/x.sh"',
            # while its value options still consume
            b'setpriv --reuid 0 sh -c "bash scripts/x.sh"',
            b'setpriv --regid 0 sh -c "bash scripts/x.sh"',
            b'setpriv --pdeathsig keep sh -c "bash scripts/x.sh"'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # query modes reject a trailing command entirely — nothing
            # executes (verified live: both error "mutually
            # exclusive"/"incompatible")
            b'setpriv --dump sh -c "bash scripts/x.sh"',
            b'setpriv -d sh -c "bash scripts/x.sh"',
            b'prlimit -p 1 sh -c "bash scripts/x.sh"',
            b'prlimit --pid 1 sh -c "bash scripts/x.sh"'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round32(tmp_path):
    """Round-32: interpreter value-options between a program flag and
    its operand (bash -O, python -X), positional captures a `-c`
    program echoes to stdout, substitutions inside describe-mode
    wrapper tails, and --help/--version never execing (Devin on #11 —
    verified live)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r32", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r32"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    for line in (
            # a value-option may sit between the program flag and its
            # operand (verified live: `dash -O` is not an option)
            b'bash -O extglob -c "$(cat scripts/x.sh)"',
            b'bash -o nounset -c "$(cat scripts/x.sh)"',
            b'dash -o nounset -c "$(cat scripts/x.sh)"',
            b'python -X dev -c "$(cat scripts/x.sh)"',
            b'python -W ignore -c "$(cat scripts/x.sh)"',
            b'perl -I lib -e "$(cat scripts/x.sh)"',
            b'node -r lib -e "$(cat scripts/x.sh)"',
            # a positional capture still executes when the `-c`
            # program echoes it to stdout piped to an exec head
            b'bash -c "printf %s \"$0\"" "$(cat scripts/x.sh)" | sh',
            b'bash -c "echo $0" "$(cat scripts/x.sh)" | sh',
            b'bash -c "echo \"$@\"" "$(cat scripts/x.sh)" | sh',
            # describe mode drops the command but substitutions in
            # its operands still run during expansion
            b'cat scripts/x.sh | setpriv --dump "$(sh)"',
            b'cat scripts/x.sh | setpriv -d "$(sh)"',
            b'cat scripts/x.sh | command -v "$(sh)"',
            b'cat scripts/x.sh | prlimit -p 1 "$(sh)"'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `dash -O` errors (no such option) — no exec
            b'dash -O extglob -c "$(cat scripts/x.sh)"',
            # positional capture with no positional reference stays
            # data (round-30 rule)
            b'bash -O extglob -c "true" "$(cat scripts/x.sh)" | sh',
            b'bash -c "true" "$(cat scripts/x.sh)" | sh',
            b'python -c x "$(cat scripts/x.sh)" | sh',
            # positional emit needs the downstream exec — bare output
            # to the terminal is not executed
            b'bash -c "printf %s \"$0\"" "$(cat scripts/x.sh)"',
            # describe tails still classify: a `$(cat)` capture eats
            # the pipe into argv — nothing executes
            b'cat scripts/x.sh | setpriv --dump "$(cat)"',
            b'cat scripts/x.sh | command -v "$(cat)"',
            # --help/--version print and exit on every wrapper
            b'cat scripts/x.sh | setpriv --help sh',
            b'cat scripts/x.sh | setpriv --dump sh -c "bash scripts/x.sh"',
            b'cat scripts/x.sh | prlimit --version sh',
            b'cat scripts/x.sh | unshare --version sh -c "bash scripts/x.sh"',
            b'cat scripts/x.sh | env --help sh',
            b'cat scripts/x.sh | sudo -V sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round33(tmp_path):
    """Round-33: GNU long-option PREFIX abbreviations still consume the
    program operand (`grep --reg PAT FILE`), a `cat FILE` mid-pipe
    replaces the stream, pinned script content reads use the pinned
    object's bytes, and a passthrough head emits a `<<<$(…)`
    here-string target (Devin on #11 — verified live)."""
    import importlib.util
    import subprocess as sp
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r33", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r33"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path / "plugin"
    pdir.mkdir()
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    for line in (
            # `--reg`/`--re` abbreviate --regexp/--regex and still take
            # the PATTERN operand — the FILE operand is read (verified
            # live: GNU long options resolve any unambiguous prefix)
            b"grep --reg PAT scripts/x.sh | sh",
            b"grep --re PAT scripts/x.sh | sh",
            b"grep --regex=PAT scripts/x.sh | sh",
            b"grep -E PAT scripts/x.sh | sh",
            b"grep -e PAT scripts/x.sh | sh",
            # `cat` with only stream-feeding operands forwards the pipe
            b"cat scripts/x.sh | cat - | sh",
            b"cat scripts/x.sh | cat /dev/null - | sh",
            # `cat`/`pv`/`tee` emit a `<<<$(…)` here-string target's
            # expansion — the capture's output reaches the exec
            b'date +$(cat <<<$(echo scripts/x.sh)) | sh',
            b'echo "$(cat <<<$(echo scripts/x.sh))" | sh',
            b'date +$(cat <<< $(echo scripts/x.sh)) | sh',
            b'date +$(cat <<<y$(echo scripts/x.sh)) | sh',
            # the LAST input redirect wins — `<<<` after `<<` makes the
            # here-string the stream (verified live)
            b'date +$(cat <<DELIM <<<$(echo scripts/x.sh)) | sh',
            # a substitution nested inside the here-string's target is
            # still emitted
            b'echo "$(cat <<<$(echo $(cat scripts/x.sh)))" | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `cat FILE` mid-pipe replaces the stream — the captured
            # script's bytes never reach `sh`
            b"cat scripts/x.sh | cat /dev/null | sh",
            b"cat scripts/x.sh | cat /etc/hostname | sh",
            # `tee`/`pv`/`echo` emitted-path-exec is out of model scope
            # (no invocation head anchors the analysis — same family
            # as bare `echo scripts/x.sh | sh`)
            b"echo scripts/x.sh | sh",
            # `<<<$(x)` is data when the head isn't a passthrough or a
            # file operand overrides stdin
            b'date +$(grep p <<<$(echo scripts/x.sh)) | sh',
            b'date +$(cat f <<<$(echo scripts/x.sh)) | sh',
            b'date +$(cat "$(echo scripts/x.sh)" <<<y) | sh',
            b'date +$(cat <<< x y$(echo scripts/x.sh)) | sh',
            b'echo "$(cat <<<$(echo scripts/x.sh) f)" | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # Pinned content reads: commit scripts/x.sh with ONE word, then dirty
    # the worktree to two — the verdict must follow the PINNED object's
    # bytes (worktree edits never flip it).
    reg_root = tmp_path / "registry"
    pf = reg_root / "platform" / "framework"
    (pf / "scripts").mkdir(parents=True)
    (pf / "scripts" / "x.sh").write_bytes(b"one\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sha = sp.run(["git", "rev-parse", "HEAD"], cwd=reg_root,
                 env=env, check=True, capture_output=True,
                 text=True).stdout.strip()
    (pf / "scripts" / "x.sh").write_bytes(b"one two\n")
    for line in (
            b"date +$(cat scripts/x.sh) | sh",
            b"cat scripts/x.sh | date +$(cat) | sh"):
        assert mod.script_dep_block(
            pf, line + b"\n", pinned_scripts={"x.sh"},
            pin_source=(reg_root, sha)), line
        # the two-word worktree alone would field-split — non-dep
        assert not mod.script_dep_block(pf, line + b"\n"), line

def test_pipe_to_exec_round34(tmp_path):
    """Round-34: GNU unambiguous option PREFIX abbreviations still bind
    (`setpriv --rui` → --ruid), glued short option values classify
    (`prlimit -p1` is query mode — the tail never runs), and a quoted
    separator is part of the literal command name (`"sh;"` is ENOENT,
    not sh) (Devin on #1957 — verified live)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r34", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r34"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    for line in (
            # unambiguous GNU long-option prefixes still take the value
            # operand — the utility word after it executes
            b"cat scripts/x.sh | setpriv --rui 1000 sh",
            b"cat scripts/x.sh | setpriv --ruid 1000 sh",
            b"cat scripts/x.sh | setpriv --bounding 5 sh",
            b"cat scripts/x.sh | prlimit --out=of sh",
            # glued short option values carry their operand in-word —
            # the command word after still runs
            b"cat scripts/x.sh | prlimit -o1 sh",
            b"cat scripts/x.sh | nice -n5 sh",
            b"cat scripts/x.sh | sudo -uroot sh",
            b"cat scripts/x.sh | ionice -c1 sh",
            # unquoted subshell/paren glue still splits
            b"cat scripts/x.sh | sh;",
            b"cat scripts/x.sh | (sh)",
            b'cat scripts/x.sh | "sh"',
            # describe-mode tails still EVALUATE their substitutions
            b'cat scripts/x.sh | setpriv --dump "$(sh)"',
            b'cat scripts/x.sh | prlimit -p 1 "$(sh)"',
            # a dynamic fd-dup target still binds stdin's default —
            # only the TARGET's own expansion counts, so `sh -s"$X"`
            # keeps the dup dynamic while `foo` stays literal
            b'cat scripts/x.sh | sh -s"$X"<&$FD',
            b'cat scripts/x.sh | sh -s<&$FD',
            b'cat scripts/x.sh | sh -s<&a"$X"',
            b'cat scripts/x.sh | sh -s<&"$FD"',
            b'cat scripts/x.sh | sh -s"$X"<&0',
            b'cat scripts/x.sh | sh >&"$FD"'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # an ambiguous GNU prefix is an option error — nothing
            # after it runs
            b"cat scripts/x.sh | setpriv --r 1000 sh",
            # glued/describe modes never exec a trailing command
            # (`prlimit -p1`: --pid and COMMAND are mutually
            # exclusive — verified live)
            b"cat scripts/x.sh | prlimit -p1 sh",
            b"cat scripts/x.sh | prlimit --pi 1 sh",
            b"cat scripts/x.sh | prlimit --pid=1 sh",
            b"cat scripts/x.sh | setpriv --du sh",
            # `timeout -s9 sh` — `sh` is the DURATION operand, an
            # invalid interval — the command never runs
            b"cat scripts/x.sh | timeout -s9 sh",
            # quoted separators are literal name bytes — `"sh;"`
            # names a `sh;` binary (ENOENT), not sh
            b'cat scripts/x.sh | "sh;"',
            b'cat scripts/x.sh | "sh{"',
            b'cat scripts/x.sh | "sh x"',
            # a literal or single-quoted non-numeric `<&`/`>&` target
            # is an ambiguous redirect — bash aborts before `sh` runs,
            # and an expansion EARLIER in the same word does not make
            # the literal target dynamic
            b'cat scripts/x.sh | sh -s"$X"<&foo',
            b'cat scripts/x.sh | sh -s<&foo',
            b"cat scripts/x.sh | sh -s\"$X\"<&'$FD'",
            b'cat scripts/x.sh | sh -s<&\\$FD',
            # a glued `-cLIST` aborts "invalid option" on util-linux
            # — the command never runs (Codex on #1393, round-52 —
            # verified live)
            b"cat scripts/x.sh | taskset -c0,1 sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_pipe_to_exec_round35(tmp_path):
    """Round-35: a nested capture's emit check recurses through every
    enclosing `$(` level (`date +"$(echo "$(echo $(cat x))")"`), the
    date field-split gate counts `cat -n`/`-b` numbering and honors a
    bare `IFS=` statement — and an ambiguous GNU long-option prefix
    (`sort --s`) is a command abort, not a value option (Devin on #128
    — verified live)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reg_r35", Path(__file__).parent.parent / "scripts" / "ai-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reg_r35"] = mod
    spec.loader.exec_module(mod)
    pdir = tmp_path
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    (pdir / "scripts" / "y.sh").write_bytes(b"a:\n")
    (pdir / "scripts" / "z.sh").write_bytes(b"a,,\n")
    for line in (
            # each enclosing capture level's emit check must recurse —
            # the inner `$(cat` reaches sh through two echo captures
            b'date +"$(echo "$(echo $(cat scripts/x.sh))")" | sh',
            b'date +"$(echo $(echo $(cat scripts/x.sh)))" | sh',
            # a bare `IFS=` statement leaves `echo HIT` one field —
            # `+echo HIT` is a valid format and reaches sh
            b"IFS=; date +$(cat scripts/x.sh) | sh",
            b"IFS=z; date +$(cat scripts/x.sh) | sh",
            # an IFS whose value contains parens survives paren-
            # stripping — `x(y` matches nothing in `echo HIT`
            b"IFS='x(y'; date +$(cat scripts/x.sh) | sh",
            b'IFS="x(y"; date +$(cat scripts/x.sh) | sh',
            # `,` never appears in `echo HIT` → still one field
            b"IFS=,; date +$(cat scripts/x.sh) | sh",
            # command substitution strips the file's trailing
            # newline BEFORE splitting — `a:\n` under IFS=: is ONE
            # field, so `+a` reaches sh
            b"IFS=:; date +$(cat scripts/y.sh) | sh",
            # a unique GNU prefix still binds its value operand —
            # `numeric` is --sort's argument; sort re-emits the pipe
            b"cat scripts/x.sh | sort --so numeric | sh",
            b"cat scripts/x.sh | sort --sort numeric | sh",
            b"cat scripts/x.sh | sort --sor numeric | sh",
            # an EXACT long option resolves even under prefix overlap —
            # `cat --show-ends` is valid though the --show-* family
            # shares the prefix shape (Codex on #1393, round-39 review)
            b"cat --show-ends scripts/x.sh | sort | sh",
            # remaining real sort options still forward the stream
            b"cat scripts/x.sh | sort --human-numeric-sort | sh",
            b"cat scripts/x.sh | sort --ignore-nonprinting | sh",
            # under `IFS=', '` `a:` has no delimiter — still one field
            b"IFS=', '; date +$(cat scripts/y.sh) | sh",
            # a dynamic `IFS=$(...)`/`IFS=$X` value can't be proven
            # to split — it binds ONE unknown value at runtime
            # (`IFS=$(echo ,)` leaves `echo HIT` one field — round-50,
            # verified live), so it binds like an EMPTY IFS
            b"IFS=$(echo ,); date +$(cat scripts/x.sh) | sh",
            b"IFS=$UNKNOWN; date +$(cat scripts/x.sh) | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `cat -n`/`cat -b` prepend a line number per numbered
            # line — the capture emits TWO words and date rejects the
            # extra operand before sh sees it
            b"date +$(cat -n scripts/x.sh) | sh",
            b"date +$(cat -b scripts/x.sh) | sh",
            b"date +$(cat --number scripts/x.sh) | sh",
            b"date +$(cat -vn scripts/x.sh) | sh",
            # `cat --number` resolves as the exact long option even
            # under the --number-nonblank prefix overlap, but its
            # numbered output can't execute downstream anyway (Devin
            # on #130, round-60 — verified live)
            b"cat --number scripts/x.sh | sort | sh",
            # an ambiguous GNU long-option prefix aborts the command —
            # `--s` matches both --sort and --stable
            b"cat scripts/x.sh | sort --s numeric | sh",
            # an unrecognized long option aborts the same way
            b"cat scripts/x.sh | sort --frobnicate numeric | sh",
            # a prefix IFS binds only for the command's environment —
            # word-splitting already used the default IFS
            b"IFS=x date +$(cat scripts/x.sh) | sh",
            # IFS=H splits `echo HIT` into two fields → date rejects
            b"IFS=H; date +$(cat scripts/x.sh) | sh",
            # a whitespace-containing IFS still splits on its
            # non-whitespace chars — `IFS='c '` cuts `echo` at `c`
            b"IFS='c '; date +$(cat scripts/x.sh) | sh",
            b"IFS=c; date +$(cat scripts/x.sh) | sh",
            # a subshell `(IFS=)` assignment never reaches the parent
            # shell — the capture splits on the default IFS
            b"(IFS=); date +$(cat scripts/x.sh) | sh",
            b"x=$(IFS=); date +$(cat scripts/x.sh) | sh",
            # an all-whitespace IFS behaves like the default split
            b'IFS=" "; date +$(cat scripts/x.sh) | sh',
            # the plain 2-word capture still errors the same way
            b"date +$(cat scripts/x.sh) | sh",
            # under `IFS=', '` adjacent commas each delimit a field —
            # `a,,` is TWO fields, so date rejects the capture
            b"IFS=', '; date +$(cat scripts/z.sh) | sh",
            # terminal modes print and exit before the wrapped argv —
            # `flock --help sh P` never runs P (Codex on #1393, r39)
            b"flock /tmp/l --help sh scripts/x.sh",
            b"flock /tmp/l --version sh scripts/x.sh",
            b"find . --help -exec sh scripts/x.sh",
            b"xargs --help sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round40(tmp_path):
    """Round-40 review (Codex/Devin/CodeRabbit on #130/#1393/#1959/#12 —
    every behavior verified against real GNU bash/find/xargs/flock):
    * the wrapped-command word itself executes (`xargs ./x.sh`,
      `flock L ./x.sh` — the word AT the wrap start is the command);
    * `flock -c`/`--command` bind a command STRING that runs via the
      shell (`flock L -c ./x.sh` → x.sh runs);
    * a find terminal-mode word ANYWHERE in the expression exits
      before actions (`find . -exec A \\; -help` prints usage), while
      a predicate operand (`-name --help`) is a PATTERN, not a
      terminal;
    * IFS field-splitting uses only whitespace bytes PRESENT in IFS
      (`IFS=', '` keeps `a\tb` one field) and a whitespace-only
      capture yields ZERO fields."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_text("echo HIT\n")
    (pdir / "scripts" / "wsonly.sh").write_text("  \n")
    (pdir / "scripts" / "tab.sh").write_text("a\tb\n")
    for line in (
            # the word AT the wrap start is the wrapped command —
            # `xargs ./scripts/x.sh` runs it (verified live)
            b"xargs ./scripts/x.sh",
            b"xargs -n2 ./scripts/x.sh",
            b"flock /tmp/l ./scripts/x.sh",
            # option-looking words AFTER the command word are the
            # command's argv — `flock L ./x.sh --help` still runs
            # x.sh (verified live)
            b"flock /tmp/l ./scripts/x.sh --help",
            b"xargs ./scripts/x.sh --help",
            # `flock -c`/`--command` binds the operand as SHELL text —
            # `flock L -c ./x.sh` runs it (verified live); the bare
            # `flock -c scripts/x.sh` form stays a prose mention like
            # every bare mid-command `scripts/` path (SCRIPT_REF)
            b"flock /tmp/l -c ./scripts/x.sh",
            b"flock /tmp/l -c 'sh scripts/x.sh'",
            b"flock /tmp/l --command 'sh scripts/x.sh'",
            # a predicate operand is not a terminal — `find . -name
            # --help -exec ...` runs the action for matches (verified)
            b"find . -name --help -exec sh scripts/x.sh \\;",
            b"find . -path --version -exec sh scripts/x.sh \\;",
            # a tab is not a delimiter under `IFS=', '` — `a\tb`
            # emits ONE field (verified live)
            b"IFS=', '; date +$(cat scripts/tab.sh) | sh",
            # audit-synced real long options still forward the stream
            b"cat scripts/x.sh | sed --unbuffered s/a/b/ | sh",
            b"cat scripts/x.sh | strings --unicode=l | sh",
            b"cat scripts/x.sh | tail --max-unchanged-stats=1 | sh",
            b"cat scripts/x.sh | od --output-duplicates | sh",
            b"cat scripts/x.sh | iconv --silent | sh",
            b"cat scripts/x.sh | grep --null-data x | sh",
            b"cat scripts/x.sh | awk --no-optimize '{print}' | sh",
            b"IFS=': '; date +$(cat scripts/y.sh) | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # a terminal-mode word AFTER the action still exits
            # before it runs — `find . -exec A \; -help` prints
            # usage only (verified live)
            b"find . -exec sh scripts/x.sh \\; -help",
            b"find . -exec sh scripts/x.sh \\; --version",
            b"find . -exec sh scripts/x.sh \\; -version",
            # a predicate operand that IS `-exec` is not an action —
            # `find . -name -exec sh x` errors on the stray `sh`
            b"find . -name -exec sh scripts/x.sh \\;",
            # a whitespace-only capture under mixed IFS yields ZERO
            # fields — date gets only `+` and emits a newline
            b"IFS=', '; date +$(cat scripts/wsonly.sh) | sh",
            # the -c operand is program text — inside it the path is
            # just an echo argument, never exec'd (verified live)
            b"flock /tmp/l -c 'echo scripts/x.sh'",
            # a bare mid-command path is a prose mention even under
            # `-c` — SCRIPT_REF only counts exec contexts
            b"flock /tmp/l -c scripts/x.sh",
            # audit-synced NON-options still abort — the phantom
            # hexdump names and `--numeric-storage` never existed
            b"cat scripts/x.sh | hexdump --one-byte-hexadecimal | sh",
            b"cat scripts/x.sh | sort --numeric-storage | sh",
            b"cat scripts/x.sh | awk --character-set=x '{print}' | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round41(tmp_path):
    """Round-41 review (Codex/Devin/CodeRabbit on #130/#1393/#1959/#12 —
    every behavior verified against real GNU bash/find/flock/split):
    * find operand primaries (`-fprint0`/`-atime`/`-newerXY`) consume
      the next word, so `-fprint0 --help` writes a FILE named --help
      and `-newerXt` is the only invalid XY form;
    * `-quit` cuts the expression where it stands — actions BEFORE it
      ran, words after are dead (`find . -quit -exec` runs nothing);
    * util-linux flock binds command text only to an exact `-c`/
      `--command` AFTER the lockfile — the attached `-cCMD`/
      `--command=CMD` forms and other post-file dash words are
      literal argv[0] names that fail to exec;
    * a `scripts/` path inside a find action argv counts only where
      the action's own command would exec it (`-exec sh x.sh` yes,
      `-exec echo x.sh` just prints the name);
    * `=value` on a flag-only GNU option aborts before reading
      (`cat --number=1`), while option arguments in separate-word
      form still feed the stream (`tail --max-unchanged-stats 1`,
      `pr --pages 1`);
    * split/csplit write chunks to FILES — nothing reaches stdout
      without `--filter` (`split | sh` leaves sh at EOF)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_text("echo HIT\n")
    for line in (
            # operand primaries consume `--help` as their operand —
            # the action still runs (verified live)
            b"find . -fprint0 --help -exec sh scripts/x.sh \\;",
            b"find . -atime --help -exec sh scripts/x.sh \\;",
            b"find . -newermt REF -exec sh scripts/x.sh \\;",
            b"find . -neweram REF -exec sh scripts/x.sh \\;",
            # `-quit` only kills LATER words — earlier actions ran
            b"find . -exec sh scripts/x.sh \\; -quit",
            # an exact post-file `-c`/`--command` binds command text
            b"flock /tmp/l -c 'sh scripts/x.sh'",
            b"flock /tmp/l --command 'sh scripts/x.sh'",
            b"flock /tmp/l -c 'bash scripts/x.sh' ; true",
            # interpreter argv[0] in an action reads the script file
            b"find . -exec sh scripts/x.sh \\;",
            b"find . -exec bash scripts/x.sh \\;",
            b"find . -exec python scripts/x.sh \\;",
            # separate-word values of required-arg long options are
            # consumed — the stream still forwards (verified live)
            b"cat scripts/x.sh | tail --max-unchanged-stats 1 | sh",
            b"cat scripts/x.sh | pr --pages 1 | sh",
            b"cat scripts/x.sh | strings --unicode l | sh",
            # `--filter` pipes each chunk to a command — it reaches
            # stdout (verified live)
            b"cat scripts/x.sh | split --filter='cat' - | sh",
            # glued `=v` on an arg-taking option still forwards
            b"cat scripts/x.sh | tail --pid=1 | sh",
            b"cat scripts/x.sh | pr --pages=1 | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `-quit` before the action kills it (verified live)
            b"find . -quit -exec sh scripts/x.sh \\;",
            # post-file attached `-c`/`--command=` are literal
            # command names flock fails to exec (verified live)
            b"flock /tmp/l -c'sh scripts/x.sh'",
            b"flock /tmp/l --command='sh scripts/x.sh'",
            # a non-`-c` post-file dash word is literal argv[0] —
            # `flock L -w 1 -c P` execs `-w` and fails (verified)
            b"flock /tmp/l -w 1 -c 'sh scripts/x.sh'",
            # `-c` before the lockfile is an invalid option —
            # flock aborts (verified live)
            b"flock -c 'sh scripts/x.sh' /tmp/l",
            # `-exec` argv[0] like `echo`/`cat` prints/reads the
            # literal name — never execs it (verified live)
            b"find . -exec echo scripts/x.sh \\;",
            b"find . -exec cat scripts/x.sh \\;",
            b"find . -exec wc -l scripts/x.sh \\;",
            # `=v` on a flag-only GNU option aborts the command —
            # `cat --number=1` errors before reading (verified)
            b"cat scripts/x.sh | cat --number=1 | sh",
            b"cat scripts/x.sh | sort --human-numeric-sort=bad | sh",
            b"cat scripts/x.sh | strings --all=x | sh",
            # split/csplit emit files (byte counts), never the
            # input stream (verified live)
            b"cat scripts/x.sh | split - | sh",
            b"cat scripts/x.sh | split --separator , - | sh",
            b"cat scripts/x.sh | split --unbuffered - | sh",
            b"cat scripts/x.sh | csplit - 2 | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
