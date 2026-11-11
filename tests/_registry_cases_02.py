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
    # Positive fixtures need an authenticated system Git, not a host's
    # PATH wrapper. The declared gh stub stays first; explicit shadow-Git
    # negative cases still supply their own PATH and must fail closed.
    fixture_path = os.pathsep.join((str(bin_dir), os.defpath, os.environ["PATH"]))
    return dict(os.environ, PATH=fixture_path,
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


def test_bootstrap_mirror_seed(tmp_path):
    """bootstrap-mirror.py seeds a complete lint-clean mirror scaffold:
    marker, universe scope, vendored platform, merged index, digests-only
    trust file, minimal generated workflow, and freshly-regenerated
    ratchet allowlists (mirror mode never freezes own-org hits)."""
    import subprocess as sp
    import yaml
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "buro-registry"
    root.mkdir()
    # Mirrors are clones — a non-git root must fail (staging the scaffold
    # is required for the lint to enumerate registry/** hits), and --slug
    # must equal the checkout's origin.
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    # The visibility query must be pinned to github.com — GH_HOST
    # could otherwise point gh at an Enterprise instance where a
    # private same-slug repo passes while the bound repo is public.
    assert "--hostname" in (tmp_path / "gh_args").read_text()
    assert "github.com" in (tmp_path / "gh_args").read_text()
    assert (root / "registry/.private-mirror").is_file()
    assert (root / "registry/buro/scope.yaml").is_file()
    assert (root / "registry/platform").is_dir()
    assert (root / "registry/plugins.json").is_file()
    assert (root / "scripts/registry-lint.py").is_file()
    assert hashlib.sha256(b"buro-built/buro-registry").hexdigest() \
        in (root / "registry/private-mirrors.txt").read_text()
    doc = yaml.safe_load((root / "registry/buro/scope.yaml").read_text())
    assert doc["scope"] == "buro" and doc["visibility"] == "buro"
    assert doc["parent_scope"] == "platform"
    # Ratchets seeded for THIS mirror's content — a vendored platform tree
    # carries canonical's grandfathered hits; an empty allowlist would
    # fail the first lint.
    assert (root / "registry/leak-allowlist.txt").is_file()
    assert (root / "registry/pack-surface-allowlist.txt").is_file()
    # Secrets ratchet is vendored — its token-shape catalogue ships
    # unchanged inside registry/platform/**.
    assert (root / "registry/secrets-allowlist.txt").is_file()
    # Generated workflow must not invoke files the scaffold never seeds,
    # and must authenticate the lint's own `gh api` visibility check.
    wf = (root / ".github/workflows/registry-lint.yml").read_text()
    assert "registry-lint.py" in wf
    assert "GH_TOKEN" in wf
    assert "build-registry.py" not in wf
    assert "pytest" not in wf

def test_bootstrap_mirror_fails_closed(tmp_path):
    """Seeding refuses (rc 2, nothing written) when repo visibility cannot
    be verified or is public — and refuses at the staging step when --root
    is not a git checkout (an empty rglob-fallback allowlist would leave
    the first pushed CI run red)."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent

    def run(root, env, slug="buro-built/buro-registry", universe="buro"):
        return sp.run(
            [sys.executable, "scripts/bootstrap-mirror.py", "--root",
             str(root), "--universe", universe, "--slug", slug],
            cwd=pack, capture_output=True, text=True, env=env)

    # gh absent from PATH (git still resolvable) → visibility
    # unverifiable → refuse before writes.
    import shutil as _shutil
    root = tmp_path / "nogh"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    os.symlink(_shutil.which("git", path=os.defpath), empty_bin / "git")
    r = run(root, dict(os.environ, PATH=str(empty_bin)))
    assert r.returncode == 2, r.stderr
    assert not (root / "registry").exists()

    # A PATH-shadowing git (a real file, not a symlink into the
    # system dirs) cannot attest to itself — refuse before writes
    # even though the wrapper delegates honestly.
    eb = tmp_path / "evilbin"
    eb.mkdir()
    (eb / "git").write_text("#!/bin/sh\nexec /usr/bin/git \"$@\"\n")
    os.chmod(eb / "git", 0o755)
    ebroot = tmp_path / "ebroot"
    ebroot.mkdir()
    sp.run(["git", "init", "-q"], cwd=ebroot, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ebroot, capture_output=True)
    r = run(ebroot,
            dict(os.environ,
                 PATH=f"{eb}{os.pathsep}{os.environ['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert "trusted" in r.stderr or "origin" in r.stderr
    assert not (ebroot / "registry").exists()

    # --slug naming ANOTHER accessible repo → the digest would not bind
    # to this checkout's origin → refuse.
    mism = tmp_path / "mism"
    mism.mkdir()
    sp.run(["git", "init", "-q"], cwd=mism, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Impaktful-Platform/impaktful-registry.git"],
           cwd=mism, capture_output=True)
    r = run(mism, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (mism / "registry").exists()

    # --universe not matching the origin's owning org → refuse (the lint
    # exempts patterns by owner; a buro scope under impaktful-platform
    # would seed the wrong universe AND exempt the wrong org).
    r = run(mism, _bootstrap_env(tmp_path),
            slug="impaktful-platform/impaktful-registry")
    assert r.returncode == 2, r.stderr
    assert "requires the mirror to live under" in r.stderr
    assert not (mism / "registry").exists()

    # gh reports public → refuse.
    pub_bin = tmp_path / "pubbin"
    pub_bin.mkdir()
    gh = pub_bin / "gh"
    gh.write_text("#!/bin/sh\necho public\n")
    gh.chmod(0o755)
    r = run(root, dict(os.environ,
                       PATH=f"{pub_bin}:{os.environ['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert not (root / "registry").exists()

    # A pushurl redirecting `git push` to a different repo than the
    # verified fetch url → refuse (seeded content would land elsewhere).
    pu = tmp_path / "pushurl"
    pu.mkdir()
    sp.run(["git", "init", "-q"], cwd=pu, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pu, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other-Org/public-repo.git"], cwd=pu,
           capture_output=True)
    r = run(pu, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pu / "registry").exists()

    # A pushInsteadOf rewrite doing the same redirect → refuse.
    pi = tmp_path / "pushinsteadof"
    pi.mkdir()
    sp.run(["git", "init", "-q"], cwd=pi, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pi, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.pushInsteadOf",
            "https://github.com/"], cwd=pi, capture_output=True)
    r = run(pi, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pi / "registry").exists()

    # insteadOf rewrites pushes too when no pushInsteadOf rule exists —
    # a github→gitlab insteadOf must refuse even though the raw origin
    # url is GitHub.
    io = tmp_path / "insteadof"
    io.mkdir()
    sp.run(["git", "init", "-q"], cwd=io, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=io, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=io, capture_output=True)
    r = run(io, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (io / "registry").exists()

    # insteadOf still applies to an EXPLICIT pushurl (only pushInsteadOf
    # is ignored for those) — pushurl pinned to the verified slug but an
    # insteadOf redirecting github.com elsewhere must refuse.
    pio = tmp_path / "pushurl-insteadof"
    pio.mkdir()
    sp.run(["git", "init", "-q"], cwd=pio, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pio, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=pio,
           capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=pio, capture_output=True)
    r = run(pio, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pio / "registry").exists()

    # remote.pushDefault pointing at a second remote whose slug differs —
    # a plain `git push` would select that remote over origin → refuse.
    pd = tmp_path / "pushdefault"
    pd.mkdir()
    sp.run(["git", "init", "-q"], cwd=pd, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pd, capture_output=True)
    sp.run(["git", "remote", "add", "evil",
            "https://github.com/Other-Org/public-repo.git"],
           cwd=pd, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault", "evil"], cwd=pd,
           capture_output=True)
    r = run(pd, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pd / "registry").exists()

    # branch.<name>.pushRemote has the highest precedence — same
    # redirect via the current branch's pushRemote → refuse.
    pr = tmp_path / "pushremote"
    pr.mkdir()
    sp.run(["git", "init", "-q"], cwd=pr, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pr, capture_output=True)
    sp.run(["git", "remote", "add", "evil",
            "https://github.com/Other-Org/public-repo.git"],
           cwd=pr, capture_output=True)
    cur = sp.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=pr,
                 capture_output=True, text=True).stdout.strip()
    sp.run(["git", "config", f"branch.{cur}.pushRemote", "evil"], cwd=pr,
           capture_output=True)
    r = run(pr, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pr / "registry").exists()

    # A push destination may be a literal URL, not a remote name —
    # a URL-valued remote.pushDefault pointing elsewhere → refuse.
    pv = tmp_path / "pushdefault-url"
    pv.mkdir()
    sp.run(["git", "init", "-q"], cwd=pv, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pv, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Other-Org/public-repo.git"], cwd=pv,
           capture_output=True)
    r = run(pv, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pv / "registry").exists()

    # When NO pushInsteadOf rule matches the URL destination, git falls
    # back to insteadOf rules — a URL pushDefault at the verified slug
    # with an unrelated pushInsteadOf rule AND a github→gitlab
    # insteadOf still redirects the push → refuse.
    pf = tmp_path / "pushdefault-url-fallback"
    pf.mkdir()
    sp.run(["git", "init", "-q"], cwd=pf, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pf, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=pf,
           capture_output=True)
    sp.run(["git", "config",
            "url.ssh://git@bitbucket.org/.pushInsteadOf",
            "ssh://git@bitbucket.org/"], cwd=pf, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=pf, capture_output=True)
    r = run(pf, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pf / "registry").exists()

    # remote.<name>.vcs delegates pushes to a git-remote-<vcs> helper
    # that can forward the pack anywhere — the configured URL is not
    # evidence of the real destination → refuse.
    vc = tmp_path / "vcs-helper"
    vc.mkdir()
    sp.run(["git", "init", "-q"], cwd=vc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vc, capture_output=True)
    sp.run(["git", "config", "remote.origin.vcs",
            "helper--token=SUPERSECRET"], cwd=vc,
           capture_output=True)
    r = run(vc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "vcs" in r.stderr
    # The helper name itself may carry a credential — never echoed.
    assert "SUPERSECRET" not in r.stderr
    assert "helper--token" not in r.stderr
    assert not (vc / "registry").exists()

    # And a credential-bearing remote NAME is not echoed either.
    vn = tmp_path / "vcs-name"
    vn.mkdir()
    sp.run(["git", "init", "-q"], cwd=vn, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vn, capture_output=True)
    sp.run(["git", "remote", "add", "SUPERSECRETTOKEN",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vn, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault", "SUPERSECRETTOKEN"],
           cwd=vn, capture_output=True)
    sp.run(["git", "config", "remote.SUPERSECRETTOKEN.vcs", "evil"],
           cwd=vn, capture_output=True)
    r = run(vn, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "vcs" in r.stderr
    assert "SUPERSECRETTOKEN" not in r.stderr
    assert not (vn / "registry").exists()

    # GIT_EXEC_PATH swaps which git-remote-<scheme> helper the push
    # execs — a verified https URL is no evidence of the transport.
    gx = tmp_path / "execpath"
    gx.mkdir()
    sp.run(["git", "init", "-q"], cwd=gx, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=gx, capture_output=True)
    r = run(gx, dict(_bootstrap_env(tmp_path),
                     GIT_EXEC_PATH="/tmp/evil-exec"))
    assert r.returncode == 2, r.stderr
    assert "GIT_EXEC_PATH" in r.stderr
    assert not (gx / "registry").exists()

    # An ssh/scp URL parses to the verified slug, but core.sshCommand
    # replaces the transport entirely — the push can land anywhere →
    # refuse.
    sc = tmp_path / "sshcommand"
    sc.mkdir()
    sp.run(["git", "init", "-q"], cwd=sc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=sc, capture_output=True)
    sp.run(["git", "config", "core.sshCommand", "evil-ssh"], cwd=sc,
           capture_output=True)
    r = run(sc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "sshCommand" in r.stderr
    assert not (sc / "registry").exists()

    # GIT_SSH_COMMAND is the environment-level equivalent of
    # core.sshCommand → refuse.
    se = tmp_path / "sshenv"
    se.mkdir()
    sp.run(["git", "init", "-q"], cwd=se, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=se, capture_output=True)
    env = _bootstrap_env(tmp_path)
    env["GIT_SSH_COMMAND"] = "evil-ssh"
    r = run(se, env)
    assert r.returncode == 2, r.stderr
    assert "GIT_SSH_COMMAND" in r.stderr
    assert not (se / "registry").exists()

    # A mixed-case scp host ('GitHub.com') still binds the verified slug
    # via the case-insensitive _slug_of — the ssh transport checks must
    # run on that same normalised view or GIT_SSH_COMMAND slips past.
    mc = tmp_path / "mixedcase-scp"
    mc.mkdir()
    sp.run(["git", "init", "-q"], cwd=mc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=mc, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "git@GitHub.com:Buro-Built/buro-registry.git"], cwd=mc,
           capture_output=True)
    r = run(mc, env)
    assert r.returncode == 2, r.stderr
    assert "GIT_SSH_COMMAND" in r.stderr
    assert not (mc / "registry").exists()

    # A rejected credential-bearing push URL must not print the
    # credential to stderr (it lands in transcripts/CI logs).
    cr = tmp_path / "creds"
    cr.mkdir()
    sp.run(["git", "init", "-q"], cwd=cr, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cr, capture_output=True)
    sp.run(["git", "remote", "set-url", "--push", "origin",
            "https://x-access-token:SECRETTOKEN@github.com/"
            "Other-Org/public-repo.git"],
           cwd=cr, capture_output=True)
    r = run(cr, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETTOKEN" not in r.stderr
    assert not (cr / "registry").exists()

    # Same for a credential-bearing literal URL in remote.pushDefault —
    # the configured destination itself must be redacted.
    cl = tmp_path / "credlit"
    cl.mkdir()
    sp.run(["git", "init", "-q"], cwd=cl, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cl, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://x-access-token:SECRETTOKEN@github.com/"
            "Other-Org/public-repo.git"],
           cwd=cl, capture_output=True)
    r = run(cl, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETTOKEN" not in r.stderr
    assert not (cl / "registry").exists()

    # An 'ssh' resolved from PATH to a non-system location is a
    # wrapper — it would attest to its own '-G' output while forwarding
    # the push anywhere, so refuse without asking it.
    sh = tmp_path / "sshuntrusted"
    sh.mkdir()
    stub = tmp_path / "sshstub"
    stub.mkdir()
    s = stub / "ssh"
    s.write_text("#!/bin/sh\necho 'hostname github.com'\n")
    s.chmod(0o755)
    sp.run(["git", "init", "-q"], cwd=sh, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=sh, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = run(sh, dict(env, PATH=f"{stub}:{env['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert "ssh transport cannot be verified" in r.stderr
    assert not (sh / "registry").exists()

    # git:// is plaintext transport (no encryption, no server
    # authentication) — refused outright regardless of gitProxy config.
    gp = tmp_path / "gitproto"
    gp.mkdir()
    sp.run(["git", "init", "-q"], cwd=gp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git://github.com/Buro-Built/buro-registry.git"],
           cwd=gp, capture_output=True)
    r = run(gp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "plaintext git" in r.stderr
    assert not (gp / "registry").exists()

    # An opaque helper destination can carry credentials in its
    # arguments — they must not reach stderr.
    ex = tmp_path / "extcreds"
    ex.mkdir()
    sp.run(["git", "init", "-q"], cwd=ex, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ex, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "ext::helper --token=SUPERSECRET %S"], cwd=ex,
           capture_output=True)
    r = run(ex, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRET" not in r.stderr
    assert not (ex / "registry").exists()

    # The opaque helper address can also carry a credential with no
    # space at all — the whole '<transport>::<address>' is withheld.
    e2 = tmp_path / "extcreds-nospace"
    e2.mkdir()
    sp.run(["git", "init", "-q"], cwd=e2, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=e2, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "ext::helper--token=SUPERSECRET"], cwd=e2,
           capture_output=True)
    r = run(e2, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRET" not in r.stderr
    assert "helper" not in r.stderr
    assert not (e2 / "registry").exists()

    # An ordinary URL path can carry a credential too — the diagnostic
    # must withhold the path, keeping only scheme+host.
    cp = tmp_path / "credpath"
    cp.mkdir()
    sp.run(["git", "init", "-q"], cwd=cp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cp, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://example.test/git/SECRETPATH/repo.git"], cwd=cp,
           capture_output=True)
    r = run(cp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETPATH" not in r.stderr
    assert not (cp / "registry").exists()

    # Userinfo may contain MORE than one '@' — git preserves the whole
    # value, so masking through only the first delimiter would leak the
    # remainder to stderr.
    mu = tmp_path / "multiat"
    mu.mkdir()
    sp.run(["git", "init", "-q"], cwd=mu, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=mu, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://foo@SUPERSECRETTOKEN@github.com/Other/repo.git"],
           cwd=mu, capture_output=True)
    r = run(mu, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRETTOKEN" not in r.stderr
    assert not (mu / "registry").exists()

    # A local filesystem path is a valid git push destination — any of
    # its components may be sensitive, so none reach stderr.
    lp = tmp_path / "localpath"
    lp.mkdir()
    sp.run(["git", "init", "-q"], cwd=lp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=lp, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "/tmp/SECRETDIR/repo.git"], cwd=lp,
           capture_output=True)
    r = run(lp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETDIR" not in r.stderr
    assert "<opaque destination>" in r.stderr
    assert not (lp / "registry").exists()

    # 'file://' is the URL form of a local path — opaque too.
    lf = tmp_path / "localfile"
    lf.mkdir()
    sp.run(["git", "init", "-q"], cwd=lf, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=lf, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "file:///tmp/SECRETFILE/repo.git"], cwd=lf,
           capture_output=True)
    r = run(lf, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETFILE" not in r.stderr
    assert "file:***" in r.stderr
    assert not (lf / "registry").exists()

    # Even on github.com a path exceeding owner/repo(.git) may carry a
    # credential — it is withheld wholesale, not exempted by the host.
    gp = tmp_path / "ghpath"
    gp.mkdir()
    sp.run(["git", "init", "-q"], cwd=gp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=gp, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other-Org/public-repo/SECRETX"], cwd=gp,
           capture_output=True)
    r = run(gp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETX" not in r.stderr
    assert "github.com/***" in r.stderr
    assert not (gp / "registry").exists()

    # Plain-HTTP is refused outright — it is plaintext transport and
    # its effective proxy chain cannot be trusted for a private push.
    ht = tmp_path / "httppush"
    ht.mkdir()
    sp.run(["git", "init", "-q"], cwd=ht, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "http://github.com/Buro-Built/buro-registry.git"],
           cwd=ht, capture_output=True)
    r = run(ht, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "plaintext http" in r.stderr
    assert not (ht / "registry").exists()

    # https with TLS verification disabled is MITM-able — refuse
    # (global http.sslVerify=false, a URL-scoped override, and the
    # GIT_SSL_NO_VERIFY environment variable all count).
    for i, cfg in enumerate((["http.sslVerify", "false"],
                             ["http.https://github.com/.sslVerify",
                              "false"],
                             # An explicitly-EMPTY value canonicalises
                             # to false — its blank --get-urlmatch
                             # record must not read as 'enabled'.
                             ["http.sslVerify", ""])):
        sv = tmp_path / f"ssloff{i}"
        sv.mkdir()
        sp.run(["git", "init", "-q"], cwd=sv, capture_output=True)
        sp.run(["git", "remote", "add", "origin",
                "https://github.com/Buro-Built/buro-registry.git"],
               cwd=sv, capture_output=True)
        sp.run(["git", "config", *cfg], cwd=sv, capture_output=True)
        r = run(sv, _bootstrap_env(tmp_path))
        assert r.returncode == 2, r.stderr
        assert "tls verification" in r.stderr
        assert not (sv / "registry").exists()

    sv = tmp_path / "sslenv"
    sv.mkdir()
    sp.run(["git", "init", "-q"], cwd=sv, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=sv, capture_output=True)
    # GIT_SSL_NO_VERIFY is defined by presence — '=0' still disables.
    r = run(sv, dict(_bootstrap_env(tmp_path), GIT_SSL_NO_VERIFY="0"))
    assert r.returncode == 2, r.stderr
    assert "tls verification" in r.stderr
    assert not (sv / "registry").exists()

    # A custom CA trust store keeps verification ON while trusting a
    # root the attacker may control — refuse env and config forms.
    ca = tmp_path / "sslcaenv"
    ca.mkdir()
    sp.run(["git", "init", "-q"], cwd=ca, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ca, capture_output=True)
    r = run(ca, dict(_bootstrap_env(tmp_path),
                     GIT_SSL_CAINFO="/tmp/evil-ca.pem"))
    assert r.returncode == 2, r.stderr
    assert "custom CA" in r.stderr
    assert not (ca / "registry").exists()

    cb = tmp_path / "sslcacfg"
    cb.mkdir()
    sp.run(["git", "init", "-q"], cwd=cb, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cb, capture_output=True)
    sp.run(["git", "config", "http.sslCAInfo", "/tmp/evil-ca.pem"],
           cwd=cb, capture_output=True)
    r = run(cb, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "custom CA" in r.stderr
    assert not (cb / "registry").exists()

    # The same URL-scoped key repeated in a later scope: git resolves
    # equal-specificity entries by order — the later value wins.
    genv = _bootstrap_env(tmp_path)
    eq = tmp_path / "ssleq"
    eq.mkdir()
    sp.run(["git", "init", "-q"], cwd=eq, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=eq, capture_output=True)
    sp.run(["git", "config", "--global",
            "http.https://github.com/.sslVerify", "true"],
           cwd=eq, capture_output=True, env=genv)
    sp.run(["git", "config", "http.https://github.com/.sslVerify",
            "false"], cwd=eq, capture_output=True)
    r = run(eq, genv)
    assert r.returncode == 2, r.stderr
    assert "tls verification" in r.stderr
    assert not (eq / "registry").exists()

    # An insteadOf <base> containing spaces still applies — dropping
    # the rule via whitespace parsing would pass the pre-rewrite
    # github.com URL while git pushes to the rewritten helper.
    es = tmp_path / "extspace"
    es.mkdir()
    sp.run(["git", "init", "-q"], cwd=es, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=es, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=es, capture_output=True)
    sp.run(["git", "config",
            'url."ext::echo something ".insteadOf',
            "https://github.com/"], cwd=es, capture_output=True)
    r = run(es, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (es / "registry").exists()

    # scp-style 'user@host:path' userinfo is credential-bearing too —
    # it has no '://' for the userinfo rule to catch.
    cs = tmp_path / "credscp"
    cs.mkdir()
    sp.run(["git", "init", "-q"], cwd=cs, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cs, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "SECRETSCP@github.com:Other/public.git"], cwd=cs,
           capture_output=True)
    r = run(cs, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETSCP" not in r.stderr
    assert not (cs / "registry").exists()

    # The auth-proxy exemption is for https proxies only — a non-https
    # prefix (e.g. an 'ext::… ' helper transport) must not exempt the
    # rewritten destination.
    ep = tmp_path / "extproxy"
    ep.mkdir()
    sp.run(["git", "init", "-q"], cwd=ep, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ep, capture_output=True)
    sp.run(["git", "config",
            'url."ext::helper ".insteadOf', "https://github.com/"],
           cwd=ep, capture_output=True)
    r = run(ep, dict(_bootstrap_env(tmp_path),
                     MIRROR_GITHUB_PROXY_PREFIX="ext::helper "))
    assert r.returncode == 2, r.stderr
    assert not (ep / "registry").exists()

    # Credentials in the URL query or fragment must not reach stderr.
    cq = tmp_path / "credquery"
    cq.mkdir()
    sp.run(["git", "init", "-q"], cwd=cq, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cq, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other/public.git?access_token="
            "SECRETQUERY#frag=SECRETQUERY"], cwd=cq,
           capture_output=True)
    r = run(cq, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETQUERY" not in r.stderr
    assert not (cq / "registry").exists()

    # A non-GitHub origin that parses to a valid-looking slug — gh would
    # verify an UNRELATED github.com repo of the same name → refuse.
    gl = tmp_path / "gitlab"
    gl.mkdir()
    sp.run(["git", "init", "-q"], cwd=gl, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://gitlab.com/Buro-Built/buro-registry.git"],
           cwd=gl, capture_output=True)
    r = run(gl, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (gl / "registry").exists()

    # gh stub OK but --root is not a git checkout → refused (no origin).
    plain = tmp_path / "plainroot"
    plain.mkdir()
    r = run(plain, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr

    # A credential.helper configured INSIDE the clone (local scope or
    # a file it includes) executes during the https push — the clone
    # is untrusted input; refuse it.
    ch = tmp_path / "credhelper"
    ch.mkdir()
    sp.run(["git", "init", "-q"], cwd=ch, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ch, capture_output=True)
    sp.run(["git", "config", "credential.helper", "!leak"],
           cwd=ch, capture_output=True)
    r = run(ch, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "credential.helper" in r.stderr
    assert not (ch / "registry").exists()

    # The same helper in the operator's GLOBAL config is the env trust
    # channel (like MIRROR_TRUST_DIRS), not untrusted input → allowed.
    chg = tmp_path / "credhelper-global"
    chg.mkdir()
    gcfg = tmp_path / "gitconfig.helper"
    gcfg.write_text("[credential]\n\thelper = !leak\n")
    sp.run(["git", "init", "-q"], cwd=chg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=chg, capture_output=True)
    r = run(chg, dict(_bootstrap_env(tmp_path),
                      GIT_CONFIG_GLOBAL=str(gcfg)))
    assert r.returncode == 0, r.stderr

    # A LOCAL include.path pointing outside the checkout keeps scope
    # 'local' — the helper is still clone-derived untrusted input.
    inc = tmp_path / "incl"
    inc.mkdir()
    outside = tmp_path / "outside-helper.cfg"
    outside.write_text("[credential]\n\thelper = !leak\n")
    sp.run(["git", "init", "-q"], cwd=inc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=inc, capture_output=True)
    sp.run(["git", "config", "include.path", str(outside)],
           cwd=inc, capture_output=True)
    r = run(inc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "credential.helper" in r.stderr
    assert not (inc / "registry").exists()

    # core.askPass answers the push's authentication prompt — a
    # clone-local value is the same untrusted-input class.
    ap = tmp_path / "askpass"
    ap.mkdir()
    sp.run(["git", "init", "-q"], cwd=ap, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ap, capture_output=True)
    sp.run(["git", "config", "core.askPass", "/tmp/upload-creds"],
           cwd=ap, capture_output=True)
    r = run(ap, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "askPass" in r.stderr
    assert not (ap / "registry").exists()

    # commit.gpgSign + a LOCAL gpg.program execs the program during
    # 'git commit' — clone-local signing config is untrusted input.
    sg = tmp_path / "signer"
    sg.mkdir()
    sp.run(["git", "init", "-q"], cwd=sg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=sg, capture_output=True)
    sp.run(["git", "config", "commit.gpgsign", "true"],
           cwd=sg, capture_output=True)
    sp.run(["git", "config", "gpg.program", "/tmp/sign"],
           cwd=sg, capture_output=True)
    r = run(sg, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "signing" in r.stderr
    assert not (sg / "registry").exists()

    # Attributes binding against a path the bootstrap CREATES
    # (README.md / schemas/**) must refuse even though the path does
    # not exist yet — the seeded file enters the filter during
    # 'git add'.
    fg = tmp_path / "filtgen"
    fg.mkdir()
    sp.run(["git", "init", "-q"], cwd=fg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fg, capture_output=True)
    sp.run(["git", "config", "filter.leak.clean", "cat > /tmp/out2"],
           cwd=fg, capture_output=True)
    (fg / ".gitattributes").write_text(
        "README.md filter=leak\nschemas/** filter=leak\n")
    r = run(fg, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "filter" in r.stderr
    assert not (fg / "registry").exists()

@pytest.mark.parametrize("inherited_trust", ["", "/synthetic/existing:/synthetic/other"])
def test_bootstrap_mirror_push_target_pass(tmp_path, monkeypatch, inherited_trust):
    """Push-target validation must not over-refuse: (a) an explicit
    pushurl pinned to the verified slug stays valid even when a
    pushInsteadOf rule exists — git ignores pushInsteadOf for remotes
    with an explicit pushurl; (b) an env-supplied auth-proxy prefix
    whose insteadOf rewrites the effective push URL still lands on
    the verified github.com slug."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("MIRROR_TRUST_DIRS", inherited_trust)
    env = _bootstrap_env(tmp_path)

    def run(root, e=None):
        return sp.run(
            [sys.executable, "scripts/bootstrap-mirror.py", "--root",
             str(root), "--universe", "buro",
             "--slug", "buro-built/buro-registry"],
            cwd=pack, capture_output=True, text=True, env=e or env)

    a = tmp_path / "pushurl-ok"
    a.mkdir()
    sp.run(["git", "init", "-q"], cwd=a, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=a, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=a,
           capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.pushInsteadOf",
            "https://github.com/"], cwd=a, capture_output=True)
    r = run(a)
    assert r.returncode == 0, r.stderr

    # Hostname case is immaterial to DNS/git — 'GITHUB.COM' binds the
    # same verified slug.
    uc = tmp_path / "uppercase-host"
    uc.mkdir()
    sp.run(["git", "init", "-q"], cwd=uc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://GITHUB.COM/Buro-Built/buro-registry.git"],
           cwd=uc, capture_output=True)
    r = run(uc)
    assert r.returncode == 0, r.stderr
    assert (uc / "registry/.private-mirror").is_file()

    b = tmp_path / "proxy-ok"
    b.mkdir()
    sp.run(["git", "init", "-q"], cwd=b, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=b, capture_output=True)
    prefix = "https://auth-proxy.example.test/github.com/"
    sp.run(["git", "config",
            f"url.{prefix}.insteadOf",
            "https://github.com/"], cwd=b, capture_output=True)
    r = run(b, dict(env, MIRROR_GITHUB_PROXY_PREFIX=prefix))
    assert r.returncode == 0, r.stderr

    # remote.pushDefault selecting a DIFFERENT remote is fine when that
    # remote resolves to the same verified slug — the push still lands
    # on the private repo.
    c = tmp_path / "pushdefault-same-slug"
    c.mkdir()
    sp.run(["git", "init", "-q"], cwd=c, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=c, capture_output=True)
    sp.run(["git", "remote", "add", "mirror",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=c, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault", "mirror"], cwd=c,
           capture_output=True)
    r = run(c)
    assert r.returncode == 0, r.stderr

    # An ssh/scp destination passes when the system ssh's effective
    # config verifies a direct, authenticated connection to github.com
    # (the real 'ssh -G' on a default-configured host does).
    sh = tmp_path / "ssh-ok"
    sh.mkdir()
    sp.run(["git", "init", "-q"], cwd=sh, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=sh, capture_output=True)
    r = run(sh)
    assert r.returncode == 0, r.stderr

    # Userless scp-style 'github.com:slug' is valid git ssh syntax —
    # it must be accepted like the user@ form, verified via 'ssh -G'.
    us = tmp_path / "userlessscp"
    us.mkdir()
    sp.run(["git", "init", "-q"], cwd=us, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "github.com:Buro-Built/buro-registry.git"],
           cwd=us, capture_output=True)
    r = run(us)
    assert r.returncode == 0, r.stderr

    # A URL-valued branch.<name>.pushRemote at the verified slug — git
    # accepts a URL there just as it accepts a remote name.
    d = tmp_path / "pushremote-url-ok"
    d.mkdir()
    sp.run(["git", "init", "-q"], cwd=d, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=d, capture_output=True)
    cur = sp.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=d,
                 capture_output=True, text=True).stdout.strip()
    sp.run(["git", "config", f"branch.{cur}.pushRemote",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=d,
           capture_output=True)
    r = run(d)
    assert r.returncode == 0, r.stderr

    # A pre-push hook runs arbitrary code during the push — it can read
    # the staged universe files and exfiltrate them even when every
    # transport check passes. An executable hook in .git/hooks fails
    # closed, and core.hooksPath's directory is checked too.
    hk = tmp_path / "prepushhook"
    hk.mkdir()
    sp.run(["git", "init", "-q"], cwd=hk, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=hk, capture_output=True)
    hook = hk / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)
    r = run(hk)
    assert r.returncode == 2, r.stderr
    assert "pre-push" in r.stderr
    assert not (hk / "registry").exists()

    hd = tmp_path / "hooksdir"
    hd.mkdir()
    hp2 = tmp_path / "prepushpath"
    hp2.mkdir()
    sp.run(["git", "init", "-q"], cwd=hp2, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=hp2, capture_output=True)
    (hd / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(hd / "pre-push", 0o755)
    sp.run(["git", "config", "core.hooksPath", str(hd)],
           cwd=hp2, capture_output=True)
    r = run(hp2)
    assert r.returncode == 2, r.stderr
    assert "pre-push" in r.stderr
    assert not (hp2 / "registry").exists()

    # Commit-side hooks run during the recommended `git commit` too —
    # every hook the add/commit/push sequence invokes is checked.
    hk2 = tmp_path / "precommithook"
    hk2.mkdir()
    sp.run(["git", "init", "-q"], cwd=hk2, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=hk2, capture_output=True)
    hook = hk2 / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)
    r = run(hk2)
    assert r.returncode == 2, r.stderr
    assert "pre-commit" in r.stderr
    assert not (hk2 / "registry").exists()

    # Index/reference hooks run during `git add`/`git commit` too —
    # post-index-change fires inside the bootstrap's own staging.
    rt = tmp_path / "reftranhook"
    rt.mkdir()
    sp.run(["git", "init", "-q"], cwd=rt, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=rt, capture_output=True)
    hook = rt / ".git" / "hooks" / "post-index-change"
    hook.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)
    r = run(rt)
    assert r.returncode == 2, r.stderr
    assert "post-index-change" in r.stderr
    assert not (rt / "registry").exists()

    # core.fsmonitor=<path> executes an external command during index
    # operations — a non-boolean value must refuse.
    fm = tmp_path / "fsm"
    fm.mkdir()
    sp.run(["git", "init", "-q"], cwd=fm, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fm, capture_output=True)
    sp.run(["git", "config", "core.fsmonitor", "/tmp/leak"],
           cwd=fm, capture_output=True)
    r = run(fm)
    assert r.returncode == 2, r.stderr
    assert "fsmonitor" in r.stderr
    assert not (fm / "registry").exists()

    # A BOOLEAN core.fsmonitor is only safe on git >=2.35.1 — on 2.35.0
    # git still treats the value as a hook pathname, so 'true' execs a
    # PATH 'true' wrapper during 'git add'. The stub answers
    # '--version' with 2.35.0 and delegates everything else to the real
    # git; MIRROR_TRUST_DIRS makes the stub a trusted binary.
    fm0 = tmp_path / "fsm0"
    fm0.mkdir()
    sp.run(["git", "init", "-q"], cwd=fm0, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fm0, capture_output=True)
    sp.run(["git", "config", "core.fsmonitor", "true"],
           cwd=fm0, capture_output=True)
    real_git = os.path.realpath(shutil.which("git", path=os.defpath) or "/usr/bin/git")
    gitstub = tmp_path / "bin" / "git"
    gitstub.parent.mkdir(exist_ok=True)
    gitstub.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\n"
        "  echo \"git version 2.35.0\"\n  exit 0\nfi\n"
        f"exec {real_git} \"$@\"\n")
    gitstub.chmod(0o755)
    r = run(fm0)
    assert r.returncode == 2, r.stderr
    assert "fsmonitor" in r.stderr
    assert not (fm0 / "registry").exists()
    gitstub.unlink()

    # filter.<name>.clean/.process runs the configured program on
    # staged file contents during 'git add' — refuse when configured.
    fl = tmp_path / "fil"
    fl.mkdir()
    sp.run(["git", "init", "-q"], cwd=fl, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fl, capture_output=True)
    sp.run(["git", "config", "filter.leak.clean", "cat > /tmp/out"],
           cwd=fl, capture_output=True)
    # A configured filter is inert until attributes bind it — a
    # system git-lfs config must NOT refuse, a bound one must. The
    # binding can name paths that don't exist yet (registry/**)
    # and still receive every seeded file during 'git add'.
    (fl / ".gitattributes").write_text("registry/** filter=leak\n")
    r = run(fl)
    assert r.returncode == 2, r.stderr
    assert "filter" in r.stderr
    assert not (fl / "registry").exists()

