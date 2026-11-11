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


def test_mini_yaml_plain_scalar_continuations():
    """Plain scalars fold deeper-indented continuations: content on
    spaces, blank lines to newlines, even lines shaped like seq items
    or document markers. A key-shaped line is 'mapping values are not
    allowed here'; a comment ends the scalar entirely."""
    mod = load_resolve_module()
    assert mod._mini_yaml("x: a\n  b\n") == {"x": "a b"}
    assert mod._mini_yaml("x: a\n\n  b\n") == {"x": "a\nb"}
    assert mod._mini_yaml("x: a\n  - s\n") == {"x": "a - s"}
    assert mod._mini_yaml("x: a\n  ---\n") == {"x": "a ---"}
    assert mod._mini_yaml("x: a\n  |\n") == {"x": "a |"}
    assert mod._mini_yaml("x: a\n   \n  b\n") == {"x": "a\nb"}
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n  \tb\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x:\n  a\n  y: 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x:\n  a\n y: 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n y: 1\n")
    # an outdented key ends the continuation — new sibling, not an error
    assert mod._mini_yaml("x: a\n  b\ny: 1\n") == {"x": "a b", "y": 1}
    # a comment terminates the scalar — a following indented line is an
    # error at any depth
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n # c\n  b\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("x: a\n  # c\n  b\n")

def test_mini_yaml_empty_value_dispatch():
    """`key:` with nothing after it dispatches on the next real line:
    deeper scalar → scalar, same-indent dash → nested sequence, deeper
    key → nested map, a lone `|`/`>` → block scalar, else None."""
    mod = load_resolve_module()
    assert mod._mini_yaml("x:\n  a\n") == {"x": "a"}
    assert mod._mini_yaml("x:\n b: 2\n") == {"x": {"b": 2}}
    assert mod._mini_yaml("x:\n- s\n") == {"x": ["s"]}
    assert mod._mini_yaml("x:\n - s\n") == {"x": ["s"]}
    assert mod._mini_yaml("x:\nb: 2\n") == {"x": None, "b": 2}
    assert mod._mini_yaml("x:\n") == {"x": None}
    # lone indicator lines discovered AFTER the key
    assert mod._mini_yaml("x:\n  |\n    a\n") == {"x": "a\n"}
    assert mod._mini_yaml("x:\n  >\n    a\n") == {"x": "a\n"}
    assert mod._mini_yaml("x:\n\n  |\n    a\n") == {"x": "a\n"}
    assert mod._mini_yaml("x:\n  # c\n  |\n    a\n") == {"x": "a\n"}
    assert mod._mini_yaml("x:\n  |\n") == {"x": ""}
    # doc-root lone indicator is a scalar document
    assert mod._mini_yaml("|\n  a\n") == "a\n"
    # blank/comment lines between `x:` and a deeper scalar don't detach
    # it — the scalar still attaches as the value
    assert mod._mini_yaml("x:\n\n  a\n") == {"x": "a"}
    assert mod._mini_yaml("x:\n # c\n  a\n") == {"x": "a"}

def test_mini_yaml_comment_ends_block():
    """A `#` line deeper than the key ends the block when shallower than
    established content indent (or, pre-content, shallower than a deeper
    whitespace line). At/above content indent it is literal."""
    mod = load_resolve_module()
    assert mod._mini_yaml("x: |\n   a\n  # c\n") == {"x": "a\n"}
    assert mod._mini_yaml("x: |\n   \n  # c\n") == {"x": ""}
    assert mod._mini_yaml("x: |\n    w\n  # c\n") == {"x": "w\n"}
    assert mod._mini_yaml("- |\n   a\n  # c\n") == ["a\n"]
    # comment at/above content indent is literal
    assert mod._mini_yaml("x: |\n  a\n  # c\n  b\n") == {"x": "a\n# c\nb\n"}
    assert mod._mini_yaml("x: |\n # c\n") == {"x": "# c\n"}
    assert mod._mini_yaml("x: |\n # c\n  a\n") == {"x": "# c\n a\n"}
    # an orphan line after the ended block still errors
    with pytest.raises(ValueError):
        mod._mini_yaml("x: |\n   \n  # c\n  a\n")

