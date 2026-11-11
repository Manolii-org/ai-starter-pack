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


def test_script_dep_round42(tmp_path):
    """Round-42 review (Devin/Codex/CodeRabbit on #130/#1393/#1959/#12 —
    every behavior verified against real GNU bash/find/flock/split/strings/
    gawk):
    * `-quit` exits only when EVALUATED — later words still parse
      (`-quit -help` prints usage) and a later `-o`/`-or`/`,` branch
      revives the action (`find . -false -quit -o -exec` runs it);
    * `-D` is a pre-path debug option (no operand; `find . -D x` errors
      "unknown predicate"), while `-files0-from` is a global option
      that consumes the NUL list filename (`-files0-from --help` reads
      `--help` as the file);
    * `-newerBm` is a valid form (B = birth stamp) and consumes its
      reference operand like every other -newerXY;
    * the find action's EFFECTIVE head decides — `env bash x`,
      `timeout 5 bash x`, `sudo sh x` reach the interpreter behind the
      wrapper, an unlisted head fails closed (`-exec tsx x`), and a
      non-exec head emits the path so a `| sh` after the find still
      runs it (`-exec cat x \\; | sh`);
    * a positional AFTER an interpreter program flag is argv ($0…),
      not the script — `sh -c : x.sh` runs `:`;
    * flock post-lockfile dash words are literal argv[0] — `-n`/
      `--help` fail ENOENT and nothing after them runs;
    * `pr --indent` takes a required argument;
    * `awk -e/--source` (program text) and `-E/--exec` (program file)
      supply the program — later positionals are data files; `--trace`
      is a gawk boolean;
    * `strings` mode options validate their value — `--unicode bad`
      aborts before reading;
    * split's `--filter` resolves unique GNU prefixes (`--fil=cat`)
      and the filter's own stdout decides the stream — `cat` forwards,
      `true`/`false` emit nothing; the `-n` K/N stdout modes need a
      seekable input and never forward the pipe."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_text("echo HIT\n")
    for line in (
            # `-quit` on a false/`-o` branch leaves the action live —
            # `find . -false -quit -o -exec` runs it (verified live)
            b"find . -false -quit -o -exec sh scripts/x.sh \\;",
            # `-quit` after the action does not undo it (round-41)
            b"find . -exec sh scripts/x.sh \\; -quit",
            # `-quit -o -exec` — the model keeps dep (real quits first;
            # over-block is the safe direction)
            b"find . -quit -o -exec sh scripts/x.sh \\;",
            # `-files0-from` consumes the list filename — `--help`
            # is read as the file, not a terminal word (verified live)
            b"find -files0-from --help -exec sh scripts/x.sh \\;",
            # `-newerBm` consumes its reference like -newermt
            b"find . -newerBm REF -exec sh scripts/x.sh \\;",
            # wrapper heads unwrap to the interpreter (EXEC-RAN/
            # TO-RAN verified live)
            b"find . -exec env bash scripts/x.sh \\;",
            b"find . -exec timeout 5 bash scripts/x.sh \\;",
            b"find . -exec sudo sh scripts/x.sh \\;",
            b"find . -exec nice sh scripts/x.sh \\;",
            b"find . -exec env sh -c 'bash scripts/x.sh' \\;",
            # an unlisted head may still interpret — fail closed
            b"find . -exec tsx scripts/x.sh \\;",
            b"find . -exec deno run scripts/x.sh \\;",
            # a non-exec head emits the path — the find pipeline's
            # own `| sh` still runs it (verified live)
            b"find . -exec cat scripts/x.sh \\; | sh",
            # interpreter argv[0] reads the script file (round-41)
            b"find . -exec sh scripts/x.sh \\;",
            b"find . -exec sh -c 'sh scripts/x.sh' \\;",
            b"find . -exec python scripts/x.sh \\;",
            # split's unique-prefix `--fil`/`--f` resolve to --filter —
            # the filter's stdout forwards the chunks (verified live)
            b"cat scripts/x.sh | split --fil=cat - | sh",
            b"cat scripts/x.sh | split --f cat - | sh",
            b"cat scripts/x.sh | split --filter='cat' - | sh",
            # `pr --indent` takes a required argument — the stream
            # still forwards (verified live)
            b"cat scripts/x.sh | pr --indent 4 | sh",
            # `-n 1/1` chunk-select streams a buffered pipe on
            # coreutils ≥9.x — the input is copied to a temp file
            # (Codex on #130/#1959, round-51 — verified live on 9.4;
            # 8.32 aborts — union models the running case)
            b"cat scripts/x.sh | split -n 1/1 - | sh",
            # `--trace` is a boolean — the program positional still
            # reads the pipe (over-block on gawk < 5.3)
            b"cat scripts/x.sh | awk --trace '{print}' | sh",
            # a valid strings mode value still forwards
            b"cat scripts/x.sh | strings --unicode l | sh",
            b"cat scripts/x.sh | strings -e l | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `-quit` with no later `-o` kills the action (verified live)
            b"find . -quit -exec sh scripts/x.sh \\;",
            # `-quit` does not stop PARSING — a later `-help` still
            # prints usage and exits (verified live)
            b"find . -exec sh scripts/x.sh \\; -quit -help",
            # `-D` takes no operand — `--help` is still a terminal
            # word (`find . -D dbg --help` prints usage — verified)
            b"find . -D dbg --help -exec sh scripts/x.sh \\;",
            # post-lockfile dash words are literal argv[0] — flock
            # fails ENOENT and nothing after runs (verified live)
            b"flock /tmp/l -n sh scripts/x.sh",
            b"flock /tmp/l --help sh scripts/x.sh",
            # a positional AFTER the interpreter's program flag is
            # argv ($0), not the script — `sh -c : x.sh` runs `:`
            # (verified live)
            b"find . -exec sh -c : scripts/x.sh \\;",
            # non-exec heads print/read the literal name — no pipe to
            # an exec head means nothing runs (verified live)
            b"find . -exec cat scripts/x.sh \\;",
            b"find . -exec echo scripts/x.sh \\;",
            b"find . -exec awk '{print}' scripts/x.sh \\;",
            # a silent filter emits nothing on stdout — `--filter=
            # true`/`false` leave sh at EOF (verified live)
            b"cat scripts/x.sh | split --filter=true - | sh",
            b"cat scripts/x.sh | split --filter=false - | sh",
            # `-n l/N` writes N chunk FILES — nothing reaches stdout
            # (a K/N select streams the buffered pipe on coreutils
            # ≥9.x — moved to the dep list, round-51)
            b"cat scripts/x.sh | split -n l/1 - | sh",
            # `strings` mode options validate their value — a bad
            # mode aborts before any read (verified live)
            b"cat scripts/x.sh | strings --unicode bad | sh",
            b"cat scripts/x.sh | strings --unicode=bad | sh",
            b"cat scripts/x.sh | strings -e bad | sh",
            b"cat scripts/x.sh | strings -t q | sh",
            # awk program flags supply the program — later operands
            # are data files that replace the stream (verified live)
            b"cat scripts/x.sh | awk --source '{print}' /dev/null | sh",
            b"cat scripts/x.sh | awk --exec f /dev/null | sh",
            b"cat scripts/x.sh | awk -e '{print}' /dev/null | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round43(tmp_path):
    r"""Round-43: per-domain strings value sets, split filter's
    last-wins + operand-aware scan + input-file provenance + shell-
    list filters, required-arg missing operands, find `-context` and
    reader-head downstream pipes, cat multi-file concat, awk
    `--pretty-print` optional arg.

    Verified live (bash/dash, coreutils 9.x, binutils 2.38, gawk 5.1):
    * `strings -U/--unicode` accepts letters {d,s,i,l,e,x,h} plus the
      names {default,invalid,locale,escape,hex,highlight,show} —
      separately from `-e/--encoding` {s,S,l,L,b,B} and
      `-t/--radix` {d,o,x}; `-s SEP`/`-T BFD` are required-arg shorts;
    * `awk --pretty-print[=F]` is optional-arg — a separate word is
      the program, not the file;
    * a required option at argv end aborts ("option requires an
      argument") — `pr --indent`/`tail --pid`/`strings -U` forward
      nothing;
    * split's `--filter` binds its LAST value, option operands are
      consumed before `--filter` is recognized (`--lines --filter sh`
      errors), the filter runs via `$SHELL -c` so `cat | sh`/`true; sh`
      execute, and a positional INPUT replaces the upstream pipe;
    * `find`'s `-context` is a one-operand predicate on SELinux builds
      (over-block-safe elsewhere — non-SELinux aborts);
    * a find action whose head is a PROGRAM_FIRST reader still pipes
      its stdout onward — `-exec awk '{p}' x.sh \; | sh` runs x.sh's
      bytes;
    * `cat f1 f2` concatenates BEFORE field-splitting — `f1`=`a`,
      `f2`=`b` emits `ab` as ONE field, not two."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_text("echo HIT\n")
    # Two one-word files that merge into ONE field — `a`+`b` → `ab`
    # (no trailing newline on `a`).
    (pdir / "scripts" / "a.sh").write_bytes(b"a")
    (pdir / "scripts" / "b.sh").write_bytes(b"b")
    for line in (
            # -U/--unicode take letters AND names (verified live)
            b"cat scripts/x.sh | strings -U d | sh",
            b"cat scripts/x.sh | strings -U x | sh",
            b"cat scripts/x.sh | strings --unicode=hex | sh",
            b"cat scripts/x.sh | strings --unicode default | sh",
            b"cat scripts/x.sh | strings -Uhex | sh",
            b"cat scripts/x.sh | strings -U default | sh",
            # -s/-T required-arg shorts consume the operand (verified)
            b"cat scripts/x.sh | strings -s , | sh",
            b"cat scripts/x.sh | strings -T elf64-x86-64 | sh",
            # --pretty-print is optional-arg — 'p' is still the
            # program, `-` still the stdin file (verified live)
            b"cat scripts/x.sh | awk --pretty-print '{print}' - | sh",
            # split filters: a $SHELL -c LIST executes (verified live)
            b"cat scripts/x.sh | split --filter='cat | sh' - | sh",
            b"cat scripts/x.sh | split --filter='true; sh' - | sh",
            # the LAST repeated --filter wins (verified live)
            b"cat scripts/x.sh | split --filter=true --filter=sh - | sh",
            # stdin input + forwarding filter still flows (verified)
            b"cat scripts/x.sh | split --filter=cat - | sh",
            b"cat scripts/x.sh | split --filter=sh -- - | sh",
            # a scripts/ INPUT FILE's chunks reach the filter — `cat`
            # re-emits them downstream (verified live)
            b"split --filter=cat scripts/x.sh | sh",
            b"split --filter=sh scripts/x.sh | sh",
            # -context consumes `--help` as the CONTEXT operand —
            # the action still runs (SELinux builds; over-block-safe)
            b"find . ! -context --help -exec sh scripts/x.sh \\;",
            # a reader-headed find action still pipes its stdout to
            # a downstream exec head (verified live)
            b"find . -exec awk '{print}' scripts/x.sh \\; | sh",
            b"find . -exec sed -n p scripts/x.sh \\; | sh",
            # cat concatenates files before IFS field-splitting —
            # `a`+`b` is ONE field and still joins the format word
            # (verified live)
            b"IFS=,; date +$(cat scripts/a.sh scripts/b.sh) | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # fixed-domain rejects still abort before any read
            b"cat scripts/x.sh | strings -U z | sh",
            b"cat scripts/x.sh | strings --unicode=bad | sh",
            # a required option at argv end aborts — nothing forwards
            # (verified live: "option requires an argument")
            b"cat scripts/x.sh | strings -U | sh",
            b"cat scripts/x.sh | pr --indent | sh",
            b"cat scripts/x.sh | tail --pid | sh",
            b"cat scripts/x.sh | strings -s | sh",
            # --pretty-print exiting mode never reads stdin — a `-`
            # operand is still the input file, but gawk pretty-prints
            # and exits; the model keeps dep anyway (over-block) —
            # assert only the SEPARATE program word is not swallowed
            # (verified live: '1' ran as program, wrote awkprof.out)
            # => covered by the positive above.
            # split: a required-arg option's operand swallows --filter
            # (verified live: "invalid number of lines: '--filter'")
            b"cat scripts/x.sh | split --lines --filter sh | sh",
            b"cat scripts/x.sh | split -l --filter=sh - | sh",
            # last --filter wins — here the last is `true` (verified)
            b"cat scripts/x.sh | split --filter=sh --filter=true - | sh",
            # a named INPUT replaces the upstream pipe — /dev/null's
            # chunks reach the filter, not the script (verified live)
            b"cat scripts/x.sh | split --filter=cat /dev/null | sh",
            b"cat scripts/x.sh | split --filter=sh /dev/null | sh",
            # an unknown/ambiguous option aborts — no filter at all
            b"cat scripts/x.sh | split --bogus --filter=sh - | sh",
            # no downstream pipe — the action's file is only printed
            b"find . -exec awk '{print}' scripts/x.sh \\;"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round44(tmp_path):
    r"""Round-44 review findings, all verified against live GNU tools:

    * `split --unbuffered`/`-u` is a real flag — the scan must keep
      the filter (Devin/Codex/CodeRabbit on #130/#1959/#12/#1393);
    * obsolete `-NUM` line counts are valid split shorts (`-1000`
      still runs the filter — CodeRabbit);
    * `split --help`/`--version` exit BEFORE any filter runs,
      regardless of position (Devin/Codex);
    * invalid split operands abort before the filter: `--lines=xyz`,
      `-a xyz`, `-t ''`/`xy`, bad CHUNKS; any `-n` aborts on a pipe
      ("cannot determine file size") and `K/N` chunk-selects refuse
      `--filter` outright (Devin);
    * `xargs --he`/`flock --vers` resolve to terminal modes via GNU
      unique prefix, and ambiguous/unknown options (`--ver`, `-Z`)
      error out before the wrapped argv (Codex);
    * `flock L -- sh x` execs the literal `--` — ENOENT, nothing
      after it runs (Devin);
    * `find . -quit -exec echo -o \; -exec sh x \;` still quits —
      the `-o` inside echo's argv is not a find branch (Devin);
    * `sh -lc : x.sh` binds the program inside the cluster — x.sh is
      left as $0, never executed (Devin)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_text("echo HIT\n")
    for line in (
            # --unbuffered/-u keep the filter (verified live)
            b"cat scripts/x.sh | split --unbuffered --filter=sh - | sh",
            b"cat scripts/x.sh | split -u --filter=sh - | sh",
            b"cat scripts/x.sh | split --un --filter=sh - | sh",
            # obsolete -NUM line count (verified live)
            b"cat scripts/x.sh | split -1000 --filter=sh - | sh",
            # valid operands keep the filter (verified live)
            b"cat scripts/x.sh | split -b 1K --filter=sh - | sh",
            b"cat scripts/x.sh | split -b1KB --filter=sh - | sh",
            b"cat scripts/x.sh | split -l 5 --filter=sh - | sh",
            b"cat scripts/x.sh | split --numeric-suffixes --filter=sh - | sh",
            # `-n 2` on a FILE input still runs the filter — a
            # scripts/ input file's chunks reach sh (verified live)
            b"split -n 2 --filter=sh scripts/x.sh | sh",
            # `-n 2` + filter on a pipe: ≥9.x buffers the unseekable
            # input and runs the filter on each chunk (round-51 union)
            b"cat scripts/x.sh | split --filter=sh -n 2 - | sh",
            # find: a REAL expr branch after -quit still revives the
            # action (verified live)
            b"find . -false -quit -o -exec sh scripts/x.sh \\;",
            # xargs -E/--eof take their operand — here `sh` is the
            # eof-string and `scripts/x.sh` the utility word itself,
            # so the dep still fires through argv (verified live)
            b"xargs -E sh scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `-n` streams an unseekable pipe on ≥9.x — the filter
            # runs on the buffered input (the `-n l/2`/`r/2 -n l/2`
            # cases moved to the dep list — round-51 union)
            # terminal split modes — never run the filter, either
            # position (verified live)
            b"cat scripts/x.sh | split --filter=sh --help | sh",
            b"cat scripts/x.sh | split --help --filter=sh - | sh",
            b"cat scripts/x.sh | split --filter=sh --version - | sh",
            # invalid operands abort before the filter (verified live)
            b"cat scripts/x.sh | split --filter=sh --lines=xyz - | sh",
            b"cat scripts/x.sh | split --filter=sh -l xyz - | sh",
            b"cat scripts/x.sh | split --filter=sh -a xyz - | sh",
            b"cat scripts/x.sh | split --filter=sh -t xy - | sh",
            b"cat scripts/x.sh | split --filter=sh -n x/y - | sh",
            # K/N refuses --filter — abort (verified live)
            b"cat scripts/x.sh | split --filter=sh -n 1/1 - | sh",
            # a missing required operand aborts too (verified live:
            # "option '--filter' requires an argument")
            b"cat scripts/x.sh | split --filter=sh --filter | sh",
            # a glued positional after --numeric-suffixes is INPUT —
            # a missing file aborts before the filter (verified live)
            b"cat scripts/x.sh | split --numeric-suffixes 5 --filter=sh - | sh",
            # terminal prefixes and error exits kill the wrapped argv
            # (verified live)
            b"xargs --he sh scripts/x.sh",
            b"flock --vers /dev/null sh scripts/x.sh",
            b"xargs --ver sh scripts/x.sh",
            b"xargs -Z sh scripts/x.sh",
            # post-lockfile `--` is flock's literal argv[0] — ENOENT
            # (verified live)
            b"flock /dev/null -- sh scripts/x.sh",
            # `-o` inside an action argv is not a find branch — -quit
            # still kills the later action (verified live)
            b"find . -quit -exec echo -o \\; -exec sh scripts/x.sh \\;",
            # `-lc` binds the program inside the cluster — x.sh is $0
            # (verified live)
            b"find . -exec sh -lc : scripts/x.sh \\;",
            b"find . -exec sh -cl : scripts/x.sh \\;"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round45(tmp_path):
    """Review round-45 — split round-robin `-n` streaming, find's `{} +`
    terminator guard, xargs `--arg-f` prefix argfiles, rejected wrapper
    options, `split --separator='\\0'`, iconv `--usage`, and reader
    numeric option values — each verified against live GNU tools:

    * `split -n r/2 --filter=sh -` streams a pipe — and on coreutils
      ≥9.x EVERY `-n` form does (the unseekable input is buffered to
      a temp file — the round-51 union model keeps the running case;
      `l/2`/`r/2 -n l/2` filter cases moved to the dep list);
    * `+` ends a find `-exec` only as the `{} +` pair — a bare `+` is
      passed to the command, and `-quit` stays dead when the `-o`
      lives inside an action argv (Devin);
    * `xargs --arg-f F` binds the argfile via GNU unique prefix —
      _xargs_argfile/_xargs_utility share the prefix table (Devin);
    * `xargs --parallel`/`--buffer-size` and `split --debug` are
      rejected by the installed GNU tools — never reach argv (Devin);
    * `flock -c X LOCK`/`--command X LOCK` abort — the command-string
      options only parse AFTER the lockfile (Devin);
    * `--separator='\\0'`/`-t '\\0'` is split's NUL escape — the
      filter still runs (Codex);
    * `iconv --usage` is a print-and-exit mode — the stream dies
      unread (Codex);
    * `tail --pid nope`/`pr --indent xyz` abort on non-digit
      operands before reading (Codex)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_text("echo HIT\n")
    for line in (
            # round-robin streams a pipe — the filter runs the chunks
            # (verified live)
            b"cat scripts/x.sh | split -n r/2 --filter=sh - | sh",
            # `-n` streams an unseekable pipe on ≥9.x — the filter
            # runs on the buffered input; LAST -n wins either way
            # (round-51 union, verified live on 9.4)
            b"cat scripts/x.sh | split -n l/2 --filter=sh - | sh",
            b"cat scripts/x.sh | split -n r/2 -n l/2 --filter=sh - | sh",
            b"cat scripts/x.sh | split --number=r/2 --filter=sh - | sh",
            # a bare `+` inside the action argv is data — the later
            # action still runs (verified live)
            b"find . -exec echo + --help \\; -exec sh scripts/x.sh \\;",
            # `-e`/`-i`/`--eof` optional args leave the next word as
            # the command — argfile still binds (verified live)
            b"cat scripts/x.sh | xargs --arg-f scripts/x.sh cat | sh",
            b"cat scripts/x.sh | xargs --arg-file scripts/x.sh cat | sh",
            # NUL separator escape — the filter still runs (verified)
            b"cat scripts/x.sh | split --separator='\\0' --lines=1 --filter=sh - | sh",
            b"cat scripts/x.sh | split -t '\\0' --filter=sh - | sh",
            # digit operands stay valid (verified live)
            b"cat scripts/x.sh | tail --pid 9 | sh",
            b"cat scripts/x.sh | pr --indent 4 | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # r/K/N is a chunk-select — refuses --filter outright
            # (verified live; the `-n l/2`/`r/2 -n l/2` filter cases
            # moved to the dep list under the ≥9.x union — round-51)
            b"cat scripts/x.sh | split -n r/1/2 --filter=sh - | sh",
            # `{}`-terminated action ends at `+` — `--help` is then a
            # real terminal word (verified live)
            b"find . -exec echo {} + --help \\; -exec sh scripts/x.sh \\;",
            # `-quit` still wins when the `-o` lives inside an action
            # argv (verified live)
            b"find . -quit -exec echo + -o \\; -exec sh scripts/x.sh \\;",
            # the argfile consumes the pipe's utility — xargs reads
            # /dev/null, not the stream (verified live)
            b"cat scripts/x.sh | xargs --arg-f /dev/null echo | sh",
            # rejected options exit before argv (verified live)
            b"xargs --parallel sh scripts/x.sh",
            b"xargs --buffer-size=100 sh scripts/x.sh",
            b"cat scripts/x.sh | split --debug --filter=sh - | sh",
            # `-c`/`--command` before the lockfile abort (verified)
            b"flock -c X /dev/null sh scripts/x.sh",
            b"flock --command X /dev/null sh scripts/x.sh",
            # print-and-exit modes never read the stream (verified)
            b"cat scripts/x.sh | iconv --usage | sh",
            b"cat scripts/x.sh | iconv --version | sh",
            # non-digit option operands abort before reading
            # (verified live)
            b"cat scripts/x.sh | tail --pid nope | sh",
            b"cat scripts/x.sh | tail --pid=nope | sh",
            b"cat scripts/x.sh | pr --indent xyz | sh",
            b"cat scripts/x.sh | pr -o xyz | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round46(tmp_path):
    """Review round-46 — split zero/chunk-select counts, `-n K/N`
    stdout streams, find's pre-path `-D`, signed reader numerics,
    xargs `-E`/cluster `-a`/optional-arg `=` scan, and sort
    `--files0-from` list indirection — each verified against live
    GNU tools:

    * `split -l 0`/`-b0`/`-C0`/`-n 0`/`-n r/0`/`-n 0/1`/`-n 2/1`
      abort "invalid number …/chunk number" before any filter or
      input; `-a0` stays legal (Devin/Codex);
    * `split -n r/1/1` streams chunk 1 to stdout on a pipe; `1/1`
      and `l/1/1` stream a FILE — only the two-part K/N forms emit
      to stdout (CodeRabbit);
    * `find -D --help` consumes `--help` as the debugopts operand
      pre-path and still runs the action (Codex);
    * `tail --pid=+1`/`pr --indent=+1` run — GNU accepts a leading
      `+` but rejects `-1` (Devin);
    * `xargs -E END` consumes END as the required eof-string —
      `-e` alone is the optional form (CodeRabbit);
    * `xargs -0a/dev/null` binds the argfile mid-cluster; a glued
      `--eof=STOP`/`--replace=R` does not end the argfile scan —
      last `-a` still wins (Devin + CodeRabbit);
    * `sort --files0-from F` may name `-`/`/dev/stdin` inside F —
      the stream is never proven dead (over-block; `/dev/null` is
      the provably-empty exception) (Codex)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_text("echo HIT\n")
    for line in (
            # `-n K/N` prints the selected chunk to stdout — the
            # input flows through like a reader's (verified live)
            b"cat scripts/x.sh | split -n r/1/1 | sh",
            b"cat scripts/x.sh | split -n r/1/1 - | sh",
            b"cat scripts/x.sh | split --number=r/1/1 | sh",
            b"split -n 1/1 scripts/x.sh | sh",
            b"split -n l/1/1 scripts/x.sh | sh",
            # `-a0` is legal — the filter still runs (verified live)
            b"cat scripts/x.sh | split -a0 -l1 --filter=sh - | sh",
            # `-D` consumes the next word pre-path — `--help` is the
            # debugopts operand, not a terminal word (verified live)
            b"find -D --help /dev/null -exec sh scripts/x.sh \\;",
            # signed numeric operands stay valid (verified live)
            b"cat scripts/x.sh | tail --pid=+1 | sh",
            b"cat scripts/x.sh | tail --pid +1 | sh",
            b"cat scripts/x.sh | pr --indent=+1 | sh",
            # `-E` takes a required operand — END is not the utility
            # and the live pipe feeds the argfile-replaced utility's
            # stdin (verified live)
            b"cat scripts/x.sh | xargs -E END -a /dev/null sh -c 'sh'",
            b"cat scripts/x.sh | xargs -0E END -a /dev/null sh -c 'sh'",
            # `-a` inside a short cluster binds the argfile —
            # the utility inherits the live pipe (verified live)
            b"cat scripts/x.sh | xargs -0a/dev/null sh -c 'sh'",
            b"cat scripts/x.sh | sh -c 'xargs -0a/dev/null true; sh'",
            # glued OPTIONAL-arg longs keep the argfile scan going —
            # last `-a` wins = /dev/null, pipe stays live (verified)
            b"cat scripts/x.sh | xargs -a - --eof=STOP -a /dev/null sh -c 'sh'",
            b"cat scripts/x.sh | sh -c 'xargs -a - --eof=STOP -a /dev/null true; sh'",
            # a regular list file may name /dev/stdin — the stream
            # is never proven dead (Codex on #1393; over-block)
            b"cat scripts/x.sh | sort --files0-from scripts/x.sh | sh",
            b"cat scripts/x.sh | sort --files0-from=names.lst | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # zero/invalid split counts abort before any filter or
            # read (verified live)
            b"cat scripts/x.sh | split -l0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -l 0 --filter=sh - | sh",
            b"cat scripts/x.sh | split --lines=0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -b0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -C0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -n 0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -n r/0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -n l/0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -n 0/0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -n 1/0 --filter=sh - | sh",
            b"cat scripts/x.sh | split -n 0/1 --filter=sh - | sh",
            b"cat scripts/x.sh | split -n 2/1 --filter=sh - | sh",
            # a `-n` K/N stdout select combined with --filter aborts
            # "does not process a chunk extracted to stdout" — the
            # filter never runs (verified live)
            b"cat scripts/x.sh | split -n r/1/1 --filter=sh - | sh",
            b"split -n 1/1 --filter=sh scripts/x.sh | sh",
            # negative numerics still abort (verified live)
            b"cat scripts/x.sh | tail --pid=-1 | sh",
            b"cat scripts/x.sh | pr --indent=-1 | sh",
            # `/dev/null` is the provably-empty list — nothing is
            # emitted (round-25 semantics kept)
            b"cat scripts/x.sh | sort --files0-from /dev/null | sh",
            b"cat scripts/x.sh | sort --files0-from=/dev/null | sh",
            # the stdin-alias list form still drains the pipe as the
            # list — `; sh` sees EOF (round-25 semantics kept)
            b"cat scripts/x.sh | sh -c 'sort --files0-from=-; sh'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round47(tmp_path):
    """Review round-47 — each case verified against live GNU tools:

    * `find -D help` (anywhere in the debugopts list) prints the -D
      usage and exits BEFORE the expression — `-D exec,help` never
      runs the action; `-D all` excludes help and runs. A terminal
      word anywhere drops every action span.
    * `split`'s input is classified independently of any `< f` stdin
      rebind: a named operand still wins (`split -n r/1/1 F
      </dev/null` streams F's chunk), and a redirect feeds the
      filter (`split --filter=sh - <x` runs x's bytes through
      $SHELL).
    * `find -exec split x \\;` is NOT a dep — split writes chunk
      FILES and find's stdout stays empty — while `--filter=CMD`
      execs the operand's bytes and `-n K/N` emits a chunk (pipe
      decides).
    * `xargs -a - --help`/`--version`/a parse error exits BEFORE the
      argfile or stdin is touched — the pipe stays unread for a
      SIBLING command and the stage emits only usage text.
    * A DYNAMIC `IFS=$v`/`IFS=$(…)` assignment REPLACES the tracked
      IFS — an earlier literal cannot stay in effect.
    * The SCRIPT_REF arg window is quote-aware — a QUOTED `|`/`&`/`;`
      is operand text, so `--filter="cat | sh"` keeps a later
      `scripts/` operand in reach.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # a `< f` stdin rebind feeds split's filter — the script's
            # bytes execute through $SHELL (was: fd_in discarded the
            # classification entirely)
            b"split --filter=sh - <scripts/x.sh",
            b"split --filter=sh <scripts/x.sh",
            b"split --filter=sh - < scripts/x.sh",
            b"cat scripts/x.sh | split --filter=sh - <scripts/x.sh",
            # a named operand still wins over the rebind — F's chunk
            # streams to stdout (was: `not fd_in` short-circuited to
            # "own" before the split branch ran)
            b"split -n r/1/1 scripts/x.sh </dev/null | sh",
            b"split -n 1/1 scripts/x.sh </dev/null | sh",
            b"split -n l/1/1 scripts/x.sh </dev/null | sh",
            # terminal xargs exits before the argfile/stdin is read —
            # a SIBLING command still sees the pipe
            b"cat scripts/x.sh | (xargs -a - --help; sh)",
            b"cat scripts/x.sh | (xargs -a - --version; sh)",
            b"cat scripts/x.sh | (xargs --help >/dev/null; sh)",
            b"cat scripts/x.sh | (xargs -a /dev/null --help >/dev/null; sh)",
            # a find `-exec split` filter execs the operand's bytes
            b"find . -exec split --filter=sh scripts/x.sh \\;",
            b"find . -exec split --filter=\"cat | sh\" scripts/x.sh \\;",
            b"find . -exec split -n 1/1 scripts/x.sh \\; | sh",
            # a quoted `|`/`;` inside an option operand is TEXT — the
            # scripts/ path still counts as the filter's input
            b"split --filter=\"cat | sh\" scripts/x.sh",
            b"split --filter='cat | sh' scripts/x.sh",
            b"split --filter=sh scripts/x.sh",
            # kept semantics — the filter forwards/emits (round-46)
            b"split --filter=cat scripts/x.sh | sh",
            b"cat scripts/x.sh | split -n r/1/1 | sh",
            # `-D all` excludes help — the action still runs
            b"find -D all . -exec sh scripts/x.sh \\;"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `-D help` is terminal — the action never runs
            b"find -D help /dev/null -exec sh scripts/x.sh \\;",
            b"find -D exec,help . -exec sh scripts/x.sh \\;",
            b"find -D help -D all . -exec sh scripts/x.sh \\;",
            # `-exec split x \\;` writes chunk FILES — find's stdout
            # stays empty, a downstream exec sees nothing
            b"find . -exec split scripts/x.sh \\; | sh",
            b"find . -exec split -l1 scripts/x.sh \\; | sh",
            # a swallowed/non-running filter execs nothing — the
            # input was rebound to an empty file
            b"split --filter=sh - </dev/null | sh",
            b"cat scripts/x.sh | split --filter=sh - </dev/null | sh",
            b"split -n r/1/1 - </dev/null | sh",
            # terminal xargs emits only usage — the stream never
            # reaches the next PIPE stage's stdin
            b"cat scripts/x.sh | xargs --help | sh",
            b"cat scripts/x.sh | xargs -a - --help | sh",
            b"cat scripts/x.sh | xargs --version | sh",
            # kept semantics — bare split writes chunk files (round-41)
            b"split scripts/x.sh | sh",
            b"cat scripts/x.sh | split -l1 | sh",
            b"split --filter=true scripts/x.sh | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # a dynamic `IFS=` REPLACES the tracked value — `IFS=','; IFS=$v`
    # leaves IFS unproven (not the stale literal): it binds like an
    # EMPTY IFS (`b""`), not the shell default (round-50)
    assert mod._line_ifs(b"IFS=','; IFS=$v; date +x\n", 20) == b""
    assert mod._line_ifs(b"IFS=','; IFS=$(cat f); date +x\n", 25) == b""
    assert mod._line_ifs(b"IFS='x'; IFS=$v; date +x\n", 18) == b""
    # a literal still binds — and a command-scoped prefix does not
    assert mod._line_ifs(b"IFS=','; date +x\n", 8) == b","
    assert mod._line_ifs(b"IFS=',' date +x\n", 18) is None

def test_script_dep_round48(tmp_path):
    """Round-48 review fixes (each verified live against bash/util-linux):
    a `< file` stdin rebind is seekable so `split -n K/N` streams the
    chunk instead of aborting; a substitution-bearing `--filter`
    operand expands to an opaque command and must be treated as
    executing its chunks; flock's pre-lockfile words go through the
    shared validated GNU walk so unique-prefix terminal modes
    (`--vers` → `--version`) and ambiguous prefixes (`--ve`) exit
    before the command argv; `tail --max-unchanged-stats` shares the
    non-negative-integer domain (`=bad` aborts before any read); and
    query/describe modes (`ionice -p/-P/-u`, `taskset -p`, `chrt -p`,
    `prlimit -p`, `setpriv --dump`, `sudo -l/-v/-e`, `command -v`)
    never reach a trailing command — words after them are query
    operands."""
    mod = load_resolve_module()
    pdir = tmp_path / "pack"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_text("echo hi\n")
    for line in (
            # seekable stdin — `-n K/N` streams the rebound file's chunk
            b"split -n 1/1 - <scripts/x.sh | sh",
            b"split -n 1/1 <scripts/x.sh | sh",
            b"split -n l/1/1 <scripts/x.sh | sh",
            b"split -n r/1/1 <scripts/x.sh | sh",
            b"cat /dev/null | split -n 1/1 - <scripts/x.sh | sh",
            # the same redirect feeds `--filter` too
            b"split --filter='$(printf sh)' - <scripts/x.sh | sh",
            # an expanding filter operand resolves to an opaque
            # command — `$(printf sh)` runs sh on every chunk
            b"split --filter='$(printf sh)' scripts/x.sh",
            b'split --filter="$(printf sh)" scripts/x.sh',
            b"find . -exec split --filter='$(printf sh)' scripts/x.sh \\;",
            # kept semantics — literal filter heads and exec-mode
            # wrappers still count
            b"split --filter=sh scripts/x.sh",
            b"split --filter=cat scripts/x.sh | sh",
            b"ionice -c 2 sh scripts/x.sh",
            b"ionice -c2 -n 5 sh scripts/x.sh",
            b"taskset 0x1 sh scripts/x.sh",
            b"taskset -c 0 sh scripts/x.sh",
            b"chrt -o 0 sh scripts/x.sh",
            b"chrt -T 1 -P 2 -D 3 0 sh scripts/x.sh",
            b"flock /tmp/l -c 'sh scripts/x.sh'",
            b"flock -n /tmp/l sh -c 'sh scripts/x.sh'",
            # `-n 1/1` streams a buffered pipe on ≥9.x (round-51 union)
            b"cat scripts/x.sh | split -n 1/1 | sh",
            b"flock --verb /tmp/l -c 'sh scripts/x.sh'",
            b"flock -w5 /tmp/l -c 'sh scripts/x.sh'",
            b"sudo sh scripts/x.sh",
            b"sudo -n -u root sh scripts/x.sh",
            b"command sh scripts/x.sh",
            b"prlimit --cpu=1 sh scripts/x.sh",
            b"cat scripts/x.sh | tail --max-unchanged-stats=2 | sh",
            b"cat scripts/x.sh | tail --max-unchanged-stats=+2 | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # query/describe modes — the trailing words are PID/name
            # operands, never a command
            b"ionice -p 1 sh scripts/x.sh",
            b"ionice -p1 sh scripts/x.sh",
            b"ionice -P 1 sh scripts/x.sh",
            b"ionice -u adrian sh scripts/x.sh",
            b"ionice --pid 1 sh scripts/x.sh",
            b"ionice --pid=1 sh scripts/x.sh",
            b"ionice --process-group 1 sh scripts/x.sh",
            b"ionice --user adrian sh scripts/x.sh",
            b"taskset -p 1 sh scripts/x.sh",
            b"taskset -p1 sh scripts/x.sh",
            b"taskset --pid 1 sh scripts/x.sh",
            b"chrt -p 1 sh scripts/x.sh",
            b"chrt -p1 sh scripts/x.sh",
            b"chrt --pid 1 sh scripts/x.sh",
            b"prlimit -p 1 sh scripts/x.sh",
            b"prlimit --pid 1 sh scripts/x.sh",
            b"setpriv --dump sh scripts/x.sh",
            b"command -v sh scripts/x.sh",
            b"sudo -l sh scripts/x.sh",
            b"sudo -v sh scripts/x.sh",
            b"sudo -e sh scripts/x.sh",
            # flock's pre-lockfile walk resolves GNU prefixes — a
            # unique terminal prefix or an ambiguous one exits before
            # the command argv
            b"flock --vers /tmp/l -c 'sh scripts/x.sh'",
            b"flock --ver /tmp/l -c 'sh scripts/x.sh'",
            b"flock --ve /tmp/l -c 'sh scripts/x.sh'",
            b"flock --version /tmp/l -c 'sh scripts/x.sh'",
            b"flock --bogus /tmp/l -c 'sh scripts/x.sh'",
            b"flock -V /tmp/l -c 'sh scripts/x.sh'",
            # a bad numeric value aborts before any read
            b"cat scripts/x.sh | tail --max-unchanged-stats=bad | sh",
            b"cat scripts/x.sh | tail --max-unchanged-stats=-1 | sh",
            # an empty input means no chunk — and a pipe
            # input means the expanding filter never sees a script
            b"split -n 1/1 - </dev/null | sh",
            b"split --filter='$(printf sh)' - | sh",
            # kept semantics — an empty/benign source execs nothing
            b"split --filter=true scripts/x.sh | sh",
            b"split -n r/1/1 - </dev/null | sh",
            # `-c0` aborts "invalid option -- '0'" — util-linux `-c`
            # binds only a separate operand (Codex on #1393,
            # round-52 review — verified live)
            b"taskset -c0 sh scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round49(tmp_path):
    """Round-49 review fixes (each verified live against bash):
    a `$`/backtick in a `--filter` COMMAND WORD expands to an opaque
    head and must be treated as exec (`$(printf sh)` resolves to sh),
    while an expansion in a LATER word is just an argument
    (`true $HOME` drops the chunk bytes); the last-bound stdin file
    record (`tgts[0]`) retires on `<<`-family, `<&`-rebind/close, `0>`,
    and dynamic `<$F` targets — only a self-dup `<&0` keeps it; a
    dead fd0 (`<&-`, `0>`, `<&1`) makes a `-`/absent split input die
    EBADF before chunks or filters run; a `-exec` argv with no
    `;`/`{} +` terminator is a parse error that kills EVERY action;
    `split IN PREFIX` execs the word only as INPUT or inside the
    filter — a second positional is the written output prefix;
    `pr --pages`/`+N` aborts before any read on a bad page spec.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")
    (pdir / "scripts/y.sh").write_text("echo Y\n")

    for line in (
            # expansion in a LATER filter word is just an argument —
            # `true $HOME` drops the bytes (Devin on #1393)
            b"split --filter='true $HOME' scripts/x.sh | sh",
            b"split --filter='echo $HOME' scripts/x.sh | sh",
            # a literal-quoted $FD target does not expand — the pipe
            # is consumed by the file bind, not exec'd (Codex on
            # #1959 — `<'$FD'` keeps fd0 bound to a literal name)
            b"cat scripts/x.sh | split --filter=sh - <'$FD'",
            # stale last-bound record: `<<`-family rebinds fd0 to
            # content; `<&-` closes it; `<&1` dupes the write-side
            # fd — `split -n 1/1 -` dies before any chunk
            b"split -n 1/1 - <scripts/x.sh <<<y | sh",
            b"split -n 1/1 - <scripts/x.sh <<EOF | sh",
            b"split -n 1/1 - <scripts/x.sh <&- | sh",
            b"split -n 1/1 - <scripts/x.sh <&1 | sh",
            b"split --filter=sh - <scripts/x.sh <&-",
            # unterminated `-exec` argv is a find parse error —
            # `missing argument to '-exec'` kills every action
            b"find . -exec sh scripts/x.sh",
            b"find . -exec sh scripts/x.sh ;",
            b"find . -exec sh scripts/x.sh \\; -exec cat scripts/y.sh",
            b"find . -exec sh scripts/x.sh +",
            # `split IN PREFIX` — a second positional is the output
            # prefix, written not read (Devin on #130)
            b"find . -exec split --filter=sh /dev/null scripts/x.sh \\;",
            (b"find . -exec split -n 1/1 /dev/null scripts/x.sh \\;"
             b" | sh"),
            # `pr --pages=bad`/`+bad` abort before the read (Codex on
            # #130)
            b"cat scripts/x.sh | pr --pages=bad | sh",
            b"cat scripts/x.sh | pr +bad | sh",
            b"cat scripts/x.sh | pr --pages | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # the expansion is in the filter's COMMAND WORD — opaque
            # head; `$(printf sh)` resolves to sh and execs chunks
            b"split --filter='$(printf sh)' scripts/x.sh",
            # dynamic `<$FD`/`<"$FD"` targets — name resolved at
            # runtime; keep the pipe candidate (Codex on #1959)
            b"cat scripts/x.sh | split --filter=sh - <\"$FD\"",
            b"cat scripts/x.sh | split --filter=sh - <$FD",
            # `sh $X` — an unset X collapses to bare `sh` (reads
            # stdin, execs the chunks); a set X reads FILE X — the
            # dynamic case stays fail-closed (verified live)
            b"split --filter='sh $X' scripts/x.sh | sh",
            # self-dup `<&0` leaves fd0 — and its filename — bound
            b"split -n 1/1 - <scripts/x.sh <&0 | sh",
            # literal `< file` record still feeds the chunk
            b"split -n 1/1 - <scripts/x.sh | sh",
            b"split -n 1/1 scripts/x.sh | sh",
            # `-exec` terminated with `\\;`/`{} +` runs the argv
            b"find . -exec sh scripts/x.sh \\;",
            b"find . -exec sh scripts/x.sh {} +",
            # the word IS the input operand — read by the chunker
            b"find . -exec split --filter=sh scripts/x.sh \\;",
            b"find . -exec split -n 1/1 scripts/x.sh out \\; | sh",
            # a valid `pr --pages`/`+N` spec keeps the stream live
            b"cat scripts/x.sh | pr --pages=1 | sh",
            b"cat scripts/x.sh | pr --pages=1:9 | sh",
            b"cat scripts/x.sh | pr +2 | sh",
            b"cat scripts/x.sh | pr --pages 2 | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round50(tmp_path):
    """Round-50 review fixes (each verified live against bash):
    an unrecognised unshare option aborts before the program runs
    (`unshare --bogus sh`); GNU unique prefixes of `--files0-from`
    (`--files0-f`) bind the same list operand; a dynamic `IFS=$X`/
    `IFS=$(…)` value can't be proven to split, so it binds like an
    EMPTY IFS (one field); `--numeric-suffixes`/`--hex-suffixes`
    values are validated against the FINAL suffix length; an
    all-whitespace `--filter` has no command word; a third split
    positional is an "extra operand" abort; `pr --pages=F:L` needs
    L >= F; `flock -- L -c CMD` still binds CMD after `--`; and a
    `scripts/` path inside `--filter` text executes the bundled
    script (`sh scripts/x.sh`) or emits its bytes (`cat`).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")
    (pdir / "scripts/y.sh").write_text("echo Y\n")

    for line in (
            # unrecognised unshare options abort before the program
            # runs (Codex on #1393)
            b"unshare --bogus sh scripts/x.sh",
            b"unshare -y sh scripts/x.sh",
            # a `=` on a pure flag aborts "doesn't allow an argument"
            b"unshare --map-root-user=x sh scripts/x.sh",
            b"unshare --fork=x sh scripts/x.sh",
            # split suffix-start values are validated — non-numeric,
            # non-hex, and past the suffix-length bound all abort
            b"cat scripts/x.sh | split --numeric-suffixes=bad"
            b" --filter=sh -",
            b"cat scripts/x.sh | split --numeric-suffixes=0x10"
            b" --filter=sh -",
            b"cat scripts/x.sh | split --numeric-suffixes=100"
            b" --filter=sh -",
            b"cat scripts/x.sh | split --hex-suffixes=xyz"
            b" --filter=sh -",
            b"cat scripts/x.sh | split --hex-suffixes=100"
            b" --filter=sh -",
            # a third positional is "extra operand" — the filter
            # never runs
            b"cat scripts/x.sh | split --filter=sh in1 in2 in3 | sh",
            b"cat scripts/x.sh | split -- in1 in2 in3 | sh",
            b"cat scripts/x.sh | split --filter=sh - p extra | sh",
            # `pr --pages=F:L` with L < F aborts before any read
            b"cat scripts/x.sh | pr --pages=1:0 | sh",
            b"cat scripts/x.sh | pr --pages=2:1 | sh",
            # a `-c`-shaped word directly after `--` is the lockfile
            # — `sh` is argv[0] but nothing pipes in
            b"flock -- -c sh",
            # a literal `--` word post-lockfile is the dead argv[0]
            b"cat scripts/x.sh | flock -- /tmp/l -- sh",
            # an all-whitespace `--filter` has no command word — the
            # $SHELL -c no-op drops the chunk bytes
            b"cat scripts/x.sh | split --filter=' ' - | sh",
            # two positionals are legal (INPUT + PREFIX) but in1 is
            # not a scripts/ input — its chunks aren't script bytes
            b"cat scripts/x.sh | split --filter=sh in1 in2 | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # unique prefixes of --files0-from resolve to the same
            # operand — the list file replaces stdin (Codex on
            # #1393)
            b"cat scripts/x.sh | sort --files0-f scripts/y.sh | sh",
            b"cat scripts/x.sh | sort --files0- scripts/y.sh | sh",
            b"cat scripts/x.sh | sort --files0 scripts/y.sh | sh",
            # `flock -- L -c CMD` binds CMD even after the `--` that
            # ended the pre-lockfile options (Devin on #130)
            b"flock -- /tmp/l -c 'sh scripts/x.sh'",
            # suffix values inside the bound still run the filter
            b"cat scripts/x.sh | split --numeric-suffixes=5"
            b" --filter=sh - | sh",
            b"cat scripts/x.sh | split --hex-suffixes=ff"
            b" --filter=sh - | sh",
            b"cat scripts/x.sh | split --numeric-suffixes=100 -a4"
            b" --filter=sh - | sh",
            b"cat scripts/x.sh | split -a4 --numeric-suffixes=100"
            b" --filter=sh - | sh",
            # two positionals (INPUT + PREFIX) are legal — INPUT
            # IS the scripts/ file, and its chunks reach the filter
            b"cat scripts/x.sh | split --filter=sh scripts/x.sh p"
            b" | sh",
            # a scripts/ path in the filter text — interpreter head
            # EXECUTES it (Devin on #1959)
            b"split --filter='sh scripts/x.sh' -",
            b"split --filter='bash scripts/x.sh' -",
            b"split --filter='echo x; sh scripts/x.sh' -",
            # a reader head re-emits the script's bytes to |sh
            b"split --filter='cat scripts/x.sh' - | sh",
            b"split --filter='head -1 scripts/x.sh' - | sh",
            # valid page range keeps the stream live
            b"cat scripts/x.sh | pr --pages=2:2 | sh",
            # unshare's own options parse and still exec the program
            b"unshare --mount sh scripts/x.sh",
            b"unshare --mount=/tmp/m sh scripts/x.sh",
            b"unshare -fm sh scripts/x.sh",
            b"unshare -R /tmp sh scripts/x.sh",
            b"unshare --kill-child=SIGTERM sh scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round51(tmp_path):
    """Review round-51 fixes: `--` ends a wrapper's options (the next
    word is argv[0]), a `split --filter=CMD` operand is program text
    even with no downstream pipe, split's filter never fires on an
    EMPTY /dev/null input, an unknown find predicate aborts before
    traversal, sort refuses operands alongside --files0-from, only a
    `-c` bound DIRECTLY after flock's lockfile binds command text,
    and `-n` chunk-selects stream an unseekable pipe on coreutils
    ≥9.x (union-of-versions, fail-closed).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")
    (pdir / "scripts/y.sh").write_text("echo Y\n")
    (pdir / "scripts/f0").write_bytes(b"a\x00\n")

    for line in (
            # `--` terminates the wrapper's options — `unshare -- sh`
            # runs sh (the strict option class read `--` as an
            # abort — Devin on #1393)
            b"unshare -- sh scripts/x.sh",
            b"unshare --mount -- sh scripts/x.sh",
            # a `--filter=CMD` operand is program text the filter's
            # $SHELL -c runs even without a downstream pipe (Devin
            # on #1393/#12, Codex on #1959)
            b"split --filter='sh scripts/x.sh' input",
            b"split --filter='sh scripts/x.sh' -",
            b"split --filter='echo x; sh scripts/x.sh' input",
            # coreutils ≥9.x buffers unseekable pipe input to a temp
            # file, so `-n` chunk-selects STREAM the pipe (Codex on
            # #130/#1959 — verified live on 9.4; 8.32 aborts —
            # union models the running case)
            b"cat scripts/x.sh | split -n 1/1 | sh",
            b"cat scripts/x.sh | split -n l/1/1 | sh",
            b"cat scripts/x.sh | split -n r/1/1 | sh",
            # a `< file` rebind is seekable either way
            b"cat /dev/null | split -n 1/1 - <scripts/x.sh | sh",
            # a `-c` directly after the lockfile still binds command
            # text; `flock L sh -c PROG` runs PROG
            b"flock L -c 'sh scripts/x.sh'",
            b"flock L sh -c 'cat scripts/x.sh'",
            # the wrapped command's own argv[0] remains an exec
            b"flock -- /tmp/l -c 'sh scripts/x.sh'",
            # a wrapped emit head still forwards to |sh
            b"flock L echo -c 'sh scripts/x.sh' | sh",
            # a find action runs while only known primaries follow
            b"find . -exec bash scripts/x.sh \\; -print",
            b"find . -name '*.sh' -exec bash scripts/x.sh \\;"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # the word after `--` is argv[0] — `unshare -- echo`
            # just prints (Codex on #130)
            b"unshare -- echo scripts/x.sh",
            # an EMPTY input yields zero chunks — the filter never
            # fires (Devin on #130: `--filter='echo RAN'` does not
            # run on /dev/null)
            b"split --filter='sh scripts/x.sh' /dev/null",
            b"cat scripts/x.sh | split --filter='echo RAN' /dev/null"
            b" | sh",
            # sort refuses operands alongside --files0-from ("extra
            # operand … cannot be combined" abort — Devin on #130)
            b"sort --files0-from=scripts/f0 f | sh",
            b"cat scripts/x.sh | sort --files0-from=f0 extra | sh",
            # an unknown predicate aborts find BEFORE it traverses —
            # no action runs (Devin on #130)
            b"find . -exec bash scripts/x.sh \\; -bogus",
            b"find . -exec sh scripts/x.sh \\; --bogus",
            # `-c` inside the wrapped command's argv is argv DATA —
            # `flock L echo -c 'sh x'` prints the string (Devin on
            # #1959)
            b"flock L echo -c 'sh scripts/x.sh'",
            b"flock -- /tmp/l echo -c 'sh scripts/x.sh'",
            # a wrapped emit head's output to a terminal is never
            # exec'd — bare `xargs echo 'sh x'` prints, it does not
            # run (Devin on #1959)
            b"xargs echo 'sh scripts/x.sh'",
            b"sudo echo 'sh scripts/x.sh'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round52(tmp_path):
    """Review round-52 fixes: wrapped-argv interpreter operands exec a
    positional script file (`flock L sh x`, `xargs bash x`, `xargs
    python3 x`), taskset's `-c`/`--cpu-list` bind ONLY as a separate
    operand (`-c0`/`--cpu-list=0` abort), `sort -o`/`--output` diverts
    the stream to a FILE, `--additional-suffix` rejects a `/` value,
    `grep --binary-files` has a fixed {binary,text,without-match}
    domain, `pr --pages=00` aborts like `--pages=0`, split's filter
    never fires when fd0 is /dev/null, and `find -OLEVEL` binds only
    in the option region before the first path (verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")
    (pdir / "scripts/x.py").write_text("print(1)\n")

    for line in (
            # an interpreter head inside a wrapper's argv runs its
            # positional script file (Codex on #130/#1959 + Devin on
            # #130/#1393)
            b"flock L sh scripts/x.sh",
            b"flock L bash scripts/x.sh",
            b"xargs sh scripts/x.sh",
            b"xargs bash /tmp/scripts/x.sh",
            b"xargs python3 scripts/x.py",
            b"flock L python3 scripts/x.py",
            # taskset's separate-operand forms still reach the command
            b"taskset -c 0 sh scripts/x.sh",
            b"taskset 0 sh scripts/x.sh",
            b"taskset --cpu 0 sh scripts/x.sh",
            # sort's fd-1 output aliases keep the stream on stdout
            b"cat scripts/x.sh | sort --output=/dev/stdout | sh",
            b"cat scripts/x.sh | sort -o/dev/stdout | sh",
            # a plain suffix binds; a binary-files mode binds
            b"split --additional-suffix=ok "
            b"--filter='sh scripts/x.sh' -",
            b"cat scripts/x.sh | grep --binary-files=binary "
            b"scripts/x.sh | sh",
            # pages=01 paginates and emits
            b"cat scripts/x.sh | pr --pages=01 | sh",
            # a LIVE fd0 feeds the filter
            b"split --filter='sh scripts/x.sh' - </dev/stdin",
            # `-OLEVEL` in the pre-path option region is valid GNU
            b"find -O3 . -exec sh scripts/x.sh \\;",
            # program text after sh -c still runs (wrapped + bare)
            b"flock L -c 'sh scripts/x.sh'",
            b"flock L sh -c 'cat scripts/x.sh'"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # util-linux binds `-c`/`--cpu-list` ONLY separately —
            # `-c0` exits "invalid option", `--cpu-list=0` "doesn't
            # allow an argument" (Codex on #1393)
            b"taskset -c0 sh scripts/x.sh",
            b"taskset --cpu-list=0 sh scripts/x.sh",
            # `-o`/`--output FILE` diverts the result — the pipe sees
            # nothing; `-o -` writes a file literally named `-`
            # (Codex + Devin on #1393)
            b"cat scripts/x.sh | sort --out /tmp/o | sh",
            b"cat scripts/x.sh | sort -o /tmp/o | sh",
            b"cat scripts/x.sh | sort -o - | sh",
            # a `/` in --additional-suffix aborts "invalid suffix …
            # contains directory separator" (Codex on #130)
            b"split --additional-suffix=/bad "
            b"--filter='sh scripts/x.sh' -",
            # `wat` is not a binary-files type — aborts "unknown
            # binary-files type" (Codex on #130)
            b"grep --binary-files=wat scripts/x.sh",
            # `00` is a zero head — aborts "invalid page range" like
            # `0` (Devin on #12)
            b"cat scripts/x.sh | pr --pages=00 | sh",
            # fd0 on /dev/null yields zero chunks — the filter never
            # fires (Devin on #1959)
            b"split --filter='sh scripts/x.sh' </dev/null",
            b"split --filter='sh scripts/x.sh' - </dev/null",
            # `-O*` inside the expression is an unknown predicate /
            # bad decimal — find aborts before the action (Devin on
            # #1959/#12)
            b"find . -exec sh scripts/x.sh \\; -O3",
            b"find . -exec sh scripts/x.sh \\; -O9",
            b"find . -exec sh scripts/x.sh \\; -Oabc",
            # after a program flag the positional is argv ($0), not
            # the script (Devin on #1959, preserved)
            b"flock L echo -c 'sh scripts/x.sh'",
            b"flock L sh -c : scripts/x.sh",
            b"flock L sh -c 'echo P' scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round53(tmp_path):
    """Review round-53 fixes: sort's operand-taking long options consume
    the NEXT argument even when it looks like an option (`sort -T
    --output=/dev/null` parses --output… as the temp DIR, leaving stdout
    live), find's global `-H`/`-L`/`-P`/`-debug` flags are legal only in
    the pre-path option region while `-noignore_readdir_race` is a valid
    expression option, an `env` link inside a wrapper's argv still
    resolves to the interpreter behind it (`flock L env bash x`), and
    split accepts a leading `+` on its numeric operands (`-l +1` runs
    the filter; `-l +0` aborts — all verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")

    for line in (
            # `-T`/`--temporary-directory` (and friends) consume the
            # next word as their operand — `--output=…` is then a temp
            # dir, so the stream stays on stdout (Devin on #130)
            b"cat scripts/x.sh | sort -T --output=/dev/null | sh",
            b"cat scripts/x.sh | sort --temporary-directory "
            b"--output=/dev/null | sh",
            b"cat scripts/x.sh | sort --compress-program "
            b"--output=/dev/null | sh",
            # `-noignore_readdir_race` is a valid expression option —
            # traversal still reaches the action (Codex on #130/#1959)
            b"find . -noignore_readdir_race -exec sh scripts/x.sh \\;",
            # the GLOBAL flags work before the first path
            b"find -H . -exec sh scripts/x.sh \\;",
            b"find -L -P -debug . -exec sh scripts/x.sh \\;",
            # an `env` wrapper inside argv forwards to the command —
            # the interpreter head still reads its script operand
            # (Codex on #1393, verified live: NEST_RAN)
            b"flock L env bash scripts/x.sh",
            b"flock L env V=1 sh scripts/x.sh",
            b"xargs env bash scripts/x.sh",
            b"xargs env python3 scripts/x.sh",
            # split numeric operands may carry a leading `+` — the
            # filter still runs (Codex on #1959)
            b"split --lines=+1 --filter='sh scripts/x.sh' -",
            b"split -l +1 --filter='sh scripts/x.sh' -",
            b"split -n +1 --filter='sh scripts/x.sh' -",
            b"split -b +1K --filter='sh scripts/x.sh' -",
            b"split -b +1KB --filter='sh scripts/x.sh' -",
            b"split -a +1 --filter='sh scripts/x.sh' -"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # `-H`/`-L`/`-P`/`-debug` inside the expression are unknown
            # predicates — find aborts before any traversal (Devin on
            # #12/#1959)
            b"find . -H -exec sh scripts/x.sh \\;",
            b"find . -L -exec sh scripts/x.sh \\;",
            b"find . -P -exec sh scripts/x.sh \\;",
            b"find . -debug -exec sh scripts/x.sh \\;",
            # `env`'s own argv0 is a direct exec, not an interpreter
            # read — same family as `env x`/`xargs x`/`nice x`/`flock L
            # x`, none of which SCRIPT_REF sees (accepted gap)
            b"flock L env scripts/x.sh",
            b"env scripts/x.sh",
            # after `bash -c P` the trailing word is $0, not a script
            b"flock L env bash -c 'echo P' scripts/x.sh",
            # `-l +0`/`-l +` abort (invalid line count); `-n +0/1` and
            # the mixed-sign K/N forms error out on the stdout/filter
            # conflict even though they parse (Codex on #1959)
            b"split -l +0 --filter='sh scripts/x.sh' -",
            b"split -l + --filter='sh scripts/x.sh' -",
            b"split -n +0/1 --filter='sh scripts/x.sh' -",
            b"split -n +1/1 --filter='sh scripts/x.sh' -",
            b"split -n 1/+2 --filter='sh scripts/x.sh' -"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round54(tmp_path):
    """Review round-54 fixes: an operand-taking sort option shadows a
    later `--files0-from=` word (`sort -T --files0-from=/dev/null` reads
    it as the temp DIR — stdout stays live), find's option region ends
    at the first predicate as well as the first path (`find -name x -H`
    is `unknown predicate`), a path after the expression aborts
    (`find -noignore_readdir_race .` → `paths must precede
    expression`), `-d` is the `-depth` alias, and setsid's option set is
    fully enumerated so an unknown option exits before the wrapped
    command (all verified live).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")

    for line in (
            # `-T`/`--temporary-directory` consume the next word — the
            # files0-from word is the DIR operand, stdin survives
            # (Devin on #130)
            b"cat scripts/x.sh | sort -T --files0-from=/dev/null | sh",
            b"cat scripts/x.sh | sort --temporary-directory "
            b"--files0-from=/dev/null | sh",
            # `-d` is GNU's `-depth` alias — the action still runs
            # (Codex on #130)
            b"find . -d -exec sh scripts/x.sh \\;",
            # global flags still legal before the first path
            b"find -H . -exec sh scripts/x.sh \\;",
            # setsid's real boolean flags still reach the command
            b"cat scripts/x.sh | setsid -w sh",
            b"cat scripts/x.sh | setsid --fork sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # a predicate ends the option region — `-H`/`-L` after it
            # are unknown predicates and find aborts (Devin on #130)
            b"find -name x -H -exec sh scripts/x.sh \\;",
            b"find -name x -L -exec sh scripts/x.sh \\;",
            # a path after the expression aborts "paths must precede
            # expression" before any action runs (Devin on #1959)
            b"find -noignore_readdir_race . -exec sh scripts/x.sh \\;",
            b"find -name x . -exec sh scripts/x.sh \\;",
            b"find . -name x . -exec sh scripts/x.sh \\;",
            # setsid with an unknown/terminal option exits before the
            # wrapped command (Codex on #1393)
            b"cat scripts/x.sh | setsid --bogus sh",
            b"cat scripts/x.sh | setsid -x sh",
            b"cat scripts/x.sh | setsid --fork=x sh",
            b"cat scripts/x.sh | setsid --help sh",
            # `--files0-from=/dev/null` as a REAL option still drains
            # stdin to the list file
            b"cat scripts/x.sh | sort --files0-from=/dev/null | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round55(tmp_path):
    """Round-55 review findings (Codex on #1393, Devin on #130 —
    all verified live):

    - A shell redirection after the expression attaches to the find
      command itself, never the expression — `find . -exec sh x \\;
      >/dev/null` still runs the action. Round-54's positional-word
      terminal treated `>f`/`2>f`/`<f` words as expression
      positionals and dropped every action.
    - A one/two-operand predicate with no remaining operand aborts
      "missing argument" before traversal — `find … -exec sh x \\;
      -size` retains no action.
    - A backtick closer AT the scanned position is still inside the
      pair's word (`x.sh`` `) — the enclosing command owns it, so
      `split --filter=sh x`` ` executes the file.
    - A glued short-option value is validated against the numeric
      and page domains, not only the fixed-value domain — `pr -obad`
      aborts "invalid line offset" before the stage forwards.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")

    for line in (
            # redirects after the expression still run the action
            # (Codex on #1393 — the >/dev/null form was the finding)
            b"find . -exec sh scripts/x.sh \\; >/dev/null",
            b"find . -exec sh scripts/x.sh \\; > /tmp/o",
            b"find . -exec sh scripts/x.sh \\; 2>/dev/null | sh",
            b"find . -exec sh scripts/x.sh \\; </dev/null",
            b"find . -exec sh scripts/x.sh \\; 2>&1 | sh",
            b"find . -exec sh scripts/x.sh \\; >&2",
            b"find . -exec sh scripts/x.sh \\; <>f",
            b"find . -exec sh scripts/x.sh \\; 0<f",
            # heredoc/here-string forms attach too
            b"find . -exec sh scripts/x.sh \\; <<EOF\nfoo\nEOF",
            b"find . -exec sh scripts/x.sh \\; << EOF\nfoo\nEOF",
            b"find . -exec sh scripts/x.sh \\; <<-EOF\nfoo\nEOF",
            b"find . -exec sh scripts/x.sh \\; <<- EOF\nfoo\nEOF",
            b"find . -exec sh scripts/x.sh \\; <<<w",
            # a redirect in the option region leaves the expression
            # intact
            b"find -H >f . -exec sh scripts/x.sh \\;",
            b"find . -exec sh scripts/x.sh \\; >f -name x",
            b"find . -exec sh scripts/x.sh \\; >f -exec sh"
            b" scripts/x.sh \\;",
            # empty `` `` `` at a word's tail still resolves the
            # operand to the prefixed name (Codex on #1393)
            b"split --filter=sh scripts/x.sh``",
            b"sh scripts/x.sh``",
            b"cat scripts/x.sh`` | sh",
            # a valid glued value still forwards
            b"cat scripts/x.sh | pr -o5 | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # a missing predicate operand aborts before any action
            # (Devin on #130 — `-size` with no number)
            b"find . -exec sh scripts/x.sh \\; -size",
            b"find . -exec sh scripts/x.sh \\; -name",
            b"find . -exec sh scripts/x.sh \\; -newer",
            # a bare operator at argv end is a shell parse error
            b"find . -exec sh scripts/x.sh \\; >",
            # an invalid glued numeric/page value aborts before the
            # stage forwards (Devin on #130)
            b"cat scripts/x.sh | pr -obad | sh",
            b"cat scripts/x.sh | pr --indent=bad | sh",
            b"cat scripts/x.sh | pr -o bad | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round56(tmp_path):
    """Round-56 review findings (Codex on #130/#1393/#1959, Devin on
    #130/#1393/#1959/#12 — all verified live):

    - `find -- . -exec …` — GNU's `--` ends the OPTION region only;
      the expression still parses, so the action runs. Mid-expression
      `find . --` is an unknown predicate and aborts.
    - A QUOTED redirect word is a literal operand, not a shell
      redirect — `find . -exec sh x \\; '>f'` aborts "paths must
      precede expression".
    - A heredoc's body begins on the line AFTER the `<<` word —
      same-line words are still argv (`find . <<EOF -exec sh x \\;`
      runs the action; `-help` on that line still prints usage).
    - `split --hex-suffixes=A` aborts "invalid start value" — GNU
      accepts lowercase hex digits only.
    - `split -b 1bad` aborts "invalid number of bytes" — a SIZE is
      digits plus one optional unit; `1K`/`1KB`/`1k`/`1b` are legal.
    - `sort -o a -o b` aborts "multiple output files specified".
    - `bash "missing | filename" scripts/x.sh` names the quoted word
      as the script file — x.sh is $0, never executed.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")

    for line in (
            # `--` ends only the option region — the action runs
            b"find -- . -exec sh scripts/x.sh \\;",
            b"find -- -exec sh scripts/x.sh \\;",
            # heredoc argv still parses on the redirect's own line
            b"find . <<EOF -exec sh scripts/x.sh \\;",
            b"find . <<EOF -exec sh scripts/x.sh \\;\nbody\nEOF",
            b"find . -exec sh scripts/x.sh \\; <<EOF\nbody\nEOF",
            # lowercase hex start is legal
            b"cat scripts/x.sh | split --hex-suffixes=a "
            b"--filter=sh - | sh",
            # legal GNU sizes
            b"split -b 1K --filter=sh scripts/x.sh",
            b"split -b 1KB --filter=sh scripts/x.sh",
            b"split -b 1k --filter=sh scripts/x.sh",
            b"split -b 1b --filter=sh scripts/x.sh",
            # a single -o to an fd-1 alias keeps the stream
            b"cat scripts/x.sh | sort -o /dev/stdout | sh",
            # a scripts/ word in interpreter argv is still a bundled
            # dependency — declared or not it must block
            # (test_script_dep_block_second_arg semantics: the word
            # is a reference, not only an exec position — Codex's
            # operand-position claim is out of scope for the dep
            # gate)
            b"bash scripts/x.sh arg0",
            b'bash "missing | filename" scripts/x.sh',
            b"sh scripts scripts/x.sh",
            b"bash -c 'true' scripts/x.sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # `--` mid-expression is an unknown predicate — aborts
            b"find . -- -exec sh scripts/x.sh \\;",
            # a QUOTED redirect is a positional after the expression
            b"find . -exec sh scripts/x.sh \\; '>f'",
            b"find . -exec sh scripts/x.sh \\; '2>f'",
            # `-help` on the redirect's own line still exits first
            b"find . <<EOF -help",
            # uppercase hex start value aborts split
            b"cat scripts/x.sh | split --hex-suffixes=A "
            b"--filter=sh - | sh",
            # a malformed SIZE aborts before the filter runs
            b"split -b 1bad --filter=sh scripts/x.sh",
            b"split -b 0 --filter=sh scripts/x.sh",
            # a second output spec aborts sort
            b"cat scripts/x.sh | sort -o /dev/stdout -o /tmp/o | sh",
            b"cat scripts/x.sh | sort -o /tmp/a -o /tmp/b | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round57(tmp_path):
    """Round-57 review findings (Devin/Codex on #130/#1393/#1959/#12 —
    all verified live):

    - A GNU SIZE's unit is OPTIONAL — `split -b 1` is a legal plain
      count; `1KiB` is the binary unit (round-56 over-rejected both).
    - GNU sort compares OUTFILE PATHS — `-o X -o X` is legal and
      streams; only a different second path aborts "multiple output
      files specified" (round-56 over-blocked).
    - A shell redirect never reaches find's argv — `-H 2>/dev/null
      -L` still binds both global flags (round-56 wrongly ended the
      option region on the redirect word).
    - `-D`/`-files0-from` mid-expression are unknown predicates and
      abort (`find . -D help` → "unknown predicate `-D'").
    - GNU non-negative numeric options cap at uintmax —
      `tail --max-unchanged-stats=<2**64>` aborts "Value too large
      for defined data type"; 2**64-1 runs.
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")

    for line in (
            # plain nonzero counts and binary units are legal
            b"split -b 1 --filter=sh scripts/x.sh",
            b"split --bytes=1 --filter=sh scripts/x.sh",
            b"split -b 1KiB --filter=sh scripts/x.sh",
            b"split --line-bytes=1 --filter=sh scripts/x.sh",
            b"split -C1 --filter=sh scripts/x.sh",
            # identical output specs keep the stdout stream
            b"cat scripts/x.sh | sort -o /dev/stdout "
            b"-o /dev/stdout | sh",
            # a redirect between global flags still binds them
            b"find -H 2>/dev/null -L . -exec sh scripts/x.sh \\;",
            b"find -H >/dev/null -P . -exec sh scripts/x.sh \\;",
            # uintmax boundary value runs
            b"cat scripts/x.sh | tail "
            b"--max-unchanged-stats=18446744073709551615 | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # malformed SIZE still aborts before the filter runs
            b"split -b 1bad --filter=sh scripts/x.sh",
            b"split -b 1Ki --filter=sh scripts/x.sh",
            b"split -b 0 --filter=sh scripts/x.sh",
            # a second DIFFERENT output spec aborts
            b"cat scripts/x.sh | sort -o /dev/stdout -o /tmp/o | sh",
            b"cat scripts/x.sh | sort -o /tmp/a -o /tmp/b | sh",
            # overflow past uintmax aborts before any read
            b"cat scripts/x.sh | tail "
            b"--max-unchanged-stats=18446744073709551616 | sh",
            b"cat scripts/x.sh | tail "
            b"--max-unchanged-stats=9999999999999999999999 | sh",
            # mid-expression -D/-files0-from are unknown predicates
            b"find . -D help -exec sh scripts/x.sh \\;",
            b"find . -files0-from f -exec sh scripts/x.sh \\;"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round58(tmp_path):
    """Round-58 review findings (Devin/Codex on #130/#1393/#1959/#12 —
    all verified live on coreutils 8.32):

    - A split SIZE may be zero-PADDED (`-b 01` runs; `00` aborts
      "Numerical result out of range") and the computed bytes cap at
      INTMAX — `...808`, `1ZiB`, and the coreutils>=9.5 `1R`/`1Q`
      units all abort "Value too large" before the filter runs.
    - `-l` lines count to UINTMAX; `-a` suffix length and `-n` chunk
      components cap at INTMAX.
    - `_num_ok` must not crash on >Python-4300-digit operands (a
      digit-length guard precedes int()).
    - `pr`'s operand-taking shorts are only `-D`/`-h`/`-l`/`-N`/`-o`/
      `-w`/`-W` — `-r` (`--no-file-warnings`), `-d`/`-J`/`-T` are
      flags and `-e`/`-i`/`-n`/`-s`/`-S` bind only glued; `nl -p` is
      a flag too (`nl -p f` reads f as the file).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts/x.sh").write_text("echo X\n")

    for line in (
            # zero-padded sizes and boundary values run the filter
            b"split -b 01 --filter=sh scripts/x.sh",
            b"split -b 01K --filter=sh scripts/x.sh",
            b"split -b 9223372036854775807 --filter=sh scripts/x.sh",
            b"split -l 18446744073709551615 --filter=sh scripts/x.sh",
            b"split -a 9223372036854775807 --filter=sh scripts/x.sh",
            # l/N writes EVERY chunk, so the filter still runs
            b"split -n l/9223372036854775807 --filter=sh "
            b"scripts/x.sh",
            # flag-only pr shorts never consume the next word
            b"cat scripts/x.sh | pr -r | sh",
            b"cat scripts/x.sh | pr -d | sh",
            b"cat scripts/x.sh | pr -J | sh",
            # optional-arg shorts bind only glued — `,` is a file
            b"cat scripts/x.sh | pr -s | sh",
            # nl -p is --no-renumber, a flag — but nl still numbers
            # body lines by default (round-59): moved to the False
            # block below.
            # zero-padded suffix starts fit the suffix length
            b"split --numeric-suffixes=01 -l1 --filter=sh "
            b"scripts/x.sh",
            # -a 0 selects auto-length (runs); padded counts parse
            b"split -a 0 --filter=sh scripts/x.sh",
            b"split -a 0001 --filter=sh scripts/x.sh",
            b"split -a " + b"0" * 5000 + b"1 --filter=sh scripts/x.sh",
            b"cat scripts/x.sh | head -n 00001 | sh",
            b"cat scripts/x.sh | pr -N 00001 | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

    for line in (
            # `pr -n`/`nl` prefix line numbers — `sh` runs `1`, not
            # the script (round-59 — verified live; supersedes the
            # former flag-only readings above)
            b"cat scripts/x.sh | pr -n | sh",
            b"cat scripts/x.sh | nl -p | sh",
            # zero values and intmax overflow abort before the filter
            b"split -b 00 --filter=sh scripts/x.sh",
            b"split -b 0K --filter=sh scripts/x.sh",
            b"split -b 9223372036854775808 --filter=sh scripts/x.sh",
            b"split -b 1ZiB --filter=sh scripts/x.sh",
            b"split -b 1R --filter=sh scripts/x.sh",
            b"split -b 1Q --filter=sh scripts/x.sh",
            b"split -l 18446744073709551616 --filter=sh scripts/x.sh",
            b"split -a 9223372036854775808 --filter=sh scripts/x.sh",
            b"split -n 9223372036854775808/1 --filter=sh scripts/x.sh",
            b"split -n 0 --filter=sh scripts/x.sh",
            # a K/N chunk-select refuses --filter outright
            b"split -n 1/9223372036854775807 --filter=sh "
            b"scripts/x.sh",
            # past Python's int() limit — must not crash, and aborts
            b"cat scripts/x.sh | tail --max-unchanged-stats="
            + b"9" * 5000 + b" | sh",
            b"split -b " + b"9" * 5000 + b" --filter=sh scripts/x.sh",
            # pr required-arg shorts at argv end abort
            b"cat scripts/x.sh | pr -D | sh",
            b"cat scripts/x.sh | pr -N | sh",
            # pr numeric options reject non-numeric operands
            b"cat scripts/x.sh | pr -N nope | sh",
            b"cat scripts/x.sh | pr -N2x | sh",
            b"cat scripts/x.sh | pr -l nope | sh",
            b"cat scripts/x.sh | pr -w nope | sh",
            b"cat scripts/x.sh | pr --columns=nope | sh",
            b"cat scripts/x.sh | pr --first-line-number=nope | sh",
            # a suffix start one digit past the suffix length aborts
            b"split --numeric-suffixes=100 -a2 --filter=sh "
            b"scripts/x.sh",
            b"split --numeric-suffixes=" + b"9" * 5000
            + b" --filter=sh scripts/x.sh",
            b"split --hex-suffixes=" + b"f" * 5000
            + b" --filter=sh scripts/x.sh",
            # pr --pages components past uintmax abort "too large"
            b"cat scripts/x.sh | pr --pages=18446744073709551616 | sh",
            b"cat scripts/x.sh | pr --pages=1:18446744073709551616 "
            b"| sh",
            b"cat scripts/x.sh | pr --pages=" + b"9" * 5000 + b" | sh",
            # chrt -m/--max prints the priority table — never execs
            b"cat scripts/x.sh | chrt -m 1 sh scripts/x.sh",
            b"cat scripts/x.sh | chrt --max sh scripts/x.sh",
            # past uintmax even padded — 'invalid number' aborts
            b"cat scripts/x.sh | head -n " + b"9" * 5000 + b" | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