def test_bootstrap_ssh_effective_config(monkeypatch, tmp_path):
    """_ssh_host_unchanged must refuse any effective-config evidence of
    redirection or disabled server authentication, and must not trust a
    PATH-shadowing ssh executable to attest to itself."""
    import subprocess as _sp
    import types
    mod = _load_bootstrap()
    calls = []

    class R:
        def __init__(self, out, rc=0, err=""):
            self.stdout, self.returncode, self.stderr = out, rc, err

    def patch(lines, which="/usr/bin/ssh"):
        calls.clear()

        def run(argv, **_kw):
            calls.append(argv)
            return R("".join(f"{k} {v}\n" for k, v in lines))

        monkeypatch.setattr(
            mod, "subprocess",
            types.SimpleNamespace(
                run=run, TimeoutExpired=_sp.TimeoutExpired))
        monkeypatch.setattr(
            mod, "shutil", types.SimpleNamespace(which=lambda _n: which))

    CLEAN = [("hostname", "github.com"),
             ("stricthostkeychecking", "ask"),
             ("userknownhostsfile", "~/.ssh/known_hosts")]
    URL = "git@github.com:Buro-Built/buro-registry.git"

    patch(CLEAN)
    assert mod._ssh_host_unchanged(URL) is None
    # git invokes 'ssh -G <user>@<host>' for an scp-style URL; '-v'
    # traces the config sources ssh actually read.
    assert calls == [["/usr/bin/ssh", "-G", "-v", "git@github.com"]]

    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "github.com:Buro-Built/buro-registry.git") is None
    assert calls[-1] == ["/usr/bin/ssh", "-G", "-v", "github.com"]

    # The -G target carries the URL's port and the RAW user — git hands
    # ssh userinfo byte-for-byte (no percent-decoding), so 'git%40x' is
    # a different user than 'git@x' to a Match user block. Encoded or
    # control-char users cannot be attested → refused.
    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "ssh://redir%65ct@github.com:2222/Buro-Built/buro-registry.git"
    ) is not None
    assert "cannot be attested" in mod._ssh_host_unchanged(
        "ssh://redir%65ct@github.com:2222/Buro-Built/buro-registry.git")
    # A plain user with a port probes exactly what git would invoke.
    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "ssh://redirect@github.com:2222/Buro-Built/buro-registry.git"
    ) is None
    assert calls[-1] == ["/usr/bin/ssh", "-G", "-v", "-p", "2222",
                         "redirect@github.com"]

    patch([(k, "evil.example.test" if k == "hostname" else v)
           for k, v in CLEAN])
    assert "redirects" in mod._ssh_host_unchanged(URL)

    patch(CLEAN + [("proxycommand", "ssh -W %h:%p bastion")])
    assert "ProxyCommand" in mod._ssh_host_unchanged(URL)

    patch(CLEAN + [("proxyjump", "bastion")])
    assert "ProxyCommand" in mod._ssh_host_unchanged(URL)

    for shkc in ("no", "off", "false", "0", "accept-new"):
        patch(CLEAN + [("stricthostkeychecking", shkc)])
        assert "host key" in mod._ssh_host_unchanged(URL), shkc
    # HostKeyAlias swaps the name used for host-key lookup while
    # hostname still reports github.com — refused unless unset or the
    # identity alias.
    patch(CLEAN + [("hostkeyalias", "attacker.example")])
    assert "HostKeyAlias" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("hostkeyalias", "github.com")])
    assert mod._ssh_host_unchanged(URL) is None
    # A KnownHostsCommand supplies host keys beyond the files —
    # refused unless unset/'none'.
    patch(CLEAN + [("knownhostscommand", "emit-attacker-key")])
    assert "KnownHostsCommand" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("knownhostscommand", "none")])
    assert mod._ssh_host_unchanged(URL) is None

    patch(CLEAN + [("userknownhostsfile", "/dev/null"),
                   ("globalknownhostsfile", "/dev/null")])
    assert "known-hosts" in mod._ssh_host_unchanged(URL)

    # A custom known-hosts path can point at an attacker-seeded file —
    # only ~/.ssh and /etc/ssh locations are trusted.
    patch(CLEAN + [("userknownhostsfile", "/tmp/attacker_hosts")])
    assert "untrusted" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("userknownhostsfile", "/etc/ssh/ssh_known_hosts")])
    assert mod._ssh_host_unchanged(URL) is None

    # An approved LOCATION is not proof of KEY identity: ssh accepts
    # any matching entry, so every github.com entry under ~/.ssh must
    # fingerprint to GitHub's published host keys — content is checked,
    # not just the directory the file lives in.
    import base64
    import hmac
    home = tmp_path / "h"
    kh_file = home / ".ssh" / "attacker_hosts"
    kh_file.parent.mkdir(parents=True)
    monkeypatch.setattr(mod, "pwd", types.SimpleNamespace(
        getpwuid=lambda _uid: types.SimpleNamespace(
            pw_dir=str(home), pw_name="u")))
    bad = base64.b64encode(b"forged-attacker-key-blob").decode()
    kh_file.write_text(f"github.com ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "published" in mod._ssh_host_unchanged(URL)

    # The pinned set holds GitHub's real published fingerprints.
    assert "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" \
        in mod._GITHUB_HOST_KEY_SHA256
    assert "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s" \
        in mod._GITHUB_HOST_KEY_SHA256
    assert "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM" \
        in mod._GITHUB_HOST_KEY_SHA256

    # A github.com entry whose blob fingerprints to the pinned set
    # anchors the host — patch the pinned set to a test blob's digest.
    blob = b"fake-github-key-blob"
    fp = "SHA256:" + base64.b64encode(
        hashlib.sha256(blob).digest()).decode().rstrip("=")
    monkeypatch.setattr(mod, "_GITHUB_HOST_KEY_SHA256", {fp})
    good = base64.b64encode(blob).decode()
    kh_file.write_text(f"github.com ssh-rsa {good}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert mod._ssh_host_unchanged(URL) is None

    # A certificate-authority entry covering github.com lets a CA sign
    # any host key — refused even beside a pinned key.
    kh_file.write_text(f"github.com ssh-rsa {good}\n"
                       f"@cert-authority github.com ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "certificate-authority" in mod._ssh_host_unchanged(URL)

    # Hashed known-hosts entries (HashKnownHosts) resolve via HMAC-SHA1
    # — a forged hashed github.com entry refuses the same way.
    salt = b"somesalt"
    tok = "|1|" + base64.b64encode(salt).decode() + "|" + \
        base64.b64encode(hmac.new(
            salt, b"github.com", hashlib.sha1).digest()).decode()
    kh_file.write_text(
        f"{tok} ssh-rsa {base64.b64encode(b'attacker-hashed').decode()}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "published" in mod._ssh_host_unchanged(URL)

    # No github.com entry anywhere → interactive TOFU stands (shkc=ask
    # already passed) — not refused.
    kh_file.write_text(f"other.example ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert mod._ssh_host_unchanged(URL) is None

    # Non-default effective port: 'Host github.com / Port 443' makes
    # ssh look up '[github.com]:443' — a forged key under that hashed
    # token must fail too (the EFFECTIVE port, not the URL's).
    salt = b"portsalt"
    tok443 = "|1|" + base64.b64encode(salt).decode() + "|" + \
        base64.b64encode(hmac.new(
            salt, b"[github.com]:443", hashlib.sha1).digest()).decode()
    kh_file.write_text(
        f"{tok443} ssh-rsa {base64.b64encode(b'forged-443').decode()}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("port", "443")])
    assert "published" in mod._ssh_host_unchanged(URL)

    # A padded known-hosts file beyond the verification cap is refused
    # outright — skipping it would still let ssh read a forged entry.
    kh_file.write_text(f"github.com ssh-rsa {bad}\n" + "#" * (8 << 20))
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "size" in mod._ssh_host_unchanged(URL)

    # A quoted path containing spaces is emitted WITHOUT its quoting —
    # 'one two' parses as two absent fragments while ssh loads the
    # real spaced file. A spaced join naming an existing file is
    # ambiguous → refuse.
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh" / "one two").write_text(f"github.com ssh-rsa {bad}\n")
    patch([(k, f"{home}/.ssh/one two"
           if k == "userknownhostsfile" else v) for k, v in CLEAN])
    assert "ambiguous" in mod._ssh_host_unchanged(URL)

    # PermitLocalCommand + LocalCommand executes a command locally
    # after connecting — an exfil path invisible to the host checks.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("permitlocalcommand", "yes"),
                                 ("localcommand", "/bin/evil %h")])
    assert "local command" in mod._ssh_host_unchanged(URL)

    # 'Match exec' in ANY config source ssh read is state-dependent
    # local execution — a condition false now can be true at push
    # time, so -G's snapshot cannot attest it. The -v debug trace
    # names the sources; 'Match host exec.example.com' is a hostname
    # pattern and must NOT trip the scan.
    cfg = tmp_path / "ssh_match_exec"
    cfg.write_text('Match exec "test -f /tmp/marker"\n'
                   '  HostName attacker.example\n'
                   'Match host exec.example.com\n')

    def run_v(argv, **_kw):
        return R("hostname github.com\nstricthostkeychecking ask\n"
                 "userknownhostsfile ~/.ssh/known_hosts\n",
                 err=(f"debug1: Reading configuration data {cfg}\n"))
    monkeypatch.setattr(
        mod, "subprocess",
        types.SimpleNamespace(run=run_v,
                              TimeoutExpired=_sp.TimeoutExpired))
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(which=lambda _n: "/usr/bin/ssh"))
    assert "Match exec" in mod._ssh_host_unchanged(URL)
    hostpat = tmp_path / "ssh_host_pattern"
    hostpat.write_text('Match host exec.example.com\n'
                       '  HostName attacker.example\n')
    assert mod._match_exec_in([str(hostpat)]) is False
    # OpenSSH accepts quoted criteria — Match "exec" must refuse too.
    qcfg = tmp_path / "ssh_quoted_exec"
    qcfg.write_text('Match "exec" "test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(qcfg)]) is True
    # Optional '=' keyword separators: 'Match=exec cmd' and
    # 'Match exec="cmd"' are the same criterion to ssh.
    eqcfg = tmp_path / "ssh_eq_exec"
    eqcfg.write_text('Match=exec "test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(eqcfg)]) is True
    eqcfg2 = tmp_path / "ssh_eq_exec2"
    eqcfg2.write_text('Match exec="test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(eqcfg2)]) is True
    # A criterion name in ARGUMENT position is a value, not a
    # condition — 'Match host exec' targets a host literally named
    # exec and must not trip.
    argpos = tmp_path / "ssh_arg_pos"
    argpos.write_text("Match host exec\n  HostName attacker.example\n")
    assert mod._match_exec_in([str(argpos)]) is False
    # Flag criteria take no argument — 'final' must not swallow a
    # following 'exec' criterion as its value.
    flag = tmp_path / "ssh_flag_exec"
    flag.write_text('Match final exec "test -e /tmp/x"\n')
    assert mod._match_exec_in([str(flag)]) is True
    # Negated criteria: 'Match !exec "cmd"' is still state-dependent
    # execution — the command's result can differ between check and
    # push, so it refuses; a negated VALUE ('Match host
    # !exec.example.com') is a pattern and stays allowed.
    neg = tmp_path / "ssh_neg_exec"
    neg.write_text('Match !exec "test ! -e /tmp/marker"\n')
    assert mod._match_exec_in([str(neg)]) is True
    negval = tmp_path / "ssh_neg_val"
    negval.write_text('Match host !exec.example.com\n')
    assert mod._match_exec_in([str(negval)]) is False
    # GlobalKnownHostsFile 'none' disables the global file — the
    # remaining user file is still validated, not refused as a path.
    kh_file.write_text(f"github.com ssh-rsa {good}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("globalknownhostsfile", "none")])
    assert mod._ssh_host_unchanged(URL) is None

    # Provider libraries dlopen during authentication — non-default
    # PKCS11Provider/SecurityKeyProvider refuse, defaults pass.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("pkcs11provider", "/tmp/libp11.so")])
    assert "PKCS" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("securitykeyprovider", "/tmp/sk.so")])
    assert "security-key" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("securitykeyprovider", "internal")])
    assert mod._ssh_host_unchanged(URL) is None

    # ControlMaster attaches the push to an existing session — the
    # peer may not be github.com though -G reads clean. Refuse any
    # enabled form; 'no' (the default) passes.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN]
          + [("controlmaster", "auto"), ("controlpath", "/tmp/cm-%r@%h:%p")])
    assert "multiplex" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("controlmaster", "no")])
    assert mod._ssh_host_unchanged(URL) is None
    # Reaching the source cap must fail closed — a partial scan can
    # leave a 'Match exec' in an unscanned Include'd file.
    benign = tmp_path / "ssh_benign"
    benign.write_text("Host *\n")
    assert mod._match_exec_in([str(benign)] * 64) is False
    assert mod._match_exec_in([str(benign)] * 65) is True

    # A PATH-resolved binary outside the system dirs earns no trust —
    # a wrapper can attest to itself.
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(
            which=lambda _n: str(tmp_path / "nogit")))
    assert mod._trusted_prog("git") == ""
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(which=lambda _n: "/usr/bin/ssh"))
    assert mod._trusted_prog("ssh").startswith("/usr/")

    # Windows: the trust root is OS-derived (_windows_dir → kernel32),
    # never the caller-controlled SystemRoot env. When the OS cannot
    # answer there is NO trusted directory — a system-looking ssh
    # still refuses, and on POSIX _windows_dir reports nothing.
    assert mod._windows_dir() == ""
    monkeypatch.setattr(mod, "_windows_dir", lambda: "")
    monkeypatch.setattr(mod.os, "name", "nt")
    patch(CLEAN)
    assert "cannot be verified" in mod._ssh_host_unchanged(URL)
    monkeypatch.setattr(mod.os, "name", "posix")

    # A PATH-resolved ssh outside the system dirs is never even asked.
    patch(CLEAN, which="/tmp/evil/ssh")
    assert "cannot be verified" in mod._ssh_host_unchanged(URL)
    assert calls == []