def test_mini_yaml_dash_key_colon_dispatch():
    """`- k:` (empty value inside a seq item) resolves like PyYAML:
    a key AT the key column is a SIBLING of the item map, a deeper key
    or deeper scalar is the VALUE, a dash attaches at >= key column,
    and a marker in between detaches the next line."""
    mod = load_resolve_module()
    assert mod._mini_yaml("- k:\n  b: 2\n") == [{"k": None, "b": 2}]
    assert mod._mini_yaml("- k:\n    b: 2\n") == [{"k": {"b": 2}}]
    assert mod._mini_yaml("- k:\n   b: 2\n") == [{"k": {"b": 2}}]
    with pytest.raises(ValueError):
        mod._mini_yaml("- k:\n - s\n")
    assert mod._mini_yaml("- k:\n   \n  b: 2\n") == [{"k": None, "b": 2}]
    assert mod._mini_yaml("- k:\n\n  - x\n") == [{"k": ["x"]}]
    assert mod._mini_yaml("- k:\n  - x\n") == [{"k": ["x"]}]
    assert mod._mini_yaml("- k:\n  |\n    v\n") == [{"k": "v\n"}]
    assert mod._mini_yaml("- k:\n\n   a\n") == [{"k": "a"}]
    with pytest.raises(ValueError):
        mod._mini_yaml("- k:\n\n  a\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("- k:\n - s\n")

def test_mini_yaml_dash_scalar_and_seq_folds():
    """`- scalar` and deeper plain items fold continuations at their own
    thresholds; a sibling dash inside a deeper seq's fold region is text."""
    mod = load_resolve_module()
    assert mod._mini_yaml("- foo\n  b\n") == ["foo b"]
    # a dash line inside the scalar's fold region is text, not an item
    assert mod._mini_yaml("- foo\n - s\n") == ["foo - s"]
    assert mod._mini_yaml("- foo\n- s\n") == ["foo", "s"]
    assert mod._mini_yaml("- k: v\n   c\n") == [{"k": "v c"}]
    assert mod._mini_yaml("-\n  v\n c\n") == ["v c"]
    # a shallower dash after a nested seq is a PyYAML error — orphan
    with pytest.raises(ValueError):
        mod._mini_yaml("-\n  - a\n - s\n")
    assert mod._mini_yaml("x:\n  - foo\n    - bar\n") == {"x": ["foo - bar"]}
    # a blank line inside the fold keeps a real newline
    assert mod._mini_yaml("- foo\n   \n  b\n") == ["foo\nb"]
    with pytest.raises(ValueError):
        mod._mini_yaml("- desc: x\n    y: 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("- a: 1\n      b: 2\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("-\n  a\n    y: 1\n")

def test_mini_yaml_lone_indicator_after_dash():
    """`- |` values are every line deeper than the dash; the first
    content line's indent may be deeper than the indicator's own."""
    mod = load_resolve_module()
    # deeper content than the first line's indent → PyYAML error
    with pytest.raises(ValueError):
        mod._mini_yaml("-\n  |\n   a\n  b\n")
    # a leading ws line deeper than the first content line → error
    with pytest.raises(ValueError):
        mod._mini_yaml("- |\n   \n  a\n")
    assert mod._mini_yaml("- |\n  a\n   b\n") == ["a\n b\n"]
    # - | indented still ends cleanly at outdent
    assert mod._mini_yaml("- |\n  a\n- s\n") == ["a\n", "s"]
    # content below a bare dash's `|` dedents to its own first line
    assert mod._mini_yaml("-\n  |\n a\n") == ["a\n"]

def test_script_ref_separator_and_extensionless():
    """Invocation prefixes allow ANY amount of whitespace before
    scripts/ (`uv  run`, `sh\\t`), and extension-less names count too —
    but prose `top/scripts/x` still doesn't invoke."""
    mod = load_resolve_module()
    assert mod.SCRIPT_REF.search(b"uv  run scripts/setup.py")
    assert mod.SCRIPT_REF.search(b"sh\tscripts/setup.sh")
    assert mod.SCRIPT_REF.search(b"env bash scripts/setup.sh")
    assert mod.SCRIPT_REF.search(b"pipenv run scripts/setup.py")
    assert mod.SCRIPT_REF.search(b"./scripts/setup.sh")
    assert mod.SCRIPT_REF.search(b"source ./scripts/setup.sh")
    assert not mod.SCRIPT_REF.search(b"top/scripts/setup.sh")
    # extension-less scripts/ names are invocable too
    body = (b"---\nconsumer_scripts: [helper]\n---\n"
            b"source scripts/helper\n")
    assert mod.SCRIPT_REF.search(body)
    n = mod.SCRIPT_NAME.search(mod.SCRIPT_REF.search(body).group(0))
    assert n.group(1) == b"helper"

def test_script_dep_block_dot_slash_declared(tmp_path):
    """`consumer_scripts: [./scripts/x]` declares `x` — a bundled script
    stays a dep, an undeclared unbundled name still gates."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    # unbundled + declared via ./scripts/ → not a dep
    body = (b"---\nconsumer_scripts: [./scripts/helper.sh]\n---\n"
            b"bash scripts/helper.sh\n")
    assert not mod.script_dep_block(pdir, body)
    # same name declared WITHOUT the ./ prefix also counts
    body2 = (b"---\nconsumer_scripts: [helper.sh]\n---\n"
             b"bash scripts/helper.sh\n")
    assert not mod.script_dep_block(pdir, body2)
    # unbundled + NOT declared → dep
    assert mod.script_dep_block(pdir, b"bash scripts/other.sh\n")
    # bundled → dep regardless of declaration
    (pdir / "scripts" / "helper.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, body)

def test_secure_dir_fd_refuses_symlink_ancestors(tmp_path):
    """secure_dir_fd must raise when ANY path component is a symlink —
    a swapped-in link cannot redirect the write outside the tree."""
    mod = load_resolve_module()
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    fd = mod.secure_dir_fd(root, "a/b")
    os.close(fd)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "a" / "b").rmdir()
    (root / "a" / "b").symlink_to(outside)
    with pytest.raises(OSError):
        mod.secure_dir_fd(root, "a/b")
    (root / "a" / "b").unlink()
    (root / "a" / "b").mkdir()
    fd = mod.secure_dir_fd(root, "a/b")
    # writes through the fd land in the real dir even if a later level
    # is swapped — verified via atomic_replace's dfd path
    mod.atomic_replace(
        root / "a" / "b" / "out.md", lambda f: f.write(b"payload"),
        mode=0o600, dfd=fd)
    os.close(fd)
    assert (root / "a" / "b" / "out.md").read_bytes() == b"payload"
    assert not (root / "a" / "b" / "out.md").is_symlink()

def test_dirfd_apply_writes_through_planted_parent_symlink(tmp_path):
    """An --apply that prefetched a dir_fd must write beneath the real
    directory even if the parent path is swapped for a symlink between
    planning and apply."""
    load_resolve_module()
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    assert dst.is_file() and not dst.is_symlink()

def test_mini_yaml_dquote_backslash_escape_fold():
    """In a double-quoted scalar, an ODD trailing '\\' run escapes the line
    break itself — join with NO separator. An even run is an escaped
    backslash + an ordinary space fold. Single-quoted '\\' is literal."""
    mod = load_resolve_module()
    assert mod._mini_yaml('x: "a\\\n  b"\n') == {"x": "ab"}
    assert mod._mini_yaml('x: "platform/\\\n  framework"\n') == \
        {"x": "platform/framework"}
    assert mod._mini_yaml('x: "a\\\n\n  b"\n') == {"x": "a\nb"}
    assert mod._mini_yaml('x: "a \\\n  b"\n') == {"x": "a b"}
    assert mod._mini_yaml('x: "a\\\\\n  b"\n') == {"x": "a\\ b"}
    assert mod._mini_yaml("x: 'a\\\n  b'\n") == {"x": "a\\ b"}

def test_mini_yaml_commented_key_lone_block_indicator():
    """`key: # note` strips to `key: ` — a lone `|`/`>` on the next line
    still opens a block scalar."""
    mod = load_resolve_module()
    assert mod._mini_yaml("ref: # pin\n  |\n    1.2\n") == {"ref": "1.2\n"}
    assert mod._mini_yaml("- key: # c\n   |\n    a\n") == [{"key": "a\n"}]
    assert mod._mini_yaml("- # c\n  |\n   a\n") == ["a\n"]

def test_mini_yaml_quoted_seq_item_with_colon():
    """A quoted scalar containing ': ' inside a seq item is a string, not
    a key — the plain-key alternative must not start with a quote char."""
    mod = load_resolve_module()
    assert mod._mini_yaml("- 'setup: done'\n- other\n") == \
        ["setup: done", "other"]
    assert mod._mini_yaml("examples:\n  - 'setup: done'\n") == \
        {"examples": ["setup: done"]}
    # a genuinely quoted KEY still parses
    assert mod._mini_yaml("- 'key': v\n") == [{"key": "v"}]

def test_mini_yaml_inline_doc_start_node():
    """`--- <node>` — the root node may share the marker line."""
    mod = load_resolve_module()
    assert mod._mini_yaml("--- {a: 1}\n") == {"a": 1}
    assert mod._mini_yaml("--- # c\n{a: 1}\n") == {"a": 1}
    mod._mini_yaml("--- \n")  # bare marker with empty doc tolerated
    with pytest.raises(ValueError):
        mod._mini_yaml("--- - 1\n")
    with pytest.raises(ValueError):
        mod._mini_yaml("--- {a: 1}\n--- {b: 2}\n")

def test_script_dep_block_python_dash_m(tmp_path):
    """`python -m scripts.check` invokes the same file — a bundled module
    is an unsatisfiable dep; a consumer-declared one materialises."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/plugm": [
            ("commands/run.md",
             "run `python3 -m scripts.check` to verify"),
            ("scripts/check.py", "print(1)"),
        ],
        "platform/plugm2": [
            ("commands/run2.md",
             "run `python3 -m scripts.check` to verify"),
        ],
        "platform/plugm3": [
            ("commands/run3.md",
             "---\nconsumer_scripts: [scripts/check.py]\n---\n"
             "run `python3 -m scripts.check` to verify"),
        ],
        "platform/plugm4": [
            ("commands/run4.md",
             "run `python3 -m scripts.pkg.check` to verify"),
            ("scripts/pkg/check/__init__.py", ""),
        ],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(
        consumer, "manolii",
        [{"plugin": "platform/plugm", "ref": "1.0.0"},
         {"plugin": "platform/plugm2", "ref": "1.0.0"},
         {"plugin": "platform/plugm3", "ref": "1.0.0"},
         {"plugin": "platform/plugm4", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    cmds = consumer / ".claude" / "commands"
    installed = {p.name for p in cmds.glob("*.md")}
    assert "run3.md" in installed
    assert not (installed & {"run.md", "run2.md", "run4.md"})

def test_script_dep_block_mixed_invocation(tmp_path):
    """`python -m scripts.check scripts/extra.py` has TWO deps — the
    bundled module still blocks even when the later slash-path is
    declared."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    # the module dep is unbundled but undeclared -> blocks
    body = (b"---\nconsumer_scripts: [scripts/extra.py]\n---\n"
            b"python -m scripts.check scripts/extra.py\n")
    assert mod.script_dep_block(pdir, body)
    # declaring the module too -> allowed
    body2 = (b"---\nconsumer_scripts: [scripts/check.py,"
             b" scripts/extra.py]\n---\n"
             b"python -m scripts.check scripts/extra.py\n")
    assert not mod.script_dep_block(pdir, body2)
    # bundle the MODULE -> blocked even though extra.py is declared
    (pdir / "scripts" / "check.py").write_bytes(b"x")
    assert mod.script_dep_block(pdir, body2)

def test_script_dep_block_dash_m_hyphenated_module(tmp_path):
    """`python -m scripts-tools` is a DIFFERENT module argument, not bare
    `-m scripts` — it must not pin a dep on scripts/__main__.py
    (Codex/Devin on bcp-core#1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert not mod.script_dep_block(pdir, b"python -m scripts-tools\n")
    # bare `python -m scripts` still gates on scripts/__main__.py
    assert mod.script_dep_block(pdir, b"python -m scripts\n")
    (pdir / "scripts").mkdir()
    (pdir / "scripts" / "__main__.py").write_bytes(b"x")
    # bundled dep -> blocks; hyphenated still free
    assert not mod.script_dep_block(pdir, b"python -m scripts-tools\n")

def test_script_dep_exec_capable_program_heads(tmp_path):
    """sed's `e` command and awk's system()/cmd|getline execute text inside
    their program arguments — grouped with pure-output heads, `sed '1e
    bash scripts/x.sh'` read as inert (Codex + Devin on vendored
    review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"sed '1e bash scripts/x.sh' f\n")
    assert mod.script_dep_block(
        pdir, b"awk 'BEGIN{system(\"bash scripts/x.sh\")}' f\n")
    assert mod.script_dep_block(
        pdir, b"sed -n -e 'p' -e '1e bash scripts/x.sh' f\n")
    # inert heads stay literal
    assert not mod.script_dep_block(pdir, b'echo "bash scripts/x.sh"\n')
    assert not mod.script_dep_block(pdir, b"grep -l bash scripts/x.sh\n")

def test_script_dep_command_substitution_executes(tmp_path):
    """`$(...)` runs BEFORE the outer command head — `echo "$(bash x)"`
    executes x even though echo only prints. Single-quoted or escaped
    `$(` stays literal (Codex on vendored review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"echo $(bash scripts/x.sh)\n")
    assert mod.script_dep_block(pdir, b'echo "$(bash scripts/x.sh)"\n')
    assert mod.script_dep_block(pdir,
                                b'echo "$(x"$(bash scripts/x.sh)")"\n')
    assert not mod.script_dep_block(
        pdir, b"echo '$(bash scripts/x.sh)'\n")
    assert not mod.script_dep_block(
        pdir, b'echo "\\$(bash scripts/x.sh)"\n')

def test_script_dep_quoted_separator_is_literal(tmp_path):
    """A quoted `;`/`|`/`&` is not a command boundary — `echo "note; bash
    x"` stays an echo. A backward _command_start scan cannot tell an
    opening from a closing quote and split mid-string, turning the head
    into `bash` (Codex on vendored review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert not mod.script_dep_block(
        pdir, b'echo "note; bash scripts/x.sh"\n')
    assert not mod.script_dep_block(
        pdir, b'echo "a|b; bash scripts/x.sh"\n')
    # real separators still split
    assert mod.script_dep_block(pdir, b"echo ok; bash scripts/x.sh\n")

def test_script_dep_python_module_after_options(tmp_path):
    """`python -u -m scripts.check` and `python -X dev -m scripts.check`
    run the module — only `python -m` matched before (Devin on
    impaktful_3.0#1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"python -u -m scripts.check\n")
    assert mod.script_dep_block(pdir, b"python -B -m scripts.check\n")
    assert mod.script_dep_block(
        pdir, b"python -X dev -m scripts.check\n")
    assert mod.script_dep_block(
        pdir, b'python -X "dev mode" -m scripts.check\n')
    assert mod.script_dep_block(
        pdir, b"python3 -B -u -m scripts.check\n")
    # `-m` after a program operand is argv, not a module flag
    assert not mod.script_dep_block(
        pdir, b"python foo.py -m scripts.check\n")

def test_script_dep_word_concatenation(tmp_path):
    """A scripts/ word concatenated with a quoted suffix is a DIFFERENT
    argument — `scripts'-tools'` unquotes to `scripts-tools`, never the
    module `scripts` (Codex on vendored review). Quoted whole paths
    still invoke."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"bash 'scripts/x.sh'\n")
    assert mod.script_dep_block(pdir, b'bash "scripts/x.sh"\n')
    assert not mod.script_dep_block(pdir, b"python -m scripts'-tools'\n")
    assert not mod.script_dep_block(pdir, b"bash scripts/x.sh' more'\n")
    # Any dotted extension is invocable — bash runs `x.sh.bak` if it is
    # bundled (the extension allowlist was dropped on Codex's #123
    # finding), so the invocation is a dep like any other script call.
    assert mod.script_dep_block(pdir, b"bash scripts/x.sh.bak\n")

def test_yaml_load_strips_bom(tmp_path):
    """A UTF-8 BOM is a signature, not content — PyYAML skips it; leaving
    it would corrupt the first key for the mini parser (Codex on
    vendored review)."""
    mod = load_resolve_module()
    doc = mod._yaml_load("\ufeffversion: 1\nuniverse: manolii\n")
    assert doc == {"version": 1, "universe": "manolii"}

def test_prune_restores_when_lock_write_fails(tmp_path):
    """A failed apply must restore a staged prune by RENAME — the old
    byte-rewrite rollback needed free space and could clobber a path
    recreated after staging (Codex + Devin on vendored review)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")]})
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    orphan = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    assert orphan.is_file()
    write_manifest(consumer, "manolii", [])
    # Break only the lock write: .ai becomes non-writable.
    (consumer / ".ai").chmod(0o555)
    try:
        r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    finally:
        (consumer / ".ai").chmod(0o755)
    assert r.returncode == 2
    assert orphan.is_file()          # renamed back, not byte-restored
    assert not list(orphan.parent.glob(".ai-prune-*"))

def test_legacy_null_digest_lock_fails_closed(tmp_path):
    """A v1 lock entry with no digest cannot attribute byte/mode drift —
    planning fails closed (conflict) rather than scheduling a repair it
    cannot verify. The apply-time snapshot ALSO compares against the
    plan's recorded digest, so a write that does get scheduled for a
    null-digest entry can't be blind-allowed either (Devin on
    vendored-resolver review)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/run.sh",
                                "#!/bin/sh\ntrue\n")]})
    reg_sh = (reg_root / "registry" / "platform" / "framework"
              / "skills" / "demo" / "run.sh")
    reg_sh.chmod(0o755)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "run.sh"
    assert dst.stat().st_mode & 0o777 == 0o755
    # Simulate a v1 lock: digest-less entry.
    lockf = consumer / ".ai" / "capability-lock.json"
    lock = json.loads(lockf.read_text())
    rel = ".claude/skills/demo/run.sh"
    assert rel in lock["files"]
    lock["files"][rel] = None
    lockf.write_text(json.dumps(lock))
    # Mode drift with no digest to attribute it -> conflict, not repair.
    dst.chmod(0o644)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "refusing to clobber" in r.stdout
    assert dst.stat().st_mode & 0o777 == 0o644

def test_script_dep_block_literal_text_not_invocation(tmp_path):
    """An interpreter+path inside text that only PRINTS (echo/printf/cat,
    quoted or not) or inside a `#` comment is documentation, not an
    invocation — it must not suppress the capability (Devin on
    impaktful_3.0#1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert not mod.script_dep_block(
        pdir, b'echo "bash scripts/demo.sh"\n')
    assert not mod.script_dep_block(
        pdir, b"echo bash scripts/demo.sh\n")
    assert not mod.script_dep_block(
        pdir, b'printf "%s\\n" "python scripts/x.py"\n')
    assert not mod.script_dep_block(
        pdir, b'# run bash scripts/demo.sh first\n')
    assert not mod.script_dep_block(
        pdir, b'grep -l bash scripts/x.sh\n')
    # Executing contexts still gate: -c strings and eval DO run.
    assert mod.script_dep_block(
        pdir, b"sh -c 'bash scripts/demo.sh'\n")
    assert mod.script_dep_block(
        pdir, b'eval "bash scripts/demo.sh"\n')
    assert mod.script_dep_block(
        pdir, b'bash scripts/demo.sh  # note\n')

def test_stale_skip_recheck(tmp_path):
    """A skipped (identical) destination whose bytes/mode drifted between
    planning and the lock write must be flagged — its digest would
    otherwise enter the lock stale (Devin on impaktful_3.0#1953)."""
    import hashlib as _hl
    mod = load_resolve_module()
    root = tmp_path
    dst = root / ".claude" / "agents" / "a.md"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"same")
    plan = mod.Plan(
        skips=[(dst, "identical")],
        resolved=[{"files": {".claude/agents/a.md":
                             _hl.sha256(b"same").hexdigest()},
                   "exec": {".claude/agents/a.md": 0o644}}])
    def ok_snap(p):
        return (b"same", 0o644, (1, 2))
    assert mod._stale_skip(root, plan, ok_snap) is None
    # changed bytes -> stale
    def bad_snap(p):
        return (b"changed", 0o644, (1, 2))
    assert mod._stale_skip(
        root, plan, bad_snap) == ".claude/agents/a.md"
    # vanished -> stale
    def gone_snap(p):
        return None
    assert mod._stale_skip(
        root, plan, gone_snap) == ".claude/agents/a.md"
    # mode drift -> stale
    def mode_snap(p):
        return (b"same", 0o755, (1, 2))
    assert mod._stale_skip(
        root, plan, mode_snap) == ".claude/agents/a.md"
    # and the reverse: bundled path + declared module still blocks
    pdir2 = tmp_path / "plug2"
    (pdir2 / "scripts").mkdir(parents=True)
    (pdir2 / "scripts" / "extra.py").write_bytes(b"x")
    body3 = (b"---\nconsumer_scripts: [scripts/check.py]\n---\n"
             b"python -m scripts.check scripts/extra.py\n")
    assert mod.script_dep_block(pdir2, body3)

def test_script_dep_block_module_main_py(tmp_path):
    """`python -m scripts.pkg` runs pkg/__main__.py (or pkg.py) — an
    __init__.py declaration alone does NOT satisfy the invocation."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    body_init = (b"---\nconsumer_scripts: [scripts/pkg/__init__.py]\n---\n"
                 b"python -m scripts.pkg\n")
    assert mod.script_dep_block(pdir, body_init)
    body_main = (b"---\nconsumer_scripts: [scripts/pkg/__main__.py]\n---\n"
                 b"python -m scripts.pkg\n")
    assert not mod.script_dep_block(pdir, body_main)
    body_mod = (b"---\nconsumer_scripts: [scripts/pkg.py]\n---\n"
                b"python -m scripts.pkg\n")
    assert not mod.script_dep_block(pdir, body_mod)
    # a bundled __init__.py is NOT an entry point — a consumer-declared
    # __main__.py still satisfies `python -m pkg` (Codex on #1344)
    (pdir / "scripts" / "pkg").mkdir()
    (pdir / "scripts" / "pkg" / "__init__.py").write_bytes(b"")
    assert not mod.script_dep_block(pdir, body_main)
    # but a bundled __main__.py is a real bundled dep — still blocks
    (pdir / "scripts" / "pkg" / "__main__.py").write_bytes(b"")
    assert mod.script_dep_block(pdir, body_main)

def test_mini_yaml_doc_start_mapping_rejected():
    """`--- key: v` — a block mapping entry may not share the marker
    line; PyYAML raises 'mapping values are not allowed here'."""
    mod = load_resolve_module()
    for bad in ("--- version: 1\nuniverse: manolii\n",
                "--- x:\n", "--- 'k': v\n", "--- -\n",
                "--- 'k: v'\n", "--- a\n"):
        try:
            mod._mini_yaml(bad)
            raise AssertionError(f"{bad!r} must raise")
        except ValueError:
            pass
    # legal remainders keep working: a flow collection or block-scalar
    # indicator is a COMPLETE node; bare scalars stay unsupported
    # (fail-closed — the supported subset never had a scalar root)
    assert mod._mini_yaml("--- {a: 1}\n") == {"a": 1}
    assert mod._mini_yaml("--- [a]\n") == ["a"]
    assert mod._mini_yaml("--- |\n  x\n") == "x\n"

def test_mini_yaml_plain_key_charset():
    """Plain keys may contain '/', '+', '=' etc. — in flow maps and in
    block mappings alike (PyYAML accepts them)."""
    mod = load_resolve_module()
    assert mod._mini_yaml(
        "feature_flags: {rollout/phase: true, a+b: 2}") == {
            "feature_flags": {"rollout/phase": True, "a+b": 2}}
    assert mod._mini_yaml("rollout/phase: true\n") == {
        "rollout/phase": True}
    assert mod._mini_yaml("x: {a:b: v}") == {"x": {"a:b": "v"}}

def test_apply_rollback_on_lock_failure(tmp_path):
    """If the lock write fails after outputs were materialised, the
    resolver must restore the prior state — files written this run are
    removed and pre-existing tracked files get their old bytes back.
    An unrolled-back output would be adopted WITHOUT provenance next
    run and conflict later on files the resolver itself wrote."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    assert skill.read_text().endswith("v1")
    # registry drifts to v2; break the lock path so apply fails at the
    # lock write after rewriting SKILL.md
    (reg_root / "registry" / "platform" / "framework" / "skills" / "demo"
     / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\n---\nv2")
    ai_dir = consumer / ".ai"
    os.chmod(ai_dir, 0o555)
    try:
        r = run_resolver(m, reg_root, consumer, "--apply")
    finally:
        os.chmod(ai_dir, 0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    # rolled back: v1 content restored, not half-applied v2
    assert skill.read_text().endswith("v1"), skill.read_text()

def test_apply_rollback_removes_new_outputs(tmp_path):
    """A FIRST apply that fails at the lock write must leave no
    materialised outputs behind."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1"),
                               ("commands/run.md", "run")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    # A git checkout routes the apply mutex under .git — without it the
    # mutex lands in the read-only .ai and the resolver refuses at rc=1
    # before materialising anything, never exercising the rollback path
    # this test exists to verify (CodeRabbit on #9, round-9 review).
    subprocess.run(["git", "-C", str(consumer), "init", "-q"],
                   check=True)
    ai_dir = consumer / ".ai"
    ai_dir.mkdir()
    os.chmod(ai_dir, 0o555)
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    try:
        r = run_resolver(m, reg_root, consumer, "--apply")
    finally:
        os.chmod(ai_dir, 0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (consumer / ".claude" / "skills" / "demo"
                / "SKILL.md").exists()
    assert not (consumer / ".claude" / "commands" / "run.md").exists()

def test_apply_rollback_restores_file_timestamps(tmp_path):
    """Rollback must put back the prior file's mtimes, not a fresh
    `now` — a resolver-owned file restored byte-identically should be
    indistinguishable from the pre-failure state (Codex on #123)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    times = (1_600_000_000_000_000_000, 1_500_000_000_000_000_000)
    os.utime(skill, ns=times)
    # registry drift triggers a replace; lock write fails -> rollback
    reg_file = (reg_root / "registry" / "platform" / "framework"
                / "skills" / "demo" / "SKILL.md")
    reg_file.write_text("---\nname: demo\ndescription: d\n---\nv2")
    os.chmod(consumer / ".ai", 0o555)
    try:
        r = run_resolver(m, reg_root, consumer, "--apply")
    finally:
        os.chmod(consumer / ".ai", 0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    assert skill.read_bytes() == b"---\nname: demo\ndescription: d\n---\nv1"
    # mtime survives the round-trip. atime is NOT asserted — the resolver
    # reads the file during planning, which refreshes it before the
    # snapshot is taken, so the recorded atime is legitimately ~now.
    assert skill.stat().st_mtime_ns == times[1]

def test_apply_restores_deleted_installed_file(tmp_path):
    """An installed file the consumer deleted is a planned RESTORE, not
    a 'vanished destination' — apply must put it back, not refuse
    forever (Devin on #123)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    skill.unlink()
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert skill.read_bytes() == b"---\nname: demo\ndescription: d\n---\nv1"

def test_mode_drift_does_not_hide_local_edit(tmp_path):
    """Install A@0644, hand-edit the consumer copy to B@0644, then move the
    registry to B@0600 — the local edit must still CONFLICT, not be
    rewritten + re-provenanced as resolver-owned. Mode drift
    (mode_consistent False) must not bypass the modified-since-install
    guard."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    os.chmod(skill, 0o644)
    skill.write_text("---\nname: demo\ndescription: d\n---\nv2")
    # registry carries the SAME new bytes but at a different mode
    reg_file = (reg_root / "registry" / "platform" / "framework"
                / "skills" / "demo" / "SKILL.md")
    reg_file.write_text("---\nname: demo\ndescription: d\n---\nv2")
    os.chmod(reg_file, 0o600)
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "modified since install" in r.stdout
    # the lock must NOT claim ownership of the local edit
    lock = json.loads((consumer / ".ai" / "capability-lock.json")
                      .read_text())
    assert lock["files"][".claude/skills/demo/SKILL.md"] != \
        hashlib.sha256(skill.read_bytes()).hexdigest()

def test_mini_yaml_next_line_collections():
    """`key:` empty followed by a deeper flow collection or quoted
    scalar — the node IS the value (Devin Review): `requires:` and
    `surfaces:` written this way must resolve, not come back as a
    string."""
    mod = load_resolve_module()
    doc = ('version: 1\nuniverse: manolii\nrequires:\n'
           '  [{plugin: platform/framework, ref: "1.0.0"}]\n')
    assert mod._mini_yaml(doc) == {
        "version": 1, "universe": "manolii",
        "requires": [{"plugin": "platform/framework", "ref": "1.0.0"}]}
    assert mod._mini_yaml('surfaces:\n  [claude-code]\n') == {
        "surfaces": ["claude-code"]}
    assert mod._mini_yaml('ref:\n  "1.0.0"\n') == {"ref": "1.0.0"}
    assert mod._mini_yaml('ref:\n  \'v\'\n') == {"ref": "v"}
    assert mod._mini_yaml('x:\n  {a: 1}\n') == {"x": {"a": 1}}
    # a deeper stray line after the collection still rejects
    try:
        mod._mini_yaml('x:\n  [a]\n  junk\n')
        raise AssertionError("nested structure must raise")
    except ValueError:
        pass
    # plain folded scalars still fold
    assert mod._mini_yaml('x:\n  a\n  b\n') == {"x": "a b"}
    assert mod._mini_yaml('x:\n  a\n  b\n') == {"x": "a b"}

def test_script_dep_block_second_arg(tmp_path):
    """`bash scripts/first.sh scripts/second.sh` — the regex match ends at
    first.sh but second.sh is just as much a dependency: bundled it must
    block, undeclared it must block (Devin Review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    body = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
            b"bash scripts/first.sh scripts/second.sh\n")
    # second.sh bundled -> block even though first.sh is declared
    (pdir / "scripts" / "second.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, body)
    # second.sh unbundled + undeclared -> block
    (pdir / "scripts" / "second.sh").unlink()
    assert mod.script_dep_block(pdir, body)
    # both declared -> allowed
    body2 = (b"---\nconsumer_scripts: [scripts/first.sh,"
             b" scripts/second.sh]\n---\n"
             b"bash scripts/first.sh scripts/second.sh\n")
    assert not mod.script_dep_block(pdir, body2)
    # a scripts/ path inside a trailing shell comment is NOT a dep
    body3 = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
             b"bash scripts/first.sh # see scripts/notes.sh\n")
    assert not mod.script_dep_block(pdir, body3)

def test_prune_dir_cleanup_after_lock_write(tmp_path):
    """--prune removes a dir's last file; if the lock write then fails,
    rollback must restore the file — so empty-dir cleanup may only run
    AFTER the lock commit (Devin Review + Codex on #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    skill = consumer / ".claude" / "skills" / "demo" / "SKILL.md"
    # drop the requirement so --prune removes the file AND its now-empty
    # dir; then break the lock write
    m.write_text("version: 1\nuniverse: manolii\nrequires: []\n")
    ai_dir = consumer / ".ai"
    os.chmod(ai_dir, 0o555)
    try:
        r = run_resolver(m, reg_root, consumer, "--apply", "--prune")
    finally:
        os.chmod(ai_dir, 0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    # rollback restored the file — its parent dir survived cleanup
    assert skill.read_text().endswith("v1")

def test_pinned_mode_normalised_to_tree(tmp_path):
    """Under a tag:/sha: pin a worktree chmod on the r/w bits (0644 ->
    0600) hides from status AND the blob compare — the install + lock
    record must carry the pinned TREE mode, not the worktree's."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "v1")],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    os.chmod(reg_root / "registry" / "platform" / "framework" / "skills"
             / "demo" / "x.md", 0o600)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    dst = consumer / ".claude" / "skills" / "demo" / "x.md"
    assert dst.stat().st_mode & 0o777 == 0o644
    lock = json.loads((consumer / ".ai" / "capability-lock.json")
                      .read_text())
    assert lock["exec"][".claude/skills/demo/x.md"] & 0o777 == 0o644

def test_mini_yaml_quoted_fold_trailing_comment():
    """A comment on the line that CLOSES a folded quoted scalar is a
    comment, not scalar text — `plugin: "platform/\n framework" # c` is
    the value 'platform/ framework' (Devin Review on #1344)."""
    mod = load_resolve_module()
    doc = 'plugin: "platform/\n    framework" # plugin name\n'
    assert mod._mini_yaml(doc) == {"plugin": "platform/ framework"}
    # a '#' inside the folded quote is literal
    doc2 = 'plugin: "a\n    b#c" x\n'
    try:
        mod._mini_yaml(doc2)
        raise AssertionError("trailing token must raise")
    except ValueError:
        pass
    assert mod._mini_yaml('plugin: "a\n    b#c" # tail\n') == {
        "plugin": "a b#c"}

def test_script_dep_block_quoted_hash(tmp_path):
    """`"a#b"`/`a#b` mid-word is NOT a shell comment — later script args
    still reach the gate (Devin Review); a real ` # comment` still ends
    the scan."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    body = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
            b'bash scripts/first.sh "a#b" scripts/second.sh\n')
    (pdir / "scripts" / "second.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, body)
    (pdir / "scripts" / "second.sh").unlink()
    assert mod.script_dep_block(pdir, body)
    # mid-word unquoted hash is not a comment either
    body2 = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
             b"bash scripts/first.sh a#b scripts/second.sh\n")
    assert mod.script_dep_block(pdir, body2)
    # a real comment still bounds the window
    body3 = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
             b"bash scripts/first.sh # scripts/notes.sh\n")
    assert not mod.script_dep_block(pdir, body3)

def test_dq_escape_space(tmp_path):
    """YAML's standard `\\ ` escape decodes to a space (PyYAML accepts it —
    Codex)."""
    import yaml
    mod = load_resolve_module()
    for s, want in [
            ('a: "Escaped\\ space"', 'Escaped space'),
            ('a: "x\\ty"', 'x\ty'),
            ('a: "\\u0041"', 'A')]:
        doc = f"{s}\n"
        assert mod._mini_yaml(doc)['a'] == want == yaml.safe_load(doc)['a']

def test_prune_type_change_fails_closed(tmp_path):
    """A prune candidate swapped for a symlink/dir after planning fails the
    apply (rollback) instead of being silently skipped while its lock entry
    drops (Codex on #1953)."""
    import json
    repo = tmp_path / "repo"
    (repo / ".claude" / "skills" / "old-skill").mkdir(parents=True)
    stale = repo / ".claude" / "skills" / "old-skill" / "SKILL.md"
    stale.write_text("stale")
    reg = tmp_path / "reg"
    (reg / "registry").mkdir(parents=True)
    lockdir = repo / ".ai"
    lockdir.mkdir()
    (lockdir / "capability-lock.json").write_text(json.dumps({
        "files": {".claude/skills/old-skill/SKILL.md":
                  {"scope": "platform", "plugin": "old-skill",
                   "sha256": "0" * 64, "exec": 0}},
        "resolved": {"platform/old-skill": {"version": "0.0.0"}}}))
    # swap the tracked file for a symlink before --apply --prune
    stale.unlink()
    stale.symlink_to(repo / ".ai" / "capability-lock.json")
    manifest = repo / "ai-manifest.yaml"
    manifest.write_text("version: 1\nuniverse: platform\nrequires: {}\n")
    out = run_resolver(manifest, reg, repo, "--apply", "--prune")
    assert out.returncode != 0
    # the symlink itself must not have been deleted or followed
    assert stale.is_symlink()
    # lock must not have been rewritten (drop would leave it untracked)
    assert json.loads((lockdir / "capability-lock.json"
                       ).read_text())["files"]

def test_mini_yaml_next_line_scalar_typed(tmp_path):
    """`key:` + next-line scalar node — the value is typed (`1` -> int,
    `on` -> bool, `{k: v}` -> map), matching PyYAML (Devin on #123)."""
    mod = load_resolve_module()
    doc = mod._mini_yaml("version:\n  1\nuniverse:\n  manolii\n"
                         "requires: []\nfeature_flags:\n  {a: 1}\n")
    assert doc == {"version": 1, "universe": "manolii",
                   "requires": [], "feature_flags": {"a": 1}}
    # A bare `-` + deeper flow map is a mapping item, not folded text.
    doc2 = mod._mini_yaml(
        "requires:\n  -\n    {plugin: platform/framework, ref: '1.0.0'}\n")
    assert doc2 == {"requires": [{"plugin": "platform/framework",
                                  "ref": "1.0.0"}]}
    # And a bare `-` + deeper plain/quoted scalar stays typed too.
    doc3 = mod._mini_yaml("items:\n  -\n    5\n  -\n    'six'\n")
    assert doc3 == {"items": [5, "six"]}

def test_line_continuation_script_dep(tmp_path):
    """`python3 \\` + `scripts/x.py` on the next line is ONE command — the
    dependency must be detected (Codex on #1953)."""
    reg = make_registry(tmp_path / "reg", {
        "platform/plugin": [(
            "commands/do.md",
            "run:\n```sh\npython3 \\\n    scripts/setup.py\n```\n")],
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = write_manifest(repo, "manolii",
                              [{"plugin": "platform/plugin", "ref": "1.0"}])
    out = run_resolver(manifest, reg, repo, "--apply")
    assert out.returncode == 0, out.stderr
    assert "not materialised" in out.stdout
    assert not (repo / ".claude" / "commands" / "do.md").exists()

def test_prune_missing_parent_dir_noop(tmp_path):
    """An orphan whose parent dir was deleted — under a read-only ancestor —
    is a no-op lock clear, never a mkdir failure (Devin on #123)."""
    import os
    import json
    repo = tmp_path / "repo"
    skills = repo / ".claude" / "skills"
    skills.mkdir(parents=True)
    reg = tmp_path / "reg"
    (reg / "registry").mkdir(parents=True)
    (reg / "registry" / "plugins.json").write_text(json.dumps(
        {"schema_version": 1, "scopes": {}, "plugins": []}))
    lockdir = repo / ".ai"
    lockdir.mkdir()
    (lockdir / "capability-lock.json").write_text(json.dumps({
        "files": {".claude/skills/old/SKILL.md": "0" * 64},
        "resolved": [{"plugin": "platform/old", "version": "0.0.0"}]}))
    manifest = repo / "ai-manifest.yaml"
    manifest.write_text("version: 1\nuniverse: platform\nrequires: []\n")
    os.chmod(skills, 0o555)  # recreation of old/ would fail here
    try:
        out = run_resolver(manifest, reg, repo, "--apply", "--prune")
    finally:
        os.chmod(skills, 0o755)
    assert out.returncode == 0, out.stderr
    assert not (skills / "old").exists()  # never resurrected
    assert json.loads((lockdir / "capability-lock.json"
                       ).read_text())["files"] == {}

def test_join_continuations_parity_and_quotes():
    """`\\<newline>` joins only where the shell keeps tokens contiguous —
    odd runs outside quotes or in dq join; even runs and everything in
    single quotes keep the backslash+newline literal (a literal backslash-newline
    inside sq is NOT a join — joining it forged invocations like
    `printf 'bash \\` + nl + `scripts/x.sh'`)."""
    mod = load_resolve_module()
    j, BS, NL = mod._join_continuations, b"\\", b"\n"
    assert j(b"python3 " + BS + NL + b"echo") == b"python3 echo"
    # even run: the last backslash is itself escaped — separate commands
    assert j(b"python3 " + BS * 2 + NL + b"echo") == (
        b"python3 " + BS * 2 + NL + b"echo")
    assert j(b"x " + BS * 3 + NL + b"y") == b"x " + BS * 2 + b"y"
    assert j(b'x "a' + BS + NL + b'b" y') == b'x "ab" y'
    # single quotes: every backslash is literal — no join
    assert j(b"x 'a" + BS + NL + b"b' y") == b"x 'a" + BS + NL + b"b' y"
    assert j(b"x " + BS + b"\r\n" + b"y") == b"x y"
    assert j(b"x " + BS + b"y") == b"x " + BS + b"y"

def test_mini_yaml_embedded_colon_plain_key():
    """`rollout:phase: true` keys on `rollout:phase` — a `:` inside a plain
    key is legal when not followed by whitespace (PyYAML-verified; Codex
    on #123)."""
    mod = load_resolve_module()
    try:
        import yaml
    except ImportError:
        yaml = None
    for doc, want in [
        ('feature_flags:\n  rollout:phase: true\n',
         {'feature_flags': {'rollout:phase': True}}),
        ('f:\n  a:b: [1, 2]\n', {'f': {'a:b': [1, 2]}}),
        ('k:v: |\n  txt\n', {'k:v': 'txt\n'}),
        ('k:v: 1\n', {'k:v': 1}),
        ('x: a:b\n', {'x': 'a:b'}),
    ]:
        assert mod._mini_yaml(doc) == want, doc
        if yaml is not None:
            assert mod._mini_yaml(doc) == yaml.safe_load(doc), doc

def test_script_dep_even_run_keeps_commands_separate(tmp_path):
    """`python3 \\\\` + `echo scripts/x.sh` are TWO commands — fusing them
    made x.sh look like a python3 dep (Codex on #1953 / Devin on #6)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    body = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
            b"python3 " + b"\\" * 2 + b"\necho scripts/x.sh\n")
    # x.sh is an echo arg, not a python dep — no block either way
    assert not mod.script_dep_block(pdir, body)
    # Sanity: a genuinely undeclared python dep still blocks
    body2 = (b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
             b"python3 " + b"\\" + b"\nscripts/x.sh\n")
    assert mod.script_dep_block(pdir, body2)

def test_script_dep_redirect_target_not_a_dep(tmp_path):
    """`cmd > scripts/out` creates the file — an output-redirect target is
    not a dependency the command reads (Devin on #1344). `<` still is."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    declared = b"---\nconsumer_scripts: [scripts/first.sh]\n---\n"
    for op in (b">", b">>", b"2>"):
        body = (declared + b"bash scripts/first.sh "
                + op + b" scripts/generated.sh\n")
        assert not mod.script_dep_block(pdir, body), op
    # input redirect still counts — that file must exist
    body = declared + b"bash scripts/first.sh < scripts/input.sh\n"
    assert mod.script_dep_block(pdir, body)

def test_script_dep_module_init_only_package(tmp_path):
    """`python -m scripts.pkg` runs pkg.py or pkg/__main__.py — a bundled
    __init__.py alone is no entry point, so a consumer-declared
    __main__.py satisfies the dep (Codex on #1344)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts" / "pkg").mkdir(parents=True)
    (pdir / "scripts" / "pkg" / "__init__.py").write_bytes(b"x = 1\n")
    body = (b"---\nconsumer_scripts: [scripts/pkg/__main__.py]\n---\n"
            b"python3 -m scripts.pkg\n")
    assert not mod.script_dep_block(pdir, body)
    # undeclared __main__.py still blocks
    body2 = (b"---\nconsumer_scripts: [scripts/other.py]\n---\n"
             b"python3 -m scripts.pkg\n")
    assert mod.script_dep_block(pdir, body2)

def test_legacy_exec_record_mode_drift_conflicts(tmp_path):
    """An untagged (legacy any-exec) exec record can't attribute r/w-bit
    drift — dst mode differing from src mode in non-exec bits must
    conflict, not pass as 'identical' or silently rewrite (Devin on
    #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/SKILL.md",
                                "---\nname: demo\ndescription: d\n---\nv1")],
    })
    # Establish a real 0644 -> 0600 drift even under a restrictive umask.
    (reg_root / "registry" / "platform" / "framework" / "skills"
     / "demo" / "SKILL.md").chmod(0o644)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    assert run_resolver(m, reg_root, consumer, "--apply").returncode == 0
    rel = ".claude/skills/demo/SKILL.md"
    lock_file = consumer / ".ai" / "capability-lock.json"
    lock = json.loads(lock_file.read_text())
    lock["exec"][rel] = False  # legacy any-exec record — no full mask
    lock_file.write_text(json.dumps(lock))
    (consumer / rel).chmod(0o600)  # r/w-bit drift a legacy record can't name
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "legacy exec record" in r.stdout
    # Same drift provable under a tagged record (exec class unchanged):
    # still refuses to clobber a local chmod — different message.
    lock["exec"][rel] = 0o10000 | 0o644
    lock_file.write_text(json.dumps(lock))
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "installed-mode record" in r.stdout

def test_prune_staging_name_collision_aborts(tmp_path):
    """A leftover `.ai-prune-<name>` sibling means the compare-and-unlink
    staging slot is taken — apply must abort, not clobber it (Codex on
    #1953)."""
    repo = tmp_path / "repo"
    skills = repo / ".claude" / "skills"
    (skills / "old").mkdir(parents=True)
    stale = skills / "old" / "SKILL.md"
    stale.write_text("stale")
    (skills / "old" / ".ai-prune-SKILL.md").write_text("leftover")
    reg = tmp_path / "reg"
    (reg / "registry").mkdir(parents=True)
    (reg / "registry" / "plugins.json").write_text(json.dumps(
        {"schema_version": 1, "scopes": {}, "plugins": []}))
    lockdir = repo / ".ai"
    lockdir.mkdir()
    lockdir.joinpath("capability-lock.json").write_text(json.dumps({
        "files": {".claude/skills/old/SKILL.md":
                  {"scope": "platform", "plugin": "old",
                   "sha256": "0" * 64, "exec": 0}},
        "resolved": [{"plugin": "platform/old", "version": "0.0.0"}]}))
    manifest = repo / "ai-manifest.yaml"
    manifest.write_text("version: 1\nuniverse: platform\nrequires: []\n")
    # digest mismatch alone would already abort — make it match first
    import hashlib
    dig = hashlib.sha256(b"stale").hexdigest()
    lock = json.loads(lockdir.joinpath("capability-lock.json").read_text())
    lock["files"][".claude/skills/old/SKILL.md"]["sha256"] = dig
    lockdir.joinpath("capability-lock.json").write_text(json.dumps(lock))
    out = run_resolver(manifest, reg, repo, "--apply", "--prune")
    assert out.returncode != 0
    assert stale.read_text() == "stale"  # restored, never unlinked
    assert (skills / "old" / ".ai-prune-SKILL.md").read_text() == "leftover"

def test_mini_yaml_signed_and_dot_scalars():
    """PyYAML numeric grammar: `+1` -> 1, `+1.5` -> 1.5, `.5`/`1.` are
    floats — but bare exponents (`-1e3`) stay strings (Codex on #123)."""
    mod = load_resolve_module()
    try:
        import yaml
    except ImportError:
        yaml = None
    for doc, want in [
        ('x: +1\n', {'x': 1}),
        ('x: +1.5\n', {'x': 1.5}),
        ('x: .5\n', {'x': 0.5}),
        ('x: 1.\n', {'x': 1.0}),
        ('x: -1e3\n', {'x': '-1e3'}),
        ('x: +1e3\n', {'x': '+1e3'}),
        # Non-decimal spellings Codex flagged on #123 — hex, binary,
        # legacy octal, underscore runs, signed exponents, sexagesimal,
        # .inf/.nan. `0o`/`0X`/`0B`/unsigned-exponent/`.Nan`/`-.nan` are
        # NOT PyYAML numbers and stay strings.
        ('x: 0x1\n', {'x': 1}),
        ('x: 0x10\n', {'x': 16}),
        ('x: +0x1\n', {'x': 1}),
        ('x: -0x10\n', {'x': -16}),
        ('x: 0b101\n', {'x': 5}),
        ('x: 012\n', {'x': 10}),
        ('x: -012\n', {'x': -10}),
        ('x: 00\n', {'x': 0}),
        ('x: 1_000\n', {'x': 1000}),
        ('x: 1_2_3\n', {'x': 123}),
        ('x: 1_0.5\n', {'x': 10.5}),
        ('x: 0.5_0\n', {'x': 0.5}),
        ('x: 1.0E+3\n', {'x': 1000.0}),
        ('x: -1.5e-2\n', {'x': -0.015}),
        ('x: 1e+3\n', {'x': '1e+3'}),  # no dot in mantissa → string
        ('x: 1:30\n', {'x': 90}),
        ('x: -1:2:3\n', {'x': -3723}),
        ('x: 08\n', {'x': '08'}),
        ('x: 09\n', {'x': '09'}),
        ('x: 0o7\n', {'x': '0o7'}),
        ('x: 0X1\n', {'x': '0X1'}),
        ('x: 0B1\n', {'x': '0B1'}),
        ('x: 1.0e3\n', {'x': '1.0e3'}),
        ('x: +1.e5\n', {'x': '+1.e5'}),
        ('x: .inf\n', {'x': float('inf')}),
        ('x: -.INF\n', {'x': -float('inf')}),
        ('x: -.nan\n', {'x': '-.nan'}),
        ('x: .Nan\n', {'x': '.Nan'}),
    ]:
        assert mod._mini_yaml(doc) == want, doc
        if yaml is not None:
            assert mod._mini_yaml(doc) == yaml.safe_load(doc), doc
    assert math.isnan(mod._mini_yaml('x: .NaN\n')['x'])
    if yaml is not None:
        assert math.isnan(yaml.safe_load('x: .NaN\n')['x'])

def test_mini_yaml_bool_case_is_pyyaml_exact():
    """PyYAML's bool/null regexes take only lower/Title/UPPER spellings —
    `tRuE`, `yEs`, `oFf`, `nUll` are STRINGS (Devin on #123)."""
    mod = load_resolve_module()
    for doc, want in [
        ('x: tRuE\n', {'x': 'tRuE'}),
        ('x: yEs\n', {'x': 'yEs'}),
        ('x: oFf\n', {'x': 'oFf'}),
        ('x: nUll\n', {'x': 'nUll'}),
        ('x: YeS\n', {'x': 'YeS'}),
        ('x: true\n', {'x': True}),
        ('x: True\n', {'x': True}),
        ('x: TRUE\n', {'x': True}),
        ('x: off\n', {'x': False}),
        ('x: Off\n', {'x': False}),
        ('x: OFF\n', {'x': False}),
        ('x: Null\n', {'x': None}),
        ('x: NULL\n', {'x': None}),
        ('x: ~\n', {'x': None}),
    ]:
        got = mod._mini_yaml(doc)
        assert got == want and type(got['x']) is type(want['x']), doc

def test_mini_yaml_sexagesimal_float_scale():
    """Sexagesimal floats scale every field but the LAST by 60^k:
    `1:20.5` = 80.5, `1:02:03.5` = 3723.5 (PyYAML; Codex on #1370)."""
    mod = load_resolve_module()
    for doc, want in [
        ('x: 1:20.5\n', 80.5),
        ('x: 1:02:03.5\n', 3723.5),
        ('x: -1:20.5\n', -80.5),
    ]:
        got = mod._mini_yaml(doc)['x']
        assert got == pytest.approx(want), doc
        try:
            import yaml as pyyaml
        except ImportError:
            pyyaml = None
        if pyyaml is not None:
            assert got == pyyaml.safe_load(doc)['x'], doc

def test_mini_yaml_rejects_embedded_mapping_value():
    """`description: foo: bar` is a ScannerError in PyYAML — a `:` + space
    inside a plain scalar is a nested mapping value, never text. The mini
    parser must fail closed, not ship 'foo: bar' as a valid value
    (Devin on #123)."""
    mod = load_resolve_module()
    for bad in ["description: foo: bar", "x: a: b", "x: foo:",
                "x: [a: b]", "x:\n  description: foo: bar"]:
        with pytest.raises(ValueError):
            mod._mini_yaml(bad + "\n")

def test_mini_yaml_block_scalar_tab_indentation():
    """A tab in block-scalar INDENTATION is a PyYAML ScannerError; a tab
    at/past the content column is literal content — including a tab-led
    FIRST content line (cursor[bot] on #1344, corrected against PyYAML)."""
    mod = load_resolve_module()
    try:
        import yaml
    except ImportError:
        yaml = None
    for doc, want in [
        ('x: |-\n  \ta\n', {'x': '\ta'}),
        ('x: |-\n  a\n  \tb\n', {'x': 'a\n\tb'}),
    ]:
        assert mod._mini_yaml(doc) == want, doc
        if yaml is not None:
            assert mod._mini_yaml(doc) == yaml.safe_load(doc), doc
    for doc in ('x: |-\n  a\n \tb\n', 'x: |-\n  a\n\tb\n'):
        with pytest.raises(ValueError):
            mod._mini_yaml(doc)
        if yaml is not None:
            with pytest.raises(Exception):
                yaml.safe_load(doc)

def test_script_dep_redirect_targets_and_bare_module(tmp_path):
    """Redirect targets that create/fill a file or take a word are not
    dependencies; input redirects (`<`, `<>`) still are — and a bare
    `python -m scripts` probes scripts/__main__.py."""
    mod = load_resolve_module()
    def dep(b):
        return mod.script_dep_block(tmp_path, b.encode())
    # output / fd / heredoc / here-string targets — created, not read
    for body in [
        "python run.py > scripts/out.py",
        "python run.py >> scripts/out.py",
        "python run.py 2> scripts/err.txt",
        "python run.py &> scripts/log.txt",
        "python run.py >| scripts/x.py",
        "python run.py >& scripts/x.py",
        "python run.py << scripts/delim.py",
        "python run.py <<- scripts/delim.py",
        "python run.py <<< scripts/word.py",
        "python run.py <& scripts/fd.py",
    ]:
        assert not dep(body), body
    # input redirects READ the file — real deps
    for body in [
        "python run.py < scripts/in.py",
        "python run.py <> scripts/rw.py",
    ]:
        assert dep(body), body
    # a redirect target then a real dep in the next pipeline stage
    assert dep("cmd > scripts/x.py | bash scripts/y.sh")
    # bare package invocation needs __main__.py: unbundled + undeclared
    # blocks, bundled blocks (scripts/ is never materialised), declared
    # consumer-side materialises
    assert dep("python -m scripts")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "__main__.py").write_text("x=1")
    assert dep("python -m scripts")
    shutil.rmtree(tmp_path / "scripts")
    body = (b"---\nconsumer_scripts: [scripts/__main__.py]\n---\n"
            b"python -m scripts\n")
    assert not mod.script_dep_block(tmp_path, body)

def test_script_dep_extensionless_option_values(tmp_path):
    """An extensionless scripts/ path after a long option or `=` is an
    argument value (`--directory scripts/site`), not an invocation;
    after a bare `--` or a short flag it still gates (Codex on #1344)."""
    mod = load_resolve_module()
    def dep(b):
        return mod.script_dep_block(tmp_path, b.encode())
    assert not dep("python -m http.server --directory scripts/site")
    assert not dep("python -m http.server --directory=scripts/site")
    assert not dep("python -m http.server -d scripts/site")
    assert not dep("python -m http.server -dscripts/site")
    assert not dep("python run.py --input=scripts/fixtures")
    # still gated: positional after `--`, short flag, plain arg
    assert dep("bash -- scripts/run")
    assert dep("bash -x scripts/run")
    assert dep("bash opts scripts/run")
    # An env-prefix `VAR=scripts/x` is a mention, not an invocation — the
    # interpreter's args carry no scripts/ path; a real dep declares
    # requires_scripts/consumer_scripts in frontmatter.
    assert not dep("DATA=scripts/fixtures bash run.sh")
    # extended names count even after a long option — fail-closed
    assert dep("python run.py --out scripts/out.py")
    assert dep("python run.py --input scripts/data.py")

def test_script_dep_sq_continuation_is_not_a_join(tmp_path):
    """`printf 'bash \\<nl>scripts/x.sh'` prints text — a literal
    backslash-newline inside single quotes is NOT a line continuation,
    so it must not register a dependency (cursor[bot] on #1344)."""
    mod = load_resolve_module()
    assert not mod.script_dep_block(
        tmp_path, b"printf 'bash \\\nscripts/missing.sh'\n")

def test_apply_aborts_on_symlink_destination(tmp_path):
    """A dst that is a link/non-regular at apply time must abort, not be
    journaled 'absent' and written over (Codex on #1953)."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    dst = consumer / ".claude" / "skills" / "a" / "SKILL.md"
    dst.parent.mkdir(parents=True)
    dst.symlink_to("/nonexistent-target")
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode != 0  # plan conflict or apply abort — fail-closed
    assert dst.is_symlink()  # untouched — no clobber, no unlink

def test_apply_rollback_only_unlinks_its_own_inode(tmp_path, monkeypatch):
    """Rollback must delete the inode the write created — a path swapped
    to a different file before rollback is left alone (Codex on #1953)."""
    import errno
    mod = load_resolve_module()
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx"),
                       ("skills/b/SKILL.md",
                        "---\nname: b\ndescription: d\n---\ny")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    monkeypatch.setattr(sys, "argv", [
        str(RESOLVE), "--manifest", str(m), "--registry", str(reg_root),
        "--repo-root", str(consumer), "--apply"])
    real = mod.atomic_replace
    swap_src = tmp_path / "swapped"
    swap_src.write_text("SWAPPED")
    state = {"first": None}

    def fake(dst, fill, times=None, mode=None, dfd=None):
        if state["first"] is None:
            state["first"] = dst
            return real(dst, fill, times=times, mode=mode, dfd=dfd)
        # Concurrent edit simulation: the first written path is now a
        # DIFFERENT inode, then this write fails and unwinds.
        os.replace(swap_src, state["first"])
        raise OSError(errno.ENOMEM, "simulated write failure")

    monkeypatch.setattr(mod, "atomic_replace", fake)
    assert mod.main() == 2
    assert state["first"] is not None
    # Rollback left the swapped file alone — it is not ours to delete.
    assert state["first"].read_text() == "SWAPPED"

def test_prune_restores_after_staged_read_failure(tmp_path, monkeypatch):
    """A failed read of the staged `.ai-prune-*` name must put the file
    back, not strand it under the staging name (Devin on #6/#1344)."""
    import errno
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stderr
    f = consumer / ".claude" / "skills" / "a" / "SKILL.md"
    assert f.is_file()
    # Drop the requirement → f becomes a prune candidate.
    m.write_text("version: 1\nuniverse: buro\nrequires: []\n")
    mod = load_resolve_module()
    monkeypatch.setattr(sys, "argv", [
        str(RESOLVE), "--manifest", str(m), "--registry", str(reg_root),
        "--repo-root", str(consumer), "--apply", "--prune"])
    real_open = os.open

    def fake_open(path, flags, *a, **kw):
        if str(path).startswith(".ai-prune-"):
            raise OSError(errno.EACCES, "denied")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(mod.os, "open", fake_open)
    assert mod.main() == 2
    assert f.read_text() == "---\nname: a\ndescription: d\n---\nx"
    assert not list(f.parent.glob(".ai-prune-*"))

def test_script_dep_inner_substitution_head(tmp_path):
    """The head INSIDE `$(...)` decides, not the outer command —
    `$(echo bash x)` only prints, `$(bash x)` runs (Devin on
    #123/#6/#1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    # inner non-executing heads -> literal, not a dep
    assert not mod.script_dep_block(
        pdir, b'x="$(echo bash scripts/x.sh)"\n')
    assert not mod.script_dep_block(
        pdir, b"x=$(printf 'bash scripts/x.sh')\n")
    # inner EXECUTING head still gates
    assert mod.script_dep_block(
        pdir, b'x="$(bash scripts/x.sh)"\n')
    # nesting descends to the innermost command
    assert mod.script_dep_block(
        pdir, b'x="$(echo $(bash scripts/x.sh))"\n')
    assert not mod.script_dep_block(
        pdir, b'x="$(bash $(echo scripts/x.sh))"\n'
        .replace(b"scripts/x.sh", b'"$(echo ok)"'))

def test_script_dep_pipe_to_executor(tmp_path):
    """A non-executing head piped into an interpreter is not inert —
    `printf 'bash x' | sh` runs the text (Devin on #1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | sh\n")
    assert mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' |& sh\n")
    # harmless downstreams stay literal
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | wc -l\n")
    # `||` is a fallback, not a pipe to exec
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' || true\n")

def test_script_dep_heredoc_body(tmp_path):
    """Heredoc bodies are stdin data unless the head (or a pipe
    consumer) executes them (Devin on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    inert = b"cat <<EOF\nbash scripts/x.sh\nEOF\n"
    assert not mod.script_dep_block(pdir, inert)
    assert not mod.script_dep_block(
        pdir, b"cat <<- EOF\n\tbash scripts/x.sh\n\tEOF\n")
    # exec head / pipe-to-exec still gates
    assert mod.script_dep_block(
        pdir, b"sh <<EOF\nbash scripts/x.sh\nEOF\n")
    assert mod.script_dep_block(
        pdir, b"cat <<EOF | sh\nbash scripts/x.sh\nEOF\n")
    # quoted delimiter disables expansion -> stays literal
    assert not mod.script_dep_block(
        pdir, b"cat <<'EOF'\n$(bash scripts/x.sh)\nEOF\n")
    # unquoted body still expands $( ) — the substitution's own head runs
    assert mod.script_dep_block(
        pdir, b"cat <<EOF\n$(bash scripts/x.sh)\nEOF\n")
    # ...but non-expanding text in that same body stays inert
    assert not mod.script_dep_block(
        pdir, b"cat <<EOF\n$(date)\nbash scripts/x.sh\nEOF\n")

def test_script_dep_word_member_forms(tmp_path):
    """Glued short-option operands, $PWD-prefixed paths and input
    redirects still invoke the script (Codex + Devin on #1370)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    pdir.mkdir()
    assert mod.script_dep_block(pdir, b"node -rscripts/preload.js\n")
    assert mod.script_dep_block(pdir, b"python $PWD/scripts/tool.py\n")
    assert mod.script_dep_block(pdir, b"python ${PWD}/scripts/tool.py\n")
    assert mod.script_dep_block(pdir, b"python <scripts/tool.py\n")
    # the -d directory-operand suppression still holds
    assert not mod.script_dep_block(pdir, b"node -dscripts/site\n")
    # bare-prefix concatenation still rejected
    assert not mod.script_dep_block(pdir, b"bash xscripts/tool.py\n")

def test_mini_yaml_inline_comment_ends_scalar(tmp_path):
    """`k: v # c` followed by a deeper line is a PyYAML error — the
    comment ends the scalar; folding a continuation across it must
    fail closed (Devin on #123)."""
    mod = load_resolve_module()
    try:
        mod._mini_yaml("key: safe # note\n  continuation\n")
        raise AssertionError("must raise")
    except ValueError:
        pass
    # normal inline comments still parse fine
    assert mod._mini_yaml("k: v # note\n") == {"k": "v"}
    assert mod._mini_yaml("k: v # note\nother: x\n") == {
        "k": "v", "other": "x"}
    # a deeper line after a NON-commented scalar still folds
    assert mod._mini_yaml("k: v\n  more\n") == {"k": "v more"}

def test_prune_staged_unlink_uses_dir_fd(tmp_path, monkeypatch):
    """Staged prune files are deleted through the verified dir FD while
    it is still open — a parent swapped to a symlink after staging
    cannot redirect the delete (Codex on #1953)."""
    mod = load_resolve_module()
    if not getattr(mod, "_HAS_DIRFD", False):
        pytest.skip("dirfd platform only")
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stderr
    m.write_text("version: 1\nuniverse: buro\nrequires: []\n")
    monkeypatch.setattr(sys, "argv", [
        str(RESOLVE), "--manifest", str(m), "--registry", str(reg_root),
        "--repo-root", str(consumer), "--apply", "--prune"])
    real_unlink = os.unlink
    seen = []

    def rec(path, *a, **kw):
        seen.append(kw.get("dir_fd"))
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(mod.os, "unlink", rec)
    assert mod.main() == 0
    assert any(fd is not None for fd in seen)

def test_registry_dotdot_index_path_fails_closed(tmp_path):
    """An index `path` escaping via `..` — even to a REAL sibling dir —
    is outside the canonical registry tree and conflicts (Devin SEC on
    #1953): resolved containment, not lexical string shape, is the
    boundary."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/p": [("skills/a/SKILL.md",
                        "---\nname: a\ndescription: d\n---\nx")],
    })
    # redirect the index entry through `..` into a real sibling tree
    evil = reg_root / "evil" / "p"
    (evil / "skills" / "a").mkdir(parents=True)
    (evil / "skills" / "a" / "SKILL.md").write_text(
        "---\nname: a\ndescription: d\n---\nx")
    idx = reg_root / "registry" / "plugins.json"
    doc = json.loads(idx.read_text())
    doc["plugins"][0]["path"] = "registry/../evil/p"
    idx.write_text(json.dumps(doc))
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "buro",
                       [{"plugin": "platform/p", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode != 0
    assert "outside the registry" in r.stdout

def test_script_dep_pipe_through_wrapper(tmp_path):
    """`printf 'bash x' | env bash` runs the emitted script — the head
    after the pipe is `env`, which execs bash with that stdin (Codex +
    Devin on vendored review). Same for command/sudo/nohup/exec."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    for wrapped in (b"env bash", b"env -i FOO=1 bash", b"command bash",
                    b"sudo bash", b"nohup sh", b"exec sh", b"time sh"):
        assert mod.script_dep_block(
            pdir, b"printf 'bash scripts/x.sh' | " + wrapped + b"\n"), wrapped
    # a genuine non-interpreter consumer still does not execute
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | env cat\n")
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | env\n")
    # wrapper options that take an operand: the operand must be skipped,
    # not mistaken for the pipe's effective head (Devin Review on #1370)
    for wrapped in (b"sudo -u root bash", b"stdbuf -o L bash",
                    b"env -C /tmp bash", b"env -u FOO bash",
                    b"env --unset=FOO bash", b"exec -a sh bash",
                    b"time -o t.txt bash", b"sudo -uroot bash",
                    b"FOO=1 bash"):
        assert mod.script_dep_block(
            pdir, b"printf 'bash scripts/x.sh' | " + wrapped + b"\n"), wrapped
    # `command -v`/`-V` describe a command — nothing executes (same review)
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | command -v bash\n")
    assert not mod.script_dep_block(
        pdir, b"printf 'bash scripts/x.sh' | command -V sh\n")

def test_script_dep_pipe_multi_hop_and_sink(tmp_path):
    """The pipeline walk continues past forwarding heads —
    `| tee /dev/stderr | sh` executes (Devin on #123) — and stops at a
    sink: an interpreter taking its program from argv (`sh -c`,
    `python f.py`) ignores the pipe's contents (Devin on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for pipe in (b"cat scripts/x.sh | tee /dev/stderr | sh",
                 b"cat scripts/x.sh | grep -v '^#' | bash",
                 b"cat scripts/x.sh | sort | uniq | python",
                 b"cat scripts/x.sh | tee f | command bash"):
        assert mod.script_dep_block(pdir, pipe + b"\n"), pipe
    for pipe in (b"cat scripts/x.sh | sh -c 'true'",
                 b"cat scripts/x.sh | bash -c 'echo hi'",
                 b"cat scripts/x.py | python -c 'print(1)'",
                 b"cat scripts/x.sh | perl -e '1'",
                 b"cat scripts/x.py | python other.py",
                 b"cat scripts/x.sh | tee f | command -v sh",
                 b"cat scripts/x.sh | wc -l"):
        assert not mod.script_dep_block(pdir, pipe + b"\n"), pipe
    # stdin-reading forms still execute the pipe's contents
    assert mod.script_dep_block(pdir, b"cat scripts/x.py | python -u\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.py | python -X dev\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | sh -s\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | bash -O extglob\n")

def test_script_dep_output_exec_substitution(tmp_path):
    """`bash -c "$(cat scripts/x.sh)"` and `bash <(cat scripts/x.sh)`
    execute the FILE's contents — the substitution's output is code, so
    the reader head inside supplies a dependency (Codex on #123)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for line in (b'bash -c "$(cat scripts/x.sh)"',
                 b"bash <(cat scripts/x.sh)",
                 b"source <(cat scripts/x.sh)",
                 b'. <(cat scripts/x.sh)',
                 b'eval "$(cat scripts/x.sh)"',
                 b'python -c "$(cat scripts/x.py)"',
                 b'node -e "$(cat scripts/x.sh)"',
                 b'eval `cat scripts/x.sh`'):
        assert mod.script_dep_block(pdir, line + b"\n"), line
    # the substitution's output is NOT code under a reader/echo head
    for line in (b'echo "$(cat scripts/x.sh)"',
                 b'cat "$(cat scripts/x.sh)"',
                 b'cat <(cat scripts/x.sh)',
                 b'bash "$(cat scripts/x.sh)"'):  # operand is a path, not code
        assert not mod.script_dep_block(pdir, line + b"\n"), line

def test_script_dep_process_substitution(tmp_path):
    """`cat <(bash scripts/x.sh)` runs the script before cat sees the
    /dev/fd path — process substitution is an executing context, not an
    outer-cat literal (Codex on vendored review)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, b"cat <(bash scripts/x.sh)\n")
    # the inner head decides: `<(echo bash x)` only prints the text
    assert not mod.script_dep_block(
        pdir, b"cat <(echo bash scripts/x.sh)\n")
    assert mod.script_dep_block(pdir, b"diff <(bash scripts/x.sh) f\n")
    # output process substitution runs the consumer too
    assert mod.script_dep_block(
        pdir, b"bash scripts/x.sh | tee >(cat)\n")
    # quoted/escaped openers stay literal text — `cat "<(x)"` passes the
    # literal string to cat (Devin Review: process substitution does NOT
    # expand inside double quotes; `$(` does)
    assert not mod.script_dep_block(
        pdir, b"echo '<(bash scripts/x.sh)'\n")
    assert not mod.script_dep_block(
        pdir, b'cat "<(bash scripts/x.sh)"\n')
    # `$(` inside double quotes DOES expand — still a dep
    assert mod.script_dep_block(
        pdir, b'x="$(bash scripts/x.sh)"\n')
    # a plain input redirect is still a read, not execution
    assert not mod.script_dep_block(
        pdir, b"cat < other-file\n")

def test_script_dep_cat_piped_to_interpreter(tmp_path):
    """`cat scripts/x.sh | sh` executes the script's contents — the file
    must register as a dep, including through a wrapped consumer
    (`| env sh`) (Devin Review on cpdcheck #6)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | sh\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh | env sh\n")
    assert mod.script_dep_block(pdir, b"cat scripts/x.sh |& bash\n")
    # bare `cat` only reads the file — no dep
    assert not mod.script_dep_block(pdir, b"cat scripts/x.sh\n")
    # piping to a non-interpreter reads, doesn't run
    assert not mod.script_dep_block(
        pdir, b"cat scripts/x.sh | grep foo\n")

def test_script_dep_dash_ksh_and_variable_interpreters(tmp_path):
    """`dash`/`ksh`/`ash scripts/x.sh` run like bash (Devin Review on
    #1953); `$PYTHON`/`${PYTHON} scripts/x.py` invoke through a variable
    the resolver can't see (Codex on #1953)."""
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.sh").write_bytes(b"x")
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for head in (b"dash", b"ksh", b"ash"):
        assert mod.script_dep_block(
            pdir, head + b" scripts/x.sh\n"), head
    assert mod.script_dep_block(pdir, b"$PYTHON scripts/x.py\n")
    assert mod.script_dep_block(pdir, b"${PYTHON} scripts/x.py\n")
    assert mod.script_dep_block(pdir, b"${NODE} scripts/x.js\n")
    # double-quoted var head still expands and runs (Devin on #1953)
    assert mod.script_dep_block(pdir, b'"$PYTHON" scripts/x.py\n')
    # options bind operands — `$PYTHON -X dev scripts/x.py` runs x.py
    # (Devin on cpdcheck #6); a bare non-dash operand is the script
    assert mod.script_dep_block(
        pdir, b"$PYTHON -X dev scripts/x.py\n")
    assert mod.script_dep_block(
        pdir, b"$PYTHON -u -X dev scripts/x.py\n")
    # loose prose must not become an invocation
    assert not mod.script_dep_block(
        pdir, b"set $FOO to the scripts/x.py path\n")
    assert not mod.script_dep_block(
        pdir, b"$FOO and scripts/x.py are mentioned\n")

def test_script_dep_runner_forms(tmp_path):
    """Runner heads (`poetry run`, `pdm run`, `hatch run`) execute the
    script argument exactly like `uv run`/`pipenv run` (Codex on #1953).
    """
    mod = load_resolve_module()
    pdir = tmp_path / "plug"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "scripts" / "x.py").write_bytes(b"x")
    for head in (b"uv run", b"pipenv run", b"poetry run", b"pdm run",
                 b"hatch run"):
        assert mod.script_dep_block(
            pdir, head + b" scripts/x.py\n"), head

def test_mini_yaml_block_scalar_inline_comment():
    """`ref: |- # comment` is legal YAML — the comment trails the block
    indicator, and deeper lines are still the scalar's content (Codex on
    vendored-resolver review)."""
    mod = load_resolve_module()
    y = ("entry:\n"
         "  ref: |-  # pinned\n"
         "    scripts/x.sh\n")
    assert mod._mini_yaml(y) == {"entry": {"ref": "scripts/x.sh"}}
    # folding variant + a `#`-carrying content line
    y = ("entry:\n"
         "  ref: >-  # pinned\n"
         "    scripts/x.sh\n")
    assert mod._mini_yaml(y) == {"entry": {"ref": "scripts/x.sh"}}

def test_mini_yaml_plain_key_edge_chars():
    """`rollout#phase`, `-x`, and `?x` are legal plain mapping keys —
    `#` mid-token is content (a comment needs a preceding space), and a
    leading `-`/`?` is an indicator only before whitespace (Codex on
    #123)."""
    mod = load_resolve_module()
    assert mod._mini_yaml("feature_flags:\n  rollout#phase: true\n") == {
        "feature_flags": {"rollout#phase": True}}
    assert mod._mini_yaml("-x: v\n") == {"-x": "v"}
    assert mod._mini_yaml("?x: v\n") == {"?x": "v"}
    # a real comment still truncates at ` #`
    assert mod._mini_yaml("k: v # note\n") == {"k": "v"}
