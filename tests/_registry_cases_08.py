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


def test_script_dep_round59(tmp_path):
    """Round-59 review regression — Devin #130/#1393/#12/#1959 + Codex
    #1959 findings, all verified live on coreutils 8.32:
    - GNU SIZE lowercase units `m`, `kB`, `kiB`, `mB`, `miB` parse;
      `kb`/`g`/`gB`/`mb`/`gb` abort.
    - pr numeric operands are C `int` and per-option domains differ:
      `-N`/`--first-line-number` signed (INT_MIN..INT_MAX), `-o`/
      `--indent` zero-or-more, `-l`/`-w`/`-W`/`--columns` positive.
    - `pr --columns` aborts "page width too narrow" once each column
      is under two chars — bound (width+1)/2, default width 72.
    - `split -b` counts SIGNIFICANT digits — zero padding past 19
      chars still parses.
    - `split -a0` auto-computes suffix length — any numeric/hex
      `--numeric-suffixes=`/`--hex-suffixes=` start fits.
    - `cat -n`/-b/--number/--number-nonblank, `nl` (default/-b t) and
      `pr -n`/`--number-lines` prefix line numbers — `sh` runs `N`,
      not the script, so the dep must NOT fire. `nl -b n` pads with
      spaces only — still executes.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")

    for line in (
            # lowercase/multi-char GNU SIZE units that GNU accepts
            b"cat scripts/x.sh | head -n 1m | sh",
            b"cat scripts/x.sh | head -n 1mB | sh",
            b"cat scripts/x.sh | head -n 1miB | sh",
            b"cat scripts/x.sh | head -n 1kB | sh",
            b"cat scripts/x.sh | head -n 1kiB | sh",
            b"cat scripts/x.sh | head -c 1kiB | sh",
            b"split -b 1m --filter=sh scripts/x.sh",
            b"split -b 1kB --filter=sh scripts/x.sh",
            b"split -b 1miB --filter=sh scripts/x.sh",
            # -b significant digits — padding past 19 chars still runs
            b"split -b 0000000000000000000001 --filter=sh "
            b"scripts/x.sh",
            # -a0 auto-length: any suffix start fits
            b"split -a0 --numeric-suffixes=0 --filter=sh "
            b"scripts/x.sh",
            b"split -a0 --numeric-suffixes=999 --filter=sh "
            b"scripts/x.sh",
            b"split -a0 --hex-suffixes=fff --filter=sh "
            b"scripts/x.sh",
            # pr -N signed: -1, INT_MIN, INT_MAX all run
            b"cat scripts/x.sh | pr -N -1 | sh",
            b"cat scripts/x.sh | pr -N-1 | sh",
            b"cat scripts/x.sh | pr -N -2147483648 | sh",
            b"cat scripts/x.sh | pr -N 2147483647 | sh",
            b"cat scripts/x.sh | pr --first-line-number=-1 | sh",
            b"cat scripts/x.sh | pr --first-line-number=+5 | sh",
            # pr -o accepts zero and +; -w/-l/-W up to INT_MAX run
            b"cat scripts/x.sh | pr -o 0 | sh",
            b"cat scripts/x.sh | pr -o +3 | sh",
            b"cat scripts/x.sh | pr -o0 | sh",
            b"cat scripts/x.sh | pr -l 2147483647 | sh",
            b"cat scripts/x.sh | pr -l +5 | sh",
            # --columns fits the page-width bound
            b"cat scripts/x.sh | pr --columns 36 | sh",
            b"cat scripts/x.sh | pr --columns +2 | sh",
            b"cat scripts/x.sh | pr -w 100 --columns 50 | sh",
            b"cat scripts/x.sh | pr --columns 50 -w 100 | sh",
            # nl -b n forwards verbatim (space-padded still executes)
            b"cat scripts/x.sh | nl -b n | sh",
            b"cat scripts/x.sh | nl --body-numbering=n | sh",
            b"cat scripts/x.sh | nl -bn | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # unknown/unparseable units abort before the read
            b"cat scripts/x.sh | head -n 1kb | sh",
            b"cat scripts/x.sh | head -n 1g | sh",
            b"cat scripts/x.sh | head -n 1gB | sh",
            b"cat scripts/x.sh | head -n 1mb | sh",
            b"cat scripts/x.sh | head -c 1giB | sh",
            b"split -b 1g --filter=sh scripts/x.sh",
            b"split -b 1kb --filter=sh scripts/x.sh",
            # pr -N/-o bounds: int32 domain, -o rejects negatives
            b"cat scripts/x.sh | pr -N -2147483649 | sh",
            b"cat scripts/x.sh | pr -N 2147483648 | sh",
            b"cat scripts/x.sh | pr -o -1 | sh",
            b"cat scripts/x.sh | pr -o 2147483648 | sh",
            # pr -l/-w/-W positive-only domain
            b"cat scripts/x.sh | pr -l 0 | sh",
            b"cat scripts/x.sh | pr -l -1 | sh",
            b"cat scripts/x.sh | pr -w 0 | sh",
            b"cat scripts/x.sh | pr -W 0 | sh",
            b"cat scripts/x.sh | pr -l 2147483648 | sh",
            # --columns past (width+1)/2 aborts "page width too narrow"
            b"cat scripts/x.sh | pr --columns 37 | sh",
            b"cat scripts/x.sh | pr --columns 0 | sh",
            b"cat scripts/x.sh | pr --columns -1 | sh",
            b"cat scripts/x.sh | pr --columns 100 | sh",
            b"cat scripts/x.sh | pr -w 100 --columns 51 | sh",
            b"cat scripts/x.sh | pr -w 7 --columns 5 | sh",
            # numbering heads transform every content line — `sh`
            # receives `     1\tCMD` and runs `1`, not the script
            b"cat scripts/x.sh | cat -n | sh",
            b"cat scripts/x.sh | cat --number | sh",
            b"cat scripts/x.sh | cat -b | sh",
            b"cat scripts/x.sh | cat --number-nonblank | sh",
            b"cat scripts/x.sh | cat -An | sh",
            b"cat scripts/x.sh | nl | sh",
            b"cat scripts/x.sh | nl -b t | sh",
            b"cat scripts/x.sh | nl --body-numbering=a | sh",
            b"cat scripts/x.sh | pr -n | sh",
            b"cat scripts/x.sh | pr -tn | sh",
            b"cat scripts/x.sh | pr --number-lines | sh",
            b"cat scripts/x.sh | pr --number-lines=: | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round60(tmp_path):
    """Round-60 review regression — Devin #130/#1393/#12/#1959 + Codex
    #1959 findings, all verified live on coreutils 8.32:
    - A `cat` leading a pipe with `-n`/-b/--number/--number-nonblank
      rewrites every output line — the numbered text can't run
      downstream (`cat --number scripts/x.sh | sh` runs `1`, never
      the script — Devin on #130).
    - split's `/dev/stdin`/`/dev/fd/0`/`/proc/self/fd/0` INPUT operand
      obeys the same dead-stdin check as `-`/absent — `split -l1
      /dev/stdin --filter=X </dev/null` yields no chunks, the filter
      never fires (Devin on #12).
    - A `-exec` whose terminator is its FIRST argv word aborts the
      whole find expression ("invalid argument `;' to `-exec`")
      before any action runs — earlier spans die too (Devin on
      #1959).
    - A split `--filter` interpreter's program flag (`-c`/`-e`/`-m`/
      `--eval`) makes a following scripts/ path argv ($0), not the
      program (`bash -c true scripts/x.sh` — Codex on #1959); a
      shell `-s` reads the program from stdin the same way. The
      program word itself still execs (`bash -c 'sh x' y`).
    - The int() conversions strip `+`/zero-padding — a 5000-digit
      padded `-a`/`pr -w` operand parses without hitting Python's
      4300-digit limit (Devin on #1393).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    pad = b"0" * 5000 + b"1"
    for line in (
            # verbatim cat still deps; numbered cat anywhere in the
            # leading stage does not
            b"cat scripts/x.sh | sh",
            b"cat -E scripts/x.sh | sh",
            b"cat /etc/hosts scripts/x.sh | sh",
            b"cat <scripts/x.sh | sh",
            # a live stdin makes the fd-0 alias operand dep the same
            # way `-` does
            b"split -l1 /dev/stdin --filter='sh scripts/x.sh' <scripts/x.sh",
            b"split -l1 /dev/stdin --filter='sh scripts/x.sh'",
            # a real -exec action still deps
            b"find . -exec sh scripts/x.sh \\; | cat",
            b"find . -exec sh x \\; -exec sh scripts/x.sh \\; | cat",
            # the -c program text itself execs the script inside it
            b"split --filter='bash -c \"sh scripts/x.sh\" foo' -l1 f",
            b"split --filter='sh scripts/x.sh' -l1 f",
            # padded numeric operands parse (no int() crash) and dep
            b"split -a +" + pad + b" --filter='sh scripts/x.sh' f",
            b"split -a " + pad + b" -l1 f --filter='sh scripts/x.sh'",
            b"cat scripts/x.sh | pr --pages=+" + pad + b" | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # the flagged over-flag: numbering transforms the bytes
            # before any downstream head sees them
            b"cat --number scripts/x.sh | sh",
            b"cat -n scripts/x.sh | sh",
            b"cat -b scripts/x.sh | sh",
            b"cat --number-nonblank scripts/x.sh | sh",
            b"cat -An scripts/x.sh | sh",
            b"cat --number /etc/hosts scripts/x.sh | sh",
            b"cat --number <scripts/x.sh | sh",
            b"cat --number scripts/x.sh | sort | sh",
            # dead stdin through every fd-0 alias — no chunks, no
            # filter
            b"split -l1 /dev/stdin --filter='sh scripts/x.sh' </dev/null",
            b"split -l1 /dev/fd/0 --filter='sh scripts/x.sh' </dev/null",
            b"split -l1 /proc/self/fd/0 --filter='sh scripts/x.sh' </dev/null",
            b"split -l1 /dev/stdin --filter='sh scripts/x.sh' <&-",
            # `-exec`/`{} +`-empty argv or a missing terminator kills
            # every action in the expression
            b"find . -exec \\; | cat",
            b"find . -exec echo A \\; -exec \\; | cat",
            b"find . -exec \\; -exec sh scripts/x.sh \\; | cat",
            b"find . -exec echo A \\; -exec | cat",
            # a pure-reader filter head only emits the script's bytes
            # to the chunk stream — chunk files, not an exec
            b"split --filter='cat scripts/x.sh' -l1 f",
            # the scripts/ path after a program flag is argv ($0),
            # never read or executed
            b"split --filter='bash -c true scripts/x.sh' -l1 f",
            b"split --filter='sh -c true scripts/x.sh' -l1 f",
            b"split --filter='sh -ec \"true\" scripts/x.sh' -l1 f",
            b"split --filter='sh -s scripts/x.sh' -l1 f",
            b"split --filter='python -c \"x\" scripts/x.py' -l1 f",
            b"split --filter='perl -e \"x\" scripts/x.pl' -l1 f",
            # a `pr` operand parse that would have crashed int() —
            # GNU aborts "invalid line width" on the huge value
            b"cat scripts/x.sh | pr -w " + pad + b" --columns 36 | sh",
            b"cat scripts/x.sh | pr --pages=-" + pad + b" | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round61(tmp_path):
    """Round-61 review regression — Devin #1393/#1959/#130 + Codex
    #1393 findings, all verified live (bash 5 / dash / coreutils
    8.32 / util-linux 2.37):
    - A shell `-c` in a cluster takes the NEXT word as its program —
      the chars after it are still flags (`bash -sc 'sh x'` runs x;
      `-cs`, `-cx`, `-scx` too), and `-s` only moves the program to
      stdin when NO `-c` appears (`sh -s x` -> x is argv; `sh -s -c
      'sh x'` still runs x).
    - `-` as a word is per-interpreter: shells read it as
      end-of-options (`sh - x` runs x — bash AND dash) while
      python/perl/ruby/node/lua read the PROGRAM from stdin.
    - `cat -n`/`-b`/`--number` inside `date +$(…)` numbers the
      captured bytes — a narrow IFS emits the NUMBERED field, `sh`
      runs `1`, never the script (Devin on #130).
    - `sed --binary`/`--zero-terminated` are real GNU options that
      forward the stream (Codex on #1393).
    - `taskset --cpu 0 CMD` uniquely abbreviates `--cpu-list` — the
      mask already binds via the option, so `sh` is the command
      (Devin on #1959); `taskset --cpu=0` still aborts.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    for line in (
            # -c wins over -s in any cluster order — the program is
            # always the next word
            b"split --filter='bash -sc \"sh scripts/x.sh\" foo' -l1 f",
            b"split --filter='bash -sc scripts/x.sh' -l1 f",
            b"split --filter='bash -cs scripts/x.sh' -l1 f",
            b"split --filter='sh -cx scripts/x.sh' -l1 f",
            b"split --filter='sh -scx scripts/x.sh' -l1 f",
            b"split --filter='sh -s -c \"sh scripts/x.sh\" f' -l1 f",
            # `-` ends shell options — the next word is the program
            b"split --filter='sh - scripts/x.sh' -l1 f",
            b"split --filter='bash - scripts/x.sh' -l1 f",
            # python's `-s` is a plain flag, not stdin-program
            b"split --filter='python -s scripts/x.py' -l1 f",
            # verbatim capture still deps
            b"IFS=,; date +$(cat scripts/x.sh) | sh",
            b"IFS=,; date +$(cat -E scripts/x.sh) | sh",
            # sed's stream mode options forward the pipe
            b"cat scripts/x.sh | sed '' --binary | sh",
            b"cat scripts/x.sh | sed '' --zero-terminated | sh",
            b"cat scripts/x.sh | sed --bin '' | sh",
            # taskset cpu-list abbreviations bind the mask inline —
            # sh is the wrapped command
            b"cat scripts/x.sh | taskset --cpu 0 sh",
            b"cat scripts/x.sh | taskset --cp 0 sh",
            b"cat scripts/x.sh | taskset --cpu-list 0 sh",
            b"cat scripts/x.sh | taskset -c 0 sh",
            b"cat scripts/x.sh | taskset 0x1 sh",
            # `-ok … ;` is the legal interactive action
            b"find . -exec sh scripts/x.sh \\; -ok echo {} \\;",
            b"find . -ok echo {} \\; -exec sh scripts/x.sh \\;",
            b"find . -execdir sh scripts/x.sh \\; -exec echo {} +"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # -s moves the program to stdin only without -c
            b"split --filter='sh -s scripts/x.sh' -l1 f",
            b"split --filter='sh -es scripts/x.sh' -l1 f",
            b"split --filter='sh -s -c \"true\" scripts/x.sh' -l1 f",
            # `-` is the stdin program for non-shell interpreters —
            # the scripts/ word is argv
            b"split --filter='python - scripts/x.py' -l1 f",
            b"split --filter='perl - scripts/x.pl' -l1 f",
            b"split --filter='ruby - scripts/x.rb' -l1 f",
            # numbered captures emit line-prefixed bytes — never the
            # script
            b"IFS=,; date +$(cat -n scripts/x.sh) | sh",
            b"IFS=,; date +$(cat -b scripts/x.sh) | sh",
            b"IFS=,; date +$(cat --number scripts/x.sh) | sh",
            b"IFS=,; date +$(cat -An scripts/x.sh) | sh",
            b"date +$(cat -n scripts/x.sh) | sh",
            # `taskset --cpu=0` aborts "doesn't allow an argument";
            # `-p` is the pid query mode
            b"cat scripts/x.sh | taskset --cpu=0 sh",
            b"cat scripts/x.sh | taskset -p 0 sh",
            # `-ok`/`-okdir` accept only `;` — a `{} +` terminator is
            # a "missing argument" parse abort BEFORE traversal, so
            # no action in the expression runs (Devin on #12,
            # round-61 — verified live on findutils 4.8)
            b"find . -exec sh scripts/x.sh \\; -ok echo {} +",
            b"find . -exec sh scripts/x.sh \\; -okdir echo {} +",
            b"find . -ok echo {} + -exec sh scripts/x.sh \\;",
            b"find . -exec sh scripts/x.sh \\; -ok echo"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round61_dash_c_cluster(tmp_path):
    """`bash -ctrue x.sh`: chars after `-c` are still flags — the
    NEXT word is the program (`bash -ctrue` parses -t/-r/-u/-e and
    tries to exec x.sh — Devin on #1393, round-61 — verified live),
    so a scripts/ operand in program position deps like any other
    `sh x` invocation."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo HIT\n")
    for line in (
            b"split --filter='bash -ctrue scripts/x.sh' -l1 f",
            b"split --filter='sh -ctrue scripts/x.sh' -l1 f",
            b"split --filter='bash -cl scripts/x.sh' -l1 f"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round62_operand_sink_gate(tmp_path):
    """An emit/transform head that aborts on its own flags or ends the
    stream emits none of the operand's bytes — the dep must not fire
    (verified live): `tail --pid nope`/`--follow=wat` exit "invalid
    argument" before any read, `tail -n 0`/`head -n 0` emit nothing,
    `cat -n` prefixes `N<TAB>` per line (`sh` runs `1`, not the
    script), and `grep -q` emits no bytes at all. `cat -n` of a file
    WITH separators still executes the post-separator command (the
    round-62 `_numbered_flows` rule), while a `-`/stdin operand gets
    the same prefix and is neutered (`cat -n f -` runs `1`, not the
    piped script)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo SEPFREE\n")
    (pdir / "scripts" / "sep.sh").write_bytes(b"true; echo RAN\n")
    for line in (
            # flag-abort / zero-limit emit heads feed nothing downstream
            b"tail --pid nope scripts/x.sh | sh",
            b"tail --pid=nope scripts/x.sh | sh",
            b"tail --follow=wat scripts/x.sh | sh",
            b"tail -n 0 scripts/x.sh | sh",
            b"head -n 0 scripts/x.sh | sh",
            # numbered operands feed `N` to sh, never the command
            b"cat -n scripts/x.sh | sh",
            b"cat -b scripts/x.sh | sh",
            b"cat --number scripts/x.sh | sh",
            # quiet grep emits nothing
            b"grep -q p scripts/x.sh | sh",
            # a `-`/stdin operand inside a numbering read is numbered
            # and neutered too
            b"cat scripts/x.sh | cat -n - | sh",
            b"cat scripts/x.sh | cat -n f - | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # the live-head cases still dep
            b"tail scripts/x.sh | sh",
            b"tail -n 1 scripts/x.sh | sh",
            b"tail -n +1 scripts/x.sh | sh",
            b"tail -c +1 scripts/x.sh | sh",
            b"tail --pid=1 scripts/x.sh | sh",
            b"head scripts/x.sh | sh",
            b"cat scripts/x.sh | sh",
            b"grep p scripts/x.sh | sh",
            # `cat -n` of a file WITH a top-level separator still
            # executes the post-separator command
            b"cat -n scripts/sep.sh | sh",
            # sh-family program positions are untouched
            b"sh scripts/x.sh",
            b"bash scripts/x.sh",
            b"sh -c scripts/x.sh",
            b"bash -o nounset scripts/x.sh",
            b"bash -o nounset -c scripts/x.sh",
            # a scripts/ argv word is a reference dep even at $0
            b"sh -c 'true' scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round62_quoted_positional(tmp_path):
    """A bare positional of a sh-family head is the program FILE — but
    only unquoted: a quoted name is one literal filename whose
    scripts/ bytes are filename fragments (`bash 'x.sh;safe'` opens
    the literal `x.sh;safe` name, `find -exec sh 'bash x.sh'` a file
    named `bash x.sh` — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"bash 'scripts/x.sh;safe'",
            b"bash 'scripts/x.sh safe'",
            b"find . -exec sh 'bash scripts/x.sh' \\;",
            b"flock /tmp/l.lock 'bash scripts/x.sh'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"bash 'scripts/x.sh'",
            b"sh 'scripts/x.sh'"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round63_numbered_stream_operand(tmp_path):
    """A `-`/feed-stream operand inside a numbering cat reads the
    UPSTREAM stream — `cat x | cat -n -` emits `1\tX` (digits+tab
    per line), which `|sh` cannot run (Devin on #1393, round-63
    review — verified live). The provenance must thread through
    `$(`-capture and emit-head gates too (`echo "$(cat -n -)"` —
    the capture's stdout is numbered text, not dep bytes)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    (pdir / "f").write_bytes(b"echo F\n")
    for line in (
            b"cat scripts/x.sh | cat -n - | sh",
            b"cat scripts/x.sh | cat -n | sh",
            b"cat scripts/x.sh | cat -n f - | sh",
            b"cat scripts/x.sh | cat -n - f | sh",
            b"cat scripts/x.sh | cat -n - | sh; echo S",
            b"cat scripts/x.sh | echo \"$(cat -n f -)\" | sh",
            b"cat scripts/x.sh | echo \"$(cat -n -)\" | sh",
            b"cat scripts/x.sh | echo \"$(cat f)\" | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # bare `cat -` forwards verbatim — still a dep
            b"cat scripts/x.sh | cat - | sh",
            b"cat scripts/x.sh | cat - f | sh",
            b"cat scripts/x.sh | head -n 1 - | sh",
            b"cat scripts/x.sh | echo \"$(cat)\" | sh",
            # non-numbered mixed operands still forward x verbatim
            b"cat scripts/x.sh | echo \"$(cat f -)\" | sh",
            b"cat scripts/x.sh | echo \"$(cat -)\" | sh",
            # `-v` keeps the bytes runnable (show-nonprinting doesn't
            # alter `echo X`)
            b"cat scripts/x.sh | cat -v - | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round63_find_redirect_operand(tmp_path):
    """`find . -name > out -exec sh x` never runs -exec: the redirect
    words leave find's argv, so `-name` binds `-exec` itself as the
    pattern and find aborts "paths must precede expression" before
    reaching the action (Devin on #130, round-63 review — verified
    live). Only a COMPLETE predicate + redirect + action still runs
    the action."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"find . -name > out -exec sh scripts/x.sh \\;",
            b"find . -type > out -exec sh scripts/x.sh \\;",
            b"find . -newer > out -exec sh scripts/x.sh \\;",
            b"find . -perm > out -exec sh scripts/x.sh \\;"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"find . -exec sh scripts/x.sh \\;",
            b"find . -name 'a*' > out -exec sh scripts/x.sh \\;",
            b"find . -type f > out -exec sh scripts/x.sh \\;",
            b"find . -newer a > out -exec sh scripts/x.sh \\;"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round63_nice_strict_table(tmp_path):
    """util-linux `nice` aborts "unrecognized option" on any unknown
    option — `nice --bogus x` never runs x (Codex on #1393, round-63
    review — verified live) — but `nice -5` is the legacy glued
    ADJUSTMENT, not an unknown option, and `-n`/`--adjustment` take
    separate operands."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"nice --bogus sh scripts/x.sh",
            b"nice --bogus=n sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"nice -5 sh scripts/x.sh",
            b"nice -+5 sh scripts/x.sh",
            b"nice --5 sh scripts/x.sh",
            b"nice -n 2 sh scripts/x.sh",
            b"nice -n2 sh scripts/x.sh",
            b"nice --adjustment=2 sh scripts/x.sh",
            b"nice --adjustment 2 sh scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round63_split_output_prefix(tmp_path):
    """`split -n 1/1 - pfx` treats `pfx` as the chunk output PREFIX —
    it is never opened as input (Devin on #1959, round-63 review —
    verified live). The INPUT positional and the --filter value are
    the only dep words."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"split -n 1/1 - scripts/x.sh | sh",
            b"split -n 2/4 - scripts/x.sh | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"split -n 1/1 scripts/x.sh pfx | sh",
            b"split -n 1/1 scripts/x.sh | sh",
            b"split --filter='sh scripts/x.sh' /etc/hosts",
            b"split --filter='./scripts/x.sh' /etc/hosts",
            b"split --filter=./scripts/x.sh /etc/hosts"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round63_filter_emit_head(tmp_path):
    """`split --filter='echo sh scripts/x.sh'` runs `echo` — the
    filter PRINTS `sh scripts/x.sh` as chunk text, it never execs
    x.sh (Devin on #1959, round-63 review — verified live). The
    emit-head check applies to the filter's command word, not the
    head word of the scripts/ reference inside it."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"split --filter='echo sh scripts/x.sh' /etc/hosts",
            b"split --filter='true; echo sh scripts/x.sh' /etc/hosts",
            b"split --filter='printf %s scripts/x.sh' /etc/hosts",
            b"split --filter='yes sh scripts/x.sh' /etc/hosts",
            # printed PATH text exec'd by the filter's own `|sh` is
            # the accepted emitted-path-exec gap — only content bytes
            # count
            b"split --filter='echo scripts/x.sh | sh' /etc/hosts"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"split --filter='sh scripts/x.sh' /etc/hosts",
            b"split --filter='bash scripts/x.sh' -",
            b"split --filter='cat scripts/x.sh' - | sh",
            # a later command still execs past a printed match
            b"split --filter='echo x; sh scripts/x.sh' /etc/hosts",
            # echo's emitted TEXT re-parses as an invocation for the
            # inner `|sh` — `echo sh x | sh` runs `sh x` (Devin on
            # #1959, round-65 review — verified live)
            b"split --filter='echo sh scripts/x.sh | sh' /etc/hosts",
            # the filter's own `|sh` execs the emitted CONTENT bytes
            b"split --filter='cat scripts/x.sh | sh' /etc/hosts"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round64_filter_command_head(tmp_path):
    """Round-64 review regression — Devin #1393/#130/#12/#1959
    findings, all verified live (bash 5 / coreutils 8.32 /
    util-linux 2.37):
    - A scripts/ match's role comes from the COMMAND containing it,
      not the filter's first word: `true; echo sh x` only PRINTS the
      path (Devin on #130) while `echo x; sh x` still execs a later
      command past the printed match (Devin on #1393).
    - A filter's own pipe tail execs emitted content bytes:
      `cat x | sh` inside --filter runs x (Devin on #12).
    - A bare numbered reader (`nl`/`pr -n`/`cat -n` with NO operand)
      defaults to stdin — a sep-carrying scripts/ stream still execs
      the post-`;` command downstream (Devin on #1959).
    - `nice -+N`/`--N` are valid SIGNED legacy adjustments — the
      command still runs (Devin on #1393); `-+x`/`-2x` still abort.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    (pdir / "scripts" / "sep.sh").write_bytes(b"true; echo RAN\n")
    for line in (
            b"split --filter='true; echo sh scripts/x.sh' /etc/hosts",
            b"split --filter='echo sh scripts/x.sh' /etc/hosts",
            # printed PATH text exec'd by an inner `|sh` is the
            # accepted emitted-path-exec gap — only content bytes count
            b"split --filter='echo scripts/x.sh | sh' /etc/hosts",
            b"cat scripts/x.sh | nl | sh",
            b"cat scripts/x.sh | pr -n | sh",
            b"cat scripts/x.sh | cat -n | sh",
            b"nice -+x sh scripts/x.sh",
            b"nice -2x sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"split --filter='echo x; sh scripts/x.sh' /etc/hosts",
            b"split --filter='cat scripts/x.sh | sh' /etc/hosts",
            b"cat scripts/sep.sh | nl | sh",
            b"cat scripts/sep.sh | pr -n | sh",
            b"cat scripts/sep.sh | cat -n | sh",
            b"cat scripts/sep.sh | nl -b n | sh",
            b"nice -+5 sh scripts/x.sh",
            b"nice --5 sh scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round65_terminal_and_pipeline(tmp_path):
    """Round-65 review regression — Codex/Devin on #130/#1393/#12/#1959,
    all verified live (bash 5 / dash / coreutils / util-linux):
    - Terminal interpreter modes inside a filter exit before the
      program: `bash -n`/`--help`/`--version`, `python3 -V`/`-h`/
      `--version`/`--help` never touch the operand.
    - A reader/emitted match is bounded by its OWN pipeline tail:
      `cat x | true; sh` dies at `true` (the `; sh` sibling never
      sees the bytes); `cat x | sh` inside the filter still execs.
    - Emit heads whose emitted TEXT is an invocation re-parse in the
      tail (`echo sh x | sh`) and in an outer `|sh` downstream.
    - A `<` stdin rebind is only read when the head has no non-feeder
      file operand: `head /dev/null <x` never opens x.
    - A redirect word never satisfies a required option operand:
      `unshare --setuid >/dev/null sh` aborts at the option parse.
    - Redirects don't leak into split's argv scan:
      `split --filter=sh x > /tmp/out` still execs the chunks.
    - Shell argv-positionals past `-s`, a `-c` operand, or `--` are
      $0/argv, never read (`bash -s -- -c x`, `bash -c : x`,
      `bash -- -c x`); `bash -- x` still runs x.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    (pdir / "scripts" / "sep.sh").write_bytes(b"true; echo RAN\n")
    for line in (
            b"split --filter='bash --help scripts/x.sh' /etc/hosts",
            b"split --filter='bash -n scripts/x.sh' /etc/hosts",
            b"split --filter='bash -n -c \"echo HI scripts/x.sh\"' /etc/hosts",
            b"split --filter='python3 -V scripts/x.sh' /etc/hosts",
            b"split --filter='python3 --version scripts/x.sh' /etc/hosts",
            b"split --filter='cat scripts/x.sh | true; sh' /etc/hosts",
            b"split --filter='cat scripts/x.sh | true' - | sh",
            b"cat scripts/x.sh; sh",
            b"cat scripts/x.sh | true | sh",
            b"head /dev/null <scripts/x.sh | sh",
            b"cat scripts/x.sh | unshare --setuid >/dev/null sh",
            b"split --filter='echo scripts/x.sh' -",
            b"split --filter='echo scripts/x.sh' - | sh",
            b"bash -s -- -c scripts/x.sh",
            b"bash -s scripts/x.sh",
            b"bash -- -c scripts/x.sh",
            b"bash -n scripts/x.sh",
            b"bash --help scripts/x.sh",
            b"bash --version scripts/x.sh",
            b"python3 -V scripts/x.sh",
            b"python3 -h scripts/x.sh",
            b"python3 --help scripts/x.sh",
            b"python3 -c 'import os' scripts/x.sh",
            b"perl -e 'print 1' scripts/x.sh",
            b"nl -bn /dev/null <scripts/x.sh | sh",
            b"sort /dev/null <scripts/x.sh | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"split --filter='true; echo sh scripts/x.sh | sh' /etc/hosts",
            b"split --filter='echo sh scripts/x.sh' - | sh",
            b"split --filter='echo sh scripts/x.sh | sh' /etc/hosts",
            b"split --filter=sh scripts/x.sh > /tmp/out",
            b"split --filter='cat scripts/x.sh | sh' /etc/hosts",
            b"split --filter='cat scripts/x.sh' - | sh",
            b"cat scripts/x.sh | sh",
            b"cat <scripts/x.sh | sh",
            b"cat - <scripts/x.sh | sh",
            b"cat scripts/x.sh | tr a b | sh",
            b"cat scripts/x.sh | unshare --setuid 0 sh",
            b"bash -- scripts/x.sh",
            # post-`-c`-operand argv words are still reference deps —
            # same convention as `bash -c 'true' x` (round-56/62)
            b"bash -c : scripts/x.sh",
            b"python3 scripts/x.sh",
            b"perl scripts/x.sh",
            b"sh scripts/x.sh",
            b"bash -c 'sh scripts/x.sh'"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round66_sudo_shell_xargs_abort_interp(tmp_path):
    """Review round 66 — verified against live bash/coreutils:

    - `sudo -s`/`--shell`/`-i`/`--login` run the tail as ONE `$SHELL
      -c` program string — the tail head word is program text
      (`sudo -s 'sh x.sh'` execs x.sh; `sudo -s 'echo x.sh'` follows
      the `bash -c 'echo x'` over-include convention). Bare `sudo
      x.sh` stays a literal argv[0] name, `sudo -- -s x` execs `-s`
      (ENOENT), `-u` swallows an operand letter in a cluster
      (`sudo -us` = -u "s", not shell mode).
    - A malformed xargs option operand aborts before the utility:
      `-n`/`-s`/`-l`/`--max-args` <1 or non-numeric, `-P`/`--max-procs`
      <0, `-d`/`--delimiter` multi-char (`xargs -n 0 sh x` runs
      nothing); `-P 0`, `-n +2`, `-d ,` are valid.
    - Interpreter option-operands bind their own word — `python3 -X
      dev -V` still prints the version (`-X`/`-W`/
      `--check-hash-based-pycs` consume a separate operand word);
      `-c` ends option parsing at its operand.
    - Inert interpreter modes inside find -exec / flock / xargs tails
      never run the word (`-exec bash -n x` parses, `-exec sh -s x`
      reads stdin, `-exec python3 -V x` prints the version).
    - Redirect words no longer leak into split's filter/input argv
      scan (`split --filter='cat x' in > /tmp/o` reads the file
      operand — emitted-to-file bytes are a dep only when the filter
      itself execs).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"sudo scripts/x.sh",
            b"sudo -- scripts/x.sh",
            b"sudo -- -s scripts/x.sh",
            b"sudo -u root scripts/x.sh",
            b"sudo rm scripts/x.sh",
            b"sudo rm scripts/x.sh | sh",
            b"sudo scripts/x.sh | sh",
            b"sudo 'sh scripts/x.sh'",
            b"sudo -us 'sh scripts/x.sh'",
            b"echo hi | xargs -n 0 sh scripts/x.sh",
            b"echo hi | xargs -n -1 sh scripts/x.sh",
            b"echo hi | xargs -n bad sh scripts/x.sh",
            b"echo hi | xargs --max-args=bad sh scripts/x.sh",
            b"echo hi | xargs -P -1 sh scripts/x.sh",
            b"echo hi | xargs -d xy sh scripts/x.sh",
            b"echo hi | xargs -s 0 sh scripts/x.sh",
            b"echo hi | xargs -n scripts/x.sh sh y",
            b"find . -exec bash -n scripts/x.sh \\;",
            b"find . -exec sh -n scripts/x.sh \\;",
            b"find . -exec python3 -V scripts/x.py \\;",
            b"find . -exec bash --version scripts/x.sh \\;",
            b"find . -exec bash --help scripts/x.sh \\;",
            b"find . -exec sh -s scripts/x.sh \\;",
            b"find . -exec sh -c 'true' scripts/x.sh \\;",
            b"flock /tmp/l bash -n scripts/x.sh",
            b"flock /tmp/l sh -s scripts/x.sh",
            b"flock /tmp/l python3 -V scripts/x.py",
            b"echo hi | xargs bash -n scripts/x.sh",
            b"echo hi | xargs sh -s scripts/x.sh",
            b"python3 -X dev -V scripts/x.py",
            b"python3 -W default -V scripts/x.py",
            b"python3 -c 'print(1)' -V",
            b"split --filter='cat scripts/x.sh' in > /tmp/o",
            b"split --filter='cat scripts/x.sh' in > /tmp/o | sh",
            b"split --filter='sh scripts/x.sh' -n 1/1 in",
            b"split -n 1/1 scripts/x.sh out"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"sudo -s 'sh scripts/x.sh'",
            b"sudo --shell 'sh scripts/x.sh'",
            b"sudo -i 'sh scripts/x.sh'",
            b"sudo --login 'sh scripts/x.sh'",
            b"sudo -s scripts/x.sh",
            b"sudo -si 'sh scripts/x.sh'",
            b"sudo -s 'echo scripts/x.sh'",
            b"sudo -u root -s 'sh scripts/x.sh'",
            b"nice sudo -s 'sh scripts/x.sh'",
            b"sudo -n -s scripts/x.sh",
            b"sudo cat scripts/x.sh",
            b"sudo sh scripts/x.sh",
            b"sudo cat scripts/x.sh | sh",
            b"echo hi | xargs -n +2 sh scripts/x.sh",
            b"echo hi | xargs -P 0 sh scripts/x.sh",
            b"echo hi | xargs -n 2 sh scripts/x.sh",
            b"echo hi | xargs -d , sh scripts/x.sh",
            b"echo hi | xargs sh scripts/x.sh",
            b"echo hi | xargs bash scripts/x.sh",
            b"find . -exec bash -c 'sh scripts/x.sh' \\;",
            b"find . -exec bash scripts/x.sh \\;",
            b"flock /tmp/l bash scripts/x.sh",
            b"flock /tmp/l sh -c 'sh scripts/x.sh'",
            b"python3 -X dev scripts/x.py",
            b"python3 -W default scripts/x.py",
            b"split --filter='sh scripts/x.sh' in > /tmp/o",
            b"bash -c : scripts/x.sh",
            b"bash scripts/x.sh",
            b"cat scripts/x.sh | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round67_opt_operands_wrapper_values(tmp_path):
    """Round-67 shell-semantics fixes — all verified live on bash/dash
    and real util-linux/python tools:

    - Shell `-o`/`-O` bind option NAMES, never program text — a name
      that can't be one (`bash -o x.sh`, `dash -o ./f`) aborts the
      whole command before any program (Codex on #130). Invented
      identifier-shaped names stay over-blocked by design (the
      union-of-versions direction).
    - `-o` operands bind following words in cluster order; `-c`'s
      operand is the first NON-OPTION word after option parsing — it
      binds LAST even when `c` precedes `o` (`bash -co X n` gives
      o→X (abort), c→n).
    - `python3 -X`/`-W` consume a following operand word — any value
      warns-or-runs (`python3 -X bogus x` still runs x); a missing
      operand aborts. `--check-hash-based-pycs` aborts on values
      outside {default,always,never} (glued or separate).
    - `xargs -n <5000 digits>` still runs (the count saturates) —
      the operand check must not call int() on unbounded digits.
    - Wrapper operand VALUES that abort before the wrapped program:
      `nice -n 5.5` ("invalid adjustment"), `ionice -c -5`
      ("unknown scheduling class"), `ionice -n x` ("invalid class
      data argument"). Values that parse but fail at the syscall
      (`nice -n -5` unprivileged, `ionice -n -1`) still count.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    (pdir / "scripts" / "y.sh").write_bytes(b"echo Y\n")
    (pdir / "scripts" / "x.py").write_bytes(b"print(1)\n")
    for line in (
            b"bash -o scripts/x.sh",
            b"bash -O scripts/x.sh",
            b"dash -o scripts/x.sh",
            b"bash -o scripts/x.sh -c p",
            b"bash -o ./scripts/x.sh scripts/y.sh",
            b"bash -onounset scripts/x.sh",
            b"bash -co scripts/x.sh nounset",
            b"bash -oc scripts/x.sh nounset",
            b"split -n 3 --filter=\"bash -o scripts/x.sh\" /tmp/d",
            b"python3 -X scripts/x.py",
            b"python3 --check-hash-based-pycs bogus scripts/x.py",
            b"python3 --check-hash-based-pycs=bogus scripts/x.py",
            b"python3 --check-hash-based-pycs scripts/x.py",
            b"python3 -X dev -V scripts/x.py",
            b"nice -n5.5 sh -c 'sh scripts/x.sh'",
            b"nice -n 5.5 sh -c 'sh scripts/x.sh'",
            b"nice --adjustment=5.5 sh -c 'sh scripts/x.sh'",
            b"ionice -c -5 sh -c 'sh scripts/x.sh'",
            b"ionice -c-5 sh -c 'sh scripts/x.sh'",
            b"ionice -c2 -n x sh -c 'sh scripts/x.sh'",
            b"xargs -n 000 sh < scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"bash -o nounset scripts/x.sh",
            b"bash -O extglob scripts/x.sh",
            b"bash +o nounset scripts/x.sh",
            b"dash -o nounset scripts/x.sh",
            b"bash -o bogus scripts/x.sh",
            b"bash -co nounset scripts/x.sh",
            b"bash -oc nounset scripts/x.sh",
            b"python3 -X dev scripts/x.py",
            b"python3 -Xdev scripts/x.py",
            b"python3 -W error scripts/x.py",
            b"python3 -Wbogus scripts/x.py",
            b"python3 -X pycache_prefix=/tmp/p scripts/x.py",
            b"python3 --check-hash-based-pycs default scripts/x.py",
            b"python3 --check-hash-based-pycs always scripts/x.py",
            b"python3 -X dev -v scripts/x.py",
            b"split -n 3 --filter=\"python3 -X dev scripts/x.py\" "
            b"/tmp/d",
            b"xargs -n " + b"9" * 5000 + b" sh < scripts/x.sh",
            b"nice -n5 sh -c 'sh scripts/x.sh'",
            b"nice -n -5 sh -c 'sh scripts/x.sh'",
            b"ionice -c0 sh -c 'sh scripts/x.sh'",
            b"ionice -c2 -n8 sh -c 'sh scripts/x.sh'"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round68_empty_operands_and_wrapped_readers(tmp_path):
    """Round-68 shell-semantics fixes — all verified live on
    bash/GNU coreutils/util-linux:

    - A quoted EMPTY operand is a real argv member — `cat f | sed -e ''
      | sh` runs sed with an empty program over the pipe (GNU sed
      accepts it and forwards stdin), and `grep -e ''` matches every
      line. `_word_redirects` yields b"" for `''`/`""` AND for pure
      redirect words, so the args collectors must distinguish a
      genuine empty operand (empty unquoted canon) from a redirect
      word (`>f`, `2>&1`) — Codex on #1393, verified live.
    - `xargs -P -0` parses `-0` as the -P operand; `-0` is numerically
      zero (max-procs 0 = unlimited) — the utility runs (verified:
      `xargs -P -0 -n1 echo` prints). Negative nonzero stays bad.
    - `xargs -d` takes ONE char or a GNU escape (`\\a` `\\n` `\\0`
      `\\04` `\\123` `\\x4` `\\x41` `\\\\`) — `\\q`, `ab`, `\\8`,
      `\\e`, `\\xZZ` abort (verified against util-linux xargs).
    - `sudo echo -s x` prints `-s x` — `echo` ends sudo's option run,
      so `-s` is echo's argument, not a sudo shell flag (verified
      live: `sudo -n echo -s scripts/x.sh` prints). The whole tail
      argv decides via the head.
    - An argv-wrapped READER still emits: `xargs cat f | sh` runs
      `cat f …` and pipes the bytes to sh (verified live), `flock L
      cat f | sh` too — the wrapped head's emit matters, not just its
      exec classification.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"xargs -d '\\q' sh scripts/x.sh",
            b"xargs -d 'ab' sh scripts/x.sh",
            b"xargs -d '\\8' sh scripts/x.sh",
            b"xargs -d '\\e' sh scripts/x.sh",
            b"sudo echo -s scripts/x.sh",
            b"sudo -u root echo -s scripts/x.sh",
            b"sudo -- -s scripts/x.sh",
            b"xargs cat scripts/x.sh",
            b"flock /tmp/L cat scripts/x.sh",
            b"xargs -P -1 sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"xargs -d 'a' sh scripts/x.sh",
            b"xargs -d '\\n' sh scripts/x.sh",
            b"xargs -d '\\x41' sh scripts/x.sh",
            b"xargs -d '\\x4' sh scripts/x.sh",
            b"xargs -d '\\123' sh scripts/x.sh",
            b"xargs -d '\\04' sh scripts/x.sh",
            b"xargs -d '\\0' sh scripts/x.sh",
            b"xargs -d '\\\\' sh scripts/x.sh",
            b"xargs -P -0 sh scripts/x.sh",
            b"xargs -P 0 sh scripts/x.sh",
            b"cat scripts/x.sh | sed -e '' | sh",
            b"cat scripts/x.sh | grep -e '' | sh",
            b"printf 'i\\n' | xargs cat scripts/x.sh | sh",
            b"flock /tmp/L cat scripts/x.sh | sh",
            b"printf 'i\\n' | xargs head -n1 scripts/x.sh | sh",
            b"sudo -s scripts/x.sh",
            b"sudo -u root -s scripts/x.sh",
            b"sudo cat scripts/x.sh",
            b"sudo grep p scripts/x.sh",
            b"sudo cat scripts/x.sh | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round69_wrapper_operands_and_wrapped_readers(tmp_path):
    """Round-69 review fixes — all verified against live bash/util-linux:

    - `sudo` cluster letters are validated: `sudo -sx sh x` aborts
      "invalid option -- 'x'" before the command, while every real
      no-arg letter (`-K`, `-n`, `-s`, `-E`, …) still reaches it.
      `-C`/`--close-from` joins the operand options.
    - `flock -w`/`--timeout` needs a strtod number (`0.5`, `.5`, `+2`,
      `1e2` run; `nope`, `5x` abort "invalid timeout value") and
      `-E`/`--conflict-exit-code` an integer 0-255 (`x`, `5.5`, `300`,
      `-1` abort) — all before the wrapped argv.
    - `xargs -d '\\x'` — a bare `\\x` IS a valid GNU escape (the hex
      prefix alone names zero digits; util-linux runs the utility).
    - A WRAPPED reader in sink mode emits none of the operand's bytes:
      `flock L head -n 0 x | sh` and `xargs head -n 0 x | sh` feed sh
      nothing (verified live).
    - Wrapped `split` writes chunk FILES, not stdout — `xargs split x
      | sh` never feeds the pipe, and `flock L split /dev/null x`
      treats x as the output PREFIX, never reading it. `-n K/N`/`l/N`
      chunk-select streams to stdout and `--filter=CMD` executes CMD
      on each chunk — both still count (verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"sudo -sx sh scripts/x.sh",
            b"sudo -x sh scripts/x.sh",
            b"sudo --bogus sh scripts/x.sh",
            b"sudo -l sh scripts/x.sh",
            b"flock -w nope /tmp/L sh scripts/x.sh",
            b"flock -w 5x /tmp/L sh scripts/x.sh",
            b"flock --timeout=nope /tmp/L sh scripts/x.sh",
            b"flock -E x /tmp/L sh scripts/x.sh",
            b"flock -E 5.5 /tmp/L sh scripts/x.sh",
            b"flock -E 300 /tmp/L sh scripts/x.sh",
            b"flock -E -1 /tmp/L sh scripts/x.sh",
            b"flock --conflict-exit-code=x /tmp/L sh scripts/x.sh",
            b"flock -w /tmp/L sh scripts/x.sh",
            b"flock /tmp/L head -n 0 scripts/x.sh | sh",
            b"flock /tmp/L head -c 0 scripts/x.sh | sh",
            b"xargs head -n 0 scripts/x.sh | sh",
            b"xargs split scripts/x.sh | sh",
            b"flock /tmp/L split /dev/null scripts/x.sh | sh",
            b"sudo -sx scripts/x.sh",
            # Round-70 verified `-K` terminal and `-w -1` a
            # timer-setup abort — the command never runs.
            b"sudo -Kns sh scripts/x.sh",
            b"flock -w -1 /tmp/L sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"sudo -s sh scripts/x.sh",
            b"sudo -E sh scripts/x.sh",
            b"sudo -u root sh scripts/x.sh",
            b"sudo -C 3 sh scripts/x.sh",
            b"sudo --close-from=3 sh scripts/x.sh",
            b"sudo --preserve-env=A sh scripts/x.sh",
            b"sudo -h myhost sh scripts/x.sh",
            b"flock -w 0.5 /tmp/L sh scripts/x.sh",
            b"flock -w .5 /tmp/L sh scripts/x.sh",
            b"flock -w 1e2 /tmp/L sh scripts/x.sh",
            b"flock -E 5 /tmp/L sh scripts/x.sh",
            b"flock -E 0 /tmp/L sh scripts/x.sh",
            b"flock --timeout=2 /tmp/L sh scripts/x.sh",
            b"xargs -d '\\x' sh scripts/x.sh",
            b"xargs cat scripts/x.sh | sh",
            b"flock /tmp/L cat scripts/x.sh | sh",
            b"xargs split -n 1/1 scripts/x.sh | sh",
            b"xargs split -n r/1/1 scripts/x.sh | sh",
            b"xargs split --filter=sh scripts/x.sh",
            b"xargs split --filter=sh scripts/x.sh | wc"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round70_flock_strtold_sudo_close_from_filter(tmp_path):
    """Round-70 review fixes — all verified against live
    bash/sudo-1.9.9/util-linux:

    - `flock -w`/`--timeout` follows the strtold grammar: leading
      whitespace, decimal and `0x` hex floats, and `inf`/`infinity`/
      `nan` literals all PARSE (` 1`, `0x1p2` run the command) while
      trailing junk aborts "invalid timeout value" (`5x`, `1 `,
      `0x`, `1e`). A parseable value still aborts the timer when it
      is `inf`/`nan`, a negative nonzero (`-1`, `-.5`), or past the
      deadline arithmetic (`1e999`, `9223372036854775807`) — "cannot
      set up timer" (exit 71).
    - `sudo -C`/`--close-from` requires a number ≥3 — `-C 2`, `-C0`,
      `-C x`, `--close-from=2` abort "must be a number >= 3".
    - `sudo -K`/`--remove-timestamp` is a TERMINAL timestamp mode
      (usage error before any command, like `-v`); `-L` is not a
      sudo option in 1.9.9 ("invalid option" abort). `-k` still
      runs.
    - `_SUDO_OPERAND_LETTERS` gains `C` — `-Cs` is `-C s` (bad
      operand abort), not shell mode.
    - A WRAPPED `split --filter` no longer counts the input
      positional outright: the filter gets the chunk's bytes on its
      stdin — `sh`/`cat | sh`/`$(…)` heads execute them, `cat`/`head
      -n 1` re-emit them to split's stdout (the downstream pipe
      decides), and `true`/`wc`/`cat > chunk` drop or store them —
      the input never runs (verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"flock -w '1 ' /tmp/L sh scripts/x.sh",
            b"flock -w '1.5x' /tmp/L sh scripts/x.sh",
            b"flock -w 0x /tmp/L sh scripts/x.sh",
            b"flock -w 0xg /tmp/L sh scripts/x.sh",
            b"flock -w 1e /tmp/L sh scripts/x.sh",
            b"flock -w e5 /tmp/L sh scripts/x.sh",
            b"flock -w . /tmp/L sh scripts/x.sh",
            b"flock -w inf /tmp/L sh scripts/x.sh",
            b"flock -w INFINITY /tmp/L sh scripts/x.sh",
            b"flock -w nan /tmp/L sh scripts/x.sh",
            b"flock -w 'nan(abc)' /tmp/L sh scripts/x.sh",
            b"flock -w -inf /tmp/L sh scripts/x.sh",
            b"flock -w 1e999 /tmp/L sh scripts/x.sh",
            b"flock -w 1e308 /tmp/L sh scripts/x.sh",
            b"flock -w 9223372036854775807 /tmp/L sh scripts/x.sh",
            b"flock -w -.5 /tmp/L sh scripts/x.sh",
            b"sudo -C 2 sh scripts/x.sh",
            b"sudo -C0 sh scripts/x.sh",
            b"sudo -C x sh scripts/x.sh",
            b"sudo --close-from=2 sh scripts/x.sh",
            b"sudo --close-from x sh scripts/x.sh",
            b"sudo -K sh scripts/x.sh",
            b"sudo --remove-timestamp sh scripts/x.sh",
            b"sudo -L sh scripts/x.sh",
            b"sudo -Cs sh scripts/x.sh",
            b"xargs split --filter='cat > /tmp/r70chunk' scripts/x.sh",
            b"xargs split --filter='cat > /tmp/r70chunk' scripts/x.sh"
            b" | sh",
            b"xargs split --filter=true scripts/x.sh",
            b"xargs split --filter=true scripts/x.sh | sh",
            b"xargs split --filter=wc scripts/x.sh | sh",
            b"xargs split --filter=' ' scripts/x.sh | sh",
            b"xargs split --filter='cat' scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"flock -w ' 1' /tmp/L sh scripts/x.sh",
            b"flock -w 0x1p2 /tmp/L sh scripts/x.sh",
            b"flock -w 0x5 /tmp/L sh scripts/x.sh",
            b"flock -w 0x1.8p1 /tmp/L sh scripts/x.sh",
            b"flock -w 0x1p-2 /tmp/L sh scripts/x.sh",
            b"flock -w -0 /tmp/L sh scripts/x.sh",
            b"flock -w +0 /tmp/L sh scripts/x.sh",
            b"flock -w 1e18 /tmp/L sh scripts/x.sh",
            b"flock -w 9223372036854 /tmp/L sh scripts/x.sh",
            b"sudo -k sh scripts/x.sh",
            b"sudo -C 3 sh scripts/x.sh",
            b"sudo --close-from=3 sh scripts/x.sh",
            b"sudo -s sh scripts/x.sh",
            b"xargs split --filter=sh scripts/x.sh",
            b"xargs split --filter='cat | sh' scripts/x.sh",
            b"xargs split --filter='$(printf sh)' scripts/x.sh",
            b"xargs split --filter=cat scripts/x.sh | sh",
            b"xargs split --filter='head -n 1' scripts/x.sh | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round71_redirect_sudo_digits_flock_bounds(tmp_path):
    """Round-71 review — Devin on #132/#16/#1961: a wrapped command's
    `> /dev/null` (or `&>`/file) divert leaves the downstream pipe
    EMPTY — `xargs cat x >/dev/null | sh` runs nothing (the wrapped
    emit path called _pipe_to_exec without checking the enclosing
    segment's fd1; _stdout_redirected closes the hole for every head).
    `sudo -C` operand length can exceed Python's int-conversion cap —
    significant-digit compare keeps a 5000-zero pad valid and marks a
    5000-digit value the strtonum ERANGE abort (INT_MAX boundary
    verified live). `flock -w` accepts any magnitude below 2^63 —
    `9.21e18` and `9223372036854775000` still run the command while
    `9223372036854775807` and `0x1p1024` (OverflowError at
    float.fromhex) abort "cannot set up timer" (all verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"xargs split -n 1/1 scripts/x.sh > /dev/null | sh",
            b"xargs split --filter=cat scripts/x.sh > /dev/null | sh",
            b"xargs cat scripts/x.sh > /dev/null | sh",
            b"xargs head -n 1 scripts/x.sh > /dev/null | sh",
            b"flock /tmp/L cat scripts/x.sh > /dev/null | sh",
            b"split --filter='cat scripts/x.sh' - > /dev/null | sh",
            b"sudo -C " + b"9" * 5000 + b" sh scripts/x.sh",
            b"sudo -C 2147483648 sh scripts/x.sh",
            b"sudo -C 99999999999 sh scripts/x.sh",
            b"sudo -C 000 sh scripts/x.sh",
            b"flock -w 0x1p1024 /tmp/L sh scripts/x.sh",
            b"flock -w 9223372036854775807 /tmp/L sh scripts/x.sh",
            b"flock -w 9223372036854775808 /tmp/L sh scripts/x.sh",
            b"flock -w 1e19 /tmp/L sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"xargs split -n 1/1 scripts/x.sh | sh",
            b"xargs split --filter=cat scripts/x.sh | sh",
            b"xargs cat scripts/x.sh | sh",
            b"xargs split -n 1/1 scripts/x.sh 2>/dev/null | sh",
            b"sudo -C " + b"0" * 5000 + b"3 sh scripts/x.sh",
            b"sudo -C 2147483647 sh scripts/x.sh",
            b"sudo -C 03 sh scripts/x.sh",
            b"flock -w 9.21e18 /tmp/L sh scripts/x.sh",
            b"flock -w 9.2e18 /tmp/L sh scripts/x.sh",
            b"flock -w 9223372036854775000 /tmp/L sh scripts/x.sh",
            b"flock -w 0x1p62 /tmp/L sh scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round72_compound_feed_flock_ws_negative(tmp_path):
    """Round-72 review — Devin on #132/#16/#1428: a compound feeding
    stage's `;`/`&` sibling split is INSIDE the group, so the pipe
    walk must aggregate the siblings' fd1, not end the statement —
    `(cat x; true >/dev/null) | sh` still runs the script while
    `(cat x >/dev/null; true) | sh` feeds sh nothing, and a `>`
    after the close (`(cat x; cat x) >/dev/null | sh`, `done >f`)
    binds the WHOLE group. `flock -E` operand is strtol — leading
    whitespace parses (`' 5'` runs, `'5 '` aborts). `flock -w`
    negative bound is a 1-microsecond floor: `[-1e-6, 0)` rounds to
    a past deadline and runs, anything more negative aborts (all
    verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"(cat scripts/x.sh; true >/dev/null) | sh",
            b"(cat scripts/x.sh; true) | sh",
            b"((cat scripts/x.sh); true) | sh",
            b"(cat scripts/x.sh & true) | sh",
            b"(cat scripts/x.sh; cat scripts/x.sh >/dev/null) | sh",
            b"(cat scripts/x.sh) | sh",
            b"flock -E ' 5' /tmp/L sh scripts/x.sh",
            b"flock --conflict-exit-code ' 200' /tmp/L sh scripts/x.sh",
            b"flock -w -0.000001 /tmp/L sh scripts/x.sh",
            b"flock -w -0.0000009999 /tmp/L sh scripts/x.sh",
            b"flock -w ' 5' /tmp/L sh scripts/x.sh",
            b"flock -w -0 /tmp/L sh scripts/x.sh",
            b"xargs cat scripts/x.sh |& sh",
            b"xargs sh -c 'cat scripts/x.sh >&2' >/dev/null |& sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"(cat scripts/x.sh > /dev/null; true) | sh",
            b"(cat scripts/x.sh; cat scripts/x.sh) >/dev/null | sh",
            b"(cat scripts/x.sh) > /dev/null | sh",
            b"(true; xargs cat scripts/x.sh >/dev/null) | sh",
            b"flock -E '5 ' /tmp/L sh scripts/x.sh",
            b"flock -E ' 256' /tmp/L sh scripts/x.sh",
            b"flock -w -0.0000010000001 /tmp/L sh scripts/x.sh",
            b"flock -w -.5 /tmp/L sh scripts/x.sh",
            b"flock -w -0.0000015 /tmp/L sh scripts/x.sh",
            b"xargs cat scripts/x.sh >/dev/null |& sh",
            b"cat scripts/x.sh; true | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round73_open_depth_comments_elif_heredoc(tmp_path):
    """Round-73 review — Devin + CodeRabbit on #16/#132/#1428/#1961:
    `_open_depth` (and the forward walk) overcounted in three ways —
    a `(` inside a comment's TAIL (`# (` — the window stops at the
    word-start `#` but the comment text itself was re-scanned), an
    `elif` opener (it re-opens a branch of the enclosing `if`, not a
    new compound — `if..elif..fi` left a phantom level that kept the
    `;` after `fi` inside the group), and heredoc BODY lines (`(`
    inside `cat <<E`'s body is inert data — unterminated bodies never
    engage, `<<-` strips tabs, a second queued heredoc waits for the
    first delimiter). Also: `xargs split --filter x.sh /dev/null`
    produced no chunks so the filter never ran — the wrapped-split
    argv scan carried the input operand's trailing newline, so its
    `/dev/null` missed the dead-input gate (all verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"# (\ncat scripts/x.sh | sh",
            b"# comment\ncat scripts/x.sh | sh",
            b"( cat scripts/x.sh | sh )",
            b"if false; then :; elif true; then cat scripts/x.sh; fi | sh",
            b"cat scripts/x.sh | if a; then cat; elif b; then cat; fi | sh",
            b"cat <<EOF\n(\nEOF\n( cat scripts/x.sh | sh )",
            b"xargs split --filter scripts/x.sh f"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"# (\ncat scripts/x.sh | true; sh",
            b"true; # (\ncat scripts/x.sh | true; sh",
            b"if false; then :; elif true; then :; fi\ncat scripts/x.sh | true; sh",
            b"cat scripts/x.sh | true; if a; then :; elif b; then :; fi; sh",
            b"if a; then if b; then :; fi; fi\ncat scripts/x.sh | true; sh",
            b"cat <<EOF\n(\nEOF\ncat scripts/x.sh | true; sh",
            b"cat <<-EOF\n\t(\n\tEOF\ncat scripts/x.sh | true; sh",
            b"cat <<A\n(\nA\ncat <<B\n)\nB\ncat scripts/x.sh | true; sh",
            b"cat <<EOF\n(\ncat scripts/x.sh | true; sh",
            b"xargs split --filter scripts/x.sh /dev/null"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round74_brace_words_split_ncount_sudo_c_heredoc(tmp_path):
    """Round-74 review — Devin + CodeRabbit on #132/#16/#1428/#1961:
    `{`/`}` only group as whole words — `foo{` prints literally and
    `a{1,2}` is brace expansion, so `_group_depth` gates braces on
    word boundaries (parens stay counted anywhere — they're operators).
    `split -n N`/`l/N`/`r/N` materialise N chunks even on empty input —
    `--filter` fires on `/dev/null` (verified live: `-n 2`/`l/2`/`r/2`
    each run the filter N times; only the two-part `-n K/N` select
    emits nothing), so the dead-input gate exempts one-part `-n` modes
    on both the wrapped and unwrapped filter gates. `sudo -C` accepts
    strtol semantics — leading whitespace and `+` parse (`-C +3` runs,
    `-C +2`/`-C -3` abort — verified live). And an _HD_EXEC heredoc's
    body lines are program text: `sh <<E` runs them, so their `(`/if
    depth must count for `;`-sibling handling just like the same
    construct at top level (literal `cat <<E` bodies stay inert).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"{ cat scripts/x.sh | cat; } | sh",
            b"sudo -C +3 sh scripts/x.sh",
            b'sudo -C " 3" sh scripts/x.sh',
            b"split -n 2 --filter scripts/x.sh /dev/null out.",
            b"split -n l/2 --filter scripts/x.sh /dev/null out.",
            b"split -n r/2 --filter scripts/x.sh /dev/null out.",
            b"split -n 2 --filter scripts/x.sh - out. < /dev/null",
            b"xargs split -n 2 --filter scripts/x.sh /dev/null",
            b"sh <<E\n( cat scripts/x.sh | cat; sh )\nE",
            b"cat <<E | sh\n( cat scripts/x.sh | cat; sh )\nE",
            b"sh <<E\n( cat scripts/x.sh | cat; sh ",
            b"if false; then { cat scripts/x.sh | cat; }; fi | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"echo foo{; cat scripts/x.sh | true; sh",
            b"echo {x; cat scripts/x.sh | true; sh",
            b"echo a{1,2}; cat scripts/x.sh | true; sh",
            b"echo foo}; cat scripts/x.sh | true; sh",
            b"cat <<E\n( cat scripts/x.sh | cat; sh )\nE"
            b"\ncat scripts/x.sh | true; sh",
            b"cat <<E\n(\nE\ncat scripts/x.sh | true; sh",
            b"split -n 1/2 --filter scripts/x.sh /dev/null out.",
            b"split --filter scripts/x.sh /dev/null out.",
            b"xargs split --filter scripts/x.sh /dev/null",
            b"xargs split -n 1/2 --filter scripts/x.sh /dev/null",
            b"sudo -C +2 sh scripts/x.sh",
            b"sudo -C -3 sh scripts/x.sh",
            b"sudo -C x sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round75_elide_closed_cluster_strtold_hdoc(tmp_path):
    """Round-75 review — Devin + CodeRabbit on #132/#1428/#1961/#16:
    `split -e`/`--elide-empty-files` drops empty chunks, so the `-n N`
    filter exemption only holds when the option is absent (`-n 2 -e
    /dev/null` yields ZERO chunks — verified live). A CLOSED stdin
    (`<&-`) aborts EBADF before any chunk — the exemption covers only
    a readable-empty input, and `- </dev/null` still fires N times
    (readable empty ≠ closed). sudo option clusters bind the LAST
    operand letter's value — `-HC2` is `-H` + `-C 2`, which aborts
    "-C must be >= 3" (verified live). `flock -w` strtold underflow:
    a nonzero literal below LDBL_MIN=2^-16382 is ERANGE unless it's
    an exactly-representable denormal (`1e-9999`/`0x1p-16446`/
    `1e-4932`/`-1e-9999` abort "invalid timeout"; `1e-4931` and the
    min-denormal `0x1p-16445` run — float64 can't see the boundary,
    verified live). Heredoc bodies of argv-program heads are inert —
    `sh -c : <<E` execs `:` and never reads the body, while the
    exec-body scan's depth must restore on delimiter pop so body
    syntax can't leak a phantom group past `E` (verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"split -n 2 --filter='cat scripts/x.sh' /dev/null | sh",
            b"split -n 2 --filter='cat scripts/x.sh' F | sh",
            b"split -n 2 --filter='sh scripts/x.sh' -",
            b"split -n 2 --filter='sh scripts/x.sh' - </dev/null",
            b"sudo -HC3 sh scripts/x.sh",
            b"sudo -C 3 sh scripts/x.sh",
            b"sudo -H sh scripts/x.sh",
            b"flock -w 1e-4931 f sh scripts/x.sh",
            b"flock -w 0x1p-16445 f sh scripts/x.sh",
            b"flock -w 0e-9999 f sh scripts/x.sh",
            b"flock -w 1.5 f sh scripts/x.sh",
            b"sh <<E\n(\nE\ncat scripts/x.sh | sh",
            b"sh -c : <<E\n(\nE\ncat scripts/x.sh | sh",
            b"xargs split --filter='cat scripts/x.sh' <f | sh",
            b"find . -type f -exec split -n 2 --filter='cat scripts/x.sh'"
            b" /dev/null \\; | sh",
            b"find . -type f -exec split --filter='cat scripts/x.sh' F"
            b" \\; | sh",
            b"find . -type f -exec split -n 2 --filter='sh scripts/x.sh'"
            b" /dev/null \\;"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"split -n 2 --filter='cat scripts/x.sh' -e /dev/null | sh",
            b"split -n 2 --elide-empty-files"
            b" --filter='cat scripts/x.sh' /dev/null | sh",
            b"split -en 2 --filter='cat scripts/x.sh' /dev/null | sh",
            b"split -n 2 --filter='cat scripts/x.sh' - <&- | sh",
            b"split -n 2 --filter='sh scripts/x.sh' - <&-",
            b"sudo -HC2 sh scripts/x.sh",
            b"sudo -nC2 sh scripts/x.sh",
            b"sudo -HC 2 sh scripts/x.sh",
            b"sudo -C 2 sh scripts/x.sh",
            b"flock -w 1e-9999 f sh scripts/x.sh",
            b"flock -w 1e-4932 f sh scripts/x.sh",
            b"flock -w -1e-9999 f sh scripts/x.sh",
            b"flock -w 0x1p-16446 f sh scripts/x.sh",
            b"sh -c : <<E\n(\nE\ncat scripts/x.sh | wc; sh",
            b"sh -c 'true' <<E\n(\nE\ncat scripts/x.sh | wc; sh",
            b"sh <<E\n(\nE\ncat scripts/x.sh | wc; sh",
            b"python -X dev s.py <<E\nx\nE\ncat scripts/x.sh | wc; sh",
            b"find . -type f -exec split --filter='cat scripts/x.sh'"
            b" /dev/null \\; | sh",
            b"find . -type f -exec split -n 2 --filter='sh scripts/x.sh'"
            b" - <&- \\;",
            b"find . -type f -exec split -n 2 --filter='cat scripts/x.sh'"
            b" -e /dev/null \\; | sh",
            b"split --filter='cat scripts/x.sh' /dev/null | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round76_group_fd1_flock_exp_filter_glued(tmp_path):
    """Round-76 review — Devin on #132/#1428/#1961/#16: a `>`/`&>` on
    the LAST sibling of a `( )`/`{ }` compound diverts only that
    command — earlier siblings share the group's fd1, so `(cat x;
    cat x >f) | sh` still writes the script into the pipe (a `>`
    AFTER the closer covers the whole group — verified live).
    `flock -w` exponent bound: `1e±10^8`/`0x1p±99999` literals made
    `_strtold_fraction` build a hundred-million-digit bigint — the
    exact fraction is only computed on the float-underflow path and
    out-of-range exponents short-circuit (verified live — strtold
    ERANGEs both directions). And `split --filter=CMD` glued onto
    the option word is program text — `--filter=scripts/x.sh`
    executes the script when a chunk materialises (`-n 2` on
    `/dev/null`, or any live input — verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            b"(cat scripts/x.sh; cat scripts/x.sh >f) | sh",
            b"{ cat scripts/x.sh; cat scripts/x.sh >f; } | sh",
            b"(cat scripts/x.sh >f; cat scripts/x.sh) | sh",
            b"split -n 2 --filter=scripts/x.sh /dev/null",
            b"split --filter=scripts/x.sh F",
            b"xargs split --filter=scripts/x.sh f",
            b"find . -exec split --filter=scripts/x.sh F \\;",
            b"split --filter='cat scripts/x.sh' F | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"(cat scripts/x.sh; cat scripts/x.sh >f) | wc; sh",
            b"(cat scripts/x.sh; cat scripts/x.sh >f) >g | sh",
            b"(echo hi; cat scripts/x.sh >f) | sh",
            b"cat scripts/x.sh >f | sh",
            b"xargs cat scripts/x.sh >/dev/null | sh",
            b"flock -w 1e10000000 f sh scripts/x.sh",
            b"flock -w 1e-100000000 f sh scripts/x.sh",
            b"flock -w 0x1p99999 f sh scripts/x.sh",
            b"flock -w 0x1p-99999 f sh scripts/x.sh",
            b"split --filter=scripts/x.sh /dev/null",
            b"find . -exec split -n 2 --filter=scripts/x.sh - <&- \\;",
            b"split --filter=cat F | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round77_pipe_amp_stderr(tmp_path):
    """`|&` merges a group's stderr into the pipe — a `>&2` sibling
    inside `(…)`/`{…}` therefore still feeds the pipe, while a
    NON-group `cmd >&2 |&` dies (fd1 binds to real stderr before `|&`
    copies fd1's binding — verified live). Compound groups that open
    AFTER the word's command are not its siblings (`cat x >&2;
    (true) |&` — the `(true)` group's pipe is a different statement),
    and `$(`/`<(`/`>(` closers are substitution spans, not command
    groups (Devin/CodeRabbit on #132/#1428/#1961/#16, round-77)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b"(cat scripts/x.sh >&2; true) |& sh",
            b"((cat scripts/x.sh >&2; true)) |& sh",
            b"{ cat scripts/x.sh >&2; true; } |& sh",
            b"(cat scripts/x.sh >f; cat scripts/x.sh >&2) |& sh",
            b"(cat scripts/x.sh >&2; cat scripts/x.sh >f) |& sh",
            b"(cat scripts/x.sh >&2; cat scripts/x.sh) |& sh",
            b"(echo safe; cat scripts/x.sh >&2) |& sh",
            # dep = the -c operand mentioning scripts/ regardless of
            # the pipeline context (interpreter program operands).
            b"xargs sh -c 'cat scripts/x.sh >&2' >/dev/null |& sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"cat scripts/x.sh >&2 |& sh",
            b"(cat scripts/x.sh >&2; true) >f |& sh",
            b"cat scripts/x.sh >&2; (true) |& sh",
            b"cat scripts/x.sh >&2; true |& sh",
            b"cat scripts/x.sh >f; (true) |& sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round77_strtold_zero_mantissa(tmp_path):
    """strtold accepts mantissa-zero float spellings — `0e-20001`,
    `0.0e-99999`, `0x0p-20001` are all zero, so `flock -w <val>`
    timeouts on them still EXECUTE the command (round-77)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b"flock -w 0e-20001 f sh scripts/x.sh",
            b"flock -w 0.0e-99999 f sh scripts/x.sh",
            b"flock -w 0x0p-20001 f sh scripts/x.sh",
            b"flock -w 00e-99999 f sh scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"flock -w 1e-9999 f sh scripts/x.sh",
            b"flock -w 0x1p-99999 f sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round78_group_emit_pipe(tmp_path):
    """A group sibling EMITTING a command text (`echo bash x`) is
    dep-carrying through the group pipe just like the non-group
    `echo bash x | sh` stream — the emitted-word gate decides
    bare-vs-invocation downstream (Devin on #1431/#1963/#17,
    round-78 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "y.sh").write_bytes(b"x")
    for line in (
            b"(echo bash scripts/x.sh; true) | sh",
            b"(echo bash scripts/x.sh >&2; true) |& sh",
            b"{ echo bash scripts/x.sh; cat scripts/y.sh >f; } | sh",
            # `)` followed by `;` closed an inner sibling — the
            # outer statement keeps scanning for the group pipe.
            b"((cat scripts/x.sh >&2; true); true) |& sh",
            b"((cat scripts/x.sh; t); t) | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # Bare emitted path stays out of scope (round-16
            # convention), group or not.
            b"(echo scripts/x.sh; true) | sh",
            b"(echo scripts/x.sh >&2; true) |& sh",
            # `)` `;` ended the group — the pipe belongs to `t`.
            b"(cat scripts/x.sh); t | sh",
            b"(cat scripts/x.sh >&2); t |& sh",
            b"echo scripts/x.sh; (t) | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round78_strtold_mantissa_digits(tmp_path):
    """`float(mant)` underflows nonzero mantissas past binary64's
    range — `0.`+400 zeros+`1` is nonzero so flock rejects `1e-4540`
    as ERANGE and the -c never runs (CodeRabbit/Devin on
    #1963/#133/#1431/#17, round-78 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    tiny = "0." + "0" * 400 + "1e-4540"
    assert not mod.script_dep_block(
        pdir, f"flock -w {tiny} f sh scripts/x.sh\n".encode())
    # Mantissa-zero spellings still execute regardless of exponent.
    for line in (
            b"flock -w 0e-20001 f sh scripts/x.sh",
            b"flock -w 0.000e+99999 f sh scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round79_quoted_group_parens(tmp_path):
    """Quoted/escaped/backtick parens are operand text, not group
    delimiters — the closer-matcher and pipe-scan must skip them
    (`(cat x >&2; echo "(") |&` — Devin on #133/#1963, round-79 —
    verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >&2; echo "(") |& sh',
            b'(cat scripts/x.sh >&2; echo ")") |& sh',
            b"(cat scripts/x.sh >&2; echo '(') |& sh",
            b'(cat scripts/x.sh >&2; echo a\\(b) |& sh',
            b'(cat scripts/x.sh >&2; echo "\\(") |& sh',
            b'(cat scripts/x.sh >&2; echo `echo (`) |& sh',
            b'((cat scripts/x.sh >&2; t); echo "(") |& sh',
            b'(cat scripts/x.sh; echo "(") | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `|` (not `|&`) — stderr still dies on real stderr.
            b'(cat scripts/x.sh >&2; echo "(") | sh',
            b'(cat scripts/x.sh >&2; echo a\\(b) | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round79_stderr_divert_order(tmp_path):
    """`>&2 2>/dev/null` keeps fd1 bound to the pipe (fd2's OLD
    target); `2>/dev/null >&2` points fd1 at /dev/null — redirect
    order is alias order (Devin on #1963, round-79 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b"(echo bash scripts/x.sh >&2 2>/dev/null; true) |& sh",
            b"(cat scripts/x.sh >&2 2>/dev/null; true) |& sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"(echo bash scripts/x.sh 2>/dev/null >&2; true) |& sh",
            b"(cat scripts/x.sh 2>/dev/null >&2; true) |& sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round80_quoted_backtick(tmp_path):
    """A single-quoted backtick is literal text — the group mask must
    not start a substitution scan there and swallow the real closer
    (`(echo '`'; cat x >&2; true) |&` — Devin on #1431, round-80 —
    verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b"(echo '`'; cat scripts/x.sh >&2; true) |& sh",
            b"(echo '`' '`'; cat scripts/x.sh >&2; true) |& sh",
            b'(cat scripts/x.sh >&2; echo `echo "("`) |& sh',
            b"(cat scripts/x.sh >&2; echo '`'; echo \")\") |& sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b"(echo '`'; cat scripts/x.sh >&2; true) | sh",):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round80_headless_group_sibling(tmp_path):
    """A `|&` group sibling with no executable head (redirect-only,
    describe-only `command -v`, assignment-only, bare `exec`) yields a
    None fd map — it emits nothing, so it is skipped, not crash
    (CodeRabbit on #133, round-80 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b"( command -v jq >&2; cat scripts/x.sh ) |& sh",
            b"( X=1 >&2; cat scripts/x.sh ) |& sh",
            b"( exec >&2; cat scripts/x.sh ) |& sh",
            b"( >log; cat scripts/x.sh ) |& sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round80_group_pipe_nesting(tmp_path):
    """`_group_pipe` skips `$(`/`<(`/`>(`/`${` bodies and counts nested
    group depth — a `|` inside a substitution is the capture's pipe,
    and an inner `(t)` does not end the containing group's scan
    (CodeRabbit on #133, round-80 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >&2; (t)) |& sh',
            b'bash -c "$(cat scripts/x.sh; echo $(date) | wc -l)"',
            b'bash -c "$(cat scripts/x.sh; echo ${HOME} | wc -l)"'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round80_mask_prose_marks(tmp_path):
    """Unterminated quotes/backticks before the group are prose marks
    (apostrophes, markdown fences), not quoted regions — the group
    delimiters stay visible so the dep is still found (CodeRabbit on
    #133, Devin on #1431/#17, round-80 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >&2; echo a\\(b) |& sh',
            b'(cat scripts/x.sh >&2; echo `echo "("`) |& sh',
            b'(cat scripts/x.sh >&2; (t)) |& sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round81_single_child_group(tmp_path):
    """A ONE-sibling compound is still a group — `(cat x >&2) |& sh`
    merges fd2 into the pipe exactly like a multi-sibling one (Devin
    on #133, round-81 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >&2) |& sh',
            b'(cat scripts/x.sh >&2; (true) | cat) |& sh',
            b'(cat scripts/x.sh >&2; true && true) |& sh',
            b'(cat scripts/x.sh >&2; true || true) |& sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round81_group_scan_delimiters(tmp_path):
    """Delimiter confusion inside the group can no longer hide the
    containing `)`/`}` closer: `\"` inside a quoted region and `}`
    inside a quoted `${}` default are not real closers, and inner
    `|`/`&&`/`||` separators do not end the group scan (Devin on
    #1431/#17, round-81 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >&2; echo "a \\" ( b") |& sh',
            b'(cat scripts/x.sh >&2; echo ${v:-"}"}) |& sh',
            b'(cat scripts/x.sh >&2; echo ${v:-${w}}) |& sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round82_post_closer_redirects(tmp_path):
    """Redirects AFTER a `)`/`}` closer apply to the group in order;
    a `|&` then rebinds fd2 onto fd1's final target. `(...) 2>/dev/null
    |& sh` still merges stderr into the pipe (the fd2 kill applies
    before `|&`), while `(...) >/dev/null |&` empties both — all
    verified live (Devin on #133/#1431, round-82)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >&2; true) 2>/dev/null |& sh',
            b'(cat scripts/x.sh >&2; true) 2>&1 |& sh',
            # fd2→file only: fd1 still feeds the plain pipe.
            b'(cat scripts/x.sh; true) 2>/dev/null | sh',
            b'{ cat scripts/x.sh >&2; true; } 2>/dev/null |& sh',
            # `2>&1` binds fd2 onto fd1's pipe target BEFORE the
            # `>/dev/null` diverts fd1 — x's `>&2` bytes still reach
            # the pipe (expectation flipped from round-82's dead
            # verdict: verified live, Devin on #133, round-83).
            b'(cat scripts/x.sh >&2) 2>&1 >/dev/null | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # fd2→file kills the `|&` merge; fd1 to file kills both.
            b'(cat scripts/x.sh >&2; true) 2>/dev/null | sh',
            b'(cat scripts/x.sh >&2; true) >/dev/null |& sh',
            b'(cat scripts/x.sh >&2; true) &>/dev/null |& sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