def test_bootstrap_mirror_dirty_clone_no_ratchet(tmp_path):
    """A first seed into a clone that ALREADY has tracked files gets merge
    semantics too — hits in pre-existing files are the org's own content
    and must surface as lint FAILs, never be silently grandfathered into
    the ratchet."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "buro-registry"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    # A tracked pre-existing file carrying another org's slug.
    (root / "notes.md").write_text("see Impaktful-Platform/impaktful_3.0\n")
    sp.run(["git", "add", "notes.md"], cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    al = (root / "registry/pack-surface-allowlist.txt").read_text()
    entries = {ln.split("#", 1)[0] for ln in al.splitlines()
               if ln.strip() and not ln.startswith("#")}
    assert "notes.md" not in entries, \
        "pre-existing own-content hit was ratcheted on a dirty first seed"
    assert any(e.startswith("registry/platform/") for e in entries), \
        "vendored platform hits must still be seeded"

def test_bootstrap_mirror_refresh_aborts_on_bad_index(tmp_path):
    """An established mirror whose plugins.json is momentarily malformed
    (e.g. mid conflict-resolution) must NOT be overwritten with the
    canonical platform-only index — abort and keep the file."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "buro-registry"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    idx = root / "registry/plugins.json"
    idx.write_text("{malformed\n")
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry",
         "--refresh-platform"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 2, r.stderr
    assert idx.read_text() == "{malformed\n", "index clobbered on abort"

