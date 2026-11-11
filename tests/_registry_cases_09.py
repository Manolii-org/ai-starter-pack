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


def test_script_dep_round82_reader_fd1_divert(tmp_path):
    """A pure reader's own `>`/`>&-` inside a pipeline group drops its
    bytes from the merged stream — `(cat x >/dev/null | cat >&2) |&`
    runs nothing (Devin on #1431, round-82 — verified live). A program
    operand's dep is unaffected by its head's fd1: `split --filter=sh
    x > /tmp/out` still runs x's chunks through sh."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >/dev/null | cat >&2) |& sh',
            b'(cat scripts/x.sh >&- | cat >&2) |& sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b'split --filter=sh scripts/x.sh > /tmp/out',
            b'(cat scripts/x.sh >&2 | cat >/dev/null) |& sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round82_brace_group_head(tmp_path):
    """A bare `{` word splits off before the head (unlike `(`, which
    glues on) — `{ echo bash x; } | sh` resolves the emitted stream
    like its `( )` twin (verified live — executes `bash x`)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "y.sh").write_bytes(b"x")
    for line in (
            b'{ echo bash scripts/x.sh; } | sh',
            b'{ echo bash scripts/x.sh; cat scripts/y.sh >f; } | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round83_fd2_pipe_binding(tmp_path):
    """A group's fd2 reaches the pipe not just under `|&` but under a
    plain `|` when a post-closer `2>&1` bound fd2 onto fd1's pipe
    target (`(cat x >&2; true) 2>&1 | sh` runs x — Devin on
    #133/#1431, round-83 — verified live). The `&` merge is a DUP of
    fd1's final binding, applied after every post-closer redirect, so
    `2>&1 >/dev/null` keeps fd2 on the pipe while `>/dev/null 2>&1`
    dead-ends both."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >&2; true) 2>&1 | sh',
            b'(cat scripts/x.sh >&2; true) 2>&1 >/dev/null | sh',
            b'(cat scripts/x.sh | cat >&2) 2>&1 | sh',
            # A `$(` substitution inside a post-closer redirect target
            # must not shadow the real group closer (CodeRabbit on
            # #133, round-83).
            b'(cat scripts/x.sh >&2) 2>$(mktemp) |& sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # fd1→file first: `2>&1` then binds fd2 to the file too.
            b'(cat scripts/x.sh >&2; true) >/dev/null 2>&1 | sh',
            # Plain `|` never merges fd2.
            b'(cat scripts/x.sh >&2; true) | sh',
            b'(cat scripts/x.sh | cat >&2) | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round83_inner_pipeline_stages(tmp_path):
    """A `;`-sibling that is itself a pipeline lands on the group's fds
    through its LAST stage only: an earlier `>` diverts just that stage
    (`true >/dev/null | cat x` still feeds x — CodeRabbit on #133,
    round-83), and a sink last stage contributes nothing whatever an
    earlier stage read (`cat x | wc -l`/`head -n 0` — Devin on #1431,
    round-83). A mid-stage `>&2` lands on the group's stderr, which
    reaches the pipe only when the group's fd2 does — all verified
    live."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(true >/dev/null | cat scripts/x.sh) | sh',
            b'(cat scripts/x.sh >&2 | cat >/dev/null) |& sh',
            b'(cat scripts/x.sh | cat >&2) |& sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b'(cat scripts/x.sh | wc -l) |& sh',
            b'(cat scripts/x.sh | head -n 0) |& sh',
            b'(cat scripts/x.sh >/dev/null | cat >&2) |& sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round83_emit_head_fd2(tmp_path):
    """An emit-head's `>&2` inside a group whose fd2 reaches the pipe
    still hands the emitted text to the downstream interpreter —
    `(echo bash x >&2) |& sh` and the inner-pipeline `(echo bash x >&2
    | cat >/dev/null) |& sh` both run `bash x` (Devin on #17/#1963/
    #1431, round-83 — verified live). The word's OWN stage fd decides,
    not the sibling's last."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(echo bash scripts/x.sh >&2) |& sh',
            b'(echo bash scripts/x.sh >&2 | cat >/dev/null) |& sh',
            b'(printf "bash %s\\n" scripts/x.sh >&2 | cat >/dev/null) '
            b'|& sh',
            b'(echo bash scripts/x.sh >&2) 2>&1 | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # Real stderr under a plain `|` — emitted text dies there.
    assert not mod.script_dep_block(
        pdir, b'(echo bash scripts/x.sh >&2) | sh\n')

def test_script_dep_round84_inner_fwd_fd2(tmp_path):
    """An fd1→fd2 forwarder as an inner-pipeline LAST stage hands the
    stream to the group's stderr — under `|&` or post-closer `2>&1`
    the pipe still receives it: `(echo bash x | cat >&2) |& sh` runs
    the emitted text, `(cat x | cat >&2) |& sh` runs the script's
    bytes (Devin on #1963, round-84 — verified live: FORWARDED).
    A numbering/sink forwarder (`cat -n >&2`, `nl >&2`, `wc -l >&2`)
    emits prefixed/replaced text the downstream can't run — still no
    dep (verified live: `cat -n` numbered stream → `sh: 1: not
    found`). And a plain `|` leaves the forwarder's fd2 on real
    stderr — emitted text dies there."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(echo bash scripts/x.sh | cat >&2) |& sh',
            b'(echo bash scripts/x.sh | cat >&2) 2>&1 | sh',
            b'(cat scripts/x.sh | cat >&2) |& sh',
            b'(cat scripts/x.sh | cat >&2) 2>&1 | sh',
            b'(cat scripts/x.sh | sh >&2) |& cat'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            b'(echo bash scripts/x.sh | cat >&2) | sh',
            b'(cat scripts/x.sh | cat -n >&2) |& sh',
            b'(cat scripts/x.sh | nl >&2) |& sh',
            b'(cat scripts/x.sh | pr -n >&2) |& sh',
            b'(cat scripts/x.sh | wc -l >&2) |& sh',
            b'(cat scripts/x.sh | head -n 0 >&2) |& sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round84_spaced_redir_target(tmp_path):
    """`2> /dev/null` and `2>&1 > /dev/null` — a blank between the
    operator and its target binds the same file as the glued form
    (Devin on #17/#1963, round-84 — verified live: `(cat x >&2; true)
    2> /dev/null |& sh` still merges — `|&` re-dups fd2 after the
    spaced divert)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b'(cat scripts/x.sh >&2; true) 2> /dev/null |& sh',
            b'(cat scripts/x.sh >&2) 2>&1 > /dev/null | sh',
            b'(cat scripts/x.sh >&2) 2> /dev/null 2>&1 | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round85_fd2_fwd_edges(tmp_path):
    """Round-85: None fd-map guard, exec-on-own-chain, numbered
    emitted streams, missing redirect target — all verified live
    (CodeRabbit/Devin on #133/#17/#1963)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # Headless/describe-only stages yield a None fd map —
            # `(cat x | command -v sh) |&` raised AttributeError
            # (CodeRabbit on #133, round-85).
            b'(cat scripts/x.sh | command -v sh) |& sh',
            b'cat scripts/x.sh | { wc -l; } |& sh',
            b'(cat scripts/x.sh | X=1) |& sh',
            # An exec stage on an `own` chain runs the REPLACEMENT
            # text, not the script — `wc -l | sh >&2` runs the count
            # (verified live: `sh:: not found`).
            b'(cat scripts/x.sh | wc -l | sh >&2) |& sh',
            # Numbered emitted streams die on `N\t` regardless of
            # provenance (verified live: `1: not found`).
            b'(echo bash scripts/x.sh | cat -n >&2) |& sh',
            b'(echo bash scripts/x.sh | cat >&2) |& cat -n | sh',
            b'(echo bash scripts/x.sh | cat >&2) |& nl | sh',
            # `2> |&` is a bash syntax error — no viable pipe.
            b'(echo bash scripts/x.sh >&2) 2> |& sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # fd1→fd2 forwarders and exec-on-fd2 still merge under `|&`.
            b'(echo bash scripts/x.sh | cat >&2) |& sh',
            b'(cat scripts/x.sh | cat >&2) |& sh',
            b'(cat scripts/x.sh | sh >&2) |& cat',
            # `nl -b n` space-pads but keeps the command word —
            # verified live: RAN_X.
            b'(cat scripts/x.sh | nl -b n >&2) |& sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round86_numbered_emit_and_fds(tmp_path):
    """Round-86: provable emit bytes through `cat -n`/`nl`/`pr -n`,
    glued `pr -h` operands, inert bare-assign words, `<&-`/`<&N`
    closer ops, `/dev/stdout`/`/dev/fd/N` fd aliases — all verified
    live (Devin on #133/#1431/#1963/#17)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "sep.sh").write_bytes(
        b"echo a; sh scripts/x.sh\n")
    for line in (
            # `$(…)` on an assign RHS runs its body on the stream
            # before the stage is done — `cat x | X="$(sh)"` executes
            # the capture (Devin on #17/#1431 — verified live).
            b'cat scripts/x.sh | X="$(sh)"',
            b'cat scripts/x.sh | X="$(sh scripts/x.sh)"',
            # A `;`/`&`/`|` tail survives the `N\t` prefix — the
            # emitted line's separators still run (verified live:
            # `1\ta;sh x` executes the post-`;` command).
            b'echo "a; sh scripts/x.sh" | cat -n | sh',
            b'echo "a; sh scripts/x.sh" | nl -ba | sh',
            b'cat scripts/sep.sh | cat -n | sh',
            b'(echo "a; sh scripts/x.sh" | cat >&2) |& cat -n | sh',
            # `pr -hname` is `-h` + glued operand — only `-n`
            # numbers; `-h` text passes through unnumbered
            # (verified live: RAN_X).
            b'echo "a; sh scripts/x.sh" | pr -hname | sh',
            b'cat scripts/x.sh | pr -hname | sh',
            # `/dev/stdout`/`/dev/fd/1` are fd-1 aliases — `2>`
            # there dups fd2 to fd1 (verified live: RAN_X).
            b'(cat scripts/x.sh >&2) 2>/dev/stdout | sh',
            b'(cat scripts/x.sh >&2) 2>/dev/fd/1 | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # Single-quoted/capture assigns are inert — `$(cat)`
            # drains the stream into the capture, `sh` sees EOF.
            b"cat scripts/x.sh | X='$(sh)' | sh",
            b'cat scripts/x.sh | X="$(cat)" | sh',
            b'cat scripts/x.sh | X=1 | sh',
            # Numbered sep-free streams die on the `N\t` prefix —
            # verified live: `1: not found`.
            b'echo first | cat -n | sh',
            b'cat scripts/x.sh | cat -n | sh',
            b'(echo bash scripts/x.sh | cat >&2) |& cat -n | sh',
            b'(echo bash scripts/x.sh | cat >&2) |& nl | sh',
            b'cat scripts/x.sh | pr -n | sh',
            # `1<&-` closes fd1 — `cat: Bad file descriptor`, the
            # pipe stays empty (Devin on #133 — verified live).
            b'(cat scripts/x.sh) 1<&- | sh',
            # `1<&2` dups fd1 onto real stderr — `cat` writes off
            # the pipe (verified live).
            b'(cat scripts/x.sh) 1<&2 |& sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round87_emit_routing_and_transforms(tmp_path):
    """Round-87: emit-byte routing through `|&` stage pipes, post-closer
    fd replay on compound feeds, `eval`/backtick/exec-ish assign bodies,
    transforming forwarders (tr/sed/cat -n), spaced `>& N` dups, and
    provable-emit own→up promotion guards — all verified live (Devin on
    #133/#1431/#1963, CodeRabbit on #133)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # A stage `>&2`'d inside an inner `|&` chain routes its
            # fd2 bytes onto the GROUP's fd2 — reaching the outer pipe
            # when it merges (verified live: RAN_X under `|&` outer).
            b'(echo "sh scripts/x.sh" >&2 |& cat -n) |& sh',
            b'(echo "a; sh scripts/x.sh" >&2 | true) |& cat -n | sh',
            # Post-closer `2>&1` binds group fd2 to the pipe BEFORE
            # `>/dev/null` rebinds fd1 — fd2's bytes still flow
            # (Devin on #1431 — verified live: RAN_X).
            b'(echo "sh scripts/x.sh" >&2) 2>&1 >/dev/null | sh',
            b'(echo "x; sh scripts/x.sh" >&2) 2>&1 | cat -n | sh',
            # `eval`/`builtin`-style heads emit their evaluated output —
            # `eval 'echo "a; sh x"'` prints the text (CodeRabbit on
            # #133 — verified live).
            b'eval \'echo "a; sh scripts/x.sh"\' | cat -n | sh',
            # Backtick bodies inherit the stream like `$(` — `X=`sh``
            # consumes it inside the capture (CodeRabbit on #133 —
            # verified live: silent exit).
            b'cat scripts/x.sh | X=`sh`',
            # A `tr`/`sed` stage may map ANY character into a separator
            # — the bytes can't be proven sep-free (CodeRabbit/Devin —
            # verified live: RAN_X).
            b'echo "q sh scripts/x.sh" | tr q ";" | cat -n | sh',
            # `>& 1` with a space still dups fd2 onto fd1's pipe
            # binding (Devin on #133 — verified live).
            b'(echo "a; sh scripts/x.sh" >&2) 2>& 1 | cat -n | sh',
            # A sibling's `>&2` redirect resolves BEFORE `|&`'s dup —
            # the later `2>/dev/null` only rebinds fd2; fd1 already
            # reached the pipe (verified live: RAN_X).
            b'(echo "sh scripts/x.sh" >&2 2>/dev/null; true) |& sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # A `(cat -n; true)` compound KILLS the numbered stream —
            # `1\t...` first commands all fail (Devin on #1431 —
            # verified live: dead).
            b'echo "sh scripts/x.sh" | (cat -n; true) | sh',
            # A diverted chain stage drops the emit — stale `chain_emit`
            # can't justify a later sibling (CodeRabbit on #133 —
            # verified live: dead).
            b'(echo "a;b" | cat >/dev/null | cat -n) | sh',
            # `cat -n >/dev/null` emits nothing — an unprovable feed
            # can't promote 'own' to 'up' (CodeRabbit on #133 —
            # verified live: dead).
            b'cat scripts/x.sh | cat -n >/dev/null | sh',
            # `X=$(true)` reads nothing and emits nothing — the
            # assign stage ends the stream (CodeRabbit on #133 —
            # verified live: dead).
            b'cat scripts/x.sh | X=$(true) | sh',
            # `(...) >/dev/null` diverts the group's fd1 — nothing
            # reaches the pipe (Devin on #1963 — verified live).
            b'(echo "a; bash scripts/x.sh") >/dev/null | cat -n | sh',
            # `tr ';' ':'` strips the only separator — unprovable
            # bytes flow, but a modeled pass-through would keep the
            # dead `;` (Devin on #133 — verified live: dead).
            b'echo "a; sh scripts/x.sh" | tr ";" ":" | cat -n | sh',
            # Inner `|&` under a plain `|` group: the `>&2` stage's
            # fd2 dups onto group fd2 — real stderr, not the pipe
            # (Devin on #133 — verified live: dead).
            b'(echo "a; sh scripts/x.sh" >&2 |& cat -n) | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round90_sib_stdin_backtick_nl_pbre_spaced_alias(tmp_path):
    """Round-90 review — Devin on #133/#1431/#17 (all verified live):
    `;`/newline/`&&` siblings inside a compound re-read the SAME shared
    stdin — a chain whose FIRST command drains it (`cat`, `wc`) leaves
    EOF for the next sibling (`(cat >/dev/null; cat)` provably emits
    nothing — dead), while a bare `&` races unprovably. Only the chain
    head reads the shared stdin — later `|` stages read the upstream
    fd1 (`( cat x | cat; sh )` — `cat x` drains nothing). Backtick
    bodies are one word's substitution text, so `X=`cat | sh`` is a
    single `NAME=` assign stage whose captured pipeline still executes
    the stream (silent but real). `nl -bpBRE` numbers only matching
    lines — unprovable, so the stream flows. And `2> /dev/stdout` /
    `&> /dev/stdout` with a SPACED target still dups onto fd1's pipe
    binding (verified live: RAN_X)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"echo X\n")
    for line in (
            # A non-draining first sibling leaves the shared stdin for
            # the next — `cat` still forwards it (verified live: RAN_X).
            b'echo "sh scripts/x.sh" | (cat; true) | sh',
            b'cat scripts/x.sh | ( cat; true ) | sh',
            b'echo "sh scripts/x.sh" | (true; cat) | sh',
            # A bare `&` sibling races the reader — unprovable, so the
            # stream conservatively flows (verified live).
            b'echo "sh scripts/x.sh" | (cat >/dev/null & cat) | sh',
            # The chain HEAD decides draining — `cat x` reads a FILE,
            # so the `;` sibling still sees the stream (round-74
            # heredoc variant, verified live: RAN_X).
            b'sh <<E\n( cat scripts/x.sh | cat; sh )\nE',
            # Backtick-captured pipeline still runs the stream —
            # `cat | sh` inside `X=`…`` executes silently (Devin on
            # #133 — verified live: EXECUTED).
            b'cat scripts/x.sh | X=`cat | sh`',
            # `nl -bpBRE` numbers only matching lines — the mode is
            # unprovable, so the stream flows (Devin on #1431 —
            # verified live: RAN_X).
            b'echo "bash scripts/x.sh" | nl -bp"^safe" | sh',
            # Spaced alias targets still dup onto fd1's binding —
            # `2> /dev/stdout` keeps the emitted text on the pipe
            # (Devin on #17 — verified live: RAN_X).
            b'(echo "sh scripts/x.sh" >&2) 2> /dev/stdout | sh',
            b'(echo "sh scripts/x.sh" >&2) &> /dev/stdout | sh',
            # A `cat >&2` last sibling under a post-closer `2>&1`
            # merge still hands the emitted text to the pipe
            # (round-84 hold, verified live).
            b'(echo bash scripts/x.sh | cat >&2) 2>&1 | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # A `;`-drained sibling leaves provable EOF for the next —
            # the group emits nothing downstream (Devin on #133 —
            # verified live: dead).
            b'echo "sh scripts/x.sh" | (cat >/dev/null; cat) | sh',
            # `&&` drains sequentially like `;` — same EOF.
            b'echo "sh scripts/x.sh" | (cat >/dev/null && cat) | sh',
            # Backtick inside single quotes is literal text — no
            # capture, no exec (verified live: dead).
            b"cat scripts/x.sh | X='`cat | sh`' | sh",
            # `nl -ba`/`-bt` number every (non-empty) line — the
            # prefixed text can't run (verified live: `1` not found).
            b'echo "bash scripts/x.sh" | nl -ba | sh',
            b'echo "bash scripts/x.sh" | nl -bt | sh',
            # A `)` group close whose fd1 is diverted feeds nothing.
            b'cat scripts/x.sh | (cat; true) > /dev/null | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round91_sibling_emit_model(tmp_path):
    """Round-91: emit model tracks per-sibling stream bytes — a `cat|true`
    drain leaves provable EOF, `&&`/`||` are sibling boundaries, numbered
    script content dies while raw script lines still run, `nl -s`/`pr -n`
    separators revive a `N;cmd` tail, and an inner `sh` inside backticks
    inherits the shared stdin (Devin on #133/#17/#1431/#1963 — all
    verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # `&&`/`||` siblings still re-read shared stdin — the
            # conditional branch runs the reader (verified live:
            # EXECUTED).
            b"cat scripts/x.sh | (true && cat) | sh",
            b"cat scripts/x.sh | (false || cat) | sh",
            b"cat scripts/x.sh | (true && cat scripts/x.sh) | sh",
            # `nl -s ';'`/`pr -n';'` turn the numbering prefix into a
            # separator — `N;cmd` executes the tail (verified live).
            b"cat scripts/x.sh | nl -s ';' | sh",
            b"cat scripts/x.sh | nl -s';' | sh",
            b"cat scripts/x.sh | nl --number-separator=';' | sh",
            b"cat scripts/x.sh | pr -t -n';' | sh",
            # An inner `sh` inside a backtick assignment inherits the
            # shared stdin (fd2-observable — verified live).
            b"cat scripts/x.sh | X=`true; sh` | wc -l",
            b"cat scripts/x.sh | X=`cat | sh` | wc -l",
            # Sequential siblings each re-read the stream — `cat`
            # emits the script bytes raw (verified live).
            b"cat scripts/x.sh | (cat; cat) | sh",
            b"cat scripts/x.sh | (cat | cat; cat) | sh",
            b"cat scripts/x.sh | (true; cat) | sh",
            b"cat scripts/x.sh | (cat; cat -n) | sh",
            # A sibling that emits a `scripts/` invocation revives
            # even beside numbered content.
            b"cat scripts/x.sh | (cat -n; echo 'sh scripts/x.sh') | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `cat|true`: cat drains ≤ one read (<64KB) then SIGPIPEs
            # on `true` — the second cat sees provable EOF and the
            # group emits nothing (verified live: dead).
            b"cat scripts/x.sh | (cat | true; cat) | sh",
            # `cat -n` numbers the script's content; `echo OK` is
            # sibling-generated text — `OK` never runs the script
            # (verified live: dead).
            b"cat scripts/x.sh | (cat -n; echo OK) | sh",
            b"cat scripts/x.sh | (cat -n; cat) | sh",
            # A `;`-drained sibling leaves EOF — nothing emitted.
            b"cat scripts/x.sh | (cat >/dev/null; cat) | sh",
            # Numbered file content dies on `N\t` alone.
            b"cat scripts/x.sh | nl | sh",
            b"cat scripts/x.sh | nl -s ':' | sh",
            # `cmd >&2 |&` binds fd1 to real stderr first — `|&`'s
            # fd2→fd1 dup lands on stderr, not the pipe.
            b"cat scripts/x.sh >&2 |& sh",
            # A bare `(`/digit scalar line is a syntax error / not a
            # command — provably dead emitted text.
            b'(cat scripts/x.sh >&2; echo "(") | sh',
            b'(cat scripts/x.sh >&2; echo a\\(b) | sh',
            b"cat scripts/x.sh | wc -l | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round92_date_format_conditional_emit(tmp_path):
    """Round-92: `date '+FORMAT'` emits its literal format text (a
    `+sh x` format runs the script — under-block fix), and `&&`/`||`
    branches behind a provable exit skip — no emission, no stdin
    drain (`true || cat; cat` still reads the full stream — Devin on
    #17/#1963 — verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # `+FORMAT` prints the literal format — `sh x` runs
            # (verified live: EXECUTED).
            b"date '+sh scripts/x.sh' | sh",
            b"date -u '+sh scripts/x.sh' | sh",
            # Provably-live conditional branches still emit.
            b"(true && echo 'sh scripts/x.sh') | sh",
            b"(false || echo 'sh scripts/x.sh') | sh",
            # A skipped `||`/`&&` cat doesn't drain — the next
            # sibling still reads the whole stream (verified live).
            b"cat scripts/x.sh | (true || cat; cat) | sh",
            b"cat scripts/x.sh | (false && cat; cat) | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # `true || sib` / `false && sib` provably skip — nothing
            # emitted (verified live: dead).
            b"(true || echo 'sh scripts/x.sh') | sh",
            b"(false && echo 'sh scripts/x.sh') | sh",
            b"cat scripts/x.sh | (true || cat; true) | sh",
            b"cat scripts/x.sh | (false && cat; true) | sh",
            # Skipped-branch stdin stays for later siblings, but a
            # DRAINED stdin stays drained (`cat >/dev/null` exits 0,
            # `||` skips, third cat sees EOF).
            b"cat scripts/x.sh | (true; cat >/dev/null || cat; cat) | sh",
            # Numbered skip-path content still dies.
            b"cat scripts/x.sh | (true || cat; cat -n) | sh",
            # `date` format with `%` directives stays unprovable but
            # a bare-digit format is dead; `-d`'s value is consumed.
            b"date '+123' | sh",
            b"date -d '+sh scripts/x.sh' | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round92_emitted_ref(tmp_path):
    """Round-92 (cont.): sibling outputs glue in the real pipe —
    `(echo -n 'sh '; echo scripts/x.sh) | sh` writes `sh ` then
    `scripts/x.sh` into ONE stream and the script runs, though no
    single source word holds keyword+path (Devin on #1431 —
    verified live). The emitted-feed ref scan gates those
    assembled invocations bundled-or-declared exactly like source
    refs."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            b"(echo -n 'sh '; echo scripts/x.sh) | sh",
            b"(echo -n 'sh '; echo scripts/nonexistent.sh) | sh",
            b"(echo -n 'cat '; echo scripts/x.sh) | sh",
            b"cat scripts/x.sh | (echo -n 'sh '; cat) | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # A bare `scripts/` command word stays outside the ref
            # model — same convention as source text (needs an exec
            # bit the resolver can't prove).
            b"echo scripts/x.sh | sh",
            b"(echo scripts/x.sh; cat) | sh",
            # printf emit is unprovable — the pass can't build a ref
            # view and skips (over-block stays with the prov path).
            b"(printf 'sh '; echo scripts/x.sh) | sh",
            b"echo 'x' | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round93_raw_emit_and_redirects(tmp_path):
    """Round-93: the emitted-ref scan runs on the byte-faithful
    stream (no synthetic newlines) — an invocation assembles across
    write boundaries either direction: `sh `+`cripts/x.sh` matches
    (real glue `sh scripts/x.sh`), `sh`+`cripts/x.sh` does not
    (glues `shscripts/x.sh` — Devin on #133/#1431/#17/#1963 —
    verified live). A file-target redirect on the `&&`/`||`
    sibling makes its exit unprovable — `true </missing` exits
    nonzero so the `||` branch runs (verified live)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (
            # Split-path assemble — pieces glue in the real stream.
            b"(echo -n 'sh s'; echo cripts/x.sh) | sh",
            b"(echo -n 'sh '; echo -n scripts/; echo x.sh) | sh",
            # Redirect failure — `||` branch provably runs.
            b"(true </missing || echo -n 'sh '; echo scripts/x.sh) | sh",
            # Unprovable file redirects keep the sibling (over-block).
            b"(true >/dev/null || echo -n 'sh '; echo scripts/x.sh) | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (
            # Glued `shscripts/` — no `sh ` command word exists.
            b"(echo -n sh; echo scripts/x.sh) | sh",
            # Provable skip still skips.
            b"(true || echo -n 'sh '; echo scripts/x.sh) | sh",
            b"(false && echo -n 'sh '; echo scripts/x.sh) | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_round93_date_option_classes(tmp_path):
    """Round-93 (cont.): `date`'s emit model classifies options —
    `--help`/`--version` exit early (dead text), a second output-
    format option conflicts with `+FORMAT` (`--rfc-3339`/`--iso-8601`/
    `-I`/`--rfc-email`/`-R` error "multiple output formats"), and a
    value-taking option (`-d`/`--date`/`-f`/`-r`/`--set`/`--file`/
    `--reference`) makes the emit unprovable (bad values error, good
    ones print the format — verified live). None of these add an
    emitted ref the source literal doesn't already gate."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    # Emit-model verdicts — the source side can't see these (no
    # scripts/ literal), so behaviour shows only through the emit
    # helper itself.
    for args, want in (
            ([b"--version"], b"0\n"),
            ([b"--version", b"'+sh x'"], b"0\n"),
            ([b"--help", b"'+sh x'"], b"0\n"),
            ([b"--rfc-3339=seconds", b"'+sh x'"], b""),
            ([b"-Iseconds", b"'+sh x'"], b""),
            ([b"--rfc-email", b"'+sh x'"], b""),
            ([b"-R", b"'+sh x'"], b""),
            ([b"--rfc-3339=seconds"], b"0\n"),
            ([b"--date=not-a-date", b"'+sh x'"], None),
            ([b"-d", b"tomorrow", b"'+sh x'"], None),
            ([b"-d", b"'+sh x'"], b"0\n"),
            ([b"--set", b"'+sh x'"], b"0\n"),
            ([b"+sh x"], b"sh x\n"),
            ([], b"0\n")):
        assert mod._emit_own_date(args) == want, args
    # A provably-dead date sibling still glues its neighbours'
    # writes — the emitted pieces assemble `sh scripts/x.sh`.
    assert mod.script_dep_block(
        pdir, b"(date --rfc-3339=s '+x'; echo -n 'sh s'; "
              b"echo cripts/x.sh) | sh\n")

def test_bootstrap_ratchet_coordinate_spaces():
    mod = _load_bootstrap()
    cases = [
        ("registry/leak-allowlist.txt", "platform/framework/a.md", True),
        ("registry/leak-allowlist.txt", "platform-other/a.md", False),
        ("registry/leak-allowlist.txt", "manolii/a.md", False),
        ("registry/leak-allowlist.txt", "cpdcheck/a.md", False),
        ("registry/leak-allowlist.txt", "plugins.json", False),
        ("registry/leak-allowlist.txt", "README.md", False),
        ("registry/pack-surface-allowlist.txt", "registry/platform/framework/a.md", True),
        ("registry/pack-surface-allowlist.txt", "registry/platform-other/a.md", False),
        ("registry/pack-surface-allowlist.txt", "registry/manolii/a.md", False),
        ("registry/pack-surface-allowlist.txt", "registry/plugins.json", False),
        ("registry/pack-surface-allowlist.txt", "platform/framework/a.md", False),
        ("registry/pack-surface-allowlist.txt", "README.md", True),
        ("registry/unknown-allowlist.txt", "registry/platform/framework/a.md", False),
    ]
    for allowlist, path, expected in cases:
        assert mod._ratchet_vendored(allowlist, path) is expected, (allowlist, path)
