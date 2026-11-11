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


def test_prune_cleanup_revalidates_ancestor_chain(tmp_path):
    """`.claude` swapped for a symlink after the lock commit must stop
    the empty-dir sweep — `islink` on the leaf alone misses the
    ancestor-level swap and rmdir would act outside repo_root (Codex on
    #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    external = tmp_path / "external"
    (external / "commands").mkdir(parents=True)
    m.write_text("version: 1\nuniverse: manolii\nrequires: []\n")
    claude = consumer / ".claude"
    real = consumer / ".claude-real"
    claude.rename(real)
    claude.symlink_to(external)
    run_resolver(m, reg_root, consumer, "--apply", "--prune")
    # the external commands dir must survive the cleanup sweep
    assert (external / "commands").is_dir()

def test_script_dep_stream_replacing_pipe_heads(tmp_path):
    """A pipe head whose output REPLACES the stream ends the chain:
    `cat x | wc -l | sh` feeds sh a line count, not the script (Devin on
    #123); `python -m` takes its program from argv so the pipe is data
    (Devin BUG_0003 on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for pipe in (b"cat scripts/x.sh | wc -l | sh",
                 b"cat scripts/x.sh | sha256sum | bash",
                 b"cat scripts/x.sh | echo done | sh",
                 b"cat scripts/x.py | python -m json.tool",
                 b"cat scripts/x.py | python -m json.tool | sh"):
        assert not mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # transformers forward content — still execute downstream
    for pipe in (b"cat scripts/x.sh | grep p | sh",
                 b"cat scripts/x.sh | sed s/a/b/ | sh",
                 b"cat scripts/x.sh | tr a b | bash"):
        assert mod.script_dep_block(pdir, pipe + b"\n"), pipe

def test_script_dep_env_split_string(tmp_path):
    """`env -S`/`--split-string` re-parses its operand into a command —
    `cat x | env -S 'bash -s'` executes the pipe (Codex on #1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for pipe in (b"cat scripts/x.sh | env -S 'bash -s'",
                 b"cat scripts/x.sh | env --split-string 'sh'",
                 b"cat scripts/x.sh | env -S'sh'",
                 b"cat scripts/x.sh | env --split-string='sh -s'",
                 b"cat scripts/x.sh | env A=1 -S 'bash'"):
        assert mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # an operand whose inner command is a sink still ends the chain
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | env -S 'wc -l' | sh\n")
    # env without -S runs the named command on the pipe, unchanged
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | env sh\n")

def test_script_dep_reader_heads_and_generic_suffix(tmp_path):
    """grep/head/tail only read, so a bare use stays inert — but under an
    executing context (`eval "$(grep p x)"`, `head x | sh`) the reader
    supplies code (Codex on #1370). Any dotted extension counts as a
    script — `bash scripts/setup.bash` is an invocation (Codex on #123).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "setup.bash").write_bytes(b"x")
    for line in (b'eval "$(grep p scripts/x.sh)"',
                 b'eval "$(head -5 scripts/x.sh)"',
                 b"grep p scripts/x.sh | sh",
                 b"head -5 scripts/x.sh | bash",
                 b"tail scripts/x.sh | python",
                 b"bash scripts/setup.bash",
                 b"./scripts/setup.bash"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # bare reader invocations still read — no dep
    for line in (b"grep p scripts/x.sh",
                 b"head -5 scripts/x.sh",
                 b"tail scripts/x.sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_substitution_output_piped(tmp_path):
    """`echo "$(cat scripts/x.sh)" | sh` executes the substitution's
    output — the file inside is a dep even though echo is a sink (Devin
    on cpdcheck #6)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.sh)" | sh',
                 b'echo `cat scripts/x.sh` | bash',
                 b'printf "%s" "$(cat scripts/x.sh)" | python'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # no pipe — the output is just printed
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)"\n')
    # pipe to a non-executor still inert
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" | wc -l\n')

def test_script_dep_single_quoted_backticks_literal(tmp_path):
    """Backticks inside single quotes are literal text — `'run `x`'` is
    an argument, not a substitution (Devin BUG_0002 on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b"echo 'run `sh scripts/x.sh` today'",
                 b"printf 'use `bash scripts/x.sh` here'",
                 b"echo 'esc \\`sh scripts/x.sh\\`'"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # inside double quotes a backtick still substitutes
    assert mod.script_dep_block(
        pdir, b'echo "run `sh scripts/x.sh` today"\n')
    # a bare backtick substitution still counts
    assert mod.script_dep_block(pdir, b"eval `cat scripts/x.sh`\n")

def test_script_dep_heredoc_multiple_ops_one_line(tmp_path):
    """`cat <<A; sh <<B` queues two heredocs on one line — B's body must
    start after A's delimiter, not after the opening line, or A's body
    is wrongly attributed to B's command (Devin on #1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    # an invocation in A's body must NOT be attributed to sh's heredoc —
    # cat only reads it, so nothing executes
    body = b"cat <<A; sh <<B\nbash scripts/x.sh\nA\ntext\nB\n"
    assert not mod.script_dep_block(pdir, body)
    # the same invocation in B's own body IS attributed to the exec head
    body = b"cat <<A; sh <<B\ntext\nA\nbash scripts/x.sh\nB\n"
    assert mod.script_dep_block(pdir, body)

def test_read_source_refuses_links_and_nonregular(tmp_path):
    """`_read_source` opens with O_NOFOLLOW and revalidates a regular
    file — a source swapped for a symlink after the resolve-time check
    cannot ship the link target's bytes, and a fifo cannot block the
    read (Codex P1 on #1953)."""
    import os
    mod = load_resolve_module()
    target = tmp_path / "target"
    target.write_bytes(b"real")
    got = mod._read_source(tmp_path, "target")
    assert got is not None and got[0] == b"real"
    link = tmp_path / "link"
    link.symlink_to(target)
    assert mod._read_source(tmp_path, "link") is None
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert mod._read_source(tmp_path, "fifo") is None
    assert mod._read_source(tmp_path, "missing") is None
    # An ANCESTOR swapped for a symlink is refused too — the leaf's
    # O_NOFOLLOW alone still follows a linked parent dir (Codex P1 on
    # cpdcheck #8).
    if mod._HAS_DIRFD:
        real_dir = tmp_path / "realdir"
        real_dir.mkdir()
        (real_dir / "leaf").write_bytes(b"x")
        linked = tmp_path / "linked"
        linked.symlink_to(real_dir, target_is_directory=True)
        assert mod._read_source(tmp_path, "linked/leaf") is None

def test_apply_writes_mutex_lockfile(tmp_path):
    """--apply holds an exclusive flock on .ai/capability-apply.lock for
    the plan→apply window so two resolvers can't lose each other's
    installs (Devin BUG_0001 on #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    # Non-git consumer → .ai fallback; a git repo locks under its
    # metadata dir instead so the worktree stays clean (Codex P2 on
    # #1374 / cpdcheck #8).
    assert (consumer / ".ai" / "capability-apply.lock").exists()
    import subprocess
    git_consumer = tmp_path / "git-consumer"
    git_consumer.mkdir()
    subprocess.run(["git", "-C", str(git_consumer), "init", "-q"],
                   check=True)
    m2 = write_manifest(git_consumer, "manolii",
                        [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m2, reg_root, git_consumer, "--apply").returncode == 0
    assert (git_consumer / ".git" / "capability-apply.lock").exists()
    assert not (git_consumer / ".ai" / "capability-apply.lock").exists()

def test_script_dep_stdin_executors_bc_dc(tmp_path):
    """`bc`/`dc` interpret stdin as program text — `cat x | bc` runs the
    file's contents, and bc's own output can carry code to a later `sh`
    (Codex on #125 / #1374). They are NOT stream-replacing sinks."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "calc.bc").write_bytes(b"x")
    for pipe in (b"cat scripts/calc.bc | bc",
                 b"cat scripts/calc.bc | dc",
                 b"cat scripts/calc.bc | bc | sh"):
        assert mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # true sinks still end the chain
    assert not mod.script_dep_block(
        pdir, b"cat scripts/calc.bc | expr 1 + 1 | sh\n")

def test_script_dep_suffix_punctuation(tmp_path):
    """A dotted script name's extension may carry `-`/`_` —
    `bash scripts/setup.foo-bar` is an invocation (Codex on #125)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "setup.foo-bar").write_bytes(b"x")
    (pdir / "scripts" / "setup.foo_bar").write_bytes(b"x")
    for line in (b"bash scripts/setup.foo-bar",
                 b"bash scripts/setup.foo_bar",
                 b"./scripts/setup.foo-bar"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_env_split_string_trailing_argv(tmp_path):
    """`env -S 'sh' -c true` appends argv AFTER the operand — the `-c`
    means stdin is never read (Devin BUG_0001 on #125); `-S 'bash'
    /dev/null` runs the positional file (BUG_0002 on #1374)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    # operand plus trailing argv classifies as sink — chain ends
    for pipe in (b"cat scripts/x.sh | env -S 'sh' -c true | sh",
                 b"cat scripts/x.sh | env -S 'bash' /dev/null | sh"):
        assert not mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # bare operand still executes the pipe
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | env -S 'sh'\n")

def test_script_dep_bare_env_is_a_sink(tmp_path):
    """`env` with no command word prints the environ — it never forwards
    stdin, so `cat x | env | sh` pipes env vars, not the script (Devin
    BUG_0005 on #125)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for pipe in (b"cat scripts/x.sh | env | sh",
                 b"cat scripts/x.sh | env -i | sh",
                 b"cat scripts/x.sh | A=1 env | bash"):
        assert not mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # env WITH a command word still wraps it
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | env sh\n")

def test_script_dep_stdout_redirect_before_pipe(tmp_path):
    """`echo "$(cat x)" > f | sh` redirects stdout — the pipe carries
    nothing, so the reader is inert (Devin BUG_0004 on #125). fd>1 and
    input redirects do NOT redirect the pipe's payload."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.sh)" > /dev/null | sh',
                 b'echo "$(cat scripts/x.sh)" >> log | bash',
                 b'echo "$(cat scripts/x.sh)" &> out | sh'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # fd2 redirect does not starve the pipe — still executes
    for line in (b'echo "$(cat scripts/x.sh)" 2>&1 | sh',
                 b'echo "$(cat scripts/x.sh)" 2>/dev/null | sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_later_substitution_is_an_arg(tmp_path):
    """A `$(...)` between the substitution and the pipe is an ARGUMENT,
    not a boundary — `echo "$(cat x)" $(true) | sh` still executes
    (Devin BUG_0002 on #125/#1374, cpdcheck #8)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.sh)" $(true) | sh',
                 b'echo "$(cat scripts/x.sh)" `true` | sh',
                 b'echo "$(cat scripts/x.sh)" "$(whoami)" | bash'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_inner_quotes_dont_hide_pipe(tmp_path):
    """Quotes inside a LATER substitution must not flip the outer quote
    state — `"$(cat x;" "y)" | sh` still pipes (Devin BUG_0003 on #125,
    Codex on cpdcheck #8)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh; echo " ")" | sh\n')
    # a comment before the pipe ends the command — nothing executes
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" # runs | sh\n')

def test_script_dep_heredoc_literal_quotes_backticks(tmp_path):
    """An unquoted heredoc body treats `'it\\'s'` as literal text yet
    backticks still expand — the apostrophe must not hide a real
    `sh scripts/x.sh` pair (CodeRabbit on #125)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    body = (b"cat <<'E'\n"
            b"it's `sh scripts/x.sh` literal\n"
            b"E\n")
    # quoted delimiter → literal body, no exec
    assert not mod.script_dep_block(pdir, body)
    body = (b"sh <<'E'\n"
            b"it's fine\n"
            b"E\n")
    # quoted delimiter on sh → literal body, nothing runs
    assert not mod.script_dep_block(pdir, body)
    # unquoted delimiter — the body is expanded by sh: a backtick
    # command following a literal apostrophe still counts
    body = (b"cat <<E\n"
            b"it's `sh scripts/x.sh` literal\n"
            b"E\n")
    assert mod.script_dep_block(pdir, body)

def test_script_dep_quoted_backtick_keeps_quote_state(tmp_path):
    """The byte right after a closing backtick must still be read — a
    skipped `"` flipped the outer quote state and hid the pipe (round-6
    review on #125/#8)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "`cat scripts/x.sh`" | sh',
                 b'echo `true` "$(cat scripts/x.sh)" | sh',
                 b'echo "`true`$(cat scripts/x.sh)" | bash'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_wrapped_env_split_string(tmp_path):
    """A wrapper in front of `env` must not end the split-string pre-pass
    (round-6 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.sh)" | command env -S sh',
                 b'echo "$(cat scripts/x.sh)" | nohup env -S bash',
                 b'echo "$(cat scripts/x.sh)" | stdbuf -o0 env -S sh'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_env_split_string_forwarding_operand(tmp_path):
    """`env -S 'cat'` is a COMMAND that forwards stdin — it must not fall
    through to the bare-`env` sink (round-6 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b"echo \"$(cat scripts/x.sh)\" | env -S 'cat' | sh",
                 b"echo \"$(cat scripts/x.sh)\" | env -S 'tee /dev/null' | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_stdout_self_duplication(tmp_path):
    """`>&1`/`1>&1` point stdout at itself — a no-op that leaves the pipe
    intact (round-6 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.sh)" >&1 | sh',
                 b'echo "$(cat scripts/x.sh)" 1>&1 | bash'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_earlier_stdout_redirect(tmp_path):
    """A fd1 redirect BEFORE the substitution empties the pipe too; a
    redirect in an earlier command segment does not (round-6 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'echo >/dev/null "$(cat scripts/x.sh)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'true >/dev/null; echo "$(cat scripts/x.sh)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'true >/dev/null && echo "$(cat scripts/x.sh)" | sh\n')

def test_script_dep_python_module_stdin(tmp_path):
    """`python -m` is a sink only for modules that parse stdin as DATA —
    `code` runs it, `base64 -d` decodes it into program text (round-6
    review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.py)" | python -m code',
                 b'echo "$(cat scripts/x.py)" | python -m base64 -d | sh',
                 b'echo "$(cat scripts/x.py)" | python3 -mcode'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (b'echo "$(cat scripts/x.py)" | python -m json.tool',
                 b'echo "$(cat scripts/x.py)" | python -m http.server'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_nested_env_split_string(tmp_path):
    """`env FOO=1 env -S sh` — the command word is itself a wrapper; the
    INNER env's -S operand must still be classified (round-7 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.sh)" | env FOO=1 env -S sh',
                 b'echo "$(cat scripts/x.sh)" | env -i env -S bash'):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_describe_wrapper_hides_env(tmp_path):
    """`command -v env -S sh` only DESCRIBES env — the split operand
    never runs (round-7 review on #8/#1374/#1955)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.sh)" | command -v env -S sh',
                 b'echo "$(cat scripts/x.sh)" | command -V env -S bash'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_read_write_redirect(tmp_path):
    """`<>`/`0<>` open fd0 read-write — they never touch stdout, so the
    pipe stays live; `1<>`/`n<>` for n>=2 do break it (round-7 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" <> tmp | sh\n')
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" 1<> tmp | sh\n')

def test_script_dep_oversized_fd_digits(tmp_path):
    """A >4300-digit fd prefix raised ValueError in int() — the fd is
    compared as a normalised byte span instead (round-7 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    huge = b"9" * 5000
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | " + huge + b">/dev/null | sh\n")
    ones = b"0" * 4999 + b"1"
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | " + ones + b">/dev/null | sh\n")

def test_script_dep_bc_dc_positional_still_reads_stdin(tmp_path):
    """`bc file`/`dc file` run the file AND THEN read stdin — a
    positional does not end the exec head's stdin use (round-7 review
    on #1955)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b"cat scripts/x.sh | bc init.bc",
                 b"cat scripts/x.sh | dc -f a.dc"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_python_module_encode_sink(tmp_path):
    """Codec modules are sinks in ENCODE mode — only `-d`/`-D`/`--decode`/
    `--decompress` turns stdin into program text (round-7 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for line in (b'echo "$(cat scripts/x.py)" | python -m base64 -d | sh',
                 b'echo "$(cat scripts/x.py)" | python -m gzip -d | sh',
                 b'echo "$(cat scripts/x.py)" | python3 -mcode'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    for line in (b'echo "$(cat scripts/x.py)" | python -m base64 | sh',
                 b'echo "$(cat scripts/x.py)" | python -m gzip > x.gz',
                 b'echo "$(cat scripts/x.py)" | python -m bz2 | sh',
                 b'echo "$(cat scripts/x.py)" | python -m pdb'):
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_segment_stdout_redirect(tmp_path):
    """A middle segment whose fd1 is redirected forwards nothing —
    `cat x | tee >/dev/null | sh` is not a dep (round-7 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b"cat scripts/x.sh | tee >/dev/null | sh",
                 b"cat scripts/x.sh | sed s/a/b/ >/dev/null | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee /dev/stderr | sh\n")

def test_script_dep_exec_head_own_redirect(tmp_path):
    """`sh > file` still READS stdin — redirections inside the exec
    head's argv are not positional arguments."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b"cat scripts/x.sh | sh > /dev/null",
                 b"cat scripts/x.sh | sh >y",
                 b"cat scripts/x.sh | bash - > out"):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh script.sh > log\n")

def test_script_dep_nested_sub_span_order(tmp_path):
    """`$(...)` spans are recorded at CLOSE, so a nested inner span
    precedes its outer opener — the pipe past `$(outer $(inner))` must
    still be found (Devin on #125/#8, round-8 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh)" $(echo x; echo $(true)) | sh\n')
    # And a `;` inside a nested sub must NOT end the outer command.
    assert mod.script_dep_block(
        pdir,
        b'echo $(cat scripts/x.sh; $(true)) | sh\n')

def test_script_dep_fd_dup_alias(tmp_path):
    """`3>&1 1>&3` leaves stdout on the pipe — fd dups take the target's
    CURRENT alias (Codex on #1955). `1>&2` still diverts."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | cat 3>&1 1>&3 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | cat 1>&2 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | cat >&2 | sh\n")
    # 3>file then 1>&3 diverts through the alias.
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | cat 3>/tmp/o 1>&3 | sh\n")
    # `1>&9` on a never-opened fd errors at runtime — treated as
    # diverted (nothing reaches the pipe).
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | cat 1>&9 | sh\n")

def test_script_dep_dup_word_filename(tmp_path):
    """`>&1foo` redirects to a FILE named `1foo` — only a pure digit
    word after `>&` is an fd dup (Codex on #125)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee >&1 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee >&1foo | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee >&1- | sh\n")

def test_script_dep_fd2_append(tmp_path):
    """`2>>err` touches fd2 — stdout still flows down the pipe
    (CodeRabbit on #1955)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee 2>>err | sh\n")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" 2>>err | sh\n')
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee >>/dev/null | sh\n")

def test_script_dep_stdin_rebound(tmp_path):
    """An fd0 redirect rebinds stdin — `sh </dev/null` never reads the
    pipe, so nothing is a dep (Devin on #125/#8/#1955)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b"cat scripts/x.sh | sh </dev/null",
                 b"cat scripts/x.sh | sh < /tmp/y",
                 b"cat scripts/x.sh | sh 0</tmp/y",
                 b"cat scripts/x.sh | sh <&3",
                 b"cat scripts/x.sh | sh <<EOF\ncat\nEOF",
                 b"cat scripts/x.sh | sh <<</tmp/y"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    # `0<&0` is a self-dup — stdin stays the pipe.
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | sh <&0\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | sh 0<&0\n")

def test_script_dep_quiet_filters(tmp_path):
    """Output-suppressing options end the pipe even on forwarding
    heads: `grep -q`, `head -n 0`, `tail -c 0` (Devin on #1374)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for line in (b"cat scripts/x.sh | grep -q x | sh",
                 b"cat scripts/x.sh | grep -vq x | sh",
                 b"cat scripts/x.sh | grep --silent x | sh",
                 b"cat scripts/x.sh | head -n0 | sh",
                 b"cat scripts/x.sh | head -n 0 | sh",
                 b"cat scripts/x.sh | tail --bytes=0 | sh",
                 b"cat scripts/x.sh | tail -c 0 | sh",
                 # `-e`'s operand glues (`-eq` = pattern "q"), so `x` is
                 # a FILE operand — grep emits the file's matches, not
                 # the pipe (round-16 review).
                 b"cat scripts/x.sh | grep -eq x | sh"):
        assert not mod.script_dep_block(pdir, line + b"\n"), line
    for line in (b"cat scripts/x.sh | grep x | sh",
                 b"cat scripts/x.sh | grep -s x | sh",
                 b"cat scripts/x.sh | head -n1 | sh"):
        assert mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_python_base64_urlsafe(tmp_path):
    """`python -m base64 -u` is a DECODE flag — the emitted stream is
    program text again (Codex on #1374)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.b64 | python -m base64 -u | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.b64 | python -m base64 -e | sh\n')

def test_script_dep_backslash_inside_singles(tmp_path):
    """`\\'` inside single quotes is literal — it does NOT escape the
    closing quote, so `'a\\''>x'` keeps `>x` quoted (CodeRabbit on
    #1955)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee 'a\\' '>x' | sh\n")

def test_script_dep_stdout_save_restore(tmp_path):
    """A `>` mid-segment is not final — `3>&1 >/dev/null 1>&3` restores
    stdout onto the pipe (Devin + CodeRabbit on #127/#1374/#9, round-9
    review). The fd map is judged at the `|`, never mid-segment."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" 3>&1 >/dev/null 1>&3 | sh\n')
    # restore through an fd-backed device path — same alias semantics
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" 3>&1 >/dev/null'
              b' 1>/dev/fd/3 | sh\n')
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >/dev/stdout | sh\n')
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >/proc/self/fd/1 | sh\n')
    # still diverted when nothing restores it
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >/dev/null | sh\n')
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" 3>/tmp/o 1>&3 | sh\n')

def test_script_dep_clobber_op(tmp_path):
    """`>|` is noclobber file redirection — its `|` is operator text,
    not a pipe (`echo "$(x)" 2>|sh` writes fd2 to a file named `sh` —
    CodeRabbit on #1955/#9, round-9 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" 2>|sh\n')
    # a real pipe AFTER the clobber target still counts
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" 2>|log | sh\n')

def test_script_dep_stdin_save_restore(tmp_path):
    """fd dups can save and restore STDIN — `3<&0 </dev/null 0<&3`
    leaves fd0 on the pipe (Codex on #1955, round-9 review); `<&00`
    and `<& 0` are self-dup forms (Codex on #127)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh 3<&0 </dev/null 0<&3\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | sh <&00\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | sh <& 0\n")
    # `<& /dev/stdin` is an ambiguous redirect — bash errors out and
    # nothing executes.
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh 0<& /dev/stdin\n")
    # rebound after the restore still sinks
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh 3<&0 0<&3 </dev/null\n")

def test_script_dep_late_stdin_redirect(tmp_path):
    """Redirects apply AFTER `-s`/`-`/`-m` verdicts too —
    `sh -s </dev/null` and `python -m base64 -d </dev/null` read the
    file, not the pipe (Codex on #1374, CodeRabbit on #9, round-9)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -s </dev/null | cat\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh - </dev/null | cat\n")
    assert not mod.script_dep_block(
        pdir,
        b"cat scripts/x.b64 | python -m base64 -d </dev/null | sh\n")
    # glued mid-word operator — `-s</dev/null` is `-s` + a redirect
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -s</dev/null | cat\n")
    # and `-s` alone still reads the pipe
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -s | cat\n")

def test_script_dep_grep_operand_hides_quiet(tmp_path):
    """An operand-taking option consumes the NEXT word — `grep -e -q`
    uses `-q` as the PATTERN and still forwards matches (Devin +
    CodeRabbit on #1374/#9, round-9 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | grep -e -q | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | grep --regexp -q | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | grep -f -q | sh\n")
    # `-qe` — quiet is set before the operand flag; still a sink
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | grep -qe -q | sh\n")
    # `-q` as a separate flag after a non-operand option still sinks
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | grep -v -q x | sh\n")

def test_script_dep_head_last_limit_wins(tmp_path):
    """The LAST `-n`/`-c` limit decides — `head -n 0 -n 10` forwards 10
    lines (CodeRabbit + Codex on #127/#1374, round-9 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n 0 -n 10 | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -c 0 -n 5 | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -5 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n 5 -n 0 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n 10 -c 0 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -0 | sh\n")

def test_script_dep_emit_head_substitution(tmp_path):
    """`echo`/`printf` emit their argv — a substitution inside re-reads
    the pipe and forwards it downstream: `cat x | echo "$(cat)" | sh`
    runs x (Devin on #1374, round-9 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | printf "%s" "$(cat)" | sh\n')
    # non-emitting sinks take the substitution as an operand — the
    # content never reaches stdout
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | wc -c "$(cat)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | echo plain | sh\n")

def test_script_dep_python_base64_cluster(tmp_path):
    """Codec modules accept clustered shorts — `python -m base64 -du`
    decodes (urlsafe), re-emitting program text for a downstream `sh`
    (Codex on #1374, round-9 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -du | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -ud | sh\n")
    # `-ed` contains a decode letter — it decodes too (verified:
    # getopt splits the cluster). `-u` is decode-family (urlsafe), so
    # `-e` alone is the encode-only negative.
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -ed | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -e | sh\n")

def test_script_dep_enclosing_sub_span(tmp_path):
    """A substitution nested in the ENCLOSING substitution's body is
    descended, not skipped — `echo "$(echo "$(cat x)" | sh)"` runs x
    (Devin on #127, round-9 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(echo "$(cat scripts/x.sh)" | sh)"\n')
    # and the outer-pipe form still counts
    assert mod.script_dep_block(
        pdir, b'echo "$(echo "$(cat scripts/x.sh)")" | sh\n')

def test_script_dep_amp_gt_binds_both_fds(tmp_path):
    """`&> f` redirects fds 1+2 to the file — the pipe after it gets
    nothing (round-9 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" &> f | sh\n')
    # target in the next word binds both fds too
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" &> f f2 | sh\n')

def test_script_dep_amp_gt_fd_alias_target(tmp_path):
    """`&>/dev/stdout` rebinds fds 1+2 to stdout's CURRENT target — the
    pipe — not a file (Devin on #127, round-10 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" &>/dev/stdout | sh\n')
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" &> /dev/stdout | sh\n')
    # a real file still diverts
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" &>/dev/null | sh\n')

def test_script_dep_clobber_fd_alias(tmp_path):
    """`>|` follows the same fd-alias rule as `>` — `>| /dev/stdout`
    keeps the pipe (Devin on #9, round-10 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >| /dev/stdout | sh\n')
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >| /dev/null | sh\n')

def test_script_dep_quoted_redirect_target(tmp_path):
    """Quotes around a redirect TARGET do not quote the operator —
    `sh -s <"/dev/null"` reads the device, not the pipe; a quoted
    OPERATOR byte stays literal (`'a<b'` is arg text) (Devin on #127,
    round-10 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh -s <"/dev/null"\n')
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -s <'/dev/null'\n")
    # quoted operator chars are literal argument text
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -s 'a<b'\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -s 'a>b'\n")
    # quoted fd alias still aliases
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh -s <"/dev/stdin"\n')

def test_script_dep_expansion_dup_target(tmp_path):
    """`1>&$FD` can restore a saved fd at runtime — bind the fd's
    default so a possible pipe-restore still registers the dep (Codex
    on #127, round-10 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" 3>&1 >/dev/null 1>&$FD | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh <&$FD\n')

def test_script_dep_codec_module_flags(tmp_path):
    """Codec decode flags are per-module: `gzip -u` aborts (no decode),
    `base64 -u` decodes, a positional operand reads a FILE not the
    pipe, a lone `-` still means stdin (Codex on #127, round-10
    review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.gz").write_bytes(b"x")
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip -d | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -u | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -du | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d - | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip -u | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip -d f.gz | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d f.b64 | sh\n")

def test_script_dep_sink_head_sub_arg(tmp_path):
    """A `$(...)` inside a stream-replacing head's args is data, not
    code — `wc "$(cat x)" | sh` hands sh a count (CodeRabbit on #1955,
    round-10 review). Emit heads still forward the text."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | wc "$(cat scripts/x.sh)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | md5sum "$(cat scripts/x.sh)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat scripts/x.sh)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date "+$(cat scripts/x.sh)" | sh\n')

def test_script_dep_flags_after_program_operand(tmp_path):
    """Words after `-c` are argv for the program string, never flags —
    `python -c 'x' -m code` runs `x`, it does not open a REPL on stdin
    (Codex on #9, round-10 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | python -c "x" -m code\n')
    # `-m` BEFORE `-c` still governs
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | python -m code -c | sh\n")

def test_script_dep_nested_sub_propagates(tmp_path):
    """A `;` inside the ENCLOSING substitution does not end the output
    flow — `echo "$(echo "$(cat x)"; echo foo)" | sh` still pipes the
    captured text to sh (Devin on #127, round-11 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir,
        b'cat scripts/x.sh | echo "$(echo "$(cat scripts/x.sh)";'
        b' echo foo)" | sh\n')
    # backtick-free single-sub form still works
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh; echo foo)" | sh\n')

def test_script_dep_emit_sub_must_read_stdin(tmp_path):
    """Emit heads forward the pipe only through an arg substitution
    that RE-READS it — `echo "$(printf foo)"` emits its own text, and
    `$(cat f)` reads a file operand, not the pipe (Devin + CodeRabbit
    on #127/#1380, round-11 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(printf foo)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat f)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat -)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(head)" | sh\n')

def test_script_dep_date_only_plus_fmt_forwards(tmp_path):
    """`date` echoes only a `+FORMAT` operand verbatim —
    `date --date="$(cat)" | sh` parses the script as a date, it never
    echoes the bytes (Codex on #1957, round-11 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date "+$(cat)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date --date="$(cat)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date "$(cat)" | sh\n')

def test_script_dep_head_zero_spelling(tmp_path):
    """Any all-zero `-n` count ends the pipe (`00`, `+0`) — but a
    NEGATIVE count like `-n -0` emits every line (Devin on #127,
    round-11 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n 00 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n +0 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head --lines=00 | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n -0 | sh\n")

def test_script_dep_codec_last_mode_wins(tmp_path):
    """Codec direction flags are last-wins: `base64 -d -e` ENCODES (the
    pipe becomes ciphertext — a sink), `-e -d` decodes, and getopt
    cluster letters apply in order (`-ed` decodes, `-de` encodes —
    Devin on #1380/#1957, round-11 review). `-e` is not a quopri flag
    at all, so `quopri -d -e` aborts."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -e -d | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -ed | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d -e | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -de | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m quopri -d -e | sh\n")

def test_script_dep_codec_operands_and_terminator(tmp_path):
    """A `--` ends option parsing (`base64 -d -- -` still decodes the
    pipe); base64 uses only its FIRST operand while quopri and gzip
    iterate (`quopri -d missing -` still decodes stdin). A `-` operand
    names stdin for ALL FOUR codec modules — verified against CPython
    3.12: `gzip -d -` and `uu -d -` decode the pipe, they do not crash
    (Devin + Codex + CodeRabbit on #127/#9/#1380/#1957, round-12)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    (pdir / "scripts" / "x.uu").write_bytes(b"x")
    (pdir / "scripts" / "x.gz").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d -- - | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d - f | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m quopri -d missing - | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m quopri -d - missing | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d f - | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip -d - | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.uu | python -m uu -d - | sh\n")
    # gzip iterates operands like quopri — a later `-` still reads the
    # pipe even after a file operand.
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip -d f.gz - | sh\n")
    # uu's SECOND operand is the output file — the decoded bytes leave
    # stdout, so the pipe ends there.
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.uu | python -m uu -d - out | sh\n")
    # A no-operand decode reads stdin for every codec module.
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip -d | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.uu | python -m uu -d | sh\n")

def test_script_dep_codec_getopt_stops_at_operand(tmp_path):
    """getopt modules stop option parsing at the FIRST operand —
    `base64 -d - -e` decodes stdin and treats `-e` as a second operand
    (verified), so the last-wins direction rule must not see it
    (Codex on #9, round-12 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d - -e | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m quopri -d - -e | sh\n")

def test_script_dep_codec_mutex_flags_abort(tmp_path):
    """Mutually-exclusive flag pairs abort the module before the pipe
    is read: `quopri -d -t` (and the cluster spelling `-dt`) and gzip
    `--fast -d` both exit with an error (CodeRabbit on #9, round-12
    review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.qp").write_bytes(b"x")
    (pdir / "scripts" / "x.gz").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.qp | python -m quopri -d -t | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.qp | python -m quopri -dt | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip --fast -d | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip --best -d | sh\n")

def test_script_dep_unquoted_sub_forwards(tmp_path):
    """An UNQUOTED `$(cat)` still forwards the pipe — paren masking
    splits it across words, so the emit-head check must scan the
    window's substitution spans, not the masked argv (Devin on #1380,
    round-12 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | echo $(cat) | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | echo $(cat -) | sh\n")
    # A cross-word substitution body still resolves.
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | echo $(printf a; cat) | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | echo $(printf cat) | sh\n")

def test_script_dep_sub_reader_must_be_head(tmp_path):
    """A reader NAME is only a read when it heads its command:
    `$(printf cat)` emits the text `cat`, and a reader whose own fd1
    is diverted contributes nothing to the captured output (Devin +
    CodeRabbit on #1380/#1957, round-12 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(printf cat)" | sh\n')
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh >/dev/null; echo safe)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat >/dev/null)" | sh\n')
    # An explicit fd0 self-dup still reads the pipe.
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat 0<&0)" | sh\n')

def test_script_dep_redirect_words_are_not_argv(tmp_path):
    """Redirect words never reach a head's argv — `grep x 2> -q` writes
    stderr to a file named `-q` (quiet mode is NOT enabled) and
    `head -n 0 2> -n 10` never sees a second limit (Codex + Devin on
    #1380/#1957, round-12 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | grep x 2> -q | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n 0 2> -n 10 | sh\n")

def test_script_dep_head_tail_signed_zero(tmp_path):
    """A signed zero's meaning is per-command (GNU coreutils):
    `tail -n +0` emits everything while `tail -n -0` emits nothing;
    `head` is the reverse (Codex on #1380, round-12 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tail -n +0 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tail -n -0 | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n -0 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n +0 | sh\n")
    # An unsigned zero ends the pipe for both.
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tail -n 0 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | head -n 0 | sh\n")

def test_script_dep_glued_digit_is_not_fd(tmp_path):
    """An fd prefix is only a STANDALONE digit run — in
    `tee log2>/dev/null` the `2` is glued to `log`, so fd1 (not fd2)
    goes to the file and the pipe ends (Devin on #1380, round-12
    review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee log2>/dev/null | sh\n")
    # A separated digit IS an fd — stdout still carries the script.
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee log 2>/dev/null | sh\n")

def test_script_dep_quoted_numeric_dup(tmp_path):
    """`>&"1"` is an fd DUP — bash removes the quotes before reading
    the target, so the stream is forwarded, not written to a file
    named `1` (Devin on #1957, round-12 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >&"1" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >&"2" | sh\n')

def test_script_dep_date_substitution_gating(tmp_path):
    """`date` echoes ONLY a `+FORMAT` operand — `date "$(cat x)" | sh`
    parses the file as a date string and emits a timestamp, never the
    bytes (Codex on #1380, round-12 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'date "+$(cat scripts/x.sh)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'date "$(cat scripts/x.sh)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'date --date="$(cat scripts/x.sh)" +%s | sh\n')

def test_script_dep_codec_long_prefix_and_abort(tmp_path):
    """argparse/optparse modules resolve unique long prefixes
    (`uu --de`, `gzip --decomp` decode); `uu -t` exits before the pipe
    is read, and an ambiguous/unknown long aborts (CodeRabbit on #127,
    round-11 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.uu").write_bytes(b"x")
    (pdir / "scripts" / "x.gz").write_bytes(b"x")
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.uu | python -m uu --de | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.uu | python -m uu --dec | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.gz | python -m gzip --decomp | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.uu | python -m uu -d -t | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.uu | python -m uu --t | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.uu | python -m uu --nope | sh\n")
    # getopt modules have no long options at all
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 --decode | sh\n")

def test_script_dep_codec_redirect_words(tmp_path):
    """Redirect words in the MODULE's argv are folded, not operands:
    `python -m base64 -d 2>/dev/null` and `0<&0` still decode (Codex +
    Devin on #127/#1380/#1957, round-11 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.b64").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d 2>/dev/null | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d 0<&0 | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d 2> f | sh\n")
    # an input redirect away from the pipe still sinks
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.b64 | python -m base64 -d </dev/null | sh\n")

def test_script_dep_python_first_m_wins(tmp_path):
    """Only the FIRST `-m` selects the module — later words are its
    argv (`python -m code -m base64` opens the REPL, `python -m
    json.tool -m code` parses data — Devin on #127, round-11)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | python -m code -m base64 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | python -m json.tool -m code | sh\n")
    # and a `-m` inside module argv is an invalid codec flag — the
    # module aborts (getopt rejects it), so the pipe is never decoded
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | python -m base64 -d -m code | sh\n")

def test_script_dep_dup_target_spacing(tmp_path):
    """`>& 1` spaced still DUPS the fd — stdout stays on the pipe —
    while `>& 2` diverts it; a `-` alone closes (Devin on #9/#1957,
    round-11 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >& 1 | sh\n')
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >& 2 | sh\n')
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >& - | sh\n')
    # and a spaced `<&` pending target behaves like the glued form
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh <& 0\n')

def test_script_dep_quoted_dup_target(tmp_path):
    """A quoted or escaped `$`/backtick in a `>&` target is a literal
    FILENAME — `>&'$FD'` and `>&\\$FD` divert the pipe, while the bare
    expansion may restore an fd (Devin on #1380, round-11 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >&\'$FD\' | sh\n')
    assert not mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >&\\$FD | sh\n')
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >&$FD | sh\n')
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh)" >&"$FD" | sh\n')

def test_script_dep_sub_newline_separates(tmp_path):
    """A newline inside a substitution body starts a new command —
    `$(printf safe\\ncat)` runs cat on the pipe (cpdcheck + impaktful
    reviews, round-13)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(printf safe\ncat)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(true\ncat)" | sh\n')
    # The last line being a sink still forwards nothing extra.
    assert mod.script_dep_block(
        pdir,
        b'cat scripts/x.sh | echo "$(cat\nwc -l >/dev/null)" | sh\n')

def test_script_dep_sub_wrapped_and_grouped_heads(tmp_path):
    """A reader counts as head past wrappers and group openers —
    `$(command cat)`, `$(env cat)`, `$(exec cat)`, `$( { cat; } )` and
    `$( (cat) )` all run cat on the pipe (Codex + Devin + CodeRabbit on
    #127/#9/#1380/#1957, round-13 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for sub in (b"command cat", b"env cat", b"exec cat",
                b"command -n cat", b" { cat; } ", b" (cat) ",
                b"sudo cat", b"stdbuf -o L cat", b"time cat"):
        assert mod.script_dep_block(
            pdir, b'cat scripts/x.sh | echo "$(' + sub + b')" | sh\n'), sub
    # `command -v cat` only describes — it never reads the pipe.
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(command -v cat)" | sh\n')
    # A wrapper name as ARGUMENT is text, not a head.
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(printf command cat)" | sh\n')

def test_script_dep_cat_stdin_operands(tmp_path):
    """`cat` reads the pipe when any operand is `-` or `/dev/stdin`,
    wherever it sits (`cat f -`, `cat -n f -` read BOTH the file and
    stdin — cpdcheck review, round-13)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for sub in (b"cat -", b"cat f -", b"cat - f",
                b"cat /dev/stdin", b"cat f /dev/stdin"):
        assert mod.script_dep_block(
            pdir, b'cat scripts/x.sh | echo "$(' + sub + b')" | sh\n'), sub
    # `cat -n f -` numbers every line — the capture emits `   1\t…`
    # and the downstream `sh` runs `1`, not the script (round-59 —
    # verified live; supersedes this case's former flag-only reading).
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat -n f -)" | sh\n')
    # Bare `cat -n` also numbers stdin — `$(…)` emits `   1\t…` and
    # `sh` runs `1` (round-59 — verified live).
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat -n)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat f)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat -n f)" | sh\n')
    # `cat <f` reads the file — a rebind on fd0, not the pipe.
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat <f)" | sh\n')

def test_script_dep_inner_pipe_suppression(tmp_path):
    """Only the LAST inner pipe stage's output reaches the capture —
    `$(cat | wc -l)` emits a count and `$(cat scripts/x.sh | head -n 0)`
    emits nothing, so neither forwards the script (Devin + CodeRabbit
    on #1380/#127, round-13 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat | wc -l)" | sh\n')
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | head -n 0)" | sh\n')
    # Pass-through last stages still forward.
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat | head -n 5)" | sh\n')
    assert mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | head -n 5)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat | cat)" | sh\n')
    # An inner exec stage runs whatever flowed in.
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(cat | sh)" | sh\n')
    assert mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | sh)" | sh\n')

def test_script_dep_input_side_fd_dup(tmp_path):
    """`tee 1<&2` dups fd2 ONTO fd1 — tee writes the pipe's bytes to
    stderr and hands the next stage nothing (Devin on #9, round-13
    review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee 1<&2 | sh\n")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(tee 1<&2)" | sh\n')
    # The restore half still restores: `2>&1 1<&2` keeps fd1 on the
    # pipe.
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee 2>&1 1<&2 | sh\n")
    # `0<&0`-style self-dups stay benign.
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | tee 0<&0 | sh\n")

def test_script_dep_date_unquoted_sub(tmp_path):
    """UNQUOTED `date +$(…)`/`date +`…`` field-split the expansion —
    the capture still joins the format operand when it splits to ONE
    word, while a provably empty/>=2-word capture makes date emit
    nothing or error (Devin on #128, round-25 review — supersedes the
    round-24 flat reject; verified live on GNU date). Only quoted
    `"+$(…)` joins unconditionally."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "two.sh").write_bytes(b"a b")
    # Pipe-fed `$(cat)` — the stream's word count is indeterminate, so
    # the fail-closed answer is dep (x.sh is single-word anyway).
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date +$(cat) | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date +`cat` | sh\n')
    # The quoted form forwards verbatim.
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date +"$(cat)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date "+$(cat)" | sh\n')
    # A provably single-word file capture joins the unquoted format.
    assert mod.script_dep_block(
        pdir, b'date +$(cat scripts/x.sh) | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date +$(cat scripts/x.sh) | sh\n')
    assert mod.script_dep_block(
        pdir, b'date "+$(cat scripts/x.sh)" | sh\n')
    # A provably multi-word capture makes date reject the extras —
    # nothing reaches the downstream exec.
    assert not mod.script_dep_block(
        pdir, b'date +$(cat scripts/two.sh) | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | date +$(cat scripts/two.sh) | sh\n')
    assert not mod.script_dep_block(
        pdir, b'date +`cat scripts/two.sh` | sh\n')
    # A missing file is indeterminate — fail closed.
    assert mod.script_dep_block(
        pdir, b'date +$(cat scripts/missing.sh) | sh\n')

def test_script_dep_quoted_separator_literals(tmp_path):
    """Separators inside quotes are literal text, not command
    boundaries — `$(printf 'safe; cat')` prints one string and never
    runs `cat` (Devin on #127/#1380, round-14 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(printf \'safe; cat\')" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(printf \'safe|cat\')" | sh\n')
    # Real separators still split — the second region runs on stdin.
    assert mod.script_dep_block(
        pdir, b'echo "$(printf a; cat scripts/x.sh)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(printf a; cat)" | sh\n')

def test_script_dep_bare_ampersand_and_pipe_amp(tmp_path):
    """A bare `&` is a background separator and `|&` a single pipe
    operator — earlier they were either glued or split wrongly
    (Codex on #127/#1380/#1957, round-14 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    # `cat x | cat & cat | sh`: background job + `cat | sh` reading the
    # script's own stdin — not the script bytes.
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | cat & cat | sh\n")
    # `|&` joins stderr into the pipe — script bytes still flow.
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh |& cat)" | sh\n')
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -c 'cat' |& sh\n")
    # `cat &` inside a substitution emits script bytes to the capture.
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh & wait)" | sh\n')

def test_script_dep_compound_keyword_transparency(tmp_path):
    """Compound keywords share the compound statement's stdin —
    `if :; then cat; fi` and `while cat; do :; done` forward the
    stream (Codex on #1380, round-14 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | if :; then cat; fi)" | sh\n')
    assert mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | while cat; do :; done)" | sh\n')
    # Inside the substitution walk a `for`/`in` list naming a plugin
    # script carries its bytes (the outer dep-detection of for-list
    # operands is a separate gap — the segment walk still detects it).
    assert mod._sub_reads_stdin(
        b'for i in scripts/x.sh; do cat "$i"; done')

def test_script_dep_shell_c_operand(tmp_path):
    """`sh -c` runs its STRING, never the pipe: `-s` does not change
    that, and a reader/exec head inside the string classifies the
    stage by the string (Devin on #127, round-14 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -s -c 'exit'\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -c 'echo safe' | sh\n")
    # `-s` must not swallow `-c` as its operand.
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | bash -s -c 'exit'\n")
    # A reader inside -c re-emits the pipe; an inner exec consumes it.
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -c 'cat' | sh\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -c 'sh'\n")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh -c 'if :; then cat; fi' | sh\n")

def test_script_dep_reader_file_operands(tmp_path):
    """Reader heads with real FILE operands emit the file, not the
    pipe — `head -n 1 /etc/hosts` forwards nothing (Devin on #1380,
    round-14 review). Program-first heads skip the pattern/script
    operand; a `-`/fd-path operand keeps the pipe."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | head -n 1 /etc/hosts)" | sh\n')
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | grep p /etc/hosts)" | sh\n')
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | sed s/a/b/ /etc/hosts)" | sh\n')
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | awk \'{print}\' /etc/hosts)" | sh\n')
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | jq . /etc/hosts)" | sh\n')
    # Program supplied via flag: first positional is already a file.
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | grep -e p /etc/hosts)" | sh\n')
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | openssl base64 -in /etc/hosts)" | sh\n')
    # No file operand — the pipe still flows.
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh | head -n 1)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh | tee /tmp/f)" | sh\n')
    # `cat < file` via a spaced target also rebinds.
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | cat < /dev/null)" | sh\n')

def test_script_dep_exec_only_on_stream_content(tmp_path):
    """An exec stage consumes a dep only when the STREAM carries it —
    `cat x | wc -l | sh` hands sh a line count, not the script
    (Devin on #127, round-14 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir,
        b'echo "$(cat scripts/x.sh | cat x | wc -l | cat)" | sh\n')
    # Upstream content still executes.
    assert mod.script_dep_block(
        pdir, b'echo "$(cat scripts/x.sh | sh)" | sh\n')

def test_script_dep_fd_path_operands(tmp_path):
    """`python -m` codec operands named by fd paths read the pipe —
    `/dev/stdin`, `/dev/fd/0`, `/proc/self/fd/0` are all stdin
    (Codex on #1957, round-14 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | python -m base64 -d /dev/stdin | sh\n")
    assert mod.script_dep_block(
        pdir,
        b"cat scripts/x.sh | python -m uu -d /proc/self/fd/0 | sh\n")
    # `python -m gzip -d` accepts only `-` or a *.gz filename — an
    # fd path fails the suffix check and the module exits before
    # touching stdin (verified against CPython gzip.main, round-21).
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | python -m gzip -d /dev/fd/0 | sh\n")
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | python -m base64 -d /etc/passwd | sh\n")

def test_script_dep_expanded_input_dup_binds_default(tmp_path):
    """`0<&$FD` can restore fd0 to the pipe at runtime — bind the fd's
    default rather than a proven-away state (CodeRabbit on #1957,
    round-14 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b"cat scripts/x.sh | sh 0<&$FD\n")
    # Input-side fd dups still divert output off the pipe.
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | cat 1<&2 | sh\n")

def test_command_start_skips_substitution_separators(tmp_path):
    """`$(a; b)` inside a command line must not move the command start
    — the argument after the substitution belongs to the OUTER
    command, not the inner `;` (Devin on #1380, round-14 review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    src = b'bash other.sh $(printf a; printf b) scripts/x.sh\n'
    pos = src.index(b'scripts/x.sh')
    cs = mod._command_start(src, pos)
    assert src[cs:].startswith(b'bash other.sh')
    # An invoked script file is still detected after the substitution.
    src = b'bash scripts/x.sh $(printf a; printf b)\n'
    assert mod.script_dep_block(pdir, src)

def test_command_start_span_end_is_exclusive(tmp_path):
    """`$(a)` after a separator must not hide the NEXT command — the
    span end is already exclusive, so `;`/`|`/`&&` after `)` still move
    the command start (Devin + CodeRabbit on #1380/#1957, round-15)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for sep in (b"; ", b"|", b"&& ", b"\n"):
        assert mod.script_dep_block(
            pdir, b"echo $(true)" + sep + b"bash scripts/x.sh\n")
        assert mod.script_dep_block(
            pdir, b'echo "$(true)"' + sep + b"bash scripts/x.sh\n")
    # A bare `(` group inside the sub stays balanced (Devin on #1957).
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$( (true); cat)" | sh\n')
    # A `)` inside the sub's own quotes is literal — the `;` after the
    # REAL close still separates (Devin on #1957).
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(printf ")"; cat)" | sh\n')
    assert mod.script_dep_block(
        pdir, b'echo $(printf ";"); cat scripts/x.sh\n') is False

def test_script_dep_sh_c_classifies_whole_list(tmp_path):
    """`sh -c '…'` runs a command LIST sharing the shell's stdin — a
    later command or inner pipeline still executes the pipe (Devin +
    CodeRabbit on #1380/#1957, round-15)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh -c "true; sh"\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh -c "cat | sh"\n')
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh -c "echo start; sh"\n')
    # A forwarder inside `-c` still hands the stream downstream.
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh -c "cat" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh -c "cat"\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | sh -c "wc -l"\n')

def test_script_dep_shell_head_operands_not_demoted(tmp_path):
    """`-c`/program positionals on shell and bc/dc heads are program
    text, not input files — `$(bash -c cat)` forwards the pipe (Codex
    on #1380, round-15)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(bash -c cat)" | sh\n')
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(bash -c wc)" | sh\n')

def test_effective_head_leading_redirects(tmp_path):
    """Redirect words before the head are not the head — `0<&0 cat`
    runs cat on the pipe and `0<&3 sh` keeps fd0 live (Codex on #1957,
    round-15)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(0<&0 cat)" | sh\n')
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | 0<&0 sh\n")
    assert not mod.script_dep_block(pdir, b"cat scripts/x.sh | <&- sh\n")
    # Output-diverted leading redirect emits nothing to the capture.
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(>/dev/null cat)" | sh\n')

def test_reader_operands_long_program_flag(tmp_path):
    """`--regexp=P`/`--file=F` set prog_seen just like `-eP` — the next
    positional is a real input file (CodeRabbit on #1957, round-15)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(grep --regexp=f /etc/hosts)"'
        b" | sh\n")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(grep --regexp f /etc/hosts)"'
        b" | sh\n")

def test_seg_prov_structural_only_segments(tmp_path):
    """A bare `fi`/`done`/`then` emits no captured data — structural
    keywords strip to the real head instead of preserving provenance
    (CodeRabbit on #1957, round-15)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert not mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(if true; then echo hi; fi)"'
        b" | sh\n")
    assert mod.script_dep_block(
        pdir, b'cat scripts/x.sh | echo "$(if true; then cat; fi)"'
        b" | sh\n")