def test_bootstrap_mirror_refresh_preserves_index(tmp_path):
    """--refresh-platform overwrites the vendored platform tree but MERGES
    plugins.json — the mirror's own universe plugin entries survive."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "impaktful-registry"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Impaktful-Platform/impaktful-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "impaktful", "--slug",
         "impaktful-platform/impaktful-registry", "--refresh-platform"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    idx = json.loads((root / "registry/plugins.json").read_text())
    idx["plugins"].append({
        "scope": "impaktful", "name": "dqms",
        "path": "registry/impaktful/dqms",
        "version_source": ".claude-plugin/plugin.json",
        "description": "org-private plugin"})
    (root / "registry/plugins.json").write_text(json.dumps(idx))
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "impaktful", "--slug",
         "impaktful-platform/impaktful-registry", "--refresh-platform"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    names = {(p["scope"], p["name"])
             for p in json.loads(
                 (root / "registry/plugins.json").read_text())["plugins"]}
    assert ("impaktful", "dqms") in names, "mirror plugin dropped on refresh"
    assert ("platform", "framework") in names

def test_bootstrap_mirror_no_refresh_keeps_platform_index(tmp_path):
    """An established-mirror rerun WITHOUT --refresh-platform keeps the
    EXISTING platform index entries — importing a newer canonical's
    platform list while the old vendored tree stays put would desync
    index↔tree (indexed-but-missing dirs trip the INDEX lint check)."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "buro-registry"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr

    # The mirror's platform tree predates a canonical platform addition:
    # locally drop one platform entry (tree+index stay consistent).
    idx_path = root / "registry/plugins.json"
    idx = json.loads(idx_path.read_text())
    plat = [p for p in idx["plugins"] if p.get("scope") == "platform"]
    assert len(plat) >= 1
    dropped = (plat[0]["scope"], plat[0]["name"])
    idx["plugins"].remove(plat[0])
    idx_path.write_text(json.dumps(idx))

    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "buro", "--slug", "buro-built/buro-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    names = {(p["scope"], p["name"])
             for p in json.loads(idx_path.read_text())["plugins"]}
    assert dropped not in names, \
        "no-refresh rerun re-imported a platform entry the tree lacks"

def test_bootstrap_mirror_refresh_preserves_owned_state(tmp_path):
    """On an established mirror, refresh must not (a) clobber the org's own
    scope.yaml edits, or (b) ratchet NEW violations in mirror-owned paths —
    only vendored-path hits may enter the regenerated allowlists, while
    pre-existing mirror-owned entries that are still live are preserved."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent
    root = tmp_path / "cpdcheck-registry"
    root.mkdir()
    # The PACK-SURFACE scan only sees registry/** in a real git checkout —
    # its non-git rglob fallback skip-lists the dir. Mirrors are clones, so
    # init the root before seeding (regen also stages via git add).
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/CPDcheck/cpdcheck-registry.git"],
           cwd=root, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "cpdcheck", "--slug",
         "cpdcheck/cpdcheck-registry"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr

    scope_file = root / "registry/cpdcheck/scope.yaml"
    scope_file.write_text(scope_file.read_text()
                          + "\n# org-specific note\n")

    # A pre-existing live ratchet entry for mirror-owned content.
    keep_line = "see Manolii-org/master for context"
    keep_key = "registry/cpdcheck/keep.md#" + \
        hashlib.sha256(keep_line.lower().encode()).hexdigest()[:8]
    (root / "registry/cpdcheck/keep.md").write_text(keep_line + "\n")
    al = root / "registry/pack-surface-allowlist.txt"
    al.write_text(al.read_text() + keep_key + "\n")

    # ORG-LEAK uses registry-relative paths, unlike PACK-SURFACE above.
    leak_al = root / "registry/leak-allowlist.txt"
    leak_keep = "cpdcheck/keep.md#" + hashlib.sha256(keep_line.lower().encode()).hexdigest()[:8]
    leak_al.write_text(leak_al.read_text() + leak_keep + "\n")

    # A NEW mirror-owned violation that was never ratcheted.
    (root / "registry/cpdcheck/new.md").write_text(
        "also see Manolii-org/master\n")

    r = sp.run(
        [sys.executable, "scripts/bootstrap-mirror.py", "--root", str(root),
         "--universe", "cpdcheck", "--slug",
         "cpdcheck/cpdcheck-registry", "--refresh-platform"],
        cwd=pack, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr

    assert "org-specific note" in scope_file.read_text()

    entries = {ln.split("#", 1)[0] for ln in al.read_text().splitlines()
               if ln.strip() and not ln.startswith("#")}
    assert "registry/cpdcheck/keep.md" in entries, \
        "live mirror-owned ratchet entry dropped on refresh"
    assert "registry/cpdcheck/new.md" not in entries, \
        "refresh ratcheted a new mirror-owned violation"
    leak_entries = {line for line in leak_al.read_text().splitlines()
                    if line.strip() and not line.startswith("#")}
    assert any(line.startswith("platform/") for line in leak_entries), \
        "refresh discarded canonical registry-relative platform ratchets"
    assert leak_keep in leak_entries, "existing owned ORG-LEAK entry was lost"
    assert not any(line.startswith("cpdcheck/new.md#") for line in leak_entries), \
        "refresh must not ratchet new owned ORG-LEAK violations"

def test_pack_surface_scans_tracked_skip_dir(tmp_path):
    """A slug committed under a skip-listed dir is still published —
    the scan enumerates tracked files, not directory names."""
    import subprocess as sp
    repo = tmp_path / "pack"
    repo.mkdir()
    (repo / "registry").mkdir()
    brand = repo / ".brand"
    brand.mkdir()
    (brand / "branded.yml").write_text("upstream: Buro-Built/bcp-core")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod.results = []
    mod.check_pack_surface()
    fails = [f for f in mod.results
             if f.check == "PACK-SURFACE" and f.status == "FAIL"]
    assert any(".brand" in f.detail for f in fails)

def test_pack_surface_private_slug_fails(tmp_path):
    """A private-repo slug outside registry/ is recon surface in a public
    repo — PACK-SURFACE fails on it."""
    repo = tmp_path / "pack"
    repo.mkdir()
    (repo / "registry").mkdir()
    (repo / "docs").mkdir()
    (repo / "docs/x.md").write_text("see Buro-Built/internal-repo here")
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod.results = []
    mod.check_pack_surface()
    fails = [f for f in mod.results
             if f.check == "PACK-SURFACE" and f.status == "FAIL"]
    assert any("docs/x.md" in f.detail for f in fails)

def test_pack_surface_self_repo_slug_ok(tmp_path):
    """References to ai-starter-pack itself are legitimate — the repo may
    name its own slug."""
    repo = tmp_path / "pack"
    repo.mkdir()
    (repo / "registry").mkdir()
    (repo / "README.md").write_text(
        "install: copier copy gh:Manolii-org/ai-starter-pack .")
    mod = load_lint_module()
    mod.REPO = repo
    mod.REGISTRY = repo / "registry"
    mod.PACK_SURFACE_ALLOWLIST = repo / "registry" / "pack-surface-allowlist.txt"
    mod.results = []
    mod.check_pack_surface()
    assert not [f for f in mod.results if f.status == "FAIL"]

def test_missing_plugin_manifest_conflicts(tmp_path):
    """A plugin dir without .claude-plugin/plugin.json must conflict — a
    silent 0.0.0 default would let `ref: "0"` satisfy it."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / "registry/platform/framework/.claude-plugin/plugin.json"
     ).unlink()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "non-semver" in r.stdout or "missing" in r.stdout

def test_non_semver_manifest_version_conflicts(tmp_path):
    """'1.bad.2' must not satisfy ref '1.2' — version is a resolution input,
    non-semver values conflict instead of permissive parsing."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    (reg_root / "registry/platform/framework/.claude-plugin/plugin.json"
     ).write_text(json.dumps(
         {"name": "framework", "version": "1.bad.2", "description": "t"}))
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.2"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "non-semver" in r.stdout

def test_exact_ref_abbreviated_matches_zero_padded_version(tmp_path):
    """ref '1.0' must satisfy registry version '1.0.0' — the grammar accepts
    x[.y[.z]] so exact compares normalize to three components."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 0, r.stdout
    assert (consumer / ".claude" / "skills" / "demo" / "x.md").is_file()

def test_wrongly_typed_lock_conflicts(tmp_path):
    """{"files": null} is valid JSON but not a lock — it must report a
    repairable conflict, not crash locked_digests with TypeError."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    (consumer / ".ai").mkdir(parents=True)
    (consumer / ".ai" / "capability-lock.json").write_text(
        '{"files": null}')
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "malformed" in r.stdout
    assert "Traceback" not in r.stderr

def test_nested_resolved_files_null_conflicts(tmp_path):
    """{"resolved":[{"files":null}]} — the entry is a dict but its nested
    files value is None; locked_digests must not TypeError."""
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [("skills/demo/x.md", "x")],
    })
    consumer = tmp_path / "consumer"
    (consumer / ".ai").mkdir(parents=True)
    (consumer / ".ai" / "capability-lock.json").write_text(
        '{"resolved": [{"plugin": "platform/framework", "files": null}]}')
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--check")
    assert r.returncode == 1
    assert "malformed" in r.stdout
    assert "Traceback" not in r.stderr

def test_skip_worktree_file_conflicts_under_pin(tmp_path):
    """git update-index --skip-worktree hides modified worktree bytes from
    status AND ls-files — only a comparison against the pinned git object
    catches the divergence."""
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
    rel = "registry/platform/framework/skills/demo/x.md"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    (reg_root / rel).write_text("tampered")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "differs from the pinned git object" in r.stdout

def test_skip_worktree_manifest_conflicts_under_pin(tmp_path):
    """A skip-worktree .claude-plugin/plugin.json keeps status clean and every
    component file can match HEAD while the lock records a MODIFIED version —
    the manifest itself must be compared against the pinned object."""
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
    rel = "registry/platform/framework/.claude-plugin/plugin.json"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    (reg_root / rel).write_text(
        '{"name": "framework", "version": "9.9.9", "description": "x"}')
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "differs from or is absent at the pinned" in r.stdout

def test_skip_worktree_deleted_file_conflicts_under_pin(tmp_path):
    """A tracked component DELETED under skip-worktree leaves git status
    clean and vanishes from worktree enumeration — the per-file git show
    would never run, materialising an incomplete plugin under the pin.
    The resolver must compare the pinned TREE's file list instead."""
    import subprocess as sp
    reg_root = make_registry(tmp_path / "src", {
        "platform/framework": [
            ("skills/demo/x.md", "v1"),
            ("skills/demo/y.md", "v1"),
        ],
    })
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sp.run(["git", "init", "-q"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "add", "-A"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=reg_root, env=env, check=True)
    sp.run(["git", "tag", "v1.0.0"], cwd=reg_root, env=env, check=True)
    rel = "registry/platform/framework/skills/demo/y.md"
    sp.run(["git", "update-index", "--skip-worktree", rel],
           cwd=reg_root, env=env, check=True)
    (reg_root / rel).unlink()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = write_manifest(consumer, "manolii",
                       [{"plugin": "platform/framework", "ref": "tag:v1.0.0"}])
    r = run_resolver(m, reg_root, consumer, "--apply")
    assert r.returncode == 1
    assert "absent from the worktree" in r.stdout
